---
name: oscar-audit-query
description: Use when the user asks "what happened today?", "show me errors in the last hour", "what did the cloud connector cost yesterday?", "who's linked to which Signal number?", "what timers are armed?". Reads from OSCAR's domain-audit Postgres tables via `python -m oscar_audit query`. Read-only — never mutates state.
version: 0.1.0
author: OSCAR
license: MIT
metadata:
  hermes:
    tags: [audit, observability, phase-1]
    related_skills: [oscar-debug-set]
---

# OSCAR — audit.query

## Overview

Generic filter over OSCAR's domain-audit tables. One CLI call returns a JSON page of rows; the agent summarises in natural language for the user.

Three streams in Phase 1:
- `cloud_audit` — every cloud-LLM call (timestamp, uid, trace_id, vendor, lengths, latency, cost-estimate, router score + reason; prompt/response fulltext only when debug-mode is on)
- `gateway_identities` — phone-number / chat-id → LLDAP-uid mappings
- `time_jobs` — armed timers and alarms

More streams plug into the same dispatch (`gatekeeper_decisions` Phase 2+, `ingestion_classifications` Phase 3a+).

## When to use

- "What did OSCAR send to the cloud today?"
- "Wieviel hat das gestern gekostet?"
- "Show me errors in the last hour."
- "Welche Wecker sind gerade scharf?"
- "Wer ist hinter der Signal-Nummer +49…?"
- "Find every event tied to trace_id <X>."

Out of scope:
- Anything that mutates state (use the corresponding skill instead — `oscar-timer`, `oscar-identity-link`, `oscar-debug-set`).
- Reading **operational** logs (stdout-JSON). Those go through ServiceBay-MCP `get_container_logs` — different mechanism entirely.

## Operating sequence

1. Parse the user request into:
   - `stream` (which table): infer from the topic. "cloud cost" → `cloud_audit`. "Signal user" → `gateway_identities`. "timer/alarm" → `time_jobs`.
   - `since` / `until`: parse natural-language time. "today" → `today`. "last hour" → `1h`. "yesterday evening" → ISO timestamp.
   - filter fields (`uid`, `vendor`, `trace_id`, `gateway`, `kind`, `state`, `min_cost_micro_usd`) as they apply.
2. Run:
   ```
   python -m oscar_audit query --stream <stream> --since <time> [--uid X] [--vendor X] [--trace-id X] [--limit N]
   ```
3. Parse the JSON output:
   ```
   {"ok": true, "stream": "cloud_audit", "count": 7, "rows": [...]}
   ```
4. Summarise verbally in 1–3 sentences. **Don't read UUIDs or hashes aloud.** Aggregate when sensible: "Heute 7 Cloud-Anfragen, alle Claude Sonnet, Gesamt ~12 Cent, längste 2.3 s."

## Filter cheat sheet

| Stream | Useful filters |
|---|---|
| `cloud_audit` | `--since`, `--uid`, `--vendor`, `--trace-id`, `--min-cost-micro-usd` |
| `gateway_identities` | `--gateway`, `--uid`, `--since` |
| `time_jobs` | `--uid`, `--kind`, `--state`, `--since` |

`--limit` defaults to 50; bump to 200 for trends but don't read all rows back, summarise.

## Failure paths

- Postgres unreachable → CLI exits non-zero, brief: "Ich kann das Audit-Log gerade nicht lesen."
- Unknown stream → "Den Audit-Stream gibt's nicht." (Should never happen with proper parsing.)
- Empty result → "Heute hat OSCAR nichts an die Cloud geschickt." / "Keine Wecker scharf."

## PII

`cloud_audit.prompt_fulltext` / `response_fulltext` are returned only when `OSCAR_DEBUG_MODE=true`. Otherwise the CLI returns the metadata (lengths, hash, latency, cost) plus `_pii_redacted: true` to make the masking explicit. **Don't try to reconstruct prompts from hashes.**

For deep debugging of a specific failure: instruct the user to flip debug-mode for a short window via `oscar-debug-set`, re-run the failing query, then turn debug-mode off.

## Phase mapping

| Phase | Streams |
|---|---|
| **1 (now)** | cloud_audit, gateway_identities, time_jobs |
| **2** | + gatekeeper_decisions (speaker-ID confidence, harness chosen, embedding distance) |
| **3a** | + ingestion_classifications (Gemma vision class, confidence, final domain) |
