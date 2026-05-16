# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

O.S.C.A.R. is a privacy-first, fully local home assistant for a family household. **It is intentionally small.** OSCAR is a thin household-identity-and-memory layer on top of two upstream projects we treat as load-bearing:

- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** (`docker.io/nousresearch/hermes-agent`) is the agent runtime — conversation, skills, gateways, cron, Honcho memory, MCP client, self-improvement. OSCAR does **not** fork it.
- **[ServiceBay](https://github.com/mdopp/servicebay)** (v3.16+) is the platform — LLDAP/Authelia, HA, Immich, Radicale, media, file-share, nginx, AdGuard, Vaultwarden, MCP control surface, Podman-Quadlet runtime on Fedora CoreOS. We control this project too.

Capabilities that are generic — voice gateway, smart-home skill, structured logging, health probes, data-plane deployment — get **contributed upstream** to one of those two projects. OSCAR keeps only the household-specific layer.

The architectural constitution is [`oscar-architecture.md`](oscar-architecture.md). Read it first; everything in this file is a working-rule digest of that.

## Hard constraints

- **No fork of Hermes.** If we need behaviour Hermes doesn't have, it either becomes a Hermes PR or an MCP server Hermes can mount. Never patch Hermes' Python core in place.
- **ServiceBay is "upstream" too, but in our hands.** Generic platform features (Postgres deploy, Ollama deploy, structured logging, health probes) belong in `mdopp/servicebay`, not in OSCAR's `shared/` or `templates/`. Open an issue there instead of building a shim here.
- **One OSCAR template.** `templates/oscar-household/` is the only ServiceBay template OSCAR ships. Anything more is a smell — it usually means we're rebuilding ServiceBay infrastructure that should be a generic template.
- **Runtime is ServiceBay v3.16+ on Podman Quadlet.** Templates are **Kubernetes Pod manifests** (`template.yml`), Mustache-templated, with `variables.json` (typed: `text|secret|select|device|subdomain`, optional `oidcClient` block). Never write `docker-compose.yml`, Dockerfiles for the templating layer, or raw `.container` units.
- **No data leaves the house by default.** Cloud LLM calls are opt-in per harness, every call writes to `cloud_audit`, every audit row is family-readable via the `oscar-audit-query` skill.
- **Identity = LLDAP, SSO = Authelia.** Both ship in ServiceBay's `auth` pod. OSCAR services reference LLDAP `uid`s and groups; OSCAR services with a web UI register OIDC clients via the `oidcClient` block in their `variables.json`.
- **Voice ↔ uid is OSCAR's job, not Hermes'.** The gatekeeper does speaker embedding + LLDAP-uid lookup + passes uid as a request parameter to Hermes. Voice embeddings live in OSCAR's SQLite, **never** in LLDAP — biometric PII.
- **Harness = configuration, not code.** Phase 2 onward. When OSCAR behaves wrongly, the fix usually goes into a harness YAML (guides or sensors), not into application code.
- **Documentation and code are English.** Maintainer conversation is German; every versioned artefact (docs, READMEs, identifiers, comments, issue bodies, commit messages) is English.

## Repo structure

```
oscar-architecture.md         # architectural constitution
templates/
└── oscar-household/          # the one OSCAR ServiceBay template
                              #   - runs Alembic against the local SQLite (oscar.db)
                              #   - bind-mounts skills/ into Hermes
                              #   - wires HA-MCP + ServiceBay-MCP via post-deploy
gatekeeper/                   # Python source for the gatekeeper image
                              #   (published as ghcr.io/mdopp/oscar-gatekeeper,
                              #    runs as a container inside oscar-household;
                              #    reaches ServiceBay's voice template via host loopback)
schema/                       # Alembic migrations for cloud_audit,
                              # system_settings, voice_embeddings
                              # (+ Phase 3a domain collections)
skills/                       # household-specific Hermes skills:
                              #   oscar-status, oscar-audit-query, oscar-debug-set
stacks/oscar/                 # ServiceBay stack walkthrough
```

What is **not** in this repo:

- Data-plane templates (Postgres, Qdrant, Ollama) — belong to ServiceBay's future `ai-stack`
- Hermes container template — belongs to ServiceBay as a generic `hermes` template
- Voice-pipeline template — ServiceBay's unchanged `voice` template provides Whisper/Piper/openWakeWord; the gatekeeper container lives in OSCAR's `oscar-household` (both pods hostNetwork, host-loopback bridge)
- Connector code (weather, etc.) — belongs to third-party MCP servers
- Structured-logging / health-probe libraries — belong to ServiceBay platform contracts
- `oscar-light` skill — upstreamed as `smart-home/home-assistant` to Hermes Skills Hub

If you find yourself adding any of the above to OSCAR, stop and open an issue in the appropriate upstream project instead.

## Platform consumed from ServiceBay (don't rebuild)

| Need | Source |
|---|---|
| Smart-home hub | `home-assistant` (via HA's native MCP server) |
| Identity, SSO, OIDC | `auth` (LLDAP + Authelia) |
| Photos | `immich` |
| CalDAV/CardDAV | `radicale` |
| Audiobooks, music | `media` (Audiobookshelf + Navidrome) |
| File drop / sync | `file-share` (Syncthing + Samba + FileBrowser + WebDAV) |
| Reverse proxy + LE certs | `nginx` (NPM) |
| DNS sinkhole | `adguard` |
| Passwords | `vaultwarden` |
| Platform MCP control surface | ServiceBay `/mcp`, scopes `read\|lifecycle\|mutate\|destroy` |
| **Ollama, Hermes** | `ai-stack` for Phase 0 (planned in `mdopp/servicebay` — [#538](https://github.com/mdopp/servicebay/issues/538), [#539](https://github.com/mdopp/servicebay/issues/539), [#540](https://github.com/mdopp/servicebay/issues/540)) |
| **Postgres, Qdrant** | only if Phase 3a chooses to migrate off SQLite (`ai-stack` extension, conditional) |
| **Voice pipeline (Whisper + Piper + openWakeWord)** | ServiceBay's unchanged `voice` template (shipped via [#348](https://github.com/mdopp/servicebay/issues/348)) deployed alongside `oscar-household` |
| **Structured logging** | ServiceBay's `src/lib/logger.ts` (SQLite-backed; shape `{ts, level, tag, message, args}`). Doc-only contract tracked in [#542](https://github.com/mdopp/servicebay/issues/542). |
| **Health checks** | ServiceBay's 16-check-type health system (v3.35–v3.37). OSCAR's `oscar-status` consumes the existing MCP tools (`get_health_checks`, `diagnose`). Doc-only contract tracked in [#543](https://github.com/mdopp/servicebay/issues/543). |

## Gatekeeper

Wyoming-protocol server. **Runs as a container inside the `oscar-household` pod**, not as a sidecar in ServiceBay's `voice` template. Both pods are `hostNetwork: true` — same host netns — so the gatekeeper reaches the `voice` template's Whisper/Piper through `127.0.0.1`.

One inbound satellite connection = one half-duplex pipeline turn:

1. HA Voice PE (or any `wyoming-satellite` client) connects on `<host>:<GATEKEEPER_PORT>`, streams `AudioStart` + `AudioChunk*` + `AudioStop`.
2. The gatekeeper calls Whisper at `tcp://127.0.0.1:10300` (provided by ServiceBay's `voice` template) for STT.
3. *Phase 0/1:* `uid = DEFAULT_UID`. *Phase 2:* SpeechBrain ECAPA-TDNN extracts a 256-d voice embedding; lookup against `voice_embeddings` in OSCAR's SQLite (3–10 vectors per family — brute-force cosine in Python) resolves to an LLDAP `uid`.
4. The gatekeeper POSTs `(text, uid, endpoint, trace_id)` to Hermes at `HERMES_URL` (default `http://127.0.0.1:8642`).
5. Hermes' response → Piper at `tcp://127.0.0.1:10200` → audio back to the satellite.
6. Outbound `POST /push {endpoint: "voice-pe:<name>", text}` lets Hermes' cron and proactive deliveries address a specific Voice PE device by name.

The gatekeeper is published as an image (`ghcr.io/mdopp/oscar-gatekeeper`); `oscar-household`'s `template.yml` references it via the `GATEKEEPER_IMAGE` variable. ServiceBay's `voice` template stays unchanged ([reasoning](https://github.com/mdopp/servicebay/issues/541): the previously proposed `voice`-sidecar would have hidden two mutually-exclusive deploy shapes behind one variable and forced a schema-version bump).

Long term, the Phase-0/1 pass-through path (steps 1, 2, 4, 5) is upstream work for Hermes (`hermes gateway voice`). Phase 2+ logic (speaker ID, multi-room routing, voice-tone analysis) stays in `oscar-household`.

## Memory and identity

Two memory layers, both SQLite-shaped today, both `uid`-namespaced via the request parameter the gatekeeper passes per turn:

- **Hermes (Honcho + FTS5 SQLite)**: conversation history, skill curation. Persisted under the Hermes container's data volume.
- **OSCAR SQLite (`oscar.db`)**: audit + Phase-3a domain memory. Lives as a single file in the `oscar-household` container's volume — no external Postgres for Phase 0–2.

Three Phase 0–2 tables: `cloud_audit`, `system_settings`, `voice_embeddings`. Phase 3a adds the domain collections (`books`, `records`, `documents`, `audiobooks`, `experiences`) and re-opens the storage choice — Postgres + Qdrant only if the data scale or semantic-search needs justify the move.

Voice embeddings are **never** in LLDAP. Biometric PII goes in OSCAR's SQLite only.

## Cross-cutting

- **Debug mode** is a single global flag in `system_settings.debug_mode`. Voice toggle via `oscar-debug-set` (admin-only). TTL via `verbose_until`. Components re-query on every audit event (no caching > 5 s). No component-specific verbose flags.
- **Audit policy.** Every cloud-LLM call writes a row to `cloud_audit`. Family-readable via `oscar-audit-query` ("Was hat der Cloud-Connector heute gemacht?"). The audit *mechanic* is upstream-able as a separate `mcp-audit-proxy` package; OSCAR keeps the *policy* (every call is family-visible) and the schema.
- **Logging.** Emit one JSON line per event to stdout matching ServiceBay's logger contract: `{ts, level, tag, message, args}`. The platform's own `src/lib/logger.ts` is SQLite-backed and queryable via `/api/logs/query`. Contract doc tracked in [`mdopp/servicebay#542`](https://github.com/mdopp/servicebay/issues/542).
- **Health checks.** Outside-in — declare what should be probed (via ServiceBay-MCP `create_health_check`), don't expose `/health` endpoints. The 16 check types in ServiceBay's `src/lib/health/types.ts` cover the cases. OSCAR's `oscar-status` calls `get_health_checks` / `diagnose` rather than running its own probes. Contract doc tracked in [`mdopp/servicebay#543`](https://github.com/mdopp/servicebay/issues/543).
- **No `podman exec` operator instructions for deploy-time setup.** Hermes' first-boot config (LLM provider, API server) is driven non-interactively from the ServiceBay `hermes` template's post-deploy. MCP-server registration is driven non-interactively from `oscar-household`'s post-deploy by editing `${DATA_DIR}/hermes/config.yaml` and triggering a pod restart via `POST /api/services/hermes/action`. The one *required* interactive step is messaging-gateway pairing (Signal QR-scan etc.) — that's a one-off post-install operator action, not a deploy-time instruction, and `UX_PHILOSOPHY.md §2` carves it out explicitly.

## Phase plan (digest)

- **Phase 0 — Chat on Hermes + lights.** Prereqs: ServiceBay v3.16+ with full-stack; `mdopp/servicebay#443` merged (registry sync); the new ServiceBay `ai-stack` templates (`ollama`, `hermes`). Deploy `ai-stack` + `oscar-household` (the latter ships its own SQLite). Operator pairs Signal once via `podman exec -it hermes signal-cli link -n "HermesAgent"` + QR-scan ([Hermes Signal docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/signal)). `oscar-household`'s post-deploy merges HA-MCP and ServiceBay-MCP into `${DATA_DIR}/hermes/config.yaml` and restarts Hermes via the ServiceBay API. First household skill: `smart-home/home-assistant` (upstreamed to Hermes Skills Hub).
- **Phase 1 — Voice path.** Prereqs: `mdopp/servicebay#348` merged (HA without bundled Wyoming); ServiceBay's unchanged `voice` template deployed alongside `oscar-household`. The gatekeeper container in `oscar-household` reaches Whisper/Piper via host loopback (both pods are `hostNetwork: true`). HA Voice PE points its Wyoming endpoint at `<host>:<GATEKEEPER_PORT>`. Gatekeeper in pass-through mode (`DEFAULT_UID`).
- **Phase 2 — Speaker ID + harnesses.** SpeechBrain ECAPA-TDNN in the gatekeeper, `voice_embeddings` table, enrolment wizard, harness YAML schema, `system.yaml` + `michael.yaml` + `guest.yaml`. Harness `uid` flows from the gatekeeper into Hermes per turn.
- **Phase 3a — Streaming ingestion.** Build the ingestion pipeline + enrichment connectors (Open Library, MusicBrainz, Discogs). Roll-out per material type.
- **Phase 3b — Bulk import + MCP wrappers.** `immich-search`, `radicale-cal`, `audiobookshelf-list`. Signal/Telegram history import.
- **Phase 4 — Active extensions.** Voice-tone analysis, multi-room voice routing, custom "Oscar" wakeword, proactive memos, TuneIn / internet-radio MCP.

## Upstream work tracked from OSCAR

These are not OSCAR tickets — they live in the projects they're filed against, with OSCAR's tracking issue linking to them:

- `mdopp/servicebay`: `ollama` ([#538](https://github.com/mdopp/servicebay/issues/538)), `hermes` ([#539](https://github.com/mdopp/servicebay/issues/539)) templates and `ai-stack` walkthrough ([#540](https://github.com/mdopp/servicebay/issues/540)) for Phase 0. Doc-only tickets for the existing logging contract ([#542](https://github.com/mdopp/servicebay/issues/542)) and health-check system ([#543](https://github.com/mdopp/servicebay/issues/543)). `postgres` + `qdrant` are Phase-3a-conditional and only if we decide to migrate off SQLite. The `voice` template stays unchanged — gatekeeper moved into `oscar-household` ([#541](https://github.com/mdopp/servicebay/issues/541) closed as superseded).
- `NousResearch/hermes-agent`: voice-gateway PR (Phase-0 pass-through path of the gatekeeper)
- Hermes Skills Hub / agentskills.io: `smart-home/home-assistant` skill (from current `oscar-light`)
- New separate repo: `mcp-audit-proxy` — the generic cloud-LLM auditing MCP

## When you start a task in OSCAR

Default questions to ask, before writing code:

1. **Does this belong upstream?** If the capability is generic (not specifically about *this household*), check whether it's already filed against `mdopp/servicebay` or Hermes. If not, file it there instead of building it here.
2. **Does OSCAR already have it?** Read `oscar-architecture.md` and `skills/`/`gatekeeper/`/`schema/` before building anything new.
3. **Is the change reversible?** Adding `oscar-household` variables or schema columns is reversible. Adding a new template, a new shared lib, or a Hermes-core patch is a smell.
4. **Will it survive a Hermes upgrade?** OSCAR runs on the upstream Hermes image. Anything that assumes a specific Hermes internal layout is fragile.

If you find yourself adding a new template directory, a new `shared/` library, or a wrapper around Hermes' core, stop. Open an issue describing what you wanted to do; ask the maintainer whether the right home is OSCAR, ServiceBay, or Hermes.
