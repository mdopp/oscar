# oscar-household

The one OSCAR-owned ServiceBay template. The household-specific overlay on top of ServiceBay's `ai-stack` (Hermes + Ollama) and — for the voice path — ServiceBay's existing `voice` template. See [`../../oscar-architecture.md`](../../oscar-architecture.md) for the full architecture.

## What it does

1. **Schema init.** Runs the migration sidecar (`ghcr.io/mdopp/oscar-household-init`, built from [`../../schema/`](../../schema/)) against `/var/lib/oscar/oscar.db` on every pod start. Creates `system_settings`, `cloud_audit`, `voice_embeddings`. Idempotent.
2. **Skill mount.** The bind-mounted `/var/lib/oscar` volume holds OSCAR's [`../../skills/`](../../skills/) clone (placed there by ServiceBay's registry sync). The ServiceBay `hermes` template mounts the same path into the Hermes container at `/opt/data/skills/oscar`, so Hermes picks the skills up alongside its built-in Skills Hub.
3. **Voice gatekeeper.** A gatekeeper container (OSCAR-published image `ghcr.io/mdopp/oscar-gatekeeper`) runs in this same pod. It speaks Wyoming to HA Voice PE satellites on `<host>:<GATEKEEPER_PORT>`, drives Whisper/Piper via host loopback, and posts each turn to Hermes. Phase 2 will also read `voice_embeddings` from `oscar.db` for speaker-ID. Skip the gatekeeper entirely by deploying without HA Voice PE devices and ServiceBay's `voice` template — the rest of `oscar-household` works without it.
4. **MCP wiring.** Non-interactive `post-deploy.py` reads `${DATA_DIR}/hermes/config.yaml` (written by ServiceBay's `hermes` template's post-deploy with the `model:` block), splices in an `mcp_servers:` block for HA-MCP and ServiceBay-MCP per the [Hermes MCP Config Reference](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference), writes back, then `POST /api/services/hermes/action {action: "restart"}` so Hermes picks up the new config on boot. No `podman exec` operator instructions. Caveat: re-deploying the `hermes` template overwrites `config.yaml` with just the `model:` block — re-deploy `oscar-household` afterwards to restore the `mcp_servers:` block.
5. **Audit hook.** *(Pending.)* Once `mcp-audit-proxy` ships as its own MCP server, `post-deploy.py` will also register it with Hermes so every cloud-LLM call writes a `cloud_audit` row.

This template does **not** deploy Postgres, Qdrant, Ollama, Hermes, Whisper, Piper or any other generic infrastructure. Those come from ServiceBay's `ai-stack` and `voice` templates.

## Phase status

| Phase | Behaviour |
|---|---|
| **0** | Schema init + skill mount + MCP wiring. The gatekeeper container runs but is idle (no Wyoming satellites pointed at it). |
| **1** | ServiceBay's `voice` template is deployed alongside; HA Voice PE satellites point at the host's `GATEKEEPER_PORT`; full voice loop works in pass-through mode (`uid = DEFAULT_UID`). |
| **2** | The gatekeeper enrolment wizard populates `voice_embeddings`; subsequent turns resolve uid per voice. |
| **3a** | Additional migrations land for the domain-collection tables (`books`, `records`, `documents`, `audiobooks`, `experiences`). The storage choice is re-opened at that point — SQLite likely still fits. |

## Variables

The wizard collects these (see [`variables.json`](variables.json) for the canonical list):

| Variable | Type | Purpose |
|---|---|---|
| `DEFAULT_UID` | text | Hermes user-id used until Phase 2 speaker ID lands. Typically the household admin's LLDAP uid. |
| `GATEKEEPER_IMAGE` | text | OSCAR-published image. Leave default unless running a fork PoC. |
| `GATEKEEPER_PORT` | text | Host port for incoming Wyoming-satellite connections. Default `10700`. |
| `WHISPER_URI` / `PIPER_URI` / `OPENWAKEWORD_URI` | text | Wyoming endpoints on the host loopback (defaults match ServiceBay's `voice` template's published ports). |
| `VOICE_PE_DEVICES` | text | JSON map `<device-name> -> <wyoming-uri>` for `POST /push` reverse delivery. Empty until at least one device is paired. |
| `HERMES_API_PORT` / `HERMES_API_KEY` | text + secret | Hermes' HTTP API port and bearer token. `HERMES_API_KEY` matches the name surfaced as `__SB_CREDENTIAL__` by the ServiceBay `hermes` template's post-deploy, so the operator pastes once. |
| `PUSH_TOKEN` | secret | Bearer Hermes presents on the gatekeeper's pod-internal `POST /push`. |
| `HA_MCP_URL` / `HA_MCP_TOKEN` | text + secret | Home Assistant MCP server endpoint and token. |
| `SERVICEBAY_MCP_URL` / `SERVICEBAY_MCP_TOKEN` | text + secret | ServiceBay MCP control surface endpoint and token. |
| `LLDAP_GROUP` | text | Group whose members are family (vs. guests). Default `family`. |
| `OSCAR_DEBUG_MODE` | select | Initial `system_settings.debug_mode.active` value. Runtime toggle via the `oscar-debug-set` skill. |
| `TZ` | text | IANA time zone for log timestamps. |

The template does **not** ask for a database DSN — `oscar.db` lives in the bind-mounted volume, no external Postgres for Phase 0–2.

## Volumes

| Mount | Purpose |
|---|---|
| `/var/lib/oscar` (host: `{{DATA_DIR}}/oscar-household`) | Owns `oscar.db` (SQLite) and the OSCAR `skills/` checkout. Bind-mounted into both containers in this pod and into the Hermes container by the `hermes` template. |

## hostNetwork: true

Necessary so:

- the gatekeeper reaches ServiceBay's `voice` template's Wyoming services at `127.0.0.1:10300/10200/10400` (two hostNetwork pods share the host netns)
- the gatekeeper and the post-deploy hook reach Hermes at `127.0.0.1:8642` (same reason)
- HA Voice PE satellites on the LAN reach the gatekeeper at `<host-ip>:<GATEKEEPER_PORT>`

Without hostNetwork the gatekeeper couldn't cross pod boundaries to the `voice` template's Whisper/Piper containers.

## Deploy prerequisites

This template declares `servicebay.dependencies: "hermes,voice"`. ServiceBay's wizard topo-sorts and refuses to deploy `oscar-household` until both dependencies are present.

- ServiceBay's `hermes` template (planned in [mdopp/servicebay#539](https://github.com/mdopp/servicebay/issues/539)) — the gatekeeper and post-deploy talk to its HTTP API
- ServiceBay's `ollama` template (planned in [mdopp/servicebay#538](https://github.com/mdopp/servicebay/issues/538)) — Hermes' LLM provider points at it
- ServiceBay's `voice` template (shipped via [mdopp/servicebay#348](https://github.com/mdopp/servicebay/issues/348)) — Whisper/Piper/openWakeWord, reached via host loopback
- Home Assistant with its native MCP server enabled
- ServiceBay-MCP token (`read+lifecycle`)

See [`../../stacks/oscar/README.md`](../../stacks/oscar/README.md) for the end-to-end walkthrough.
