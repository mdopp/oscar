# oscar-connectors

ServiceBay Pod-YAML template: one container per connector, all in the same pod, all sharing a single bearer (`CONNECTORS_BEARER`) for Hermes auth.

Phase 1 ships with **weather** as the reference external connector and **cloud-llm** as the per-request cloud-escalation connector. Web-search and the Phase-3a enrichment connectors (Open Library, MusicBrainz, Discogs) follow the same pattern when their issues are scoped.

## Containers

| Container | Image | Port (host) | Tools |
|---|---|---|---|
| `weather` | `ghcr.io/mdopp/oscar-connector-weather:latest` | `WEATHER_PORT` (8801) | `current_weather(location)`, `forecast(location, days)` |
| `cloud-llm` | `ghcr.io/mdopp/oscar-connector-cloud-llm:latest` | `CLOUD_LLM_PORT` (8802) | `complete(vendor, model, prompt, …)` — Anthropic + Google backends, writes to `cloud_audit` |

The connector code lives under [`../../connectors/`](../../connectors/) (`connectors/weather/`, copy-template at `connectors/_skeleton/`). Build pattern: [`docs/connector-skeleton.md`](../../docs/connector-skeleton.md).

## Hermes wiring

Hermes is host-installed (not a container). To make it use these connectors:

```bash
hermes mcp add http://<oscar-host>:{{WEATHER_PORT}}     --token <CONNECTORS_BEARER>
hermes mcp add http://<oscar-host>:{{CLOUD_LLM_PORT}}   --token <CONNECTORS_BEARER>
```

Hermes picks them up on next start; the connectors expose standard MCP `tools/list` for discovery. The smoke test below works against each connector directly without going through Hermes.

## cloud-llm vs Hermes' direct cloud provider

Two different things — easy to mix up:

| | This connector | Hermes' direct cloud provider |
|---|---|---|
| Container | `oscar-connectors/cloud-llm` | (none — Hermes talks straight to Anthropic/Google/OpenRouter) |
| When | Per-request escalation from an otherwise-local stack | Whole agent uses cloud as primary LLM |
| Audit | Every call lands in `cloud_audit` (OSCAR Postgres) | Hermes' own session log; no household audit table |
| Privacy stance | OSCAR default — escalations are explicit | Opt-out of OSCAR's default privacy |

You typically use **one or the other**, not both. With local Ollama on oscar-brain, this connector handles the rare escalation. With no local LLM at all, set Hermes' provider directly via `hermes model`; this connector becomes redundant.

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
6. Update `oscar-brain` to point Hermes at the new connector URL.

Architecture: [`../../oscar-architecture.md`](../../oscar-architecture.md) → "7. External connectors".
