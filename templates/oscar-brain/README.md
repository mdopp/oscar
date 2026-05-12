# oscar-brain

ServiceBay Pod-YAML template: HERMES (GPU-capable) + Ollama (Gemma 4-12B Q4 + Gemma 4-1B router) + Qdrant + Postgres + a `pg_dump` backup sidecar.

Phase 0 target. Phase 1 adds a `signal-cli-daemon` sidecar for the HERMES Signal gateway (separate issue / template diff).

## Containers

| Container | Image | Purpose |
|---|---|---|
| `hermes` | `ghcr.io/nousresearch/hermes-agent:latest` | Agent core. Hosts OSCAR skills, talks to Ollama, Postgres, Qdrant, and (via MCP) to ServiceBay-MCP, HA-MCP, and `oscar-connectors`. |
| `ollama` | `docker.io/ollama/ollama:latest` | Local LLM runtime. Pulls `HERMES_MODEL` + `ROUTER_MODEL` on first start. **Requires GPU passthrough** (see below). |
| `qdrant` | `docker.io/qdrant/qdrant:latest` | Vector store for OSCAR domain memory. Empty in Phase 0; populated from Phase 3a. |
| `postgres` | `docker.io/postgres:16-alpine` | Initial schema is dropped into `/docker-entrypoint-initdb.d/` by a wrapper command; the standard postgres entrypoint picks it up on first start. |
| `pg-backup` | `docker.io/postgres:16-alpine` | Sidecar that runs `pg_dump` weekly and prunes dumps older than 28 days. |

## Initial Postgres schema

Created on first deploy (empty data dir) by the inline init script. Updates on re-deploys do **not** run again — schema migrations from Phase 1 onward go through a dedicated migration tool (alembic / sqitch — open question #4 in the architecture doc).

- `system_settings (key, value JSONB, updated_at)` — global flags. Seeded with `debug_mode = {active: true, verbose_until: null, latency_annotations: false}` for Phase 0.
- `time_jobs` — backing store for the `timer` and `alarm` skills. Full schema: [`docs/timer-and-alarm.md`](../../docs/timer-and-alarm.md).
- `gateway_identities` — phone-number / chat-id → LLDAP-uid mapping for Phase 1 Signal/Telegram. Full schema: [`docs/gateway-identities.md`](../../docs/gateway-identities.md).
- `cloud_audit` — structured record of every Cloud-LLM-connector call (Phase 1+). Metadata-only by default; `prompt_fulltext`/`response_fulltext` filled only when `debug_mode.active=true`.

## Host prerequisites

- **Fedora CoreOS with nvidia-container-toolkit + CDI configured**, so the Ollama container can use `resources.limits.nvidia.com/gpu: "1"`. If `nvidia-smi` works on the host but the container sees no GPU, the CDI spec is missing (`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`).
- ServiceBay v3.16+ on the same host.
- HA-MCP integration (`mcp_server`) enabled in your HA pod and a long-lived access token minted for HERMES.
- ServiceBay-MCP bearer token with scope `read+lifecycle`.

## Deploy steps

1. In ServiceBay, add `github.com/mdopp/oscar.git` as an external registry under Settings → Registries.
2. Pick `oscar-brain` from the wizard.
3. Fill in the variables. The wizard auto-generates `POSTGRES_PASSWORD`; the rest you provide:
   - `HA_MCP_URL` (e.g. `https://ha.dopp.cloud/mcp_server/sse`) and `HA_MCP_TOKEN`
   - `SERVICEBAY_MCP_URL` (usually `https://<this-host>/mcp`) and `SERVICEBAY_MCP_TOKEN`
   - `HERMES_MODEL` and `ROUTER_MODEL` — defaults assume Gemma 4 is available on Ollama; fall back to `gemma3:12b-instruct-q4_K_M` / `gemma3:1b` if not.
   - `OSCAR_REGISTRY_DIR` — only override if your ServiceBay install uses a non-default registry path.
4. Deploy. First start takes ~5–10 minutes while Ollama downloads the models (depending on bandwidth).
5. Smoke test:
   - `curl http://localhost:{{HERMES_PORT}}/health` should answer 200.
   - `curl http://localhost:{{OLLAMA_PORT}}/api/tags` should list the pulled models.
   - In ServiceBay-MCP: `get_container_logs(id="oscar-brain-hermes")` should show structured JSON lines.

## Storage paths

All under `{{DATA_DIR}}/oscar-brain/` on the host:

| Subdir | Contents | Backed up by `pg-backup`? |
|---|---|---|
| `hermes/` | HERMES Honcho DB, cron jobs in `~/.hermes/cron/jobs.json`, session data | no — covered by ServiceBay's own backup pipeline |
| `ollama/` | Downloaded model blobs (large; re-downloadable) | no |
| `qdrant/` | Vector index | no in Phase 0; review when Phase 3a populates it |
| `postgres/` | OSCAR domain tables, audit, settings | **yes** — `pg_dump` weekly into `postgres-backups/` |
| `postgres-backups/` | `oscar-YYYYMMDD-HHMMSS.sql.gz`, 4-week retention | this *is* the backup |

## Shared library

`shared/oscar_logging/` is mounted read-only at `/opt/oscar/shared/oscar_logging` and prepended to `PYTHONPATH` so OSCAR skills can `from oscar_logging import log` without an image rebuild. The mount points at `OSCAR_REGISTRY_DIR/shared/oscar_logging/src/oscar_logging`, which is the package directory inside the src-layout project at `shared/oscar_logging/`.

To switch to a real pip install later (e.g. in a derived `ghcr.io/mdopp/oscar-hermes` image): `pip install /path/to/shared/oscar_logging` works thanks to the `pyproject.toml`.

## Logging

stdout-JSON from every container goes to journald; read it via ServiceBay-MCP `get_container_logs(id="oscar-brain-<container>")`. Full convention: [`docs/logging.md`](../../docs/logging.md).

`OSCAR_DEBUG_MODE=true` is set as a pod env in Phase 0 so HERMES and skills log full bodies. Switch off via the `debug.set` admin skill once OSCAR is in productive family use.

## Open follow-ups

- **GPU passthrough validation:** the `resources.limits.nvidia.com/gpu: "1"` declaration relies on ServiceBay's Pod-to-Quadlet translation honouring it. If the resulting Quadlet unit doesn't pass through the GPU, a small ServiceBay change may be required; track upstream once deployed.
- **Schema migrations:** the inline init script handles Phase 0 only. Phase 1 schema changes (e.g. adding `gatekeeper_voice_embeddings` in Phase 2) need a real migration tool — alembic or sqitch, decision deferred (architecture doc open point #4).
- **Custom HERMES image:** mounting `shared/oscar_logging` via hostPath works but is fragile against ServiceBay registry-path changes. A derived `ghcr.io/mdopp/oscar-hermes` image with `oscar-logging` pre-installed is the long-term answer.
