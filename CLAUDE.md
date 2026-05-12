# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

O.S.C.A.R. is a privacy-first, fully local home assistant for a family household. All AI runs locally; cloud LLMs are opt-in per request via explicit "Schleusen" (gateways).

OSCAR is consumed by **ServiceBay** (mdopp/servicebay, v3.16+) as an external template registry. ServiceBay provides the platform layer (LLDAP/Authelia identity, Immich, Radicale, file-share, NPM, AdGuard, MCP server, Home Assistant as device hub); OSCAR adds the **voice pipeline, cognition, voice-identity, and ingestion** layer on top.

**Architectural direction**: OSCAR owns the entire voice pipeline (Wakeword + STT + Orchestrator + TTS + Multi-Room + Speaker-ID). Home Assistant is consumed as an **MCP tool** for device/scene control via HA's native MCP server integration. HA's own voice pipeline is **not used** in an OSCAR deployment.

Architecture document is the source of truth: `oscar-architecture.md` (will move to `docs/architecture.md`).

## Hard constraints

- **Runtime is ServiceBay v3.16+ on Podman Quadlet.** Templates are **Kubernetes Pod manifests** (`template.yml`), Mustache-templated, deployed as Quadlet `.kube` units by ServiceBay. Each template dir contains `template.yml` + `variables.json` (typed variables: `text|secret|select|device|subdomain`, optional `oidcClient` block) + `README.md`, optionally `post-deploy.py`. Never write `docker-compose.yml`, Dockerfiles, or raw `.container` units.
- **OS is Fedora CoreOS** (immutable, auto-updating). No assumptions about package managers on the host.
- **Hardware: GPU server.** A workstation/server with consumer GPU (e.g. RTX 4070, ≥12 GB VRAM) is the target platform from Phase 0 onward — voice latency targets and Gemma 4-12B+ are unreachable on CPU-only. No Mac mini path.
- **No data leaves the house by default.** External API calls only through an explicit Schleuse module in `schleusen/`. Documents are never sent externally (not even for enrichment).
- **Identity = LLDAP, SSO = Authelia.** Both ship in ServiceBay's `auth`-Pod. OSCAR services reference LLDAP `uid`s and groups; OSCAR services with a web UI register OIDC clients via the `oidcClient` block in their `variables.json`.
- **Voice belongs to OSCAR.** HA's bundled Wyoming pipeline is not used; HA Voice Preview Edition devices speak Wyoming directly to `oscar-voice`. HA exposes its device control via its native MCP server, which HERMES consumes as one of several MCP tools.
- **Harness = configuration, not code.** When O.S.C.A.R. behaves wrongly, the fix goes into `harnesses/*.yaml` (Guides or Sensors), not into application code.

## Repo structure

```
templates/        # ServiceBay Pod-YAML templates (consumed via External Registry)
├── oscar-voice/      # Rhasspy-3 pipeline + Whisper + Piper + openWakeWord + Türsteher
├── oscar-brain/      # HERMES + Ollama (GPU) + Qdrant + Postgres
├── oscar-schleusen/  # 1 container per Schleuse, each exposes an MCP server
└── oscar-ingestion/  # Pipeline + Syncthing-watcher + Material-store

stacks/
└── oscar/        # Bundle: voice + brain + schleusen + ingestion

tuersteher/       # Python code for the Türsteher container inside oscar-voice
ingestion/        # Python code for the oscar-ingestion container
schleusen/        # One subdir per Schleuse — module code for containers in oscar-schleusen
harnesses/        # YAML per LLDAP uid + system.yaml + gast.yaml
skills/           # HERMES skills (conversation flows, routines)
docs/             # Architecture, schemas, phase plan
```

ServiceBay clones this repo via Settings → Registries → `github.com/mdopp/oscar.git`, then reads `templates/` + `stacks/`. The four OSCAR templates appear alongside ServiceBay's built-in ones in the wizard.

## Platform components from ServiceBay (don't rebuild)

| Need | Comes from ServiceBay full-stack |
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
| Platform MCP control surface | ServiceBay `/mcp` endpoint, Bearer token, scopes `read\|lifecycle\|mutate\|destroy` |

ServiceBay's `voice` template (after mdopp/servicebay#348 lands) is **for non-OSCAR setups**. An OSCAR deployment skips it — `oscar-voice` provides the full voice stack. #348 is still required so that the HA pod can be deployed **without** the bundled Wyoming containers (`VOICE_BUILTIN=disabled`); otherwise Wyoming would run in both the HA pod and `oscar-voice` on the same ports.

## OSCAR's own templates

| Template | Containers | Purpose |
|---|---|---|
| `oscar-voice` | faster-whisper-large-v3 + piper + openWakeWord + Rhasspy 3 + Türsteher | **Full voice pipeline.** HA Voice PE devices speak Wyoming directly to this pod. Rhasspy 3 orchestrates the audio-stream flow (Wakeword → STT → Conversation → TTS → audio-back); Türsteher adds speaker-ID + LLDAP-uid mapping + harness composition + conversation-handoff to HERMES. |
| `oscar-brain` | HERMES + Ollama (GPU, Gemma 4-12B+ Q4) + Qdrant + Postgres | Agent core + LLM + OSCAR-side memory (domain collections, harness namespaces). |
| `oscar-schleusen` | One container per Schleuse, each an MCP server | Cloud-LLM, TuneIn, Wetter, Websuche, Open Library, MusicBrainz, Discogs. |
| `oscar-ingestion` | Pipeline + Syncthing-watcher | Photo/scan/file → classifier → confirmation dialog → domain collection. |

