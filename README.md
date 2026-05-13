# O.S.C.A.R.

> Privacy-first, fully-local home assistant for a family household. All AI runs inside the house; cloud LLMs are opt-in per request through audited connectors.

OSCAR is the household-specific layer on top of [Hermes Agent](https://github.com/nousresearch/hermes-agent) (host-installed) and [ServiceBay](https://github.com/mdopp/servicebay) v3.16+ (platform). Hermes provides the agent loop, gateways (Signal/Telegram/etc.), cron, conversation memory, skill registry, and self-improvement loop. ServiceBay provides the platform (LLDAP/Authelia identity, Home Assistant as a device hub, Immich, Radicale, file-share, NPM, AdGuard). **OSCAR adds:**

- a voice pipeline that **OSCAR owns end to end** — HA Voice PE devices speak Wyoming directly to `oscar-voice`, not to HA;
- a data plane (`oscar-brain`: Postgres for household-domain audit + Qdrant for semantic memory + Ollama for the local LLM Hermes points at);
- per-resident voice identity (Phase 2, SpeechBrain) and per-resident **harnesses** (Böckeler/Fowler sense) that compose system + personal + guest;
- household-specific skills (light control via HA-MCP, status checks, audit query, debug-mode toggle) symlinked into Hermes' skills dir;
- an ingestion pipeline that turns photos/scans/voice memos from Signal or a Syncthing inbox into structured long-term memory (Phase 3a).

The architecture document is the source of truth: [`oscar-architecture.md`](oscar-architecture.md). Working notes for Claude Code live in [`CLAUDE.md`](CLAUDE.md).

## Status

Active build, Phase 0 / 1. The specs in [`docs/`](docs/) are stable; the templates and container code are landing through the open issues at [github.com/mdopp/oscar/issues](https://github.com/mdopp/oscar/issues) and the matching PRs.

| Phase | Scope | Status |
|---|---|---|
| **0** | Voice pipeline (`oscar-voice`) + cognition core (`oscar-brain`) + first Hermes skill (light) | designed, PRs open |
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
  │ whisper ▣   │HTTP│           │   Hermes   │          │MCP │  (3a)      │
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
templates/         # ServiceBay Pod-YAML templates (deployed via scripts/install.sh or wizard)
├── oscar-voice/       # Wyoming services + gatekeeper
├── oscar-brain/       # Data plane: Postgres + Qdrant + Ollama + db-migrate + pg-backup
├── oscar-connectors/  # 1 container per connector
└── oscar-ingestion/   # Material pipeline (Phase 3a)

stacks/oscar/      # Documentation-only walkthrough
scripts/           # install.sh + render-template.py (Hermes install + template deploy)
gatekeeper/        # Python container code for the gatekeeper (Wyoming + push endpoint)
ingestion/         # Python container code for the ingestion pipeline (Phase 3a)
connectors/        # One subdir per connector, _skeleton/ as copy template
harnesses/         # YAML per LLDAP uid + system.yaml + guest.yaml (Phase 2)
skills/            # Household-specific skills — symlinked into ~/.hermes/skills/oscar
shared/            # Cross-container Python libs (oscar_logging, oscar_audit, oscar_health, oscar_db)
docs/              # Specs adjacent to the architecture doc (incl. docs/architecture/oscar-on-hermes.md)
```

## Specs

- [`oscar-architecture.md`](oscar-architecture.md) — top-level architecture, phase plan, key decisions
- [`docs/architecture/oscar-on-hermes.md`](docs/architecture/oscar-on-hermes.md) — May 2026 reset rationale (why Hermes is host-installed, what OSCAR contributes on top)
- [`docs/logging.md`](docs/logging.md) — operational stdout-JSON + domain-audit Postgres tracks, `trace_id` correlation, retention policies
- [`docs/connector-skeleton.md`](docs/connector-skeleton.md) — FastMCP + Pydantic pattern, shared-bearer auth, `variables.json` example

## Deploy

```bash
git clone https://github.com/mdopp/oscar.git
cd oscar
export SB_URL=http://<your-host>:5888/mcp
export SB_TOKEN=<servicebay-mcp-token>
scripts/install.sh
```

The script (idempotent):
1. Installs Hermes Agent on the host via Nous Research's installer.
2. Deploys `oscar-brain` (data plane) via ServiceBay-MCP. Workaround for [mdopp/servicebay#443](https://github.com/mdopp/servicebay/issues/443) is built-in (renders the template locally instead of relying on registry sync).
3. Symlinks `skills/` into `~/.hermes/skills/oscar` so Hermes picks them up.

After that: `hermes setup` (LLM + messaging gateway) + `hermes mcp add <ha-mcp-url>` + `hermes mcp add <servicebay-mcp-url>` + `hermes gateway start`. Detailed walkthrough: [`stacks/oscar/README.md`](stacks/oscar/README.md).

Each template's `README.md` walks through its own variables + smoke tests.

## Language

Conversation with the maintainer is German. **All versioned artefacts — docs, READMEs, code identifiers, comments, issue titles, commit messages — are English.**

## Debugging with Claude Code (MCP)

The repo ships a [`.mcp.json`](.mcp.json) that wires four MCP servers into Claude Code so the maintainer's debug sessions can query OSCAR's state directly:

| Server | Reads | When useful |
|---|---|---|
| `oscar-postgres` | `cloud_audit`, `system_settings` (read-only role recommended) | "Why was last night's cloud call so expensive?" |
| `oscar-servicebay` | container logs, health, services, diagnostics | "Why did oscar-voice crash-loop after the last deploy?" |
| `oscar-ha` | Home Assistant entities, areas, services | "Did the office light actually turn on after that voice command?" |

Setup: copy [`.env.example`](.env.example) to `~/.config/oscar.env` (or wherever your shell sources from), fill in real values, ensure those env vars are in scope when you open the repo. Claude Code substitutes `${...}` in `.mcp.json` from the environment.

**Security note:** project MCP servers require explicit user approval the first time Claude Code uses them. Treat the credentials as secret — `oscar-postgres` in particular gives Claude full read access to `cloud_audit` and, in debug-mode, full prompts and responses. Best practice: a dedicated read-only Postgres role (`claude_ro` — see `.env.example` for the SQL).

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

[MIT](LICENSE). The same intent is declared in every OSCAR-owned `pyproject.toml`.
