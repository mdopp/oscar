# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

O.S.C.A.R. is a privacy-first, fully local home assistant for a family household. All AI runs locally; cloud LLMs are opt-in per request via explicit **connectors** (audited boundary modules; the original German term "Schleuse" — canal lock — was retired in favour of "Connector" in May 2026).

OSCAR is consumed by **ServiceBay** (mdopp/servicebay, v3.16+) as an external template registry. ServiceBay provides the platform layer (LLDAP/Authelia identity, Immich, Radicale, file-share, NPM, AdGuard, MCP server, Home Assistant as device hub); OSCAR adds the **voice pipeline, cognition, voice identity, and ingestion** layer on top.

**Architectural direction:** OSCAR owns the entire voice pipeline (wakeword + STT + orchestrator + TTS + multi-room + speaker ID). Home Assistant is consumed as an **MCP tool** for device/scene control via HA's native MCP server integration. HA's own voice pipeline is **not used** in an OSCAR deployment.

Architecture document is the source of truth: `oscar-architecture.md` (will move to `docs/architecture.md`).

## Hard constraints

- **Runtime is ServiceBay v3.16+ on Podman Quadlet.** Templates are **Kubernetes Pod manifests** (`template.yml`), Mustache-templated, deployed as Quadlet `.kube` units by ServiceBay. Each template directory contains `template.yml` + `variables.json` (typed variables: `text|secret|select|device|subdomain`, optional `oidcClient` block) + `README.md`, optionally `post-deploy.py`. Never write `docker-compose.yml`, Dockerfiles, or raw `.container` units.
- **OS is Fedora CoreOS** (immutable, auto-updating). No assumptions about host-side package managers.
- **Hardware: GPU server.** A workstation/server with a consumer GPU (e.g. RTX 4070, ≥12 GB VRAM) is the target platform from Phase 0 onward — voice latency targets and Gemma 4-12B+ are unreachable on CPU only. No Mac mini path.
- **No data leaves the house by default.** External API calls only through an explicit connector module in `connectors/`. Documents are never sent externally (not even for enrichment).
- **Identity = LLDAP, SSO = Authelia.** Both ship in ServiceBay's `auth` pod. OSCAR services reference LLDAP `uid`s and groups; OSCAR services with a web UI register OIDC clients via the `oidcClient` block in their `variables.json`.
- **Voice belongs to OSCAR.** HA's bundled Wyoming pipeline is not used; HA Voice Preview Edition devices speak Wyoming directly to `oscar-voice`. HA exposes device control via its native MCP server, which HERMES consumes as one of several MCP tools.
- **Harness = configuration, not code.** When O.S.C.A.R. behaves wrongly, the fix goes into `harnesses/*.yaml` (guides or sensors), not into application code.
- **Documentation and code are English.** Conversation language with the maintainer (Michael Dopp) is German, but every versioned artefact — docs, READMEs, code identifiers/comments, issue bodies, commit messages — is English.

## Repo structure

