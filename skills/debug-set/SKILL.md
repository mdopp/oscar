---
name: oscar-debug-set
description: Use when the user (admin-harness only) asks to turn debug-mode on or off, or to enable verbose logging for a bounded window. Writes `system_settings.debug_mode` in `oscar-brain.postgres`; containers that opted into the runtime watcher pick the change up within ~5 seconds. Admin-only — never invoke from a guest harness.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [debug, observability, admin, phase-1]
    related_skills: [oscar-audit-query]
---

# OSCAR — debug.set

## Overview

Cluster-wide debug-mode toggle. When on, OSCAR-owned containers log full prompts / responses / tool args / connector bodies; audit-table retention policies are suspended; cloud-LLM-fulltext fields are returned by `audit-query` instead of redacted.

Source of truth is `system_settings.debug_mode` in `oscar-brain.postgres`. This skill rewrites that row; containers that have opted into `oscar_logging.runtime.watch_debug_mode` poll it every ~5 seconds and update their in-process override. Containers that haven't opted in stay on their `OSCAR_DEBUG_MODE` env-var setting.

## When to use

- "Schalt mal Debug-Mode an für eine Stunde."
- "Turn debug logging on while we investigate this."
- "Turn debug-mode off, we're done."
- "Is debug mode on right now?" → `debug-show` subcommand.

## Hard guards

- **Admin gate.** Before any DB write: confirm the active harness includes the `admins` group. If not, refuse with "Only an admin can change debug mode" and log `skill.debug_set.refused_non_admin` (warn).
- **Always show what was set.** After every write, read back via `debug-show` and confirm verbally: "Debug-Mode an bis 14:30 Uhr." or "Debug-Mode aus." Don't say "set" without the resulting state.
- **Defaults that protect.** When the user says "on" without a duration, suggest a TTL ("Eine Stunde okay?") rather than leaving it on indefinitely. The architecture's intent is that bounded-window is the normal case; unbounded-on is the build-phase default and a deliberate choice once productive.

## Operating sequence

### Set

```
python -m oscar_logging.admin debug-set --active true  --ttl-hours 1
python -m oscar_logging.admin debug-set --active true  --ttl-hours 4 --latency-annotations
python -m oscar_logging.admin debug-set --active false
```

Output:
```json
{"ok": true, "active": true, "verbose_until": "2026-05-12T15:30:00+00:00", "latency_annotations": false}
```

`--latency-annotations` enables "STT 230ms · router 80ms → 12B local · 1.4s" markers on voice responses (see architecture's Debug-Modus section). Only relevant when paired with admin uids; family members shouldn't be exposed to this.

### Show

```
python -m oscar_logging.admin debug-show
```

Output:
```json
{"ok": true, "set": true, "value": {"active": true, "verbose_until": "...", "latency_annotations": false}, "updated_at": "..."}
```

## Privacy reminder

When the user asks to turn debug-mode on, OSCAR will start writing full conversation content (prompts and responses) to `cloud_audit` and to stdout-JSON. Mention this briefly the first time in a session — "Debug-Mode loggt jetzt auch Volltexte." — so the household isn't surprised.

## Failure paths

- Postgres unreachable → "Ich kann debug-mode gerade nicht ändern." Don't retry the CLI in a loop.
- Past `--ttl-hours` value (negative) → CLI exits non-zero; tell the user the duration was nonsense and ask back.

## Phase mapping

| Phase | Behaviour |
|---|---|
| **1 (now)** | Writes `system_settings.debug_mode`. Containers that opt into `oscar_logging.runtime.watch_debug_mode` pick it up in ≤5 s. Others remain env-var-controlled until they're restarted. |
| **1+** | As components grow runtime-toggle requirements, they add the watcher in their startup path. Tracked per-component, not in this skill. |

## Related

- `oscar-audit-query` to inspect what happened while debug-mode was on.
- Architecture spec for debug-mode semantics: `oscar-architecture.md` → "Querschnitt: Debug-Modus" (translated: "Cross-cutting: Debug mode").
