# HERMES skills

Conversation flows, routines, domain actions. Loaded by HERMES via its skill system ([HERMES docs](https://github.com/NousResearch/hermes-agent)).

Planned OSCAR skills:
- Phase 0: light, heating, music (local via HA media player → Navidrome), `timer`, `alarm` (one doc covers both), `audit.query`, `debug.set`
- Phase 1: `identity.link` (admin), `reminder` (deferred)
- Phase 4: "good morning" routine (HA + TuneIn), proactive memo creation

Skill specs live per-skill in `docs/skill-<name>.md` (combined skills can share a doc, e.g. [`../docs/timer-and-alarm.md`](../docs/timer-and-alarm.md)).
