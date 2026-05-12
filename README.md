# O.S.C.A.R.

> Privacy-first, fully-local home assistant for a family household. All AI runs inside the house; cloud LLMs are opt-in per request through audited connectors.

OSCAR is a layer on top of [ServiceBay](https://github.com/mdopp/servicebay) v3.16+. ServiceBay provides the platform (LLDAP/Authelia identity, Home Assistant as a device hub, Immich, Radicale, file-share, NPM, AdGuard, …); OSCAR adds:

- a voice pipeline that **OSCAR owns end to end** — HA Voice PE devices speak Wyoming directly to `oscar-voice`, not to HA;
- a cognition core ([HERMES](https://github.com/nousresearch/hermes-agent) + Ollama running Gemma 4 on GPU + Postgres + Qdrant);
- per-resident voice identity (Phase 2, SpeechBrain) and per-resident **harnesses** (Böckeler/Fowler sense) that compose system + personal + guest;
- an ingestion pipeline that turns photos/scans/voice memos from Signal or a Syncthing inbox into structured long-term memory.

The architecture document is the source of truth: [`oscar-architecture.md`](oscar-architecture.md). Working notes for Claude Code live in [`CLAUDE.md`](CLAUDE.md).

## Status

Active build, Phase 0 / 1. The specs in [`docs/`](docs/) are stable; the templates and container code are landing through the open issues at [github.com/mdopp/oscar/issues](https://github.com/mdopp/oscar/issues) and the matching PRs.

| Phase | Scope | Status |
|---|---|---|
| **0** | Voice pipeline (`oscar-voice`) + cognition core (`oscar-brain`) + first HERMES skill (light) | designed, PRs open |
| **1** | Signal/Telegram gateway + first connectors (Cloud-LLM, weather, web-search) | designed, PRs open |
| 2 | Speaker ID + per-user harnesses | designed (specs) |
| 3a | Streaming ingestion + enrichment connectors (Open Library, MusicBrainz, Discogs) | designed |
| 3b | Bulk import + Immich/Radicale/Audiobookshelf MCP wrappers | sketched |
| 4 | Multi-room voice, voice-tone analysis, "good morning" routine, custom wakeword "Oscar" | sketched |

## Architecture at a glance

```
                                    INPUTS (LAN, private)
        ┌──────────────────┬─────────────────────┬─────────────────────┐
   ┌────▼─────┐      ┌─────▼──────┐        ┌─────▼──────┐
   │HA Voice  │      │  Phone     │        │  Phone     │
   │PE (ESP32 │      │  Signal /  │        │  Syncthing │
   │ + mic)   │      │  Telegram  │        │   folder   │
   └────┬─────┘      └──────┬─────┘        └─────┬──────┘
        │ Wyoming           │ HTTPS              │ file sync
        ▼                   ▼                    ▼
  ┌─────────────┐    ┌───────────────────────────────────┐    ┌────────────┐
  │ oscar-voice │    │            oscar-brain            │    │ oscar-     │
  │ gatekeeper  │───►│           ┌────────────┐          │◄───│ ingestion  │
  │ whisper ▣   │HTTP│           │   HERMES   │          │MCP │  (3a)      │
  │ piper       │    │           │  skills    │          │    │ classifier │
  │ openWake-   │    │           │  cron      │          │    │ + watcher  │
  │  Word       │    │           │  gateways  │          │    └────────────┘
  └─────────────┘    │           │  MCP clients          │
        ▲            │           └─────┬──────┘          │
        │ TTS audio  │  ollama ▣  postgres  qdrant       │
        │ back to    │  signal-cli daemon   pg-backup    │
        │ originating└─────────────────│─────────────────┘
        │ Voice PE                     │
        │                              │ MCP fanout
        │            ┌─────────────────┼─────────────────┐
        │            ▼                 ▼                 ▼
        │     ┌────────────┐   ┌──────────────┐  ┌──────────────┐
        │     │  HA-MCP    │   │ ServiceBay-  │  │   oscar-     │
        │     │ devices,   │   │   MCP        │  │  connectors  │
        │     │ scenes,    │   │ logs, diag,  │  │ weather │ …  │──► WORLD
        │     │ media      │   │ start/stop   │  │ cloud-llm    │  (audited
        │     └─────┬──────┘   └──────────────┘  │ web-search   │   egress)
        │           ▼                            └──────────────┘
        │     ┌────────────┐
        └─────│ Home       │
   media-     │ Assistant  │     ▣ = GPU passthrough
   player via │ Z-Wave,    │     ━━━ trace_id propagates on every
   HA-MCP     │ Matter,    │         inter-pod hop (logging spec)
              │ Zigbee     │
              └────────────┘
```

Four Quadlet pods, three input channels (voice, chat, file sync), three output channels (HA for devices, ServiceBay for platform ops, connectors for the world).

## Repo structure

```
templates/         # ServiceBay Pod-YAML templates (consumed via external registry)
├── oscar-voice/       # Wyoming services + gatekeeper
├── oscar-brain/       # HERMES + Ollama (GPU) + Qdrant + Postgres
├── oscar-connectors/  # 1 container per connector
└── oscar-ingestion/   # Material pipeline (Phase 3a)

stacks/oscar/      # Bundle template that deploys all four pods at once
gatekeeper/        # Python container code for the gatekeeper
ingestion/         # Python container code for the ingestion pipeline
connectors/        # One subdir per connector, _skeleton/ as copy template
harnesses/         # YAML per LLDAP uid + system.yaml + guest.yaml
skills/            # HERMES skills (Markdown with YAML frontmatter)
shared/            # Cross-container Python libs (oscar_logging)
docs/              # Specs adjacent to the architecture doc
```

## Specs

- [`oscar-architecture.md`](oscar-architecture.md) — top-level architecture, phase plan, key decisions
- [`docs/logging.md`](docs/logging.md) — operational stdout-JSON + domain-audit Postgres tracks, `trace_id` correlation, retention policies
- [`docs/connector-skeleton.md`](docs/connector-skeleton.md) — FastMCP + Pydantic pattern, shared-bearer auth, `variables.json` example
- [`docs/gateway-identities.md`](docs/gateway-identities.md) — phone-number / chat-id → LLDAP-uid mapping (Phase 1)
- [`docs/timer-and-alarm.md`](docs/timer-and-alarm.md) — twin `timer` + `alarm` skills sharing `time_jobs` (Phase 0/1)

## Deploy

OSCAR ships as a ServiceBay external registry. There is no standalone deploy path.

1. Install [ServiceBay v3.16+](https://github.com/mdopp/servicebay) on Fedora CoreOS, full stack deployed.
2. Confirm [mdopp/servicebay#348](https://github.com/mdopp/servicebay/issues/348) is merged — the HA template needs `VOICE_BUILTIN=disabled` so it doesn't collide with `oscar-voice` on Wyoming ports.
3. ServiceBay → Settings → Registries → add `https://github.com/mdopp/oscar.git`.
4. From the wizard, deploy `oscar-brain` first (it provisions the Postgres schema everything else writes into). Then `oscar-voice`, then `oscar-connectors`, then add an HA Voice PE.

Each template's `README.md` walks through variables, smoke tests, and per-pod open follow-ups.

## Language

Conversation with the maintainer is German. **All versioned artefacts — docs, READMEs, code identifiers, comments, issue titles, commit messages — are English.**

## Hardware expectations

- Single GPU server (RTX 4070 / ≥12 GB VRAM target). Voice latency and Gemma 4-12B+ are unreachable on CPU only. No Mac mini path planned.
- Fedora CoreOS host with `nvidia-container-toolkit` + CDI configured so Pod-YAML `resources.limits.nvidia.com/gpu: "1"` reaches Whisper and Ollama.
- HA Voice PE devices on the same LAN.

## Contributing

Single-maintainer for now. The cleanest places to chip in are the open follow-ups called out across the template READMEs:

- GPU-passthrough validation under Podman Quadlet (does ServiceBay translate `nvidia.com/gpu` cleanly?)
- HA Voice PE pairing path (firmware patch vs. HA-as-bridge)
- Schema-migration tool (alembic / sqitch) — Phase-1+ topic
- Multi-arch container images (the GHCR workflow ships `linux/amd64` only)

Issues with concrete reproductions or design suggestions are welcome at [github.com/mdopp/oscar/issues](https://github.com/mdopp/oscar/issues).

## License

MIT (declared in the `pyproject.toml` of every OSCAR-owned Python project). A top-level `LICENSE` file is on the open-follow-up list.
