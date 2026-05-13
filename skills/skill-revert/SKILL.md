---
name: oscar-skill-revert
description: Use when the user (admin-harness only) asks to undo the last skill edit — "OSCAR, mach den letzten Edit am Licht rückgängig", "/revert oscar-light", "nimm die letzte Reviewer-Änderung am Timer zurück". Calls `oscar_skill_author revert` against `skills-local/`, which `git revert`s the matching commit and stamps `skill_edits.reverted_at`. Admin-only.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [skill-management, admin, phase-1]
    related_skills: [oscar-skill-author, oscar-skill-reviewer]
---

# OSCAR — skill-revert

## Overview

Single-purpose admin skill: undo a skill edit. The mechanics live in
`oscar_skill_author.revert_edit` (the same library that applies edits).
This skill is the conversational wrapper.

Every revert is itself a git commit in `skills-local/` and a new
`skill_edits` row with `diff = "REVERT of <sha>"`. Reverting a revert
is a normal operation — the chain stays sane.

## When to use

- "Mach den letzten Edit am Licht rückgängig."
- "Stell oscar-timer auf die Version von gestern Abend."
- "/revert oscar-light"  (Signal command form)
- "Nimm die letzte Reviewer-Änderung zurück."

Out of scope:
- "Setze alle lokalen Skills auf default zurück" — this would `git reset --hard`
  to the baseline commit. Not exposed via skill — manual `git -C skills-local reset`
  is the path if you ever need it. Tell the user that and stop.

## Required env

- `POSTGRES_DSN`, `OSCAR_SKILLS_LOCAL_DIR` (default `/opt/oscar/skills-local`).
- Git binary in the runtime.

## Operating sequence

### Simple revert (most common)

```
python -m oscar_skill_author revert --skill-name <oscar-name> --n 1
```

CLI returns JSON with the new `git_sha` + the reverse-diff + a `restart_hint`. Quote it short to the user:
> "Den letzten Edit von oscar-light habe ich zurückgenommen (Quelle: reviewer, vor 2 h). Beim nächsten Pod-Restart aktiv."

### Revert n versions back

```
python -m oscar_skill_author revert --skill-name <oscar-name> --n <N>
```

`N=2` reverts the *second* most recent un-reverted edit. Useful when "the version from yesterday morning" is several edits back.

### Revert all reviewer edits since last user edit

Not a single CLI flag — compose with `oscar-audit-query`:

1. Pull the `skill_edits` list for the skill:
   ```
   python -m oscar_audit query --table skill_edits --where "skill_name='<name>' AND reverted_at IS NULL" --order applied_at:desc --limit 20
   ```
2. Walk the list backwards, calling `revert --n 1` until you hit a `source='user'` row.
3. Tell the user how many reviewer edits got reverted.

## Failure paths

- No unreverted edits → CLI exits 3 with "no unreverted edits found for 'X'". Tell the user: "Da ist nichts zum Rückgängigmachen — keine Edits aktiv."
- Git conflict during revert (rare; happens if two non-adjacent commits touched the same lines and the most recent was reverted earlier) → CLI returns the git error verbatim. Tell the user "Git-Konflikt beim Revert — bitte manuell schauen, ich melde mich nicht erfolgreich." Don't try to auto-resolve.
- Skill name doesn't exist anywhere → "Den Skill kenne ich nicht. Meinst du <closest-match>?" Use `oscar-help describe` to suggest.

## Post-run logging

```
python -m oscar_skill_runs append \
  --trace-id <trace> --uid <admin-uid> --endpoint <endpoint> \
  --skill-name oscar-skill-revert \
  --utterance "<user request>" \
  --response "reverted edit <edit-id> on <skill-name>" \
  --outcome ok
```

This counts as a "user touched the skill" event — the reviewer will then stay off the skill for 24 h (#41).

## Why this is its own skill

The Operating Sequence is tiny, but giving it a separate skill keeps the *routing* clean: HERMES gets a clear signal for "user wants undo" without overloading the author skill. Also makes the audit trail readable ("oscar-skill-revert was called 3 times this week" is a meaningful sign).
