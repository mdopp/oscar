# O.S.C.A.R. — Architecture

> Living document. May 2026 lean reset: OSCAR is a thin household-identity-and-memory layer on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent) and [ServiceBay](https://github.com/mdopp/servicebay). Everything that is not specifically about *this household* lives in those two projects.

## Vision

OSCAR is a private operating system for the family: voice at home, chat on the road, one assistant, every resident has their own world, nothing leaves the house by accident.

### Five intents

1. **Sovereignty.** Modern AI in the house without exposing the family. All AI runs locally on a household server. Cloud LLMs only on explicit, audited opt-in.
2. **Long memory.** The household's bookshelf, record collection, documents, photos, appointments, decisions — woven into something the assistant can query.
3. **One conversation.** Voice at home (Wyoming via HA Voice PE devices), chat on the road (Signal/Telegram/…) — same agent, same memory.
4. **Per-resident privacy.** Father, mother, child each have their own memory namespace and tool scope. Guests get a smaller, locked-down world. *Voice is identity.*
5. **Things actually happen.** Lights, heating, scenes, timers, reminders — driven through Home Assistant's MCP server.

## The boundary

Three projects, three jobs. Whenever a capability is generic, it lives in one of the other two and OSCAR consumes it.

### What [Hermes Agent](https://github.com/NousResearch/hermes-agent) gives us

Hermes is the **agent runtime**. Consumed as the upstream container `docker.io/nousresearch/hermes-agent`. Hermes provides:

- Conversation loop, skill registry, agent-curated skill creation, self-improvement loop
- Messaging gateways (Signal, Telegram, Discord, Slack, WhatsApp, Email) — paired interactively via `hermes gateway setup`
- Cron scheduler for timers, alarms, reminders, recurring tasks
- Memory: Honcho user modelling + FTS5 conversation search, per-user scoped
- MCP client for consuming external tool surfaces
- LLM-provider abstraction (local via Ollama, cloud via Claude / Gemini / OpenRouter / …)

OSCAR does **not** fork Hermes. Behaviour we miss gets contributed back as a PR or as an MCP server Hermes can mount.

### What [ServiceBay](https://github.com/mdopp/servicebay) gives us

ServiceBay is the **platform**. Consumed as an external template registry. ServiceBay provides:

- Identity: LLDAP + Authelia (full-stack)
- Smart home: Home Assistant with its native MCP server (full-stack, **without** the bundled Wyoming pipeline)
- Photos / calendar / contacts / audiobooks / music / file-share (full-stack: `immich`, `radicale`, `media`, `file-share`)
- Reverse proxy + TLS (`nginx`), DNS sinkhole (`adguard`), password manager (`vaultwarden`)
- Platform MCP control surface (`/mcp`, scopes `read|lifecycle|mutate|destroy`, bearer token)
- New `ai-stack` (to be built): a wizard walkthrough that deploys the AI infrastructure templates — `postgres`, `qdrant`, `ollama`, `hermes`. Each is a generic ServiceBay template, useful beyond OSCAR.
- Extended `voice` template (to be built): the existing Wyoming pipeline (Whisper + Piper + openWakeWord) gets an optional `GATEKEEPER_IMAGE` sidecar variable, so any deployment can opt into a Hermes-aware voice gateway.
- Structured-logging and health-probe contracts (to be built): a platform standard every template can follow.

The `ai-stack`, the voice-template extension, and the platform contracts are **work to be done in `mdopp/servicebay`**, not in OSCAR. They are tracked from OSCAR via a single tracking issue with cross-links.

### What's left for OSCAR

The irreducible household-specific layer:

1. **Voice ↔ resident identity.** Speaker embedding (SpeechBrain ECAPA-TDNN) → LLDAP `uid` lookup → Hermes turn runs in that user's scope. Phase 2.
2. **A small Postgres schema** for OSCAR-specific tables: `cloud_audit`, `system_settings`, `voice_embeddings`. Plus, in Phase 3a, the domain-collection tables (`books`, `records`, `documents`, `audiobooks`, `experiences`).
3. **Three household skills** that read those tables: `oscar-status` (system health), `oscar-audit-query` (cloud-LLM audit), `oscar-debug-set` (admin debug toggle). Read-mounted into Hermes at `/opt/data/skills/oscar`.
4. **The gatekeeper image:** Wyoming-protocol bridge that connects HA Voice PE devices to Hermes. Published as `ghcr.io/mdopp/oscar-gatekeeper` and consumed by ServiceBay's extended `voice` template as an optional sidecar. Long-term target: contribute the Phase-0 pass-through path to Hermes as a generic `hermes gateway voice`.
5. **A ServiceBay stack walkthrough** that names the templates above in the right order with the right variables, so a new household has a deterministic path from "ServiceBay installed" to "speaking with Hermes through a Voice PE in German with audit on".

Everything else from earlier OSCAR drafts (data-plane templating, Hermes-container wrapping, voice-pipeline templating, weather connectors, structured-logging library, health-probe library, light skill, ingestion module, connector skeleton) is either upstreamed, dropped, or postponed.

## Architecture overview

```
                  SIGNAL / TELEGRAM / DISCORD / …          HA Voice PE
                              │                                  │ Wyoming
                              │  Hermes-native gateway           │
                              ▼                                  ▼
                ┌──────────────────────────┐         ┌────────────────────────┐
                │     hermes               │         │   voice (ServiceBay)   │
                │     (ServiceBay)         │◄──HTTP──│  whisper + piper +     │
                │     wraps nousresearch/  │         │  openwakeword +        │
                │     hermes-agent         │         │  gatekeeper (OSCAR)    │
                │                          │         │                        │
                │  Honcho memory · cron ·  │         │  Wyoming in/out +      │
                │  skill registry · MCP    │         │  POST /push outbound   │
                │  client                  │         └────────────────────────┘
                └──┬──────┬──────────┬─────┘
                   │      │          │ MCP
                   │      │   ┌──────┴────────────────────────┐
                   │      │   ▼                               ▼
                   │      │  ┌───────────────┐   ┌─────────────────────────┐
                   │      │  │   HA-MCP      │   │  ServiceBay-MCP         │
                   │      │  │  (devices,    │   │  (services, health,     │
                   │      │  │   scenes)     │   │   logs, diagnostics)    │
                   │      │  └───────────────┘   └─────────────────────────┘
                   │      │
                   │      ▼ skills read-mount   ┌──────────────────────────┐
                   │   /opt/data/skills/oscar:  │  oscar-household         │
                   │     • oscar-status          │  (OSCAR)                │
                   │     • oscar-audit-query     │                          │
                   │     • oscar-debug-set       │  Runs Alembic against    │
                   │                              │  the shared Postgres;    │
                   │                              │  configures Hermes' MCP  │
                   │                              │  endpoints; mounts the   │
                   │                              │  OSCAR skills            │
                   │                              └──────────────────────────┘
                   │
                   ▼ Hermes-provider URL
        ┌─────────────────┐  ┌──────────────┐  ┌────────────────────────┐
        │  ollama         │  │  postgres    │  │  qdrant                │
        │  (ServiceBay,   │  │  (ServiceBay,│  │  (ServiceBay,          │
        │   ai-stack)     │  │   ai-stack)  │  │   ai-stack, Phase 3a)  │
        │  local Gemma    │  │  Honcho +    │  │  semantic index        │
        │                 │  │  OSCAR schema│  │                        │
        └─────────────────┘  └──────────────┘  └────────────────────────┘
```

Four templates from ServiceBay's `ai-stack` (`postgres`, `ollama`, `qdrant` optional, `hermes`), one extended ServiceBay template (`voice`), one OSCAR template (`oscar-household`). The gatekeeper is an OSCAR-published image consumed by ServiceBay's `voice` template.

## Components in detail

### gatekeeper (OSCAR-published image)

A Wyoming-protocol server. One inbound satellite connection = one half-duplex pipeline turn:

1. HA Voice PE (or any `wyoming-satellite` client) connects and streams `AudioStart` + `AudioChunk*` + `AudioStop`.
2. The gatekeeper calls the in-pod Whisper container (`tcp://127.0.0.1:10300`) for STT.
3. *Phase 0:* `uid = DEFAULT_UID`. *Phase 2:* SpeechBrain ECAPA-TDNN extracts a 256-d voice embedding; lookup against `voice_embeddings` in Postgres resolves it to an LLDAP `uid`; unknown → `guest`.
4. The gatekeeper POSTs `(text, uid, endpoint, trace_id)` to Hermes' API at `HERMES_URL`.
5. Hermes' response text goes to Piper (`tcp://127.0.0.1:10200`); synthesised audio streams back to the satellite.
6. Outbound: `POST /push {endpoint: "voice-pe:<name>", text}` lets Hermes' cron and proactive deliveries address a specific Voice PE device by name (resolved against `VOICE_PE_DEVICES`).

Voice embeddings live in Postgres (`voice_embeddings` table, FK to LLDAP `uid`); **never** in LLDAP — biometric PII.

Long term, the Phase-0 pass-through path (steps 1, 2, 4, 5) should land in Hermes as a generic `hermes gateway voice`. The OSCAR-specific Phase 2+ logic (speaker ID, multi-room routing, voice-tone analysis) stays here.

### oscar-household (OSCAR template)

The one ServiceBay template OSCAR ships. Responsibilities:

- **Schema init.** A one-shot Alembic container runs on every pod start against the shared Postgres (deployed by `ai-stack`'s `postgres` template); creates the three OSCAR tables (`cloud_audit`, `system_settings`, `voice_embeddings`) and, in Phase 3a, the domain-collection tables. Idempotent.
- **Skill mount.** OSCAR's `skills/` directory (cloned with the OSCAR registry) is bind-mounted into the Hermes container at `/opt/data/skills/oscar`. Hermes picks up the three OSCAR skills alongside its built-in Skills Hub.
- **MCP wiring.** Post-deploy hook adds HA-MCP and ServiceBay-MCP to Hermes via `hermes mcp add` with the tokens collected by the wizard. Default model provider points at the `ai-stack`'s Ollama port.
- **Audit hook.** Sets the cloud-LLM audit proxy URL in Hermes' env so every cloud call writes a row to `cloud_audit`. (The audit-proxy MCP itself lives in a separate repo / package — see "Upstream work".)
- **Variables.** Wizard-prompted: `LLDAP_GROUP`, `HERMES_TOKEN`, `HA_MCP_TOKEN`, `SERVICEBAY_MCP_TOKEN`, `GATEKEEPER_IMAGE` (defaults to OSCAR's published image, swappable for a fork during PoC).

### The three skills

| Skill | Reads | Purpose |
|---|---|---|
| `oscar-status` | health probes against Postgres, Ollama, Hermes, HA-MCP, ServiceBay-MCP | "Is OSCAR alive?" — answered by structured health probes. |
| `oscar-audit-query` | `cloud_audit` table | "What did the cloud connector send today?" — natural-language read over the audit table. |
| `oscar-debug-set` | `system_settings.debug_mode` row | Admin only. Voice toggle for verbose mode with a TTL ("debug on for four hours"). |

Each skill is a small Hermes skill (Markdown spec + Python tool calls), tracked in `skills/` and read-mounted via `oscar-household`.

### The schema

Three tables in Phase 0–2:

| Table | Owner | Notes |
|---|---|---|
| `cloud_audit` | OSCAR | One row per Hermes cloud-LLM call: timestamp, uid, trace_id, vendor, model, prompt-hash, prompt-length, response-length, cost, router score, escalation reason. Full text gated by `system_settings.debug_mode`. |
| `system_settings` | OSCAR | A single row with global flags: `debug_mode.active`, `debug_mode.verbose_until` (TTL), `debug_mode.latency_annotations`. Read by every component on every audit event (no caching > 5 s). |
| `voice_embeddings` | OSCAR | 256-d ECAPA-TDNN vectors per LLDAP uid + enrolment metadata. Phase 2. FK to LLDAP `uid` (string). |

Phase 3a adds the domain collections (`books`, `records`, `documents`, `audiobooks`, `experiences`) — see the phase roadmap.

Alembic migrations live in `schema/`; the migration container is part of `oscar-household`.

## Identity and harness (Phase 2)

Three harness types compose at runtime: **System** (always active) ∪ (**Personal** | **Guest**). YAML in `harnesses/`, named after the LLDAP `uid` (e.g. `michael.yaml`). Five fields per harness: `context`, `tools`, `guides`, `sensors`, `permissions`.

Until Phase 2 ships, `harnesses/` is a roadmap placeholder — the harness composition is layered on top of Hermes' own user/skill knobs by the gatekeeper at turn-handoff time.

Whether harness composition becomes a Hermes-upstream feature (so any multi-user Hermes deployment can use it) or stays an OSCAR-side wrapper is a Phase-2 decision.

## Memory layers

Two layers, two places:

| Layer | Storage | Where | Owner |
|---|---|---|---|
| Conversation history + skill curation | Honcho + FTS5 | Hermes container's data volume | Hermes |
| OSCAR audit + Phase-3a domain memory | Postgres + Qdrant | `ai-stack` pods (`postgres`, `qdrant`) | OSCAR schema, ServiceBay deploy |

Both layers respect the `uid` namespace filter that the gatekeeper passes per turn.

## Cross-cutting concerns

### Debug mode

Global switch in the `system_settings.debug_mode` Postgres row. While building OSCAR (Phase 0–2) it defaults to `on`; productive household use flips it to `off`.

When `active = true`:

- All components log full text (prompts, responses, tool args, connector bodies) instead of metadata only
- Retention policies on audit tables are suspended
- With `latency_annotations: true`, voice responses carry "STT 230ms · router 80ms → local 12B · 1.4s" annotations (filtered to admin uids)

TTL via `verbose_until`. Components re-query the row on every audit event (no caching > 5 s). Voice toggle via the `oscar-debug-set` skill: *"Debug mode on for four hours"* → sets `active=true, verbose_until=now()+4h`.

### Cloud-LLM audit policy

Every Hermes cloud-LLM call generates a `cloud_audit` row. The audit mechanism is an MCP audit-proxy (small project, separate repo — *see upstream work*). The *policy* — "every cloud call is family-visible" — is OSCAR-eigen: it shows up in the `oscar-audit-query` skill, in the family's view of what the assistant has been doing, and in the per-harness cloud opt-in.

### Logging

Operational: container stdout JSON → journald → readable via ServiceBay-MCP (`get_container_logs`). Structured-logging *contract* lives in ServiceBay (work to be done).

Domain audit: Postgres tables in the shared `ai-stack` Postgres, read via the `oscar-audit-query` skill.

Conversation: Hermes-native under its data volume.

Correlation via `trace_id` per turn.

No log aggregator (Loki / Vector) — ServiceBay's log viewer is sufficient until Phase 3+.

## Phase roadmap

### Phase 0 — Chat-on-Hermes + lights

**Prereqs.** ServiceBay v3.16+ with the full-stack deployed. `mdopp/servicebay#348` merged (HA without bundled Wyoming) — *only needed once voice is added*. `mdopp/servicebay#443` merged (`git` in ServiceBay's container) so the OSCAR registry can be cloned.

**Deliverables.** ServiceBay's `postgres`, `ollama`, `hermes` templates exist and are wizard-deployable. OSCAR's `oscar-household` template exists. `ai-stack` walkthrough plus OSCAR's stack walkthrough together produce a working setup. Hermes paired with Signal via `hermes gateway setup signal`. HA-MCP added via `hermes mcp add`. First household skill — `oscar-light` *(upstreamed as a generic `smart-home/home-assistant` skill to the Hermes Skills Hub, not held inside OSCAR)* — controls HA devices.

**Result.** Family chat in Signal, lights/heating controllable by voice via Signal-message-to-Hermes. No voice path yet.

### Phase 1 — Voice path

**Prereqs.** ServiceBay's `voice` template extended with the `GATEKEEPER_IMAGE` sidecar variable. OSCAR's gatekeeper image published to `ghcr.io/mdopp/oscar-gatekeeper`.

**Deliverables.** HA Voice PE in the office, configured against the extended ServiceBay `voice` template. Gatekeeper in pass-through mode (`DEFAULT_UID`). Whisper-large-v3 on GPU (≤50 ms for 3 s audio). Piper for German voice.

**Result.** Spoken conversation at home, single-user. Same agent and same memory as the Signal channel.

### Phase 2 — Speaker ID + per-resident namespaces

**Deliverables.** SpeechBrain ECAPA-TDNN in the gatekeeper. `voice_embeddings` table populated via an enrolment wizard. Harness YAML schema. `system.yaml` + `michael.yaml` + `guest.yaml`. The gatekeeper resolves uid per turn; Hermes runs under that uid's memory namespace and tool scope.

**Result.** Per-resident privacy. Voice is identity.

### Phase 3a — Streaming ingestion

**Deliverables.** Domain-collection tables (`books`, `records`, `documents`, `audiobooks`, `experiences`) added to the OSCAR schema. Ingestion pipeline: trigger from Hermes messaging gateway attachments **or** a Syncthing-watched per-uid material inbox; classification via local Gemma multimodal; opt-in enrichment connectors (Open Library, MusicBrainz, Discogs) added via `hermes mcp add`; confirmation dialogue. Encrypted material store on a dedicated mount.

**Result.** Long memory begins.

### Phase 3b — Bulk import + MCP wrappers

Signal/Telegram history import. Google Takeout. Audiobookshelf, Immich, Radicale wrappers as MCP tools Hermes consumes.

### Phase 4 — Active extensions

Voice-tone analysis as a parallel gatekeeper sensor. Multi-room voice routing (≥2 rooms). Custom "Oscar" wakeword. Proactive Hermes-driven memo creation. TuneIn / internet-radio MCP. Multi-household.

## Upstream work tracked from OSCAR

| Where | What |
|---|---|
| `mdopp/servicebay` | New `postgres` template |
| `mdopp/servicebay` | New `qdrant` template (Phase 3a) |
| `mdopp/servicebay` | New `ollama` template with optional GPU passthrough |
| `mdopp/servicebay` | New `hermes` template wrapping `docker.io/nousresearch/hermes-agent` |
| `mdopp/servicebay` | New `ai-stack` walkthrough bundling the four above |
| `mdopp/servicebay` | Extend `voice` template with `GATEKEEPER_IMAGE` sidecar variable |
| `mdopp/servicebay` | Structured-logging contract (platform standard) |
| `mdopp/servicebay` | Health-probe contract (platform standard) |
| `NousResearch/hermes-agent` | Voice gateway: contribute the Phase-0 gatekeeper pass-through path as `hermes gateway voice` |
| Hermes Skills Hub / agentskills.io | `smart-home/home-assistant` skill (from the current oscar-light) |
| New separate repo | `mcp-audit-proxy` — the cloud-LLM auditing MCP, generic; OSCAR provides only the policy + schema |

Tracking issue in `mdopp/oscar` links to all of the above; the OSCAR README's "Open follow-ups" section pulls live status from there.

## Key decisions

| Topic | Decision |
|---|---|
| **Agent runtime** | Hermes Agent (`docker.io/nousresearch/hermes-agent`), unforked, deployed via ServiceBay's `hermes` template |
| **Platform** | ServiceBay v3.16+ on Podman Quadlet, Fedora CoreOS host |
| **AI infrastructure** | ServiceBay `ai-stack` (Postgres + Ollama + Qdrant + Hermes); not OSCAR's job to deploy |
| **Identity** | LLDAP `uid` + groups from ServiceBay's `auth` pod; SSO via Authelia OIDC for any OSCAR web UI |
| **Voice pipeline** | ServiceBay's `voice` template (Whisper + Piper + openWakeWord) extended with OSCAR's `gatekeeper` image as a sidecar |
| **Voice identity** | Speaker embedding in the gatekeeper → uid lookup in OSCAR's Postgres `voice_embeddings` table → never in LLDAP |
| **Messaging gateways** | Hermes-native (Signal, Telegram, Discord, Slack, WhatsApp, Email). Paired via `hermes gateway setup`. No OSCAR-side gateway code. |
| **Timers / alarms / reminders** | Hermes-native cron scheduler. No OSCAR table. |
| **Memory** | Two layers: Hermes Honcho (conversation, in its volume) + OSCAR schema (audit + Phase-3a domain memory) on the shared `ai-stack` Postgres+Qdrant. Both `uid`-namespaced. |
| **Cloud LLM** | Off by default; opt-in per harness. Every call writes to `cloud_audit`. Family-visible via `oscar-audit-query`. |
| **Audit-proxy mechanic** | Separate repo / package (`mcp-audit-proxy`), not OSCAR-eigen; OSCAR contributes only the policy + the schema |
| **Hardware** | GPU server (RTX 4070 or comparable, ≥12 GB VRAM). No CPU-only path for live voice. |
| **Hermes core modding** | None. Capabilities we miss get PR'd upstream or added as MCP servers. |
| **OSCAR template count** | One (`oscar-household`). Anything more is a smell that we're rebuilding ServiceBay or Hermes. |
| **gatekeeper home** | OSCAR-published image; long-term target is to land the Phase-0 pass-through path in Hermes |
| **Phase 0 trigger** | Working Signal chat with HA control. Voice path is Phase 1, not Phase 0. |

## Open points

1. **Gatekeeper migration path.** Once the voice gateway lands in Hermes upstream, the gatekeeper image shrinks to "OSCAR-specific extensions" only (speaker ID, multi-room, voice-tone). Timeline depends on Nous review cadence.
2. **harness composition home.** Phase-2 question: does the system + uid + guest composition layer live in OSCAR (a small service the gatekeeper consults before posting to Hermes) or as a contributed feature in Hermes itself?
3. **Material-store encryption.** Phase 3a. LUKS container vs. filesystem-layer (gocryptfs). Key management (TPM, boot-time passphrase, Authelia-protected unlock UI?).
4. **MCP wrappers for ServiceBay stack apps.** Phase 3b. Do `immich-search`, `radicale-cal`, `audiobookshelf-list` live in OSCAR or as standalone MCP servers in their own repos?

## Sources

- Hermes Agent — <https://github.com/NousResearch/hermes-agent>
- Hermes Agent docs — <https://hermes-agent.nousresearch.com/docs/>
- ServiceBay — <https://github.com/mdopp/servicebay>
- agentskills.io — <https://agentskills.io/>
- Wyoming Protocol — <https://github.com/rhasspy/wyoming>
- HA MCP server — <https://www.home-assistant.io/integrations/mcp_server/>
- Harness engineering (Böckeler/Fowler) — <https://martinfowler.com/articles/harness-engineering.html>
- LLDAP — <https://github.com/lldap/lldap>
- Authelia — <https://www.authelia.com/>
- Honcho — <https://github.com/plastic-labs/honcho>
- Model Context Protocol — <https://modelcontextprotocol.io/>
