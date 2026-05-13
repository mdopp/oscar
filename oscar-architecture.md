# O.S.C.A.R. — Architecture Context

> Living document. As of May 2026 (updated after ServiceBay v3.16+ and the architecture inversion: OSCAR owns voice, HA is an MCP tool). **May 2026 reset:** Hermes Agent is now host-installed via its own installer, not packaged as a container inside `oscar-brain`. OSCAR is the household layer (data plane + voice + connectors + domain skills); Hermes is the agent layer (gateways, cron, memory, skill registry, self-improvement loop). Rationale: [`docs/architecture/oscar-on-hermes.md`](docs/architecture/oscar-on-hermes.md). Drafted through concept dialogue, handed to Claude Code for implementation.

## Vision

O.S.C.A.R. is a private operating system for family and home: a fully local, omniscient assistant that orchestrates digital and physical life, serves as infinite memory, and guarantees absolute privacy.

### Five core goals

1. **Digital sovereignty (vault)** — use modern AI without exposing data. The brain doesn't leave the house.
2. **Contextual long-term memory** — networked understanding of preferences, values, experiences, relationships.
3. **Proactive orchestration** — physical world (home) linked to digital world (documents, appointments, habits).
4. **Ironclad privacy in the room** — preserves each resident's individuality, recognises guests, releases only what is released.
5. **Frictionless, natural interaction** — voice at home, chat on the go, one coherent experience.

## Architectural foundation: voice belongs to OSCAR

Unlike the HA-centric voice architecture (HA does STT/TTS, Hermes is the HA conversation agent), **OSCAR owns the entire voice pipeline**:

- HA Voice Preview Edition speaks Wyoming **directly** to `oscar-voice` (no longer to HA)
- Wakeword + STT + pipeline orchestration + TTS + multi-room routing live in the `oscar-voice` pod
- HA remains the smart-home hub for devices (Z-Wave, Matter, sensors, automations) and exposes that via **HA's native MCP server** as a tool
- Hermes (host-installed) consumes HA-MCP like any other tool provider

**Wins:** identity without header marshalling, voice-tone/emotion analysis feasible, free conversation instead of intent grammar, multimodal inputs combinable (audio + photo in a single Gemma call).

**Prerequisite:** GPU server (no Mac mini), so Whisper-large + Gemma 4-12B+ Q4 + Piper streaming achieve latency < 500 ms.

## Family & identities

- Three people: father (Michael), mother, child — each with their own LLDAP account (`uid`)
- Family members in LLDAP group `family`, Michael additionally in `admins`
- Guests are treated as a group — guest mode activates for any unrecognised voice; no individual guest LLDAP account
- Each person has a personal harness in `harnesses/{uid}.yaml`
- The system should be installable in other households too (multi-tenant via its own LLDAP + harness repo per household)

## Central concept: harness

Term in the sense of Birgitta Böckeler / Martin Fowler:
> "Agent = Model + Harness" — the harness comprises everything in an agent except the model itself.

### Three harness types

| Type | Activation | Purpose |
|---|---|---|
| **System harness** | always active | Global persona, world/house knowledge, default tools, external-connector rules |
| **Personal harness** | on recognised resident voice (LLDAP-uid match) | Personal memory slice, preferences, extended tools, elevated permissions |
| **Guest harness** | on unrecognised voice | Public knowledge only, restricted tools, no connectors |

### Five components per harness

- **Context** — memory namespaces, preferences, history
- **Tools** — which MCP tools may be invoked
- **Guides** — response style, skills, behavioural instructions (feedforward)
- **Sensors** — feedback mechanisms, validators (feedback)
- **Permissions** — external-connector permissions, cloud-LLM access, ingestion rights

### Example YAML

```yaml
harness: michael          # matches the LLDAP uid
extends: system
context:
  memory_namespaces: [michael_private, michael_journal, family_shared]
  preferences: { language: de, response_style: concise }
tools:
  inherit_from_system: true
  additional: [finance_docs, personal_email, tax_archive, ingestion]
guides:
  - "Keep answers short, max. 3 sentences spoken"
  - "Cite the source when discussing financial documents"
sensors:
  - thumbs_feedback_via_signal
  - calendar_writeback_confirmation
permissions:
  cloud_llm_connector: allowed
  external_search: allowed
  enrichment_connectors: [open_library, musicbrainz, discogs]
  smart_home: full
```

### Steering loop

Harnesses are improved iteratively. When O.S.C.A.R. repeatedly does something wrong, we add guides or sensors — not code. That's the maintenance philosophy.

## Architecture layers

### 1. Inputs

- **Voice at home:** Home Assistant Voice Preview Edition as the hardware (ESP32 + microphone array), but **Wyoming endpoint configured to `oscar-voice`** (not HA). Office first, then living room, eventually 4–5 rooms.
- **Mobile chat:** Signal bot via Hermes gateway (Telegram as fallback)
- **Wakeword:** single ("Hey Jarvis" initially, later a custom "Oscar" model), short answers, "Guest:" prefix for unrecognised voices, beep for recognised members
- **Material inputs:** photo/scan/voice memo/file via Signal/Telegram **or** drop into a Syncthing inbox folder (`/material-inbox/{uid}/`) → inbound knowledge pipeline (see Layer 8)

### 2. Gatekeeper (voice pipeline + identity layer)

`oscar-voice` is a pod with **four containers**: faster-whisper-large-v3, piper, openWakeWord, gatekeeper. The gatekeeper sits on top of **Rhasspy 3** as the pipeline backbone and is simultaneously the identity layer + harness composer.

#### Pipeline responsibilities (gatekeeper + Rhasspy 3)

