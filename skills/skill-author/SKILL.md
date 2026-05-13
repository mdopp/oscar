---
name: oscar-skill-author
description: Use when the user (admin-harness only) explicitly asks to create a new skill or edit an existing one — "OSCAR, mach mir einen Skill der …", "Ändere den Timer-Skill so dass …", "Bau mir eine 'Gute Nacht'-Routine". Drafts the SKILL.md, sends a Signal preview, waits for "/ja" confirmation, then writes to skills-local/ with a real local git commit. Admin-only — never invoke from a guest harness, never invoke without an explicit user instruction.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [skill-management, admin, phase-1]
    related_skills: [oscar-help, oscar-skill-revert]
---

# OSCAR — skill-author

## Overview

User-initiated skill creation and editing. Two flows: create a brand-new
skill ("Bau mir einen 'Gute Nacht'-Skill") or edit an existing one
("Ändere den Timer so dass…"). In both cases:

1. You draft the proposed `SKILL.md` content in conversation context.
2. The CLI validates frontmatter constraints (see "Constraints" below).
3. A draft row is stashed in Postgres; the diff is sent to the admin via
   Signal as a preview.
4. The admin replies `/ja` (or "ja, mach das") → `confirm` runs, the file
   lands in `skills-local/`, a local git commit is recorded.
5. You tell the admin: "Beim nächsten Pod-Restart aktiv. Ich kann auch
   `restart_pod oscar-brain` aufrufen, wenn du willst" — if your harness
   has `lifecycle` permission on ServiceBay-MCP.

## When to use

- "OSCAR, mach mir einen Skill der die Lichter um Mitternacht ausmacht."
- "Ändere den Timer so dass er nach dem Feuern noch 3 Minuten Snooze ansagt."
- "Bau die `oscar-light`-Reaktion um, ich will nach dem Schalten kein 'ok' mehr hören."
- Internal: when the user asks for behavior that *almost* matches an existing skill but with a tweak that would be too narrow for the general-purpose flow.

Out of scope:
- Admin skills (anything with the `admin` tag) — refuse, this path is for non-admin skill changes only.
- HA-side automation edits — that's `ServiceBay-MCP` territory, not OSCAR skills.

## Required env

- `POSTGRES_DSN`, `OSCAR_SKILLS_LOCAL_DIR` (defaults to `/opt/oscar/skills-local`).
- The admin's Signal number for the preview DM — read from `gateway_identities` (uid → admin number).
- `SIGNAL_URL` of the in-pod signal-gateway (default `http://127.0.0.1:8090`) and `SIGNAL_TOKEN` for its bearer.

## Operating sequence

### Creating a new skill

1. From the user's request, draft a complete `SKILL.md`:
   - Frontmatter: `name`, `description` (be specific — this is the routing signal), `version: 0.1.0`, `author: OSCAR`, `license: MIT`, `metadata.hermes.{tags, related_skills}`.
   - `tags` **must not** include `admin`.
   - Body: Overview, When to use, Operating sequence, Failure paths, Phase mapping. Follow the patterns from `skills/light/`, `skills/timer/`, etc.
2. Save the draft to a temp file (e.g. `/tmp/oscar-<name>.md`) and run:
   ```
   python -m oscar_skill_author draft \
     --uid <admin-uid> --skill-name <oscar-name> \
     --source user --reason "user asked to create" \
     --proposed-md @/tmp/oscar-<name>.md
   ```
3. Capture the returned UUID (stdout is the draft id).
4. Send the diff preview to the admin via the in-pod signal-gateway:
   ```
   curl -sS -X POST http://127.0.0.1:8090/send \
     -H "Authorization: Bearer $SIGNAL_TOKEN" \
     -d '{"to": "<admin-number>", "text": "Neuer Skill <name> bereit:\n\n<diff>\n\nMit `/ja <draft-id>` bestätigst du."}'
   ```
5. Wait for the admin's confirmation utterance (the next conversation turn that contains `/ja <draft-id>` or `ja, mach das` *and* the same draft id is the most recent pending).
6. On confirmation:
   ```
   python -m oscar_skill_author confirm <draft-id>
   ```
   The CLI prints the new git SHA + the path. Tell the user "Skill ist gespeichert; beim nächsten Restart aktiv."

### Editing an existing skill

Same flow, but read the existing file first to pass as `--current-md`:

```
EXISTING=/opt/oscar/skills-local/<name>/SKILL.md
[ -f "$EXISTING" ] || EXISTING=/opt/oscar/skills/<name>/SKILL.md
python -m oscar_skill_author draft \
  --uid <admin-uid> --skill-name <name> \
  --source user --reason "user asked to change <X>" \
  --proposed-md @/tmp/edit.md \
  --current-md @$EXISTING
```

`--current-md` is what triggers strict frontmatter-immutability checks — `name` and `description` *cannot* change. If the user really wants a new routing description, they should create a *new* skill instead, then remove the old one (manual git work, not via this skill).

## Constraints (enforced by `oscar_skill_author validate`)

| Field | Rule |
|---|---|
| `name` | must equal current (or be set when creating) |
| `description` | must equal current — this is the routing signal |
| `tags` containing `admin` | always rejected |
| Adding `admin` to tags | always rejected |
| `tools:` / `permissions:` block | not editable |

If validation rejects, the CLI exits 3 with a one-line stderr explanation. Tell the user *exactly* what the rejection said — never silently rewrite the request.

## Failure paths

- Validation failure → quote the stderr to the user, ask whether they want to revise the draft or abandon ("Soll ich's anders versuchen?").
- Signal preview can't be sent (gateway down) → fall back to reading the diff out loud (voice) or `cat`-ing it back as a text response. Confirmation still requires `/ja` so the user has a chance to refuse.
- `git` not available in the runtime → CLI fails with non-zero. Tell the user that skill-author requires git in the brain pod and that this is the deployment-side fix.
- Draft expired (30 min TTL for user-source) → tell the user the proposal timed out; offer to re-draft. Do **not** re-confirm without a fresh `/ja`.

## Post-write logging

Always append a `skill_runs` row after the confirm or apply step:

```
python -m oscar_skill_runs append \
  --trace-id <trace> --uid <admin-uid> --endpoint <endpoint> \
  --skill-name oscar-skill-author \
  --utterance "<user request>" \
  --response "applied edit <edit-id>; <skill_name> updated" \
  --outcome ok
```

This is how the reviewer (`oscar-skill-reviewer`) learns "this skill got user-touched recently" → no autonomous edits for 24 h.
