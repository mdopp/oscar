# HERMES skills

Conversation flows, routines, domain actions. Loaded by HERMES via its skill system ([HERMES docs](https://github.com/NousResearch/hermes-agent)).

Each subdirectory is one skill with a `SKILL.md` carrying YAML frontmatter (`name`, `description`, `version`, `metadata.hermes.{tags, related_skills}`). The directory name and the `name:` field follow the `oscar-<short>` convention — `oscar_help` reads this when answering "what can you do?".

## Currently registered skills

| Directory | `name:` | Phase | One-liner |
|---|---|---|---|
| `light/` | `oscar-light` | 0 | Lights on/off/dim via HA-MCP |
| `timer/` | `oscar-timer` | 0 | Relative-duration reminders (PT5M, halbe Stunde) |
| `alarm/` | `oscar-alarm` | 0 | Absolute-time wake-ups + rrule recurrences |
| `status/` | `oscar-status` | 1 | `oscar_health doctor` wrapper — "is everything OK?" |
| `audit-query/` | `oscar-audit-query` | 1 | Read-only query over `cloud_audit`, `time_jobs`, `gateway_identities`, … |
| `debug-set/` | `oscar-debug-set` | 1 | Admin: toggle `system_settings.debug_mode` (verbose logging on demand) |
| `identity-link/` | `oscar-identity-link` | 1 | Admin: bind `(gateway, external_id)` → LLDAP uid |
| `help/` | `oscar-help` | 1 | Self-describing capability list (`oscar_help list`) |
| `skill-author/` | `oscar-skill-author` | 1 | Admin: drafts a new or edited `SKILL.md`, previews via Signal, applies after `/ja` (writes to `skills-local/` with local git history) |
| `skill-reviewer/` | `oscar-skill-reviewer` | 1 | Internal cron (hourly): aggregates `skill_corrections`, when k≥3 it autonomously edits operating-sequence prose and DMs admin via Signal. Rate-limited 24h/skill. |

Specs live in `docs/skill-<name>.md` (combined skills can share a doc, e.g. [`../docs/timer-and-alarm.md`](../docs/timer-and-alarm.md)).

## Adding a new skill

1. `mkdir skills/<short>/` and write `SKILL.md` with the standard frontmatter (copy from any existing skill).
2. If the skill needs a CLI, put the code under `shared/oscar_<short>/` (importable, testable, mockable) and shell out from the SKILL prose via `python -m oscar_<short> …`.
3. Add a row to the table above.
4. The `oscar_help` parser will pick up the new entry on next pod restart — no code changes elsewhere.

## Planned (not yet implemented)

- Phase 0: heating, music (HA media-player → Navidrome)
- Phase 1: `reminder` (carries a message, not just a duration — distinct from timer)
- Phase 4: "good morning" routine (HA + TuneIn), proactive memo creation
