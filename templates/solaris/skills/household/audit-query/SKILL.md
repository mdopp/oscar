---
name: solaris-audit-query
description: Read-only filter over Solaris's audit tables in solaris.db (cloud_audit — cloud-LLM calls). Use for "what happened / what did it cost / show errors" questions.
kind: skill
scope: household
command: /audit
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — audit query

Generic filter over Solaris's domain-audit tables in `solaris.db`. One query
returns a JSON page of rows; summarise in natural language. Read-only — never
mutates state. Also runnable on demand as `/audit`.

Currently one stream: `cloud_audit` — every cloud-LLM call (timestamp, uid,
trace_id, vendor, lengths, latency, cost-estimate, router score/reason;
prompt/response fulltext only when debug-mode is on).

## When to use

- "What did Solaris send to the cloud today?" / "Wieviel hat das gestern gekostet?"
- "Show me errors in the last hour."
- "Find every event tied to trace_id <X>."

Out of scope: mutating state (use `solaris-debug-set` for the debug flag);
operational stdout logs (ServiceBay-MCP `get_logs`); conversation history.

## Operating sequence

1. Parse the request into `stream` (always `cloud_audit` for now), `since`/`until`
   (natural-language time → `today` / `1h` / ISO), and filter fields (`uid`,
   `vendor`, `trace_id`, `min_cost_micro_usd`).
2. Open `solaris.db` (path from `SOLARIS_DB_PATH`, default
   `/var/lib/solaris/solaris.db`) and run a parameterised SELECT against
   `cloud_audit` with `LIMIT` (default 50, max 200).
3. Summarise in 1–3 sentences; aggregate when sensible ("Heute 7 Cloud-Anfragen,
   alle Claude Sonnet, ~12 Cent gesamt, längste 2.3 s."). Don't read UUIDs/hashes
   aloud.

## PII

`prompt_fulltext` / `response_fulltext` are returned only when
`system_settings.debug_mode.active = true` (read live); otherwise the fulltext is
nulled and only metadata (lengths, hash, latency, cost) comes back. For deep
debugging, flip debug-mode for a short window via `solaris-debug-set`, re-run, then
turn it off. Don't reconstruct prompts from hashes.

## Failure paths

- `solaris.db` missing/unreadable → "Ich kann das Audit-Log gerade nicht lesen."
- Empty result → "Heute hat Solaris nichts an die Cloud geschickt."
