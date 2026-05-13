# oscar_skill_reviewer

Autonomous correction-driven skill edits.

Periodically (HERMES cron, 1×/h):

1. Read `skill_corrections WHERE status='pending'` from the last 14 days.
2. Group by `(skill_name, normalized utterance prefix, normalized correction prefix)`.
3. For any group with `count >= K_THRESHOLD` (default 3):
   - Check rate-limit: no autonomous edit on this skill in the last 24 h, *and* no user-source edit in the last 24 h (so the user doesn't fight us).
   - Check edit-scope constraints (same as `oscar_skill_author`): description-protected, admin-rejected.
   - Hand the group + the existing SKILL.md to HERMES with a draft prompt; HERMES produces a small operating-sequence-section patch.
   - Apply via `oscar_skill_author.apply_edit(source='reviewer')`.
   - Mark all matching corrections `status='edited'`.
   - Send a Signal DM to the admin with the diff + `/revert` hint.

This library handles steps 1–3 (excluding the LLM call — that's HERMES inside the skill prose) and step 4 (apply + DM). The "ask the LLM to write the patch" step is in the SKILL.md.

## API

```python
from oscar_skill_reviewer import (
    K_THRESHOLD,
    REVIEWER_RATE_LIMIT_S,
    aggregate_corrections,
    can_apply_now,
    mark_group_edited,
    notify_admin_via_signal,
)

groups = await aggregate_corrections(dsn=dsn, window_days=14, k=K_THRESHOLD)
for g in groups:
    if not await can_apply_now(dsn=dsn, skill_name=g.skill_name):
        continue
    # ... HERMES drafts patch ...
    # ... apply via oscar_skill_author ...
    await mark_group_edited(dsn=dsn, group=g)
    await notify_admin_via_signal(...)
```

## CLI

```bash
# Show pending correction groups that meet k=3 threshold:
python -m oscar_skill_reviewer aggregate

# Mark a group edited after an apply ran:
python -m oscar_skill_reviewer mark-edited --run-ids <id1>,<id2>,<id3>

# Send the notification DM (called from the skill prose):
python -m oscar_skill_reviewer notify-admin \
  --admin-uid michael \
  --skill-name oscar-light \
  --diff @/tmp/diff.txt \
  --reason "3 corrections matching 'dim'"
```