- **Wyoming server:** receives audio streams from HA Voice PE devices (ports 10300/10200/10400, standard Wyoming). Multi-device capable — each Voice PE device addresses its own session context.
- **Wakeword confirmation flow:** openWakeWord triggers, Rhasspy 3 forwards audio after wakeword detection to Whisper, gatekeeper observes in parallel.
- **STT integration:** `faster-whisper:11300` internally, streaming chunks via Wyoming
- **Speaker recognition** (activated in Phase 2): SpeechBrain ECAPA-TDNN or Resemblyzer extracts a 256-d voice embedding from the audio stream
- **LLDAP-uid mapping:** compares embedding against the gatekeeper's own Postgres table (`gatekeeper_voice_embeddings` in `oscar-brain.postgres`, FK to LLDAP `uid`). Voice embeddings are **not** in LLDAP (biometric PII, binary)
- **Harness composition:** `system.yaml` ∪ (`{uid}.yaml` | `guest.yaml`) → effective harness
- **Conversation handoff:** calls Hermes (host-installed, reachable at `http://<host-ip>:<hermes-port>`) with `(text, uid, audio_features)` — no detour through the HA conversation agent
- **TTS generation:** receives response text from Hermes, calls `piper:11200` for TTS
- **Audio return path:** TTS audio goes back via Wyoming to the originating Voice PE device. For *outbound* delivery to a Voice PE device (a fired Hermes cron message, an unsolicited notification), the gatekeeper exposes `POST /push` with `{endpoint: "voice-pe:<device>", text}` — Hermes calls this from its delivery system.
- **Multiple people in the room:** conservative intersection of personal harnesses (refineable in Phase 4)
- **Custom wakeword "Oscar":** the openWakeWord container can load custom models (hostPath mount) — Phase 4 feature

#### What is no longer needed

- Hermes registration as HA conversation agent → gone
- HA pipeline configuration in `home-assistant/.storage/` → gone
- Marshalling identity headers through the HA pipeline → gone (gatekeeper and Hermes talk directly)

### 3. Hermes agent (core)