## Harness system

Three harness types compose at runtime: **System** (always active) ∪ (**Personal** | **Guest**). YAML files live in `harnesses/`, named after the LLDAP `uid` (e.g. `markus.yaml`). Each harness has five fields: `context`, `tools`, `guides`, `sensors`, `permissions`. See `oscar-architecture.md` for full schema + example.

Memory is two layers:
- **HERMES** (Honcho + FTS5): conversation history, skill curation.
- **OSCAR Qdrant + Postgres** (in `oscar-brain`): semantic index + structured domain collections.

The active harness `uid` is set by Türsteher when it hands off to HERMES (request parameter, not header — Türsteher calls HERMES directly, no HA pipeline in between). Both layers respect the namespace filter.

## Türsteher / Voice pipeline

Single service inside the `oscar-voice` pod, sitting on top of Rhasspy 3 as the pipeline backbone:

1. **Voice-Pipeline-Orchestrator** (Rhasspy 3 base): receives Wyoming audio from HA Voice PE devices on the standard Wyoming ports (10300/10200/10400). Drives openWakeWord → STT → conversation → TTS → audio-back. Routes the TTS audio to the originating Voice PE device.
2. **Speaker recognition** (SpeechBrain ECAPA-TDNN or Resemblyzer): extracts a 256-d voice embedding from the audio stream in parallel with STT.
3. **LLDAP-uid mapping**: matches embedding against Türsteher's own Postgres table (`tuersteher_voice_embeddings` in `oscar-brain.postgres`, FK to LLDAP `uid`). Voice embeddings are **never** in LLDAP (biometric PII).
4. **Harness composition**: `system.yaml` + (`{uid}.yaml` | `gast.yaml`) → effective harness.
5. **Conversation handoff**: Türsteher calls HERMES directly with (text, uid, audio_features) instead of HA's conversation agent. Receives response text → Piper → audio back.

## Ingestion pipeline

Triggered by **either**:
- HERMES gateway receives a Signal/Telegram message with a file/photo attachment, or
- a file appears in `/material-inbox/{uid}/` (a Syncthing-watched folder per LLDAP uid).

Four stages: Pre-processing → Classification (Gemma 4 multimodal) → Enrichment (Schleuse, opt-in) → Confirmation dialog. Material stored encrypted at `/material/{uid}/{collection}/{uuid}.{ext}` on a **dedicated OSCAR-only mount** (not via `file-share`). Unconfirmed items deleted after 24h.

Domain collections in Postgres (in `oscar-brain`):
- **Full tables** (no ServiceBay source): `books`, `records`, `documents`.
- **Thin mirror** (real source elsewhere): `audiobooks` (→ Audiobookshelf), photo-anchored `experiences` (→ Immich + Radicale). OSCAR stores meta-notes + reference IDs; live lookups go through the respective MCP tool.

## Phase plan

- **Phase 0 — Voice + Brain Fundament** (merged old Phase 0 + 1). Voraussetzung: **GPU-Server** bereit, ServiceBay v3.16+ + full-stack deployed, **mdopp/servicebay#348 gemerged**, HA-Pod deployed mit `VOICE_BUILTIN=disabled` + HA-MCP-Server aktiviert. Write `oscar-voice` (Rhasspy-3-Basis, Türsteher initial pass-through) und `oscar-brain` (HERMES + GPU-Ollama). HA Voice PE konfiguriert gegen `oscar-voice`. HERMES bekommt ServiceBay-MCP- und HA-MCP-Bearer-Tokens (`read+lifecycle`). LLDAP-User für Familie. Erste Skills: Licht, Heizung, Timer, Radio, Musik, „Guten Morgen"-Routine.
- **Phase 1 — Mobile + Schleusen**. Signal-Gateway, erste `oscar-schleusen` (Cloud-LLM, TuneIn, Wetter, Websuche). Cloud-LLM default aus.
- **Phase 2 — Speaker-ID + Harnesses**. SpeechBrain in Türsteher aktivieren, Voice-Embedding-Tabelle, Harness-YAML-Schema, Memory-Namespaces, `system.yaml` + `markus.yaml` + `gast.yaml`.
- **Phase 3a — Streaming-Ingestion**. `oscar-ingestion` + Anreicherungs-Schleusen (Open Library, MusicBrainz, Discogs). Roll-out: books → records → audiobooks → documents → experiences.
- **Phase 3b — Bulk-Import + MCP-Wrapper**. `immich-search`, `radicale-cal`, `audiobookshelf-list` MCP-Tools. Signal-Verlauf-Import, Google Takeout, Mail/CalDAV/CardDAV-Sync.
- **Phase 4 — Aktive Erweiterungen**. Voice-Tone-Analyse, Multi-Room-Voice-Routing (≥2 Räume), Multi-Haushalt, Custom-Wakeword „Oscar", proaktive HERMES-Memo-Erstellung.
