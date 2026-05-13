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
| `signal-cli-daemon` | `docker.io/bbernhard/signal-cli-rest-api:latest` | Phase-1 sidecar: HTTP front-end to signal-cli. Linked-device session state persists under `signal-cli/`. |
| `signal-gateway` | `ghcr.io/mdopp/oscar-signal-gateway:latest` | Phase-1 sidecar: polls `signal-cli-daemon` for incoming chats, maps sender numbers via `gateway_identities`, forwards to HERMES `/converse`, returns replies. Also exposes `POST /send` on 8090 for outbound DMs (timer/alarm fire, skill-reviewer notifications). |

## Initial Postgres schema

Created on first deploy (empty data dir) by the inline init script. Updates on re-deploys do **not** run again — schema migrations from Phase 1 onward go through a dedicated migration tool (alembic / sqitch — open question #4 in the architecture doc).

- `system_settings (key, value JSONB, updated_at)` — global flags. Seeded with `debug_mode = {active: true, verbose_until: null, latency_annotations: false}` for Phase 0.
- `time_jobs` — backing store for the `timer` and `alarm` skills. Full schema: [`docs/timer-and-alarm.md`](../../docs/timer-and-alarm.md).
- `gateway_identities` — phone-number / chat-id → LLDAP-uid mapping for Phase 1 Signal/Telegram. Full schema: [`docs/gateway-identities.md`](../../docs/gateway-identities.md).
- `cloud_audit` — structured record of every Cloud-LLM-connector call (Phase 1+). Metadata-only by default; `prompt_fulltext`/`response_fulltext` filled only when `debug_mode.active=true`.

## Deployment modes

`oscar-brain` supports three modes via two variables:

| Mode | `OLLAMA_ENABLED` | `GPU_PASSTHROUGH` | `HERMES_MODEL` (suggested) | `HERMES_API_KEY` | Use when |
|---|---|---|---|---|---|
| **gpu-local** (default, target) | `yes` | `yes` | `gemma4:12b-instruct-q4_K_M` | empty | RTX 4070+, full OSCAR vision. Voice <500 ms. |
| **cpu-local** | `yes` | empty | `gemma4:1b` | empty | No GPU. Privacy preserved, latency 3–10 s. Honest "test the wiring" mode. |
| **cloud** | empty | (ignored) | `anthropic/claude-sonnet-4` or `google/gemini-2.5-flash` | required | No GPU + don't want CPU latency. **Prompts leave the house** — opposite of OSCAR's default stance; declare consciously. |

Trade-offs:

|  | gpu-local | cpu-local | cloud |
|---|---|---|---|
| Voice round-trip | <500 ms | 3–10 s | 1–3 s |
| Hardware cost | GPU ≥12 GB VRAM | any 4-core CPU + 8 GB RAM | any host |
| Recurring cost | electricity | electricity | per-token cloud bill |
| Privacy | full | full | **prompts to a third party**, full audit in `cloud_audit` |
| Offline ok? | yes | yes | no |

`oscar-voice` has the matching axis (`STT_GPU_PASSTHROUGH` + `WHISPER_MODEL`). Set both pods consistently; mixed states work but are usually a mistake.

## Host prerequisites

- **gpu-local:** Fedora CoreOS with `nvidia-container-toolkit` + CDI configured (`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`). Without CDI the Ollama container fails to start when GPU passthrough is requested.
- **cpu-local:** any host with ≥8 GB RAM. No special setup; Ollama auto-uses CPU when no GPU device is passed through.
- **cloud:** any host with reachable internet. The Ollama container is skipped entirely; HERMES talks straight to the provider. The `HERMES_API_KEY` variable is exposed in the container under three names — `HERMES_API_KEY`, `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY` — so HERMES picks it up regardless of which env name the underlying SDK looks for. See "Cloud-backend setup (Gemini / Anthropic)" below for the wizard click-path.
- ServiceBay v3.16+ on the same host (all modes).
- HA-MCP integration (`mcp_server`) enabled in your HA pod and a long-lived access token minted for HERMES (all modes).
- ServiceBay-MCP bearer token with scope `read+lifecycle` (all modes).

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

## Cloud-backend setup (Gemini / Anthropic)

Concrete walkthrough for the `cloud` deployment mode. Skip if you're on gpu-local or cpu-local.

### Gemini (Google AI Studio)

1. Generate an API key at https://aistudio.google.com/apikey. Starts with `AIza…`.
2. In the ServiceBay `oscar-brain` wizard:
   - `OLLAMA_ENABLED` → empty
   - `GPU_PASSTHROUGH` → (ignored in cloud mode)
   - `HERMES_MODEL` → `google/gemini-2.5-flash` (or `google/gemini-2.5-pro` if you want stronger reasoning at higher cost/latency)
   - `ROUTER_MODEL` → leave at default; HERMES doesn't use it in cloud mode, but the variable is still required.
   - `HERMES_API_KEY` → paste the `AIza…` key.
3. Deploy. First start takes ~30 s — no Ollama model pull.
4. **If you also want the per-request `cloud-llm` connector** (Phase-1 audited escalation path for a stack that's *otherwise* local): paste the *same* key into the `oscar-connectors` wizard as `GOOGLE_API_KEY`. ServiceBay doesn't (yet) share variables across templates, so you enter it twice.

### Anthropic (Claude)

Same flow with these substitutions:
- `HERMES_MODEL` → `anthropic/claude-sonnet-4` or `anthropic/claude-haiku-4-5`
- `HERMES_API_KEY` → your `sk-ant-…` key
- `oscar-connectors` variable name → `ANTHROPIC_API_KEY`

### Verification

```bash
podman logs oscar-brain-hermes | grep -i 'model\|provider\|api_key'
```

Should show HERMES boot lines naming the provider. If you see `unauthorized` or `missing API key`, the env didn't land — re-check that the wizard saved the value.

### Privacy reminder

Cloud mode sends **every prompt** to the provider — opposite of OSCAR's default stance. `cloud_audit` rows still get written so you can review what left the house, but the data is already out. Declare consciously and inform every family member.

## Storage paths

All under `{{DATA_DIR}}/oscar-brain/` on the host:

| Subdir | Contents | Backed up by `pg-backup`? |
|---|---|---|
| `hermes/` | HERMES Honcho DB, cron jobs in `~/.hermes/cron/jobs.json`, session data | no — covered by ServiceBay's own backup pipeline |
| `ollama/` | Downloaded model blobs (large; re-downloadable) | no |
| `qdrant/` | Vector index | no in Phase 0; review when Phase 3a populates it |
| `postgres/` | OSCAR domain tables, audit, settings | **yes** — `pg_dump` weekly into `postgres-backups/` |
| `postgres-backups/` | `oscar-YYYYMMDD-HHMMSS.sql.gz`, 4-week retention | this *is* the backup |
| `signal-cli/` | Linked-device keys + session DB. **Critical** — losing this means re-pairing every family member's number. | not yet in `pg-backup`; covered by ServiceBay's own backup pipeline |

## Shared library

`shared/oscar_logging/` is mounted read-only at `/opt/oscar/shared/oscar_logging` and prepended to `PYTHONPATH` so OSCAR skills can `from oscar_logging import log` without an image rebuild. The mount points at `OSCAR_REGISTRY_DIR/shared/oscar_logging/src/oscar_logging`, which is the package directory inside the src-layout project at `shared/oscar_logging/`.

To switch to a real pip install later (e.g. in a derived `ghcr.io/mdopp/oscar-hermes` image): `pip install /path/to/shared/oscar_logging` works thanks to the `pyproject.toml`.

## Signal pairing (Phase 1)

Run once at deploy. The signal-cli daemon needs to be paired as a linked device of an existing family Signal account before HERMES can send / receive messages.

1. Pick the family Signal account that will host the link (typically the maintainer's own number) — Signal allows multiple linked devices per account.
2. Set `SIGNAL_ACCOUNT` in the ServiceBay variables (E.164 format, e.g. `+4915112345678`) and deploy.
3. After the pod is up, ask the signal-cli daemon for a link URI:
   ```bash
   curl -X POST 'http://<oscar-host>:8080/v1/qrcodelink?device_name=oscar-brain' \
     --output link.png
   ```
4. Open the PNG on a screen and scan the QR code with the **paired** Signal phone (Signal app → Settings → Linked Devices → Link New Device).
5. After a successful scan, the daemon registers the linked device. Subsequent restarts pick the session up from the persistent volume — no re-pairing needed.
6. Verify the gateway works: send any Signal message to the paired account from another contact. HERMES should pick it up; check via ServiceBay-MCP `get_container_logs(id="oscar-brain-hermes")`.

**Roll-out reminder (per issue #5):** Michael first, family later. Don't populate `gateway_identities` for other family members until the daemon has run two weeks without re-pairing.

## Logging

stdout-JSON from every container goes to journald; read it via ServiceBay-MCP `get_container_logs(id="oscar-brain-<container>")`. Full convention: [`docs/logging.md`](../../docs/logging.md).

`OSCAR_DEBUG_MODE=true` is set as a pod env in Phase 0 so HERMES and skills log full bodies. Switch off via the `debug.set` admin skill once OSCAR is in productive family use.

## Open follow-ups

- **GPU passthrough validation:** the `resources.limits.nvidia.com/gpu: "1"` declaration (only emitted when `GPU_PASSTHROUGH=yes`) relies on ServiceBay's Pod-to-Quadlet translation honouring it. If the resulting Quadlet unit doesn't pass through the GPU in gpu-local mode, a small ServiceBay change may be required; track upstream once deployed.
- **Schema migrations:** the inline init script handles Phase 0 only. Phase 1 schema changes (e.g. adding `gatekeeper_voice_embeddings` in Phase 2) need a real migration tool — alembic or sqitch, decision deferred (architecture doc open point #4).
- **Custom HERMES image:** mounting `shared/oscar_logging` via hostPath works but is fragile against ServiceBay registry-path changes. A derived `ghcr.io/mdopp/oscar-hermes` image with `oscar-logging` pre-installed is the long-term answer.
