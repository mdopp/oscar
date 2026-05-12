---
name: oscar-status
description: Use when the user asks "is OSCAR alive?", "is everything working?", "why isn't the light responding?", or any other "health-check" style question. Runs `python -m oscar_health doctor`, which pings every configured OSCAR dependency (Postgres, HERMES, Ollama, Whisper/Piper/openWakeWord, connectors, HA-MCP, ServiceBay-MCP) and reports per-component status. Read-only.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [observability, status, phase-1]
    related_skills: [oscar-audit-query, oscar-debug-set]
---

# OSCAR — status

## Overview

Quick "is everything OK?" probe across every OSCAR dependency. Backed by the shared `oscar_health` library — the same code that containers use at startup via `wait_for_ready`. Read-only — no state changes.

## When to use

- "OSCAR, bist du da?" / "Bist du wach?"
- "Funktioniert alles?" / "Geht das Licht gerade nicht?"
- "Ist Home Assistant erreichbar?"
- "Wo hakt's gerade?"
- As the **first** diagnostic step before deeper drill-down — if `oscar-status` says everything's green, the bug is application-side, not infrastructure.

## Operating sequence

1. Run:
   ```
   python -m oscar_health doctor
   ```
2. Parse the JSON output:
   ```json
   {
     "ok": false,
     "results": [
       {"name": "postgres", "ok": true, "latency_ms": 4},
       {"name": "hermes", "ok": true, "latency_ms": 12},
       {"name": "ollama", "ok": true, "latency_ms": 8},
       {"name": "ha-mcp", "ok": false, "latency_ms": 3000, "detail": "ConnectError: ..."},
       {"name": "connector:http://127.0.0.1:8801", "ok": true, "latency_ms": 6}
     ]
   }
   ```
3. Summarise verbally:
   - **All green** → "Alles ok." or "Alles grün."
   - **One red** → name it: "HA-MCP antwortet nicht — ich erreiche Home Assistant gerade nicht."
   - **Multiple red** → group by impact: "Postgres und HERMES sind beide down — das ist ernst."

## What gets probed

Auto-discovered from env vars on the HERMES container:

| Env var | Probe | Failure means |
|---|---|---|
| `OSCAR_POSTGRES_DSN` | Postgres `SELECT 1` | OSCAR can't read/write its own state |
| `OSCAR_HERMES_URL` | HTTP GET `/health` | HERMES is down (rare; we're talking to it) |
| `OSCAR_OLLAMA_URL` | HTTP GET `/api/tags` | Local LLM offline → cloud or local-only fallback |
| `OSCAR_WHISPER_HOST` | TCP open | Voice STT broken |
| `OSCAR_PIPER_HOST` | TCP open | Voice TTS broken |
| `OSCAR_OPENWAKEWORD_HOST` | TCP open | Wakeword detection broken (less critical, device often does it locally) |
| `OSCAR_CONNECTORS_URLS` | HTTP GET each | One or more connectors unreachable |
| `OSCAR_HA_MCP_URL` | HTTP GET | Home control broken |
| `OSCAR_SERVICEBAY_MCP_URL` | HTTP GET | Platform-control broken |

Missing env vars are silently skipped — that's the "this OSCAR install doesn't have a Voice PE wired yet" case, not a failure.

## What this does NOT cover

- **Skill correctness** — we know HERMES is reachable, not that a specific skill behaves. For that, `oscar-audit-query` over `cloud_audit` and the relevant SKILL events.
- **Voice latency** — TCP-open says the port is alive, not that it's fast. For latency hunting, `oscar-debug-set` + the gatekeeper's `gatekeeper.transcript` / `gatekeeper.response` timestamps.
- **HA device state** — "is the office light actually on?" is an HA-MCP query, not a status probe.

## Failure paths

- The `oscar_health` script itself crashes → respond "Ich kann das gerade selbst nicht prüfen, sieh mal in ServiceBay nach." This is rare and points at something fundamentally broken (Python image without the lib, etc.).

## Phase mapping

| Phase | Probes covered |
|---|---|
| **1 (now)** | postgres, hermes, ollama, whisper, piper, openwakeword, connectors, ha-mcp, servicebay-mcp |
| **2** | + speaker-ID model loaded? (gatekeeper local check) |
| **3a** | + ingestion-pipeline backlog (rows in incoming state) |
