# Connector skeleton

> Status: draft, May 2026. Target phase: Phase 1 (for every new external / enrichment connector). Home: `connectors/_skeleton/` as the copy template, `connectors/<name>/` for concrete connectors.

Every external integration in OSCAR is a separate connector, its own container in the `oscar-connectors` pod, its own MCP server. This document fixes the repetition pattern — if every connector were built differently, we would sabotage our own audit discipline.

## What a connector is (recap)

A defined purpose, what goes out, what comes in, logged. Hermes (in `oscar-hermes`) is the only legitimate caller; connectors therefore don't enforce any harness internally, only the shared bearer (see below). Architecture anchoring: [oscar-architecture.md — external connectors](../oscar-architecture.md).

## Repo layout

```
connectors/
├── _skeleton/                      # copy template for new connectors
│   ├── server.py                   # FastMCP entry, registers tools
│   ├── config.py                   # Pydantic settings, reads env vars
│   ├── tools/                      # one module per MCP tool
│   │   ├── __init__.py
│   │   └── example.py
│   ├── tests/
│   │   └── test_example.py
│   ├── pyproject.toml
│   └── Dockerfile
├── weather/
├── web-search/
└── cloud-llm/
```

One connector = one directory = one container image = one FastMCP server. No mono-image, no per-tool switch — isolation per external source is worth more than the MB saved.

## Server pattern (FastMCP)

```python
# connectors/<name>/server.py
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from oscar_logging import log
from .config import settings
from .tools import current_weather, forecast

auth = StaticTokenVerifier(
    tokens={settings.connectors_bearer: {"sub": "hermes", "client_id": "oscar-brain"}}
)

mcp = FastMCP(
    name=f"oscar-connector-{settings.connector_name}",
    auth=auth,                        # accepts Authorization: Bearer <CONNECTORS_BEARER>
)

mcp.tool()(current_weather.run)
mcp.tool()(forecast.run)

if __name__ == "__main__":
    log.info("connector.boot", component=settings.connector_name, port=settings.port)
    mcp.run(host="0.0.0.0", port=settings.port, transport="streamable-http")
```

```python
# connectors/<name>/tools/example.py
from pydantic import BaseModel, Field
from oscar_logging import log
from ..config import settings

class CurrentWeatherInput(BaseModel):
    location: str = Field(..., description="Place name or postal code")
    units: str = Field("metric", description="metric|imperial")

class CurrentWeatherOutput(BaseModel):
    temperature_c: float
    condition: str
    fetched_at: str

async def run(input: CurrentWeatherInput, ctx) -> CurrentWeatherOutput:
    trace_id = ctx.request_context.meta.get("trace_id")
    log.info("connector.call", event_type="current_weather",
             trace_id=trace_id, location=input.location)
    # ... API call ...
    return CurrentWeatherOutput(...)
```

Convention: the tool function is always called `run`; input/output are explicit Pydantic models (no `**kwargs`); read `trace_id` from the MCP context, include it in every log.

## Auth: shared bearer

All connectors in the `oscar-connectors` pod accept the same bearer (`CONNECTORS_BEARER`); Hermes sends it on every call:

```
Authorization: Bearer <CONNECTORS_BEARER>
```

The token is generated at ServiceBay deploy time as `type: secret` in the template `variables.json`, once per `oscar-connectors` pod, shared between all containers via a pod-internal env var. A per-connector token would be finer-grained but is overkill for a 4-person household — the Hermes harness layer is the real permission gate; the bearer only stops "some other pod on the host" from calling.

## variables.json example (weather connector)

```json
{
  "connectorsBearer": {
    "type": "secret",
    "label": "Connectors bearer (Hermes → connectors)",
    "description": "Generated at deploy and propagated to Hermes + all connector containers. Regenerating means redeploying every connector.",
    "generate": true,
    "required": true
  },
  "weatherApiKey": {
    "type": "secret",
    "label": "OpenWeatherMap API key",
    "description": "Free tier is enough for a family. Get one at openweathermap.org/api.",
    "required": true
  },
  "weatherLanguage": {
    "type": "select",
    "label": "Forecast language",
    "options": ["de", "en"],
    "default": "de"
  },
  "weatherUnits": {
    "type": "select",
    "label": "Units",
    "options": ["metric", "imperial"],
    "default": "metric"
  }
}
```

Convention: connector-specific variables carry a `<connector>` prefix, pod-global ones (like `connectorsBearer`) don't. Makes "what variables do I need for the weather connector?" greppable.

## Pod-YAML integration

Each connector is a container in the `template.yml` of the `oscar-connectors` pod:

```yaml
- name: weather
  image: ghcr.io/mdopp/oscar-connector-weather:{{version}}
  env:
    - name: CONNECTOR_NAME
      value: weather
    - name: PORT
      value: "8801"
    - name: CONNECTORS_BEARER
      valueFrom: { secretKeyRef: { name: connectors-bearer, key: token } }
    - name: WEATHER_API_KEY
      valueFrom: { secretKeyRef: { name: weather-api-key, key: token } }
    - name: WEATHER_LANGUAGE
      value: "{{weatherLanguage}}"
    - name: OSCAR_COMPONENT
      value: connector-weather
  ports:
    - containerPort: 8801
```

Port convention: 8800–8899 for connector MCP servers, incrementing per connector. Hermes holds the mapping table.

## Logging

Mandatory: the `shared/oscar_logging` library (see [docs/logging.md](logging.md)). Connectors log at minimum:

| Event | Level | When |
|---|---|---|
| `connector.boot` | info | on container start |
| `connector.call` | info | every tool call (with `trace_id`, without request body at `debug_mode=false`) |
| `connector.external_fail` | warn | external API returned an error, local fallback applied |
| `connector.external_error` | error | external API unreachable, no fallback |

Request/response bodies are only logged when `debug_mode=true` — the library checks that automatically.

## Local run pattern

```bash
# In the connector directory
cd connectors/weather

# Dev-run against local env vars
CONNECTOR_NAME=weather PORT=8801 CONNECTORS_BEARER=dev WEATHER_API_KEY=... \
  python -m server

# In a second terminal: FastMCP Inspector
mcp-inspector http://localhost:8801
```

`pyproject.toml` declares `oscar-logging` + `oscar-connector-base` (a shared library in the repo) as dependencies; per-connector deps (e.g. `httpx`, `feedparser`) on top.

## Tests

`pytest` with `httpx-mock` for external API calls — no real calls in CI, no real keys in test configs. Minimum tests:

- Happy path per tool: input → mock response → expected output
- External failure: mock 500 → `connector.external_fail` logged, tool returns a sensible default or raises a clear exception
- Auth: request without bearer → 401; wrong bearer → 401

## Deliberately not now

- **Per-connector bearer.** Shared bearer stays until Phase 3+, when connectors might warrant different trust levels.
- **Rate limiting inside the connector.** External APIs enforce that themselves; at 4 people no OSCAR-side throttling is needed.
- **Caching layer inside the connector.** Weather data is short-lived, Discogs lookups happen once per material item. Hermes can cache conversationally if the LLM step would repeat the same call.
