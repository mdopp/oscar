---
name: oscar-skill-reviewer
description: Internal cron-only skill. Runs once an hour, aggregates pending skill_corrections, applies small autonomous edits when ≥3 similar corrections land on the same skill, sends a Signal diff preview to the admin. Never invoked by the user directly. Auto-edits are constrained to operating-sequence prose; admin-tagged skills and routing descriptions are never touched.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [skill-management, cron, admin, phase-1]
    related_skills: [oscar-skill-author, oscar-skill-revert]
---

# OSCAR — skill-reviewer

## Overview

Self-improvement loop, cron-driven. Reads OSCAR's own correction trail
(`skill_corrections` from #39), groups by similar utterance/correction
prefix, and — when ≥ 3 similar entries hit the same skill — drafts a
small SKILL.md patch, applies it, and DMs you on Signal with the diff.

The aggregation / constraint / apply work is mechanical (the Python
library). The LLM-drafting step happens inside HERMES from this prose.

## Triggering

HERMES cron, every hour:

```
0 * * * *  →  run skill: oscar-skill-reviewer (mode: scan)
```

Never user-invoked. If a user says "wann passt du dich an?" you can
explain what this skill does, but don't *call* it on demand.

## Required env

- `POSTGRES_DSN`
- `OSCAR_SKILLS_LOCAL_DIR` (default `/opt/oscar/skills-local`)
- `SIGNAL_URL` (default `http://127.0.0.1:8090`), `SIGNAL_TOKEN` for the admin DM
- `OSCAR_ADMIN_UID` (default `michael`) — the LLDAP uid that maps to the admin
  Signal number via `gateway_identities`

## Operating sequence

### 1. Aggregate pending corrections

```
python -m oscar_skill_reviewer aggregate --window-days 14 --k 3
```

JSON: list of groups `{skill_name, count, sample_utterance, sample_correction, correction_ids}`. Empty → nothing to do, exit gracefully.

### 2. Look up the admin's Signal number

```sql
SELECT external_id FROM gateway_identities
 WHERE gateway = 'signal' AND uid = $OSCAR_ADMIN_UID
 LIMIT 1
```

If no row, log `skill_reviewer.no_admin_number` and stop — we can't notify, so we don't apply either. (Better to defer than to silently edit and never tell.)

### 3. For each eligible group

```
python -m oscar_skill_reviewer can-apply --skill-name <name>
```

Exits 0 if rate-limit + user-interference are ok; 1 otherwise. On `1`, skip the group, do not mark anything.

### 4. Compose the edit (LLM step — your job)

For each remaining group:

1. Read the current SKILL.md (Layer B if exists, else Layer A):
   ```
   FILE=/opt/oscar/skills-local/<name>/SKILL.md
   [ -f "$FILE" ] || FILE=/opt/oscar/skills/<name>/SKILL.md
   ```
2. Draft a *small* edit (≤ 200 token diff) that addresses the correction pattern. Constraints:
   - Touch only Operating-Sequence and Failure-Paths prose.
   - Do **not** change frontmatter `name` or `description`.
   - Do **not** add or remove `admin` from tags.
   - Prefer additive, surgical edits (one sentence, one bullet) over wholesale rewrites.
3. Write the proposed file to `/tmp/proposed.md`.

### 5. Apply

```
RESULT=$(python -m oscar_skill_author apply \
  --skill-name <name> --source reviewer \
  --proposed-md @/tmp/proposed.md \
  --reason "$COUNT corrections matching '$SAMPLE_UTTERANCE' / '$SAMPLE_CORRECTION'")
```

Constraint failures from `apply` (status 3 + stderr) → log, mark the
group's corrections as `dismissed`, *do not* notify the admin (the
admin gets surprised enough by the things we *do* edit).

### 6. Notify

```
DIFF=$(echo "$RESULT" | jq -r .diff)
python -m oscar_skill_reviewer notify-admin \
  --admin-number "$ADMIN_NUMBER" \
  --skill-name <name> \
  --diff @<(echo "$DIFF") \
  --reason "$COUNT × 'nein, ich meinte ...'"
```

### 7. Mark the corrections handled

```
python -m oscar_skill_reviewer mark-edited \
  --correction-ids "<comma-separated UUIDs from step 1>"
```

## Failure paths

- Signal-gateway down → notify-admin returns non-zero. **Treat as a hard
  failure for this group**: roll back via `oscar_skill_author revert
  --skill-name <name> --n 1`, mark corrections as `dismissed`, log
  loudly. We never edit without notifying.
- LLM-drafted patch violates a constraint → `apply` rejects. Log,
  `dismiss` the group. Do not retry in the same run.
- More than 5 groups eligible in one pass → handle only the top 5 by
  count. The rest sit pending for next hour.

## Post-run logging

Always:
```
python -m oscar_skill_runs append \
  --trace-id <trace> --uid system --endpoint cron:reviewer \
  --skill-name oscar-skill-reviewer \
  --utterance "scan" --response "applied <N> edits, dismissed <M>" \
  --outcome ok
```

## Why these constants

- **k=3** — once is noise, twice is coincidence, three is a pattern.
- **24 h rate-limit** per skill — gives the human reviewer (you, on Signal) a chance to push back before the next auto-edit lands.
- **24 h user-interference window** — if you used `oscar-skill-author` to touch this skill today, the reviewer assumes you know what you want and stays out.
- **14-day correction window** — old corrections lose context; you probably already verbally re-asked the same thing differently after a week.
