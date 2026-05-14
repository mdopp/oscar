# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

O.S.C.A.R. is a privacy-first, fully local home assistant for a family household. All AI runs locally; cloud LLMs are opt-in per request via explicit **connectors** (audited boundary modules; the original German term "Schleuse" — canal lock — was retired in favour of "Connector" in May 2026).

OSCAR is consumed by **ServiceBay** (mdopp/servicebay, v3.16+) as an external template registry. ServiceBay provides the platform layer (LLDAP/Authelia identity, Immich, Radicale, file-share, NPM, AdGuard, MCP server, Home Assistant as device hub); OSCAR adds the **voice pipeline, cognition, voice identity, and ingestion** layer on top.

**Architectural direction:** OSCAR is a thin household layer on top of [Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent) (wrapped as the `oscar-hermes` container) plus ServiceBay. Hermes owns the agent runtime (skills, gateways, cron, Honcho memory, MCP client, self-improvement); OSCAR adds the voice pipeline, the data plane (Postgres/Qdrant/Ollama), household-specific skills, and MCP connectors. OSCAR owns the entire voice pipeline (wakeword + STT + orchestrator + TTS + multi-room + speaker ID). Home Assistant is consumed as an **MCP tool** for device/scene control via HA's native MCP server integration. HA's own voice pipeline is **not used** in an OSCAR deployment.

Architecture documents: the full spec is `oscar-architecture.md`; the on-Hermes rationale (what OSCAR keeps vs. delegates) is `docs/architecture/oscar-on-hermes.md`.

## Hard constraints

- **Runtime is ServiceBay v3.16+ on Podman Quadlet.** Templates are **Kubernetes Pod manifests** (`template.yml`), Mustache-templated, deployed as Quadlet `.kube` units by ServiceBay. Each template directory contains `template.yml` + `variables.json` (typed variables: `text|secret|select|device|subdomain`, optional `oidcClient` block) + `README.md`, optionally `post-deploy.py`. Never write `docker-compose.yml`, Dockerfiles, or raw `.container` units.
- **OS is Fedora CoreOS** (immutable, auto-updating). No assumptions about host-side package managers.
- **Hardware: GPU server.** A workstation/server with a consumer GPU (e.g. RTX 4070, ≥12 GB VRAM) is the target platform from Phase 0 onward — voice latency targets and Gemma 4-12B+ are unreachable on CPU only. No Mac mini path.
- **No data leaves the house by default.** External API calls only through an explicit connector module in `connectors/`. Documents are never sent externally (not even for enrichment).
- **Identity = LLDAP, SSO = Authelia.** Both ship in ServiceBay's `auth` pod. OSCAR services reference LLDAP `uid`s and groups; OSCAR services with a web UI register OIDC clients via the `oidcClient` block in their `variables.json`.
- **Voice belongs to OSCAR.** HA's bundled Wyoming pipeline is not used; HA Voice Preview Edition devices speak Wyoming directly to `oscar-voice`. HA exposes device control via its native MCP server, which Hermes consumes as one of several MCP tools.
- **Harness = configuration, not code.** When O.S.C.A.R. behaves wrongly, the fix goes into `harnesses/*.yaml` (guides or sensors), not into application code.
- **Documentation and code are English.** Conversation language with the maintainer (Michael Dopp) is German, but every versioned artefact — docs, READMEs, code identifiers/comments, issue bodies, commit messages — is English.

## Repo structure

```
templates/        # ServiceBay Pod-YAML templates (consumed via external registry)
├── oscar-brain/       # Postgres + Qdrant + Ollama + db-migrate + pg-backup (data plane)
├── oscar-hermes/      # wraps docker.io/nousresearch/hermes-agent — agent runtime
├── oscar-voice/       # Wyoming pipeline (Whisper + Piper + openWakeWord) + gatekeeper
└── oscar-connectors/  # weather + cloud-llm MCP servers (one container per connector)

stacks/
└── oscar/        # wizard walkthrough that points to all four templates

gatekeeper/       # Python code for the gatekeeper container inside oscar-voice
ingestion/        # Python source for the ingestion pipeline (Phase 3a placeholder)
connectors/       # one subdir per connector + _skeleton/ copy template
harnesses/        # YAML per LLDAP uid + system.yaml + guest.yaml (Phase 2 placeholder)
skills/           # household-specific skills, read-mounted into Hermes at /opt/data/skills/oscar
shared/           # cross-component Python libs: oscar_logging, oscar_health, oscar_audit, oscar_db
docs/             # architecture rationale, connector skeleton, logging contract
```

