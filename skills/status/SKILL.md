---
name: oscar-status
description: Use when the user asks "is OSCAR alive?", "is everything working?", "why isn't the light responding?", or any other "health-check" style question. Probes the configured OSCAR dependencies (oscar.db, Ollama, Hermes, HA-MCP, ServiceBay-MCP) and reports per-component status. Read-only.
version: 0.3.0
author: OSCAR
license: MIT
---

# OSCAR — status

## Overview

Quick "is everything OK?" probe across every OSCAR dependency. Read-only — no state changes.

> **TODO (rewrite).** This skill was written against the deleted `shared/oscar_health` library + Postgres backend. The intent below is correct; the Python implementation needs to land as inline probes in this skill or as a small companion script in `oscar-household` once that template is wired against ServiceBay's future `hermes` template. The dependency list has been updated for the SQLite world but the "Operating sequence" still describes the pre-lean-reset shape.

## When to use

- "OSCAR, bist du da?" / "Bist du wach?"
- "Funktioniert alles?" / "Geht das Licht gerade nicht?"
- "Ist Home Assistant erreichbar?"
- "Wo hakt's gerade?"
- As the **first** diagnostic step before deeper drill-down — if `oscar-status` says everything's green, the bug is application-side, not infrastructure.

## Operating sequence

1. Probe each configured dependency (inline; see "What gets probed" below).
2. Collect results as:
   ```json
   {
     "ok": false,
     "results": [
       {"name": "oscar.db", "ok": true, "latency_ms": 1},
       {"name": "hermes", "ok": true, "latency_ms": 12},
       {"name": "ollama", "ok": true, "latency_ms": 8},
       {"name": "ha-mcp", "ok": false, "latency_ms": 3000, "detail": "ConnectError: ..."}
     ]
   }
   ```
3. Summarise verbally:
   - **All green** → "Alles ok." or "Alles grün."
   - **One red** → name it: "HA-MCP antwortet nicht — ich erreiche Home Assistant gerade nicht."
   - **Multiple red** → group by impact: "Hermes und ollama sind beide down — das ist ernst."

## What gets probed

Discovered from env vars in the skill's environment:

| Env var | Probe | Failure means |
|---|---|---|
| `OSCAR_DB_PATH` | `SELECT 1` on the SQLite file | OSCAR can't read/write its own audit state |
| `HERMES_API_URL` | HTTP GET `/health` with `HERMES_TOKEN` | Agent runtime offline |
| `OSCAR_OLLAMA_URL` | HTTP GET `/api/tags` | Local LLM offline → cloud or local-only fallback |
| `OSCAR_HA_MCP_URL` | HTTP GET | Home control broken |
| `OSCAR_SERVICEBAY_MCP_URL` | HTTP GET | Platform-control broken |
| `OSCAR_WHISPER_HOST` *(Phase 1)* | TCP open | Voice STT broken — only probed once voice is deployed |
| `OSCAR_PIPER_HOST` *(Phase 1)* | TCP open | Voice TTS broken — only probed once voice is deployed |
| `OSCAR_GATEKEEPER_URL` *(Phase 1)* | HTTP GET `/push/health` | Voice bridge broken |

Missing env vars are silently skipped — that's the "this OSCAR install doesn't have voice wired yet" case, not a failure.

## What this does NOT cover

- **Skill correctness** — we know Hermes is reachable, not that a specific skill behaves. For that, `oscar-audit-query` over `cloud_audit` and the relevant SKILL events.
- **Voice latency** — TCP-open says the port is alive, not that it's fast. For latency hunting, `oscar-debug-set` + the gatekeeper's `gatekeeper.transcript` / `gatekeeper.response` timestamps.
- **HA device state** — "is the office light actually on?" is an HA-MCP query, not a status probe.

## Failure paths

- The probe itself crashes → respond "Ich kann das gerade selbst nicht prüfen, sieh mal in ServiceBay nach." Points at something fundamentally broken (skill misconfigured, env vars missing, etc.).

## Phase mapping

| Phase | Probes covered |
|---|---|
| **0 (now)** | oscar.db, hermes, ollama, ha-mcp, servicebay-mcp |
| **1** | + whisper, piper, gatekeeper |
| **2** | + speaker-ID model loaded? (gatekeeper local check) |
| **3a** | + ingestion-pipeline backlog (rows in incoming state) |
