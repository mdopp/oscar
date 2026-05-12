# cloud-llm connector

Per-request cloud-LLM escalation from an otherwise-local OSCAR stack. One MCP tool, two backends:

- `complete(vendor, model, prompt, system?, max_tokens?, router_score?, escalation_reason?)`
  - `vendor`: `anthropic` | `google`
  - `model`: provider-side id (`claude-sonnet-4`, `gemini-2.5-flash`, …)

Every call writes a row to `cloud_audit` in `oscar-brain.postgres` (timestamp, uid, trace_id, prompt-hash, lengths, latency, cost-estimate, router score + reason). Full-text prompt/response only when `OSCAR_DEBUG_MODE=true`.

## Relation to the cloud deployment mode

Two different things, easy to mix up:

| | This connector | `oscar-brain` cloud deployment mode |
|---|---|---|
| What | **Per-request** escalation tool | Deployment-wide LLM backend |
| Local model? | Yes (Gemma local stays primary) | No (Ollama container skipped) |
| Triggered by | Gemma-1B router decides per query | Every HERMES call |
| Privacy stance | OSCAR default — escalations are explicit + audited | Opt-out of OSCAR's privacy stance |

Use this connector when you have a GPU (or CPU-local) deployment and want the option of routing hard queries to cloud. Use the deployment mode when you have no local hardware at all.

## Build

```bash
podman build -t ghcr.io/mdopp/oscar-connector-cloud-llm:latest \
  -f connectors/cloud-llm/Dockerfile .
# from the repo root so shared/oscar_logging copies in
```

CI publishes on every push to `main` and on `v*` tags via `.github/workflows/build-images.yml`.

## Run locally

```bash
pip install -e ./shared/oscar_logging
pip install -e ./connectors/cloud-llm[test]

CONNECTORS_BEARER=dev \
ANTHROPIC_API_KEY=sk-ant-... \
GOOGLE_API_KEY=AIza... \
POSTGRES_DSN=postgresql://oscar:test@localhost:5432/oscar \
OSCAR_DEBUG_MODE=true \
python -m cloud_llm.server
```

Inspect with `mcp-inspector http://localhost:8802`.

## Tests

```bash
cd connectors/cloud-llm
pytest
```

`pytest-httpx` mocks both upstream APIs; `audit.record` is patched to an `AsyncMock` so the tests need no Postgres.

## Open follow-ups

- **Streaming responses** — both Anthropic and Google support SSE streaming, the connector currently buffers full responses. Phase 2+ if voice latency starts to dominate the cloud-call round-trip.
- **Cost-table refresh** — `pricing.py` was last touched May 2026. Quarterly check or move to a remote price feed.
- **OpenAI support** — not in scope for OSCAR; both Anthropic and Google offer everything we need, and Anthropic is the model we use for OSCAR's own development.
