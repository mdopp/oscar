# oscar-connectors

ServiceBay Pod-YAML template: one container per connector, all in the same pod, all sharing a single bearer (`CONNECTORS_BEARER`) for HERMES auth.

Phase 1 ships with the **weather** connector as the reference implementation. Cloud-LLM and web-search will arrive as additional containers in the same pod once their issues are scoped.

## Containers

| Container | Image | Port (host) | Tools |
|---|---|---|---|
| `weather` | `ghcr.io/mdopp/oscar-connector-weather:latest` | `WEATHER_PORT` (8801) | `current_weather(location)`, `forecast(location, days)` |

The connector code lives under [`../../connectors/`](../../connectors/) (`connectors/weather/`, copy-template at `connectors/_skeleton/`). Build pattern: [`docs/connector-skeleton.md`](../../docs/connector-skeleton.md).

## HERMES wiring

`oscar-brain` needs `CONNECTORS_BEARER` as an env var, plus a list of connector URLs for HERMES to talk to. The Phase-1 follow-up on `oscar-brain` will:

- Add `CONNECTORS_BEARER` to `oscar-brain/variables.json` (`type: secret`, must match what's set here).
- Add `WEATHER_MCP_URL=http://127.0.0.1:{{WEATHER_PORT}}` to HERMES's env.
- Register the weather endpoint with HERMES's MCP-client config.

Until that lands, the weather connector runs but HERMES can't reach it. The smoke test below works against the connector directly.

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