- Repo: <https://github.com/nousresearch/hermes-agent>
- **Install location:** **host-installed** via the official one-liner (`curl -fsSL …/install.sh | bash`), *not* a container inside `oscar-brain`. Reasoning: Hermes is a multi-process Python+Node app with native tool-execution backends; running it as a thin container adds maintenance burden without buying anything. OSCAR's `scripts/install.sh` drives the install + the OSCAR-skill symlink + the ServiceBay-template deploys in one pass.
- Provider-agnostic (model swap via `hermes model`, transparent between local and cloud). Points at the OSCAR-brain Ollama at `http://<host>:11434` for local, any cloud provider directly for cloud.
- Built-in gateways: Signal, Telegram, Discord, Slack, WhatsApp, Email (HA-conversation-agent mode is **not** used). Pairing flow via `hermes gateway setup`.
- **Hermes's own memory** (Honcho user modelling, FTS5 session search, agent-curated skills) stays active — for conversation and skill memory. Stored under `~/.hermes/`.
- **Cron scheduler** for proactive messages and timed actions (`hermes` ships [`cron/scheduler.py`](https://github.com/NousResearch/hermes-agent/blob/main/cron/scheduler.py), storage in `~/.hermes/cron/jobs.json`, skill-side access via the `cronjob` tool). Replaces what an earlier OSCAR draft built as `oscar-timer` / `oscar-alarm` skills + `time_jobs` Postgres table — we use Hermes-cron directly, household-level reminders are normal Hermes cron-jobs.
- **Skill registry + self-improvement loop** are also Hermes-native (`/skills`, agent-curated skills, autonomous skill creation after complex tasks). OSCAR's `skills/` dir is just symlinked into `~/.hermes/skills/oscar` by `scripts/install.sh`.
- Subagent spawning for parallel workflows.
- **MCP clients:**
  - **ServiceBay-MCP:** platform operations (`list_services`, `diagnose`, `get_health_checks`, `start_service`, `restart_service`). Bearer token, initial scope `read+lifecycle`. Added via `hermes mcp add <url> --token …`.
  - **HA-MCP** (Home Assistant's native MCP server, integration `mcp_server` from HA 2025.x): device control + entity listing with areas/aliases. Tool names (`HassTurnOn` family in current HA versions) are discovered at boot via MCP `tools/list` — skills do *not* hardcode them. Auth via HA long-lived access token or Authelia OIDC.
  - **OSCAR's own MCP servers:** `oscar-connectors` (one per connector), `oscar-ingestion`, plus wrappers for stack apps (`immich-search`, `radicale-cal`, `audiobookshelf-list`).

### 4. LLM backends

- **Hardware:** GPU server (RTX 4070 or comparable, ≥12 GB VRAM). No Mac mini planned.
- **Local default:** Gemma 4-12B Q4 via Ollama (fits in ~7 GB VRAM, ~30–50 tok/s) — larger than the originally planned 4B because the GPU allows it
- **Fast router** (optional): Gemma 4-1B or Gemma 4-4B for trivial commands that don't need a full LLM call
- **Vision/multimodal:** Gemma 4 is multimodal for image + text — serves the ingestion pipeline and can later combine audio + image in the voice path ("Look at this — what is it?" with a simultaneous camera snapshot)
- **Cloud connector:** Claude or Gemini, opt-in per query, routed through the Cloud-LLM connector with audit
- **STT stays Whisper:** despite multimodal Gemma — Whisper-large-v3 (on GPU ~50 ms for 3 s audio) is superior for plain transcription and streaming-capable via Wyoming. Gemma audio input can later serve as a second parallel path for "audio understanding" (emotion, tone).

### 5. Memory — two layers

The two layers live in two different places after the May 2026 reset:

| Layer | Storage | Where it physically runs | Owner |
|---|---|---|---|
| **Hermes conversation memory** | Honcho + FTS5 (Hermes-internal SQLite) | `~/.hermes/` on the host | Hermes |
| **OSCAR domain memory** | Qdrant (semantic) + Postgres (structured) | `oscar-brain` pod (containers `qdrant`, `postgres`) | OSCAR code |

Ollama (when local-LLM mode) also lives in `oscar-brain`; Hermes points its model provider at the pod's published port. `oscar-brain` is therefore the **data plane** — Postgres + Qdrant + Ollama + db-migrate + pg-backup — and Hermes is the agent on top of it.

The harness uid is passed from the gatekeeper to Hermes as a request parameter on every conversation call; **both layers** respect it for memory-namespace filtering.

#### Domain collections (Postgres in `oscar-brain`)

OSCAR keeps its own tables only where there is **no ServiceBay source**:

| Collection | Mode | Source |
|---|---|---|
| `books` | full table | OSCAR-only (no book app in the full stack) |
| `records` | full table | OSCAR-only (vinyl — no ServiceBay app) |
| `documents` | full table | OSCAR-only (deliberately local, no external enrichment) |
| `audiobooks` | **thin mirror** | Audiobookshelf (ServiceBay `media` stack) — OSCAR table only holds meta-notes (rating, status) + ABS id |
| `experiences` | **thin mirror** | Immich (for photo anchors) + Radicale (for events) — OSCAR stores experience note + asset ids |

Schema fields per collection (vector index over generated descriptions, back-references to original material):

- `books` — title, author, isbn, status (`reading|finished|wishlist`), started_at, finished_at, rating, notes, source_image, owner_harness
- `records` — album, artist, year, format (`vinyl|cd`), source_image, owner_harness
- `audiobooks` — abs_id (FK), rating, status_override, notes, owner_harness
- `documents` — type, date, parties, amounts, ocr_text, source_images, tags, owner_harness
- `experiences` — date, type, participants, location, notes, immich_asset_ids, radicale_event_id, owner_harness

Originals (images, scans) live encrypted in the **material store** (its own encrypted mount), referenced by UUID.

### 6. Tools (MCP servers)

Hermes consumes MCP tools from **four sources**:

| Source | Hosted in | Contents |
|---|---|---|
| **ServiceBay-MCP** | `<servicebay-url>/mcp` (native in ServiceBay) | Platform operations: services, logs, diagnostics, backups, proxy routes, health checks |
| **HA-MCP** | HA integration `mcp_server` (native in Home Assistant) | Device control, entity listing, areas, services, automations |
| **OSCAR stack-app wrappers** | Container in the `oscar-connectors` pod | `immich-search`, `radicale-cal`, `audiobookshelf-list`, `vaultwarden-read` (limited, audited) |
| **External connectors** | Container in the `oscar-connectors` pod | TuneIn, weather, web search, news, cloud LLM, Open Library, MusicBrainz, Discogs |

All four are added to Hermes via `hermes mcp add <url> [--token …]` at install time (`scripts/install.sh` automates this).

Plus OSCAR's own direct APIs (no MCP, because OSCAR-internal):
- Gatekeeper status (which harness is active, which voice device)
- Gatekeeper `POST /push` (outbound Voice-PE delivery — Hermes calls this for cron-fired messages)
- `oscar-ingestion` material-pipeline trigger

### 7. External connectors

Explicit, rule-based modules for every outside connection. Each connector: defined purpose, what goes out, what comes in, logged.

**Location:** a shared pod `oscar-connectors`, one container per connector, each exposing its own MCP server. Hermes + OSCAR tools consume them via MCP call.

**Conversation & information**

- TuneIn / internet radio
- Weather API
- Web search (anonymised)
- News feeds
- Cloud LLM (Claude/Gemini) — additional logging + permission check

**Enrichment connectors** (called by the ingestion pipeline, opt-in per material type)

- Open Library / Google Books — book covers, ISBN, genre, author bios
- MusicBrainz — album metadata, tracks
- Discogs — vinyl details, pressings
- (Documents are deliberately **not** enriched — they stay strictly local.)

All outbound calls go through NPM and are logged in AdGuard as known hosts — a second audit trail next to the connector's own logs.

### 8. Inbound knowledge pipeline (ingestion)

Incoming materials (photo, scan, voice memo, file attachment) run through their own pipeline instead of the conversation loop.

#### Triggers

- **Hermes gateway:** Signal/Telegram message with a file/photo attachment → Hermes forwards it to `oscar-ingestion` (Hermes' messaging gateway natively handles attachments)
- **Syncthing inbox:** a file appears in `/material-inbox/{lldap-uid}/` (one Syncthing folder per family member, mirrored from their phone) → `oscar-ingestion` watcher detects it via inotify

Both triggers land at the same pipeline entry point.

#### Use cases

1. **Collection enrichment**
   - Photo of a book cover → entry in `books` with status
   - Photo of a record sleeve → entry in `records`
   - Photo of an Audible screenshot → entry in `audiobooks` (thin mirror against Audiobookshelf match)
   - Optional with caption or voice note: rating, source, context

2. **Document archiving**
   - Photo/scan of an insurance policy, receipt, government letter
   - OCR + classification → entry in `documents`
   - Multi-page scans (several photos in quick succession) merged into one document

3. **Experience notes**
   - Photo of a concert ticket, restaurant, outing
   - Entry in `experiences` with Immich photo anchor, optionally into `family_shared` memory

#### Four pipeline stages

```
Material arrives (Signal/Telegram ∪ Syncthing inbox)
    ↓
[1] Pre-processing
    - Store the original encrypted in the material store
    - OCR on text regions (Tesseract local or vision LLM)
    - Multi-image bundling by time window + content similarity
    ↓
[2] Classification
    - Vision LLM (Gemma 4 multimodal): book | record | audiobook | document | receipt | experience | unknown
    - Metadata extraction: title, author, date, amount, recipient
    - Caption / voice note feeds into classification
    ↓
[3] Enrichment
    - Book → Open Library / Google Books (external connector, opt-in)
    - Music → MusicBrainz / Discogs (external connector, opt-in)
    - Audiobook → Audiobookshelf match (internal MCP lookup instead of external connector)
    - Document → no external enrichment (local)
    - Experience → Immich match (internal MCP lookup)
    ↓
[4] Confirmation & filing
    - Chat dialogue: "I recognise X. Should I save it as Y? [Yes] [Adjust] [No]"
    - On confirm: entry in the domain collection (full table or thin mirror) + vector index + reference to original
    - On adjust: short correction conversation
    - On no: material discarded, image deleted
```

#### Material store

- **Its own encrypted mount**, *not* via the `file-share` stack (file-share is family-public; material must be harness-scoped)
- RAID-protected NAS mount
- Path scheme: `/material/{lldap-uid}/{collection}/{uuid}.{ext}`
- Lifecycle: unconfirmed material is auto-deleted after 24 h

## Platform: ServiceBay v3.16+

- Repo: <https://github.com/mdopp/servicebay>
- Runtime: **Podman Quadlet** (rootless, systemd-integrated) — not Docker
- OS: **Fedora CoreOS**, immutable, self-updating
- Template format: Kubernetes Pod manifests (`template.yml`) with Mustache variables, deployed as Quadlet `.kube` units
- Variable types in `variables.json`: `text`, `secret`, `select`, `device`, `subdomain` (with `proxyPort`, `proxyConfig`, `oidcClient` block)
- Multi-node management via SSH (for later GPU-server addition etc.)
- Reactive digital-twin architecture (Python agent → backend → UI without polling)
- Diagnostic probes (crash_loop, cert_expiry, proxy_route_missing, post-deploy-exit, …): sensor stream for the harness system, free via the MCP `diagnose` tool
- **MCP server** (`/mcp`): HTTP endpoint, bearer token with scopes `read|lifecycle|mutate|destroy`, auto-snapshot before destructive calls, audit log

### What OSCAR consumes from the ServiceBay full stack

| Need | Source | Relationship |
|---|---|---|
| Smart home, Z-Wave, Matter | `home-assistant` | consumed via HA-MCP server (integration `mcp_server`), **without HA's voice pipeline** |
| Identity, SSO, OIDC | `auth` (LLDAP + Authelia) | direct |
| Photos | `immich` | via the `immich-search` MCP wrapper |
| CalDAV/CardDAV | `radicale` | via the `radicale-cal` MCP wrapper |
| Audiobooks | Audiobookshelf (in the `media` pod) | via the `audiobookshelf-list` MCP wrapper |
| Music | Navidrome (in the `media` pod) | Symfonium mobile client direct; OSCAR MCP wrapper for control optional |
| File drop / sync | `file-share` (Syncthing + Samba + FileBrowser + WebDAV) | Syncthing as the material-input trigger |
| Reverse proxy + LE certs | `nginx` (NPM) | for OSCAR web UIs |
| DNS sinkhole | `adguard` | audit trail for connector outbound calls |
| Password manager | `vaultwarden` | via the `vaultwarden-read` MCP wrapper (limited) |

→ **OSCAR doesn't rebuild any of this.** OSCAR consumes them through MCP tools or by writing to shared volumes.

ServiceBay's `voice` template (after mdopp/servicebay#348) is **for non-OSCAR setups** — OSCAR doesn't deploy it. `oscar-voice` replaces it fully.

### OSCAR as an external registry

The household operator adds `github.com/mdopp/oscar.git` under Settings → Registries as an external registry. ServiceBay clones the repo into `~/.servicebay/registries/oscar/` and reads `templates/` + `stacks/` from there. The four OSCAR templates show up in the wizard next to the built-in ones.

### ServiceBay patches OSCAR depends on

- **<https://github.com/mdopp/servicebay/issues/348>** — split the Wyoming stack out of the `home-assistant` template into a dedicated `voice` template + add a `VOICE_BUILTIN` variable to the HA template. Without this patch, Whisper/Piper/openWakeWord run inside the HA pod and collide with `oscar-voice` on the same Wyoming ports. **Phase 0 blocker.**
- **<https://github.com/mdopp/servicebay/issues/443>** — ServiceBay container ships without `git`, so external registries (like `github.com/mdopp/oscar.git`) can't be cloned. Until merged, OSCAR's `scripts/install.sh` renders templates locally and deploys via the `deploy_service` MCP call (no registry sync needed).
- **HA-MCP server integration** (`mcp_server`) — part of HA Core from 2025.x. In the OSCAR setup it is enabled and authenticated against Hermes via Authelia OIDC or a long-lived access token.

## OSCAR repo layout

```
github.com/mdopp/oscar/
├── README.md
├── CLAUDE.md
├── oscar-architecture.md          # this document
├── docs/
│   ├── architecture/
│   │   └── oscar-on-hermes.md     # the May-2026 reset rationale
│   ├── harness-spec.md            # JSON Schema for harness YAMLs (Phase 2)
│   ├── ingestion-spec.md          # schema for domain collections + pipeline (Phase 3a)
│   └── logging.md
├── templates/                     # ServiceBay Pod YAMLs (Mustache-rendered)
│   ├── oscar-voice/               # Rhasspy 3 + Whisper + Piper + openWakeWord + gatekeeper
│   ├── oscar-brain/               # Postgres + Qdrant + Ollama + db-migrate + pg-backup  (NO Hermes — that's host-installed)
│   ├── oscar-connectors/          # 1 container per connector, each its own MCP server
│   └── oscar-ingestion/           # pipeline + Syncthing watcher + material-store mount (Phase 3a)
├── stacks/
│   └── oscar/                     # documentation-only walkthrough
├── scripts/
│   ├── install.sh                 # installs Hermes + deploys templates + symlinks skills
│   └── render-template.py         # local Mustache renderer (workaround for servicebay#443)
├── gatekeeper/                    # Python code for the gatekeeper container in oscar-voice
├── ingestion/                     # Python code for the oscar-ingestion container (Phase 3a)
├── connectors/                    # code per connector — bundled into oscar-connectors containers
│   ├── tunein/                    # Phase 4
│   ├── weather/                   # Phase 1
│   ├── cloud-llm/                 # Phase 1
│   ├── open-library/              # Phase 3a
│   ├── musicbrainz/               # Phase 3a
│   └── discogs/                   # Phase 3a
├── shared/                        # cross-component Python libraries
│   ├── oscar_logging/             # structured JSON logging
│   ├── oscar_audit/               # query API over cloud_audit + future domain tables
│   ├── oscar_health/              # dependency probes used by oscar-status skill
│   └── oscar_db/                  # alembic migrations of the OSCAR-domain schema
├── harnesses/                     # YAML per LLDAP uid + system.yaml + guest.yaml (Phase 2)
│   ├── system.yaml
│   ├── michael.yaml               # filename = LLDAP uid
│   ├── anna.yaml
│   ├── child.yaml
│   └── guest.yaml
└── skills/                        # household-domain skills (symlinked into ~/.hermes/skills/oscar)
    ├── light/                     # HA-MCP lighting control (tool-name-agnostic)
    ├── status/                    # `oscar_health doctor` wrapper
    ├── audit-query/               # read-only query over OSCAR domain audit tables
    └── debug-set/                 # admin: toggle system_settings.debug_mode
```

Hermes-native concerns (timer/alarm cron, signal/telegram/etc. gateways, conversation memory, skill versioning, self-improvement) are intentionally **not** in this repo — they live in Hermes' install at `~/.hermes/`.

## Phase roadmap

### Phase 0 — Voice + Brain foundation

Goal: voice control noticeably better than Google Home, fully local, with real conversation instead of intent grammar.

**Prerequisites:**
- **GPU server**: RTX 4070 (or comparable, ≥12 GB VRAM) in the PC/server setup
- ServiceBay v3.16+ installed, full stack deployed
- **mdopp/servicebay#348 merged** (HA template can be deployed without Wyoming)
- HA pod redeployed with `VOICE_BUILTIN=disabled` + HA-MCP integration (`mcp_server`) enabled

**Deliverables:**
- Run `scripts/install.sh` — installs Hermes on the host (Nous Research's installer), deploys OSCAR templates via ServiceBay-MCP, symlinks `skills/` into `~/.hermes/skills/oscar`. Workaround for servicebay#443 lives inside the script (renders templates locally and deploys via `deploy_service` instead of relying on registry sync).
- **Write the `oscar-voice` template:**
  - Pod YAML with faster-whisper-large-v3, piper, openWakeWord, Rhasspy 3, gatekeeper
  - Gatekeeper initially **as pass-through** (no speaker ID, no embedding) — focused on pipeline orchestration and Hermes handoff
  - Wyoming ports 10300/10200/10400 exposed (HA Voice PE devices connect here)
  - `POST /push` endpoint so Hermes can deliver outbound messages to a Voice PE device (timer fires, proactive notifications)
- **Write the `oscar-brain` template (data plane):**
  - Pod YAML with **Postgres + Qdrant + Ollama (GPU passthrough) + oscar-db-migrate + pg-backup**. No Hermes container — Hermes is host-installed.
  - Ollama default model: Gemma 4-12B Q4 (or Gemma 3-12B as fallback). Hermes points its provider at `http://<host>:<ollama-port>` for local mode.
  - Postgres ports published so the host-installed Hermes can reach it for `oscar_audit` queries.
  - Postgres backup: weekly `pg_dump` sidecar, 4 weeks retention.
  - Alembic-driven schema migrations via `ghcr.io/mdopp/oscar-db-migrate` sidecar that runs `alembic upgrade head` on every pod start.
- Order an HA Voice Preview Edition for the office and configure it against `oscar-voice`.
- Create LLDAP users for the family (Michael, mother, child), `family` group.
- Bind local MP3s to the music folder of the `media` pod.
- Configure Symfonium on the phone against Navidrome.
- **OSCAR-owned skills (in `skills/`, symlinked into Hermes):** `oscar-light` (HA-MCP, tool-name-agnostic), `oscar-status` (health probes), `oscar-audit-query` (read-only cloud_audit), `oscar-debug-set` (admin debug toggle). Heating + music skills follow next.
- Timer / alarm / reminder behaviours come from **Hermes' built-in cron** — household commands like "Stell einen Timer auf 5 Minuten" become normal `cronjob` tool calls, no OSCAR table required.

Result: OSCAR-owned voice pipeline end to end, Hermes-driven conversation, HA as a device tool, one identity for everyone (no voice ID yet).

### Phase 1 — Mobile + connectors

**Messaging gateways (Hermes-native):**
- `hermes gateway setup signal` — Hermes' built-in Signal gateway pairs as a **linked device** of an existing family phone number, not as a separate OSCAR number. Session state lives under `~/.hermes/signal-cli/`. No OSCAR-side `signal-cli-daemon` container, no `signal_gateway` bridge, no `gateway_identities` table — Hermes handles pairing, contact registry, and `signal:<num>` ↔ uid mapping internally.
- Telegram + Discord + Slack + Email symmetric, all via `hermes gateway setup <platform>`.
- **Roll-out order:** Michael alone first, family en bloc only after ~2 weeks of stability.
- For outbound delivery to a Voice PE device (e.g. a Hermes cron job firing a reminder while the user is at home): Hermes' delivery system POSTs to the gatekeeper's `POST /push` endpoint.

**Connectors:**
- First `oscar-connectors`: Cloud-LLM, weather, web search (**TuneIn deferred** — arrives only with Music Assistant in Phase 4).
- Connector skeleton (Python + FastMCP) as a template — repo layout, tool pattern, auth, variables.json example: spec `docs/connector-skeleton.md`.
- API keys/secrets through the ServiceBay `variables.json` (`type: secret`), wizard-prompted at deploy.
- Permission enforcement: the harness composer (loaded by Hermes via the symlinked OSCAR skills) checks the permission **before** the MCP call; connector containers trust the caller.

**Cloud LLM:**
- **Automatic escalation**, no voice keyword: a small router scores complexity. Above threshold *and* harness-permitted → Cloud-LLM connector. Otherwise local Gemma via Ollama.
- Audit table `cloud_audit` in `oscar-brain.postgres`: timestamp, uid, trace_id, prompt-hash, prompt-length, response-length, vendor, cost, **router score + escalation reason**. Full-text logging hangs off the global `debug_mode` (see the cross-cutting section) rather than a separate per-call opt-in.
- Audit query via voice/chat ("What did the cloud connector send today?") through the `oscar-audit-query` skill.

Result: conversation on the go, world access opt-in per harness, automatic up-routing for complex queries.

### Phase 2 — Gatekeeper speaker ID + harnesses

- Activate SpeechBrain ECAPA-TDNN in the gatekeeper
- Create the voice-embedding table in `oscar-brain.postgres`, FK to LLDAP uid
- Train embeddings per family member (e.g. 10 sentences each; setup wizard in the gatekeeper web UI, Authelia OIDC protected)
- Formalise the harness YAML schema (JSON Schema in `docs/harness-spec.md`)
- Introduce memory namespaces in Qdrant + Postgres
- System + Michael + guest as the first harnesses
- Verbal hints for guest mode

Result: privacy preserved.

### Phase 3a — Streaming ingestion

- `oscar-ingestion` template (pipeline container + Syncthing watcher)
- Material store as its own encrypted mount, 24 h TTL for unconfirmed material
- Syncthing inbox folder per LLDAP uid (`/material-inbox/{uid}/`)
- Vision classifier via Gemma 4 multimodal
- Schema migrations for the domain collections in Postgres
- Enrichment connectors into `oscar-connectors` (Open Library, MusicBrainz, Discogs)
- Incremental roll-out per material type:
  1. **Books first** — own table, Open Library
  2. **Records** — own table, MusicBrainz / Discogs
  3. **Audiobooks** — thin mirror onto Audiobookshelf
  4. **Documents** — fully local, OCR-focused, tax-archive tags
  5. **Experience notes** — thin mirror onto Immich + Radicale

### Phase 3b — Bulk import + MCP wrappers

- MCP wrapper tools in the `oscar-connectors` pod (or its own `oscar-mcp-wrappers` pod):
  - `immich-search` — photo search (vision + metadata)
  - `radicale-cal` — appointment CRUD
  - `audiobookshelf-list` — audiobook library
- Signal history import (parse family Signal archives)
- Google Takeout (Maps history, photos via Immich)
- Audible lists (either Audiobookshelf direct or screenshot ingestion)
- Email/CalDAV/CardDAV local sync (via Radicale)

Result: deep retroactive long-term memory.

### Phase 4 — Active extensions (ongoing)

- Hermes as note-taker (proactive memo creation from conversations)
- Voice-tone / emotion analysis as an additional gatekeeper sensor (Gemma multimodal on the audio stream in parallel to Whisper STT)
- Multi-room voice routing: multiple Voice PE devices, gatekeeper routes responses to the originating device
- `oscar-music-assistant` template, once ≥2 rooms have voice (Music Assistant for synchronised playback)
- TuneIn / internet-radio connector (previously deferred because without Music Assistant it only pays off for a single room)
- "Good morning" routine as a composite Hermes skill: HA-MCP call (lights 60%, heating +1) + TuneIn connector (DLF) on the primary Voice-PE speaker
- Refined external connectors
- Presence detection via phone
- Multi-household distribution (own LLDAP + harness repo per household)
- Custom response voices per family member (Piper voice-model mapping)
- Train a custom wakeword "Oscar" (own openWakeWord model in the `oscar-voice` pod)
- Cross-modal search: "Show me the book I was reading by the lake last summer"

## Cross-cutting: debug mode

Global switch in `system.yaml`. While we're building OSCAR (Phase 0/1, perhaps 2) it is **on by default**; once we transition to productive family use, the default flips to off.

```yaml
# system.yaml
debug_mode:
  active: true                # build-phase default
  verbose_until: null         # NULL = unbounded; otherwise timestamp = TTL
  latency_annotations: false  # path/latency annotation on voice responses, separately toggled
```

When `active: true`:
- all OSCAR components log full text (prompts, responses, tool args, connector request/response bodies) instead of metadata only
- retention policies on audit tables (`cloud_audit`, future `gatekeeper_decisions`, `ingestion_classifications`) are suspended — no auto-deletion
- with `latency_annotations: true`, voice responses additionally carry "STT 230ms · router 80ms → 12B local · 1.4s" as an annotation (useful filtered to admin uids, not for family members)

Components re-query the mode for every log/audit event (no caching > 5 s), so turning it off takes effect immediately. The admin skill `oscar-debug-set` writes the fields; voice activation works ("Debug mode on for 4 hours" → sets `active=true, verbose_until=now()+4h`). Auto-off via TTL check on read: `verbose = active AND (verbose_until IS NULL OR now() < verbose_until)`.

Consequence: there is **no** separate per-call opt-in for full-text logging in the Cloud connector or anywhere else — the only switch is `debug_mode`. User-facing permissions ("may use cloud" etc.) are unaffected.

## Cross-cutting: logging

Two tracks — **operational** (container stdout JSON → journald, read via ServiceBay-MCP `get_container_logs` / `get_service_logs` / `get_podman_logs`) and **domain audit** (Postgres tables in `oscar-brain`, read via the `oscar-audit-query` skill). Hermes has its own session log under `~/.hermes/` for conversation-level history. Connected by `trace_id` per conversation turn.

Full spec: **[`docs/logging.md`](docs/logging.md)** — shared library `shared/oscar_logging/`, retention policies per audit table, log-level convention, PII handling, ServiceBay-MCP read path including the secret-redaction layer.

**Deliberately not now:** Loki/Vector or a dedicated log aggregator (unnecessary before Phase 3+). No dedicated log web UI — ServiceBay already has a log viewer.

## Key decisions

| Topic | Decision |
|---|---|
| Hardware | GPU server (RTX 4070 or comparable, ≥12 GB VRAM). No Mac mini. |
| Identity | LLDAP uid + groups (`family`, `admins`) from the ServiceBay `auth` pod |
| SSO for OSCAR web UIs | Authelia OIDC, registered via the `oidcClient` block in `variables.json` |
| Reverse proxy + TLS | NPM (ServiceBay `nginx` pod) via wizard |
| DNS block | AdGuard (ServiceBay `adguard` pod) |
| Voice pipeline ownership | **OSCAR owns it end to end** (Rhasspy 3 + gatekeeper in `oscar-voice`). HA's voice pipeline is **not** used. |
| HA role | Device hub via the HA-MCP server (integration `mcp_server`), not a voice broker |
| STT model | faster-whisper-large-v3 on GPU (~50 ms for 3 s audio). Whisper remains superior to Gemma audio for plain transcription. |
| LLM | Gemma 4-12B Q4 default (fits in ~7 GB VRAM on GPU) |
| Wakeword | Single ("Hey Jarvis" initially, later a custom "Oscar" model), short answers, "Guest:" prefix, beep for recognised members |
| Offline behaviour | Control, music (local), memory keep working. Lost: weather, streaming, external search, enrichment connectors, cloud LLM |
| Agent runtime | **Hermes Agent host-installed** via its own installer (not a container in oscar-brain). OSCAR provides the data plane (Postgres/Qdrant/Ollama) + voice + connectors + skills; Hermes provides the agent loop, gateways, cron, memory, skill registry, self-improvement loop. Rationale: `docs/architecture/oscar-on-hermes.md`. |
| Cloud LLM | Automatic escalation from a small router's complexity score, if the harness allows. Audit (incl. `trace_id` + router score) in the `cloud_audit` table. Full-text logging is gated by `debug_mode`, no separate per-call opt-in. |
| Gateway identities | **Hermes-native** — `hermes gateway setup <platform>` handles pairing + per-user mapping under `~/.hermes/`. OSCAR keeps no `gateway_identities` table; identity-link skill retired. |
| Debug mode | Global `system_settings.debug_mode` row in oscar-brain Postgres — build-phase default on; productive off; TTL reactivation via the admin voice command. No component-specific verbose flags. |
| Logging | Operational → stdout JSON → journald, read via ServiceBay-MCP (`get_container_logs` etc.). Domain audit → Postgres tables in `oscar-brain`, read via the `oscar-audit-query` skill. Conversation/session log → Hermes-native under `~/.hermes/`. `trace_id` correlation. Shared library `shared/oscar_logging/` enforces the schema. |
| Audit backup | Weekly `pg_dump` as a sidecar in the `oscar-brain` pod, dedicated volume mount, 4 weeks retention. Off-site backup as a later roadmap phase. |
| Voice embeddings | Gatekeeper Postgres table in `oscar-brain` with FK to LLDAP uid — *not* in LLDAP. Phase 2. |
| Memory | Two layers: Hermes Honcho (conversation/skills, in `~/.hermes/`) + OSCAR Qdrant/Postgres (domain collections, in `oscar-brain` pod). Harness uid propagated as a request parameter (no HA header marshalling any more). |
| Domain collections | Full tables for `books`/`records`/`documents`; thin mirrors for `audiobooks`/`experiences`. Phase 3+. |
| Material trigger | Signal photo ∪ Syncthing inbox per LLDAP uid. Phase 3a. |
| Material store | Own encrypted mount (not via the `file-share` stack). Phase 3a. |
| Connectors | Shared `oscar-connectors` pod, 1 container per connector, each its own MCP server. Added to Hermes via `hermes mcp add`. |
| ServiceBay control by Hermes | Via the ServiceBay-MCP endpoint with bearer token (`read+lifecycle` initially), added via `hermes mcp add`. |
| HA control by Hermes | Via the HA-MCP endpoint with a long-lived access token (or Authelia OIDC), added via `hermes mcp add`. Tool names discovered live; skills don't hardcode them. |
| Mobile music | Symfonium → Navidrome (ServiceBay `media` pod) |
| Mobile audiobooks | Audiobookshelf's own apps (ServiceBay `media` pod) |
| Mobile chat | Signal/Telegram/Discord/Slack/WhatsApp/Email → Hermes' built-in gateway |
| Timers / alarms / reminders / recurring tasks | **Hermes-native cron scheduler** (`cronjob` tool, `~/.hermes/cron/jobs.json`). OSCAR no longer maintains a `time_jobs` table or timer/alarm skills. |
| Skill management | **Hermes-native** — skill registry under `~/.hermes/skills/`, agent-curated skills, autonomous skill creation, self-improvement loop. OSCAR's `skills/` dir is symlinked in for household-specific skills. |
| External VPN access | Wireguard (exists) |
| Vision model | Gemma 4 multimodal, same stack as text |
| Material originals | Encrypted in the material store, referenced by UUID |
| Document enrichment | Deliberately none — they stay strictly local |
| Music Assistant | Later (Phase 4, once ≥2 rooms) |
| Backup externalisation | Later, separate roadmap phase |

## Open points for Claude Code

1. **Track mdopp/servicebay#348** — Phase 0 blocker. Before writing the `oscar-voice` template, make sure the patch is merged. Also **mdopp/servicebay#443** (ServiceBay container missing git) — workaround lives in `scripts/install.sh`, but the proper fix removes that workaround.
2. **Integrate Rhasspy 3 as the pipeline backbone** — evaluate Rhasspy 3 (maturity, API), decide whether to run it as a container in the `oscar-voice` pod or have the gatekeeper import the Rhasspy 3 code directly. Identify the hook point for speaker-ID embedding extraction.
3. **`oscar-voice` pod layout** — Wyoming ports exposed (10300/10200/10400), internal Whisper on 11300. Handle multiple Voice-PE devices simultaneously (session routing). Voice-PE outbound push: verify `wyoming-satellite` mode on the devices accepts our gatekeeper's `POST /push` flow (#34).
4. **GPU passthrough for Ollama via Quadlet** — confirm `nvidia.com/gpu: "1"` in the Pod spec gets translated correctly to a Quadlet `AddDevice=nvidia.com/gpu=all` (currently unverified at deploy time).
5. **Enable the HA-MCP server** — configure the HA-MCP integration, token auth, test tool-listing stability. Reconcile HA areas/aliases naming conventions with what OSCAR skills expect — note `oscar-light` is now tool-name-agnostic so the live `tools/list` catalog is the source of truth.
6. **Gatekeeper ↔ LLDAP mapping** — embedding-training wizard (gatekeeper web UI with Authelia OIDC), CLI fallback, possibly a Hermes skill "Learn my voice". Phase 2.
7. **Hermes ↔ OSCAR-brain integration** — verify Hermes' MCP add flow against ServiceBay-MCP + HA-MCP + oscar-connectors. Confirm Hermes points its model provider at the pod's Ollama port for local-mode. Decide where harness-uid propagation hooks into Hermes' request shape.
8. **Material-store encryption** — LUKS container or filesystem-layer (e.g. gocryptfs)? Key management (TPM, boot-time passphrase?). Phase 3a.
9. **MCP wrapper templates** — do `immich-search` / `radicale-cal` / `audiobookshelf-list` belong in `oscar-connectors` (semantically a fit: they consume an external source with a clear in/out contract) or in their own `oscar-mcp-wrappers` pod?
10. **Authelia OIDC clients** — which OSCAR services have a web UI? Initially likely: gatekeeper admin (voice training, Phase 2), ingestion confirmation dashboard (Phase 3a).
11. **Ingestion pipeline skeleton** — trigger disambiguation (Hermes-gateway attachment vs. Syncthing inbox), confirmation-dialogue skill driven by Hermes. Phase 3a.
12. **Domain-collection schemas** — Postgres DDLs for `books`, `records`, `documents` + thin-mirror tables for `audiobooks`/`experiences`. Vector-index strategy (Qdrant collection per domain collection? A single global one with filters?). Phase 3+.

## Sources / references

- Hermes Agent: <https://github.com/nousresearch/hermes-agent>
- Hermes Agent docs: <https://hermes-agent.nousresearch.com/docs/>
- agentskills.io standard: <https://agentskills.io/>
- Rhasspy 3: <https://github.com/rhasspy/rhasspy3>
- Wyoming Protocol: <https://github.com/rhasspy/wyoming>
- ServiceBay: <https://github.com/mdopp/servicebay>
- ServiceBay voice-split issue: <https://github.com/mdopp/servicebay/issues/348>
- ServiceBay registry-sync git-binary issue: <https://github.com/mdopp/servicebay/issues/443>
- Home Assistant MCP server: <https://www.home-assistant.io/integrations/mcp_server/>
- Harness engineering (Böckeler): <https://martinfowler.com/articles/harness-engineering.html>
- Anatomy of an Agent Harness (LangChain): linked from the Fowler article
- Gemma 4: <https://deepmind.google/models/gemma/>
- LLDAP: <https://github.com/lldap/lldap>
- Authelia: <https://www.authelia.com/>
- Symfonium: <https://symfonium.app/>
- Audiobookshelf: <https://www.audiobookshelf.org/>
- Immich: <https://immich.app/>
- Radicale: <https://radicale.org/>
- Home Assistant Voice Preview Edition: via Nabu Casa
- Open Library API: <https://openlibrary.org/developers/api>
- MusicBrainz API: <https://musicbrainz.org/doc/MusicBrainz_API>
- Discogs API: <https://www.discogs.com/developers>
- Model Context Protocol (MCP): <https://modelcontextprotocol.io/>
