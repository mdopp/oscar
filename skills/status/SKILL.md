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

> **TODO (rewrite).** The implementation needs to call ServiceBay-MCP's `get_health_checks` and `diagnose` tools, **not** run inline probes. ServiceBay shipped a 16-check-type health system in v3.35–v3.37 — templates declare what should be probed (via `create_health_check` at deploy time, registered by `oscar-household`'s `post-deploy.py`), the platform polls outside-in, this skill just reads the aggregated state. Contract doc tracked in [`mdopp/servicebay#543`](https://github.com/mdopp/servicebay/issues/543). The env-var probe table below is vestigial from before the platform's health system was discovered — once the rewrite lands, this skill becomes ~10 lines that call two MCP tools and summarise.

## When to use

- "OSCAR, bist du da?" / "Bist du wach?"
- "Funktioniert alles?" / "Geht das Licht gerade nicht?"
- "Ist Home Assistant erreichbar?"
- "Wo hakt's gerade?"
- As the **first** diagnostic step before deeper drill-down — if `oscar-status` says everything's green, the bug is application-side, not infrastructure.

## Operating sequence

1. Call ServiceBay-MCP `get_health_checks` to retrieve the platform's aggregated health state.
2. For each result, the platform returns the canonical shape:
   ```json
   {
     "ok": false,
     "results": [
       {"name": "ollama", "ok": true, "latency_ms": 8, "type": "http"},
       {"name": "hermes-api", "ok": true, "latency_ms": 12, "type": "http"},
       {"name": "ha-mcp", "ok": false, "latency_ms": 3000, "type": "http", "detail": "ConnectError: ..."},
       {"name": "oscar.db", "ok": true, "latency_ms": 1, "type": "script"}
     ]
   }
   ```
3. If a result needs deeper context (specific error chain, last successful run, history), call `diagnose <check-id>` for that one check.
4. Summarise verbally:
   - **All green** → "Alles ok." or "Alles grün."
   - **One red** → name it: "HA-MCP antwortet nicht — ich erreiche Home Assistant gerade nicht."
   - **Multiple red** → group by impact: "Hermes und Ollama sind beide down — das ist ernst."

## What gets probed

This skill **does not** define what gets probed. The set of health checks is **declared at deploy time** by each template's `post-deploy.py` via `create_health_check` against ServiceBay-MCP. `oscar-household` registers:

| Check | Type | Purpose |
|---|---|---|
| `oscar.db` | `script` | SQLite open + `SELECT 1` on `cloud_audit` — OSCAR's audit state readable |
| `hermes-api` | `http` | Hermes' `/health` endpoint reachable with the token |
| `ollama` | `http` | Local LLM responding to `/api/tags` |
| `ha-mcp` | `http` | HA's native MCP server reachable |
| `servicebay-mcp` | `http` | Platform control surface reachable |
| `gatekeeper` *(Phase 1)* | `http` | Gatekeeper container's internal `/push/health` |
| `voice-whisper` *(Phase 1)* | `podman` | Whisper container running |
| `voice-piper` *(Phase 1)* | `podman` | Piper container running |

ServiceBay's existing templates (`home-assistant`, `media`, …) register their own checks the same way. The full check set lives in ServiceBay's HealthStore.

## What this does NOT cover

- **Skill correctness** — we know Hermes is reachable, not that a specific skill behaves. For that, `oscar-audit-query` over `cloud_audit` and the relevant SKILL events.
- **Voice latency** — `podman`/`http` checks say the service is up, not that it's fast. For latency hunting, `oscar-debug-set` + the gatekeeper's `gatekeeper.transcript` / `gatekeeper.response` timestamps.
- **HA device state** — "is the office light actually on?" is an HA-MCP query, not a status probe.

## Failure paths

- ServiceBay-MCP unreachable → respond "Ich kann das gerade selbst nicht prüfen — ServiceBay antwortet nicht." Points at something fundamentally broken at the platform level (network, auth, ServiceBay itself).

## Phase mapping

| Phase | Checks registered by oscar-household's post-deploy |
|---|---|
| **0 (now)** | oscar.db, hermes-api, ollama, ha-mcp, servicebay-mcp |
| **1** | + gatekeeper, voice-whisper, voice-piper |
| **2** | + gatekeeper-speaker-id (model loaded? embeddings table populated?) |
| **3a** | + ingestion-pipeline backlog (rows in incoming state) |