ServiceBay clones this repo via Settings → Registries → `github.com/mdopp/oscar.git`, then reads `templates/` + `stacks/`. The four OSCAR templates appear alongside ServiceBay's built-in ones in the wizard.

## Platform components from ServiceBay (don't rebuild)

| Need | Comes from the ServiceBay full stack |
|---|---|
| Smart-home hub, Z-Wave, Matter | `home-assistant` (consumed via HA's native MCP server — **not** its voice pipeline) |
| Identity, SSO, OIDC | `auth` (LLDAP + Authelia) |
| Photos | `immich` |
| CalDAV/CardDAV | `radicale` |
| Audiobooks, music | `media` (Audiobookshelf + Navidrome) |
| File drop / sync | `file-share` (Syncthing + Samba + FileBrowser + WebDAV) |
| Reverse proxy + LE certs | `nginx` (NPM) |
| DNS sinkhole | `adguard` |
| Passwords | `vaultwarden` |
| Platform MCP control surface | ServiceBay `/mcp` endpoint, bearer token, scopes `read\|lifecycle\|mutate\|destroy` |

ServiceBay's `voice` template (after mdopp/servicebay#348 lands) is **for non-OSCAR setups**. An OSCAR deployment skips it — `oscar-voice` provides the full voice stack. #348 is still required so the HA pod can be deployed **without** the bundled Wyoming containers (`VOICE_BUILTIN=disabled`); otherwise Wyoming would run in both the HA pod and `oscar-voice` on the same ports.

## OSCAR's own templates

| Template | Containers | Purpose |
|---|---|---|
| `oscar-brain` | Postgres + Qdrant + Ollama (GPU, Gemma 4-12B+ Q4) + db-migrate + pg-backup | **Data plane.** Structured tables (domain collections, `cloud_audit`, `system_settings`), semantic index, local LLM. Alembic-driven migrations run as a one-shot sidecar. |
| `oscar-hermes` | wraps `docker.io/nousresearch/hermes-agent` | **Agent runtime.** Conversation, skill registry, cron/reminders, Honcho memory, MCP client, messaging gateways (Signal/Telegram/Discord/Slack/WhatsApp/Email — Hermes-native), self-improvement. OSCAR's `skills/` directory is read-mounted at `/opt/data/skills/oscar`. |
| `oscar-voice` | faster-whisper-large-v3 + Piper + openWakeWord + gatekeeper | **Full voice pipeline.** HA Voice PE devices speak Wyoming directly to this pod. The gatekeeper drives Whisper → Hermes (HTTP) → Piper per turn, exposes `POST /push` for reverse delivery, and (Phase 2) handles speaker ID + LLDAP-uid mapping + harness composition. |
| `oscar-connectors` | One container per connector, each an MCP server | Phase 1: weather, cloud-llm-with-audit. Phase 3a/4: Open Library, MusicBrainz, Discogs, TuneIn. |

## Harness system

Three harness types compose at runtime: **System** (always active) ∪ (**Personal** | **Guest**). YAML files live in `harnesses/`, named after the LLDAP `uid` (e.g. `michael.yaml`). Each harness has five fields: `context`, `tools`, `guides`, `sensors`, `permissions`. See `oscar-architecture.md` for the full schema + example. The harness layer is a Phase-2 composition layer on top of Hermes' own user/skill knobs — until then, `harnesses/` is a roadmap placeholder.

Memory is two layers:
- **Hermes (in `oscar-hermes` pod)**: Honcho + FTS5 for conversation history and skill curation, persisted under `~/.hermes/` inside the container's `/opt/data` mount.
- **OSCAR Qdrant + Postgres (in `oscar-brain`)**: semantic index + structured domain collections.

The active harness `uid` will be passed by the gatekeeper to Hermes' HTTP API as a request parameter (Phase 2). In Phase 0 the gatekeeper hardcodes `uid = DEFAULT_UID`. Both memory layers respect the namespace filter.

## Gatekeeper / voice pipeline

A Wyoming-protocol server inside the `oscar-voice` pod. One inbound satellite connection = one half-duplex pipeline turn:

1. **Audio in**: HA Voice PE devices (or any `wyoming-satellite` client) connect and stream `AudioStart` + `AudioChunk*` + `AudioStop`.
2. **STT**: the gatekeeper calls the in-pod Whisper container (Wyoming, `tcp://127.0.0.1:10300`).
3. **Conversation handoff**: the gatekeeper POSTs `(text, uid, endpoint, trace_id)` to the Hermes HTTP API at `HERMES_URL` (default `http://127.0.0.1:8642`; both pods use hostNetwork). Phase 0 hardcodes `uid = DEFAULT_UID`; Phase 2 will resolve it from a SpeechBrain ECAPA-TDNN voice embedding looked up in a `gatekeeper_voice_embeddings` table in `oscar-brain.postgres` (voice embeddings are **never** stored in LLDAP — biometric PII).
4. **TTS**: the gatekeeper streams the Hermes response text into the in-pod Piper container (`tcp://127.0.0.1:10200`) and the synthesised audio back to the satellite.
5. **Push delivery**: an outbound `POST /push` endpoint (port 10750, pod-internal) lets Hermes' cron / proactive deliveries address a specific Voice PE device by name (resolved against `VOICE_PE_DEVICES`).

Multi-turn / barge-in / streaming responses + harness composition (`system.yaml` + `{uid}.yaml | guest.yaml`) are Phase 2 / Phase 4 topics.

## Ingestion pipeline

Phase 3a (designed, not built). Triggered by **either**:
- a Hermes messaging gateway (Signal/Telegram/…) receiving a message with a file/photo attachment, or
- a file appearing in `/material-inbox/{uid}/` (a Syncthing-watched folder per LLDAP uid).

Four stages: pre-processing → classification (Gemma multimodal) → enrichment (connector, opt-in) → confirmation dialog. Material stored encrypted at `/material/{uid}/{collection}/{uuid}.{ext}` on a **dedicated OSCAR-only mount** (not via `file-share`). Unconfirmed items deleted after 24 h.

Domain collections in Postgres (in `oscar-brain`):
- **Full tables** (no ServiceBay source): `books`, `records`, `documents`.
- **Thin mirror** (real source elsewhere): `audiobooks` (→ Audiobookshelf), photo-anchored `experiences` (→ Immich + Radicale). OSCAR stores meta-notes + reference IDs; live lookups go through the respective MCP tool.

## Phase plan

- **Phase 0 — voice pipeline + data plane + first HA skill.** Prereqs: GPU server ready, ServiceBay v3.16+ + full stack deployed, **mdopp/servicebay#348** merged (HA without bundled Wyoming), **mdopp/servicebay#443** merged (git in ServiceBay's container, for registry sync), HA-MCP server enabled. Deploy `oscar-brain` (Postgres/Qdrant/Ollama) + `oscar-hermes` (wraps `nousresearch/hermes-agent`) + `oscar-voice` (Whisper + Piper + openWakeWord + gatekeeper, gatekeeper in pass-through mode with `DEFAULT_UID`). HA Voice PE points its Wyoming endpoint at `oscar-voice`. Hermes gets ServiceBay-MCP and HA-MCP bearer tokens (`read+lifecycle`). First household skill: `oscar-light` (HA via MCP). Code complete; deploy/test pending mdopp/oscar#65.
- **Phase 1 — messaging + connectors.** Messaging (Signal/Telegram/Discord/Slack/WhatsApp/Email) is **Hermes-native** — paired interactively via `hermes gateway setup`, no OSCAR-side gateway code. Timers/alarms/reminders are **Hermes-native** (cron scheduler). OSCAR contributes `oscar-connectors` (cloud-llm-with-audit, weather) as MCP servers Hermes consumes. Cloud LLM off by default; per-call audit row in `cloud_audit`.
- **Phase 2 — speaker ID + harnesses.** Enable SpeechBrain ECAPA-TDNN in the gatekeeper, `gatekeeper_voice_embeddings` table, harness YAML schema, memory namespaces, `system.yaml` + `michael.yaml` + `guest.yaml`. Harness `uid` flows from the gatekeeper into Hermes per turn.
- **Phase 3a — streaming ingestion.** Build `ingestion/` into a real pipeline + enrichment connectors (Open Library, MusicBrainz, Discogs). Roll-out: books → records → audiobooks → documents → experiences.
- **Phase 3b — bulk import + MCP wrappers.** `immich-search`, `radicale-cal`, `audiobookshelf-list` MCP tools. Signal/Telegram history import, Google Takeout, mail/CalDAV/CardDAV sync.
- **Phase 4 — active extensions.** Voice-tone analysis, multi-room voice routing (≥2 rooms), multi-household, custom "Oscar" wakeword, proactive Hermes memo creation, TuneIn / internet-radio connector.
