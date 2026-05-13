# oscar_skill_author

Apply user-initiated and reviewer-autonomous edits to `skills-local/` with local git history. Used by:

- `skills/skill-author/SKILL.md` (#40) — "OSCAR, ändere den Timer-Skill so…" flow.
- `skills/skill-reviewer/SKILL.md` (#41) — autonomous correction-driven edits.
- `skills/skill-revert/SKILL.md` (#42) — undo path.

## Constraint contract

All edits — user *or* reviewer — must pass `validate(proposed, current)`:

| Field | Rule | Why |
|---|---|---|
| `name:` | must equal `current.name` (or be set when creating) | Renaming a skill = orphan in the registry |
| `description:` | must equal `current.description` | This is the routing signal. Changing it can hide or hijack skills. |
| `tags` containing `admin` | rejected unconditionally | Admin gates are part of the security model, not editable through this path |
| Adding `admin` to tags | rejected | Privilege escalation |
| `tools:` / `permissions:` block | not editable | Same reason |

The body markdown is free to change. We do *not* try to enforce "only the operating-sequence section moved" — the preview/confirm step is the human gate.

## Commands

```bash
# Stage a draft (returns UUID); on user "/ja" you call confirm.
python -m oscar_skill_author draft \
  --uid michael --skill-name oscar-timer \
  --source user --reason "User asked to add snooze prompts" \
  --proposed-md @new.md \
  [--current-md @existing.md]   # omit for creating a brand-new skill

# Resolve a pending draft: write to skills-local, git commit, log skill_edits row.
python -m oscar_skill_author confirm <draft-id>

# Plain apply (skips the draft table — used by reviewer for already-validated edits).
python -m oscar_skill_author apply \
  --skill-name oscar-light --source reviewer \
  --proposed-md @reviewer.md \
  [--current-md @existing.md]

# List pending drafts (admin overview).
python -m oscar_skill_author list-drafts [--uid michael]

# Cancel a pending draft.
python -m oscar_skill_author cancel <draft-id>

# Revert the last edit on a skill (PR-F #42).
python -m oscar_skill_author revert --skill-name oscar-light [--n 1]
```

Files are written to `$OSCAR_SKILLS_LOCAL_DIR` (default `/opt/oscar/skills-local`). Git operations run via `subprocess` against that dir — the runtime must have a `git` binary.

## Pod-restart awareness

Edits don't take effect until HERMES re-reads the skill registry. After `confirm` / `apply`, the CLI emits the line `restart_hint: <reason>` so the calling skill knows to tell the user "the change will be active after I restart". If your deployment uses ServiceBay-MCP `restart_pod()`, wire that into the skill prose.
