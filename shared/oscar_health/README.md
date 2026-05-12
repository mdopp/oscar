# oscar-health

Two use cases, one library:

- **`wait_for_ready`** — call at container startup, blocks until all dependencies (Postgres, HTTP endpoints, TCP ports) are reachable. With per-probe exponential backoff and a hard overall timeout. Solves the "Postgres came up two seconds after the gatekeeper, gatekeeper crash-looped five times" boot-flap.
- **`check_all`** — one-shot diagnostic that pings everything and returns a structured status report. Backs the `oscar-status` HERMES skill and the operator CLI.

## CLI

```bash
# Block until ready, then exec the real entry-point
python -m oscar_health wait \
  --postgres "$POSTGRES_DSN" \
  --http http://localhost:11434/api/tags \
  --tcp 127.0.0.1:10300 \
  --timeout 60

# Same checks, single round-trip, JSON output
python -m oscar_health check \
  --postgres "$POSTGRES_DSN" \
  --http http://localhost:11434/api/tags

# "Doctor" mode — auto-discover checks from env vars (used by skills/status)
python -m oscar_health doctor
```

`doctor` reads:

| Env var | Probe |
|---|---|
| `OSCAR_POSTGRES_DSN` | Postgres `SELECT 1` |
| `OSCAR_HERMES_URL` | HTTP GET `${URL}/health` |
| `OSCAR_OLLAMA_URL` | HTTP GET `${URL}/api/tags` |
| `OSCAR_WHISPER_HOST` (`host:port`) | TCP open |
| `OSCAR_PIPER_HOST` | TCP open |
| `OSCAR_OPENWAKEWORD_HOST` | TCP open |
| `OSCAR_CONNECTORS_URLS` (comma-separated) | HTTP GET each |
| `OSCAR_HA_MCP_URL` | HTTP GET |
| `OSCAR_SERVICEBAY_MCP_URL` | HTTP GET |

Missing env vars are skipped (not failed).

## Library

```python
from oscar_health import Check, CheckResult, wait_for_ready, check_all

await wait_for_ready(
    checks=[
        Check.postgres(dsn=os.environ["POSTGRES_DSN"]),
        Check.http("http://localhost:8000/health"),
        Check.tcp("127.0.0.1", 10300),
    ],
    timeout_s=60,
)

# One-shot
report = await check_all([Check.postgres(dsn=...), Check.http(...)])
# report is list[CheckResult]
```

## Tests

```bash
cd shared/oscar_health
pytest
```

Postgres probe is mocked via `unittest.mock.patch` on `asyncpg.connect`. HTTP probe uses pytest-httpx. TCP probe uses a real loopback socket.
