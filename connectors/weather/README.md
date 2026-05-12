# weather connector

OpenWeatherMap-backed MCP server. Two tools:

- `current_weather(location)` → temperature, feels-like, condition, humidity, wind
- `forecast(location, days=3)` → 3-hour buckets up to 5 days

Free-tier OpenWeatherMap key is enough for a family.

## Build

```bash
podman build -t ghcr.io/mdopp/oscar-connector-weather:latest \
  -f connectors/weather/Dockerfile .
# (run from the repo root so shared/oscar_logging copies into the image)
```

## Run locally

```bash
pip install -e ./shared/oscar_logging
pip install -e ./connectors/weather[test]

CONNECTORS_BEARER=dev \
WEATHER_API_KEY=<your-owm-key> \
OSCAR_DEBUG_MODE=true \
python -m weather.server
```

Then talk to it via `mcp-inspector http://localhost:8801` or curl through the streamable-http transport.

## Tests

```bash
cd connectors/weather
pytest
```

`pytest-httpx` mocks all outbound HTTP — no real OpenWeatherMap calls in CI.

## Open follow-ups

- Per-tool rate-limit awareness (OWM free tier is 60/min).
- Optional location resolution against a local geocoding table (Phase 4+) so users can say "at home" without typing a city.