```
templates/        # ServiceBay Pod-YAML templates (consumed via external registry)
├── oscar-voice/       # Rhasspy 3 pipeline + Whisper + Piper + openWakeWord + gatekeeper
├── oscar-brain/       # HERMES + Ollama (GPU) + Qdrant + Postgres
├── oscar-connectors/  # 1 container per connector, each exposes an MCP server
└── oscar-ingestion/   # pipeline + Syncthing watcher + material store

stacks/
└── oscar/        # bundle: voice + brain + connectors + ingestion

gatekeeper/       # Python code for the gatekeeper container inside oscar-voice
ingestion/        # Python code for the oscar-ingestion container
connectors/       # one subdir per connector — module code for containers in oscar-connectors
harnesses/        # YAML per LLDAP uid + system.yaml + guest.yaml
skills/           # HERMES skills (conversation flows, routines)
shared/           # cross-container Python libraries (e.g. oscar_logging)
docs/             # architecture, schemas, phase plan
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
| `oscar-voice` | faster-whisper-large-v3 + Piper + openWakeWord + Rhasspy 3 + gatekeeper | **Full voice pipeline.** HA Voice PE devices speak Wyoming directly to this pod. Rhasspy 3 orchestrates the audio-stream flow (wakeword → STT → conversation → TTS → audio-back); the gatekeeper adds speaker ID + LLDAP-uid mapping + harness composition + conversation handoff to HERMES. |
| `oscar-brain` | HERMES + Ollama (GPU, Gemma 4-12B+ Q4) + Qdrant + Postgres | Agent core + LLM + OSCAR-side memory (domain collections, harness namespaces). |
| `oscar-connectors` | One container per connector, each an MCP server | Cloud LLM, TuneIn, weather, web search, Open Library, MusicBrainz, Discogs. |
| `oscar-ingestion` | Pipeline + Syncthing watcher | Photo/scan/file → classifier → confirmation dialog → domain collection. |

## Harness system

Three harness types compose at runtime: **System** (always active) ∪ (**Personal** | **Guest**). YAML files live in `harnesses/`, named after the LLDAP `uid` (e.g. `michael.yaml`). Each harness has five fields: `context`, `tools`, `guides`, `sensors`, `permissions`. See `oscar-architecture.md` for the full schema + example.

Memory is two layers:
- **HERMES** (Honcho + FTS5): conversation history, skill curation.
- **OSCAR Qdrant + Postgres** (in `oscar-brain`): semantic index + structured domain collections.

The active harness `uid` is set by the gatekeeper when it hands off to HERMES (request parameter, not header — the gatekeeper calls HERMES directly, no HA pipeline in between). Both layers respect the namespace filter.

## Gatekeeper / voice pipeline

A single service inside the `oscar-voice` pod, sitting on top of Rhasspy 3 as the pipeline backbone:

1. **Voice-pipeline orchestrator** (Rhasspy 3 base): receives Wyoming audio from HA Voice PE devices on the standard Wyoming ports (10300/10200/10400). Drives openWakeWord → STT → conversation → TTS → audio-back. Routes the TTS audio to the originating Voice PE device.
2. **Speaker recognition** (SpeechBrain ECAPA-TDNN or Resemblyzer): extracts a 256-d voice embedding from the audio stream in parallel with STT.
3. **LLDAP-uid mapping**: matches the embedding against the gatekeeper's own Postgres table (`gatekeeper_voice_embeddings` in `oscar-brain.postgres`, FK to LLDAP `uid`). Voice embeddings are **never** in LLDAP (biometric PII).
4. **Harness composition**: `system.yaml` + (`{uid}.yaml` | `guest.yaml`) → effective harness.
5. **Conversation handoff**: the gatekeeper calls HERMES directly with `(text, uid, audio_features)` instead of HA's conversation agent. Receives the response text → Piper → audio back.

## Ingestion pipeline

Triggered by **either**:
- a HERMES gateway receiving a Signal/Telegram message with a file/photo attachment, or
- a file appearing in `/material-inbox/{uid}/` (a Syncthing-watched folder per LLDAP uid).

Four stages: pre-processing → classification (Gemma 4 multimodal) → enrichment (connector, opt-in) → confirmation dialog. Material stored encrypted at `/material/{uid}/{collection}/{uuid}.{ext}` on a **dedicated OSCAR-only mount** (not via `file-share`). Unconfirmed items deleted after 24 h.

Domain collections in Postgres (in `oscar-brain`):
- **Full tables** (no ServiceBay source): `books`, `records`, `documents`.
- **Thin mirror** (real source elsewhere): `audiobooks` (→ Audiobookshelf), photo-anchored `experiences` (→ Immich + Radicale). OSCAR stores meta-notes + reference IDs; live lookups go through the respective MCP tool.

## Phase plan

- **Phase 0 — voice + brain foundation** (merges previous Phase 0 + 1). Prereqs: **GPU server** ready, ServiceBay v3.16+ + full stack deployed, **mdopp/servicebay#348 merged**, HA pod deployed with `VOICE_BUILTIN=disabled` + HA-MCP server enabled. Write `oscar-voice` (Rhasspy 3 base, gatekeeper initially pass-through) and `oscar-brain` (HERMES + GPU Ollama). HA Voice PE configured against `oscar-voice`. HERMES gets ServiceBay-MCP and HA-MCP bearer tokens (`read+lifecycle`). LLDAP users for the family. First skills: light, heating, timer + alarm, music (local).
- **Phase 1 — mobile + connectors.** Signal gateway, first `oscar-connectors` (Cloud LLM, weather, web search). Cloud LLM off by default per harness.
- **Phase 2 — speaker ID + harnesses.** Enable SpeechBrain in the gatekeeper, voice-embedding table, harness YAML schema, memory namespaces, `system.yaml` + `michael.yaml` + `guest.yaml`.
- **Phase 3a — streaming ingestion.** `oscar-ingestion` + enrichment connectors (Open Library, MusicBrainz, Discogs). Roll-out: books → records → audiobooks → documents → experiences.
- **Phase 3b — bulk import + MCP wrappers.** `immich-search`, `radicale-cal`, `audiobookshelf-list` MCP tools. Signal history import, Google Takeout, mail/CalDAV/CardDAV sync.
- **Phase 4 — active extensions.** Voice-tone analysis, multi-room voice routing (≥2 rooms), multi-household, custom "Oscar" wakeword, proactive HERMES memo creation, TuneIn / internet-radio connector.
