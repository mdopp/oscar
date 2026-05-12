# oscar-connectors

ServiceBay Pod-YAML template: one container per connector, all in the same pod, all sharing a single bearer (`CONNECTORS_BEARER`) for HERMES auth.

Phase 1 ships with **weather** as the reference external connector and **cloud-llm** as the per-request cloud-escalation connector. Web-search and the Phase-3a enrichment connectors (Open Library, MusicBrainz, Discogs) follow the same pattern when their issues are scoped.

## Containers

| Container | Image | Port (host) | Tools |
|---|---|---|---|
| `weather` | `ghcr.io/mdopp/oscar-connector-weather:latest` | `WEATHER_PORT` (8801) | `current_weather(location)`, `forecast(location, days)` |
| `cloud-llm` | `ghcr.io/mdopp/oscar-connector-cloud-llm:latest` | `CLOUD_LLM_PORT` (8802) | `complete(vendor, model, prompt, …)` — Anthropic + Google backends, writes to `cloud_audit` |

The connector code lives under [`../../connectors/`](../../connectors/) (`connectors/weather/`, copy-template at `connectors/_skeleton/`). Build pattern: [`docs/connector-skeleton.md`](../../docs/connector-skeleton.md).

## HERMES wiring

`oscar-brain` needs `CONNECTORS_BEARER` as an env var, plus per-connector URLs for HERMES to talk to. The follow-up on `oscar-brain` will:

- Add `CONNECTORS_BEARER` to `oscar-brain/variables.json` (must match what's set here).
- Add `WEATHER_MCP_URL=http://127.0.0.1:{{WEATHER_PORT}}` and `CLOUD_LLM_MCP_URL=http://127.0.0.1:{{CLOUD_LLM_PORT}}` to HERMES's env.
- Register both endpoints with HERMES's MCP-client config.

Until that lands, the connectors run but HERMES can't reach them. The smoke test below works against the connector directly.

## cloud-llm vs deployment-mode `cloud`

Two different things in the codebase — easy to mix up:

| | This connector | `oscar-brain` cloud deployment mode |
|---|---|---|
| Container | `oscar-connectors/cloud-llm` | (no Ollama; HERMES talks straight to provider) |
| When | Per-request escalation from a local stack | Whole stack uses cloud as primary LLM |
| Audit | Every call lands in `cloud_audit` | Per-call audit not built-in to HERMES |
| Privacy stance | OSCAR default — escalations are explicit | Opt-out of OSCAR's default privacy |

You typically use **one or the other**, not both. With local Gemma, ship this connector. With no local LLM at all (cloud deployment), `HERMES_API_KEY` on `oscar-brain` is the path; this connector becomes redundant.

## Smoke test

After deploy, with `OSCAR_DEBUG_MODE=true` already set in the template:

```bash
# Bearer-less → should refuse
curl -X POST http://localhost:8801/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# → 401

# With bearer → list tools
curl -X POST http://localhost:8801/mcp \
  -H 'Authorization: Bearer <CONNECTORS_BEARER>' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# → current_weather and forecast in the result

# Logs show the tool call
# ServiceBay-MCP: get_container_logs(id="oscar-connectors-weather")
```

## Adding a new connector

1. Copy `connectors/_skeleton/` to `connectors/<name>/` and replace `CONNECTOR_NAME` placeholders.
2. Implement the tool modules under `src/<name>/tools/`.
3. Add a container block to `templates/oscar-connectors/template.yml` (next port in the 8800-range).
4. Add the connector-specific variables to `variables.json` (use the `<name>_` prefix convention).
5. Publish the image to GHCR (manual until the CI workflow lands).
6. Update `oscar-brain` to point HERMES at the new connector URL.

Architecture: [`../../oscar-architecture.md`](../../oscar-architecture.md) → "7. External connectors".
