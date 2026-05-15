---
name: oscar-debug-set
description: Use when an admin asks to turn debug-mode on or off, or to enable verbose logging for a bounded window. Writes `system_settings.debug_mode` in oscar.db. Components that re-query the row on every audit event pick the change up within ~5 seconds. Admin-only — never invoke without explicit admin authorization.
version: 0.3.0
author: OSCAR
license: MIT
---

# OSCAR — debug.set

## Overview

Cluster-wide debug-mode toggle. When on, OSCAR-owned containers log full prompts / responses / tool args / connector bodies; audit-table retention policies are suspended; cloud-LLM-fulltext fields are returned by `audit-query` instead of redacted.

Source of truth is the `debug_mode` row in `system_settings` in `oscar.db` (the SQLite file in `oscar-household`'s volume, default `/var/lib/oscar/oscar.db`). This skill rewrites that row; components re-query `system_settings` on every audit event (no caching > 5 s), so the change propagates within ~5 seconds without restarts.

> **TODO (rewrite).** This skill was written against the deleted `shared/oscar_logging.admin` CLI + Postgres backend. The intent below is correct; the Python implementation needs to land as inline SQLite queries in this skill or as a small companion script in `oscar-household`. The references to `python -m oscar_logging.admin` are stale.

## When to use

- "Schalt mal Debug-Mode an für eine Stunde."
- "Turn debug logging on while we investigate this."
- "Turn debug-mode off, we're done."
- "Is debug mode on right now?" → read the row, return its current value.

## Hard guards

- **Admin gate.** Before any DB write: confirm the active harness includes the `admins` group. If not, refuse with "Only an admin can change debug mode."
- **Always show what was set.** After every write, read back the row and confirm verbally: "Debug-Mode an bis 14:30 Uhr." or "Debug-Mode aus." Don't say "set" without the resulting state.
- **Defaults that protect.** When the user says "on" without a duration, suggest a TTL ("Eine Stunde okay?") rather than leaving it on indefinitely. The architecture's intent is that bounded-window is the normal case; unbounded-on is the build-phase default and a deliberate choice once productive.

## Operating sequence

### Set

Update the `system_settings` row keyed `debug_mode` with a JSON value:

```json
{"active": true, "verbose_until": "2026-05-16T15:30:00+00:00", "latency_annotations": false}
```

- `active`: bool — global on/off switch
- `verbose_until`: ISO-8601 timestamp or `null` — TTL after which `effective = false`
- `latency_annotations`: bool — adds "STT 230ms · router 80ms → 12B local · 1.4s" markers on voice responses (Phase 1+; relevant only for admin uids, hide from family members)

### Show

`SELECT value, updated_at FROM system_settings WHERE key='debug_mode'`, then derive the effective state:

```
effective_active = value.active AND (value.verbose_until IS NULL OR now() < value.verbose_until)
```

## Privacy reminder

When the user asks to turn debug-mode on, OSCAR will start writing full conversation content (prompts and responses) to `cloud_audit` and to stdout-JSON. Mention this briefly the first time in a session — "Debug-Mode loggt jetzt auch Volltexte." — so the household isn't surprised.

## Failure paths

- `oscar.db` unreachable → "Ich kann debug-mode gerade nicht ändern." Don't retry in a loop.
- Past `verbose_until` value (in the past) → reject as nonsense, ask back.

## Phase mapping

| Phase | Behaviour |
|---|---|
| **0 (now)** | Writes `system_settings.debug_mode` row in `oscar.db`. Components re-query on every audit event (no caching > 5 s). |
| **1+** | Voice components (gatekeeper) honour `latency_annotations` in synthesised responses. |

## Related

- `oscar-audit-query` to inspect what happened while debug-mode was on.
- Architecture spec for debug-mode semantics: [`../../oscar-architecture.md`](../../oscar-architecture.md) → "Cross-cutting: Debug mode".
