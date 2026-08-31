---
name: solaris-status
description: A read-only health probe across every Solaris dependency (solaris.db, Ollama, Home Assistant, ServiceBay-MCP, voice). Use for "is everything working?" questions.
kind: skill
scope: household
command: /status
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — status

Quick "is everything OK?" probe across every Solaris dependency. Read-only — no
state changes. Also runnable on demand as `/status`.

## When to use

- "Solaris, bist du da?" / "Bist du wach?"
- "Funktioniert alles?" / "Geht das Licht gerade nicht?"
- "Ist Home Assistant erreichbar?" / "Wo hakt's gerade?"
- As the **first** diagnostic step before deeper drill-down — if status is green,
  the bug is application-side, not infrastructure.

## Operating sequence

1. Call ServiceBay-MCP `get_health_checks` for the platform's aggregated health
   state. Each result has the shape:
   ```json
   {"name": "ollama", "ok": true, "latency_ms": 8, "type": "http"}
   ```
2. For a result that needs deeper context, call `diagnose <check-id>` for that
   one check.
3. Summarise verbally:
   - **All green** → "Alles ok."
   - **One red** → name it: "Home Assistant antwortet nicht — ich erreiche die
     Haussteuerung gerade nicht."
   - **Multiple red** → group by impact.

## What gets probed

This skill does **not** define the check set — it is declared at deploy time by
`solarisbay`'s `post-deploy.py` via `create_health_check` (solaris.db, ollama,
home-assistant, servicebay-mcp, gatekeeper, the voice containers). The full set
lives in ServiceBay's HealthStore.

## Not covered

- **Container logs / voice latency** → ServiceBay-MCP `get_logs` on the
  gatekeeper container, not a Solaris command.
- **HA device state** ("is the office light on?") → an HA-tool query, not a probe.

## Failure paths

- ServiceBay-MCP unreachable → "Ich kann das gerade selbst nicht prüfen —
  ServiceBay antwortet nicht." (something is broken at the platform level).
