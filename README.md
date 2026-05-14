# O.S.C.A.R.

> A privacy-first, fully-local home assistant for a family household. The brain doesn't leave the house.

## What OSCAR is for

Five intents — short version:

1. **Sovereignty.** Use modern AI without exposing the family. Everything runs on a household server; cloud LLMs only on explicit, audited opt-in.
2. **Long memory.** Books, records, documents, photos, appointments, decisions — woven together so OSCAR remembers what the household remembers.
3. **One conversation.** Voice at home, chat (Signal/Telegram) on the road — same agent, same memory.
4. **Per-resident privacy.** Father, mother, child each have their own world; guests get a smaller, locked-down one. Voice is identity (Phase 2).
5. **Things actually happen.** Lights, heating, scenes, timers, reminders — OSCAR drives Home Assistant via its MCP server.

OSCAR is **not** a from-scratch agent. The agent is [Nous Research's Hermes Agent](https://github.com/NousResearch/hermes-agent). OSCAR is the household-specific layer on top: data plane, voice pipeline, household skills, MCP connectors. The architecture rationale: [`docs/architecture/oscar-on-hermes.md`](docs/architecture/oscar-on-hermes.md). The full spec: [`oscar-architecture.md`](oscar-architecture.md).

## How it's put together today

```
              SIGNAL / TELEGRAM / DISCORD / …          HA Voice PE  (Phase 1)
                          │                                  │ Wyoming
                          │ messaging gateway                │
                          ▼                                  ▼
                ┌─────────────────────┐            ┌────────────────────┐
                │   oscar-hermes      │            │   oscar-voice      │
                │ (Hermes Agent       │◄──HTTP─────│  Whisper + Piper   │
                │  in a container)    │            │  + gatekeeper      │
                │                     │            │  (POST /push for   │
                │  • skill registry   │            │   reverse delivery)│
                │  • cron / reminders │            └────────────────────┘
                │  • Honcho memory    │
                │  • MCP client       │
                │  • self-improvement │
                └────────┬────────────┘
                         │ MCP
       ┌─────────────────┼───────────────────────────┐
       ▼                 ▼                           ▼
 ┌──────────┐    ┌───────────────┐         ┌──────────────────┐
 │  HA-MCP  │    │ ServiceBay-MCP│         │ oscar-connectors │
 │ (Home    │    │ (platform ops:│         │  • weather       │
 │  Assist. │    │  services,    │         │  • cloud-llm     │
 │  devices)│    │  health, logs)│         │    (with audit)  │
 └──────────┘    └───────────────┘         └──────────────────┘

                                                    │ persists to
                                                    ▼
                                          ┌────────────────────┐
                                          │    oscar-brain     │
                                          │ Postgres + Qdrant  │
                                          │ + Ollama (local    │
                                          │ LLM for Hermes)    │
                                          │ + db-migrate +     │
                                          │ pg-backup          │
                                          └────────────────────┘
```

Four ServiceBay templates. Hermes does the agent work; OSCAR contributes voice + data plane + household skills + MCP connectors.

## What works today

| Capability | How it's done | Phase |
|---|---|---|
| Conversation via Signal/Telegram/Discord/Slack/WhatsApp/Email | Hermes' built-in gateway, paired interactively | 1 |
| Local LLM (Gemma family) | Ollama in `oscar-brain`, Hermes points its model provider at the pod's port | 0 |
| Cloud LLM (Anthropic, Google, OpenRouter, Nous Portal) | Either direct in Hermes, or via the auditable `oscar-connector-cloud-llm` MCP | 1 |
| Light/heating/scenes via Home Assistant | The `oscar-light` skill calls HA-MCP; tool names discovered live so it survives HA upgrades | 0 |
| Timers / alarms / reminders / recurring tasks | Hermes' native cron scheduler | 1 (Hermes-native) |
| Health check ("Is OSCAR alive?") | The `oscar-status` skill calls `oscar_health doctor` over the in-pod probes | 1 |
| Cloud-LLM audit ("Was kostete der gestrige Call?") | The `oscar-audit-query` skill reads the `cloud_audit` Postgres table | 1 |
| Debug-mode toggle ("Verboser Log für eine Stunde") | The `oscar-debug-set` skill flips `system_settings.debug_mode` | 1 |
| Voice pipeline (Wyoming + gatekeeper) | `oscar-voice` pod ready in code; full hardware test pending an HA Voice PE device | 0 |

## Phase plan

| Phase | Scope | Status |
|---|---|---|
| **0** | Voice pipeline + data plane + first HA skill | code complete; deploy + test pending ([#65](https://github.com/mdopp/oscar/issues/65)) |
| **1** | Messaging gateway (Hermes-native) + first connectors (cloud-llm, weather) | Hermes path complete; OSCAR connectors deploy pending |
| 2 | Speaker ID (SpeechBrain) + per-resident harness composition | designed |
| 3a | Ingestion pipeline (books → records → audiobooks → documents → experiences) | designed |
| 3b | Bulk import + Immich/Radicale/Audiobookshelf MCP wrappers | sketched |
| 4 | Multi-room voice, voice-tone analysis, custom "Oscar" wakeword | sketched |

## Repo layout

```
templates/         # ServiceBay Pod-YAML templates — deployed via the wizard
├── oscar-brain/       # Postgres + Qdrant + Ollama + db-migrate + pg-backup
├── oscar-hermes/      # wraps docker.io/nousresearch/hermes-agent
├── oscar-voice/       # Wyoming services + gatekeeper
└── oscar-connectors/  # weather + cloud-llm MCP servers

stacks/oscar/      # the wizard-walkthrough that points to all four templates
gatekeeper/        # Python source for the gatekeeper container
connectors/        # source per MCP connector (+ _skeleton/ copy template)
shared/            # cross-component Python libs:
                   #   oscar_logging — structured JSON logging
                   #   oscar_health  — dependency probes (oscar-status backing)
                   #   oscar_audit   — cloud_audit query API
                   #   oscar_db      — alembic migrations
harnesses/         # YAML per LLDAP uid + system.yaml + guest.yaml (Phase 2)
ingestion/         # Python source for the ingestion pipeline (Phase 3a)
skills/            # household-specific skills, read-mounted into Hermes
docs/              # specs adjacent to the architecture doc
```

## Install

OSCAR ships as a **ServiceBay external registry** — no install script.

1. Prereqs: ServiceBay v3.16+ with the full-stack deployed; [mdopp/servicebay#348](https://github.com/mdopp/servicebay/issues/348) merged; [mdopp/servicebay#443](https://github.com/mdopp/servicebay/issues/443) merged (otherwise the registry sync can't clone the OSCAR git URL).
2. ServiceBay → Settings → Registries → add `https://github.com/mdopp/oscar.git`.
3. From the wizard, walk through `oscar-brain` → `oscar-hermes` → `oscar-voice` (optional) → `oscar-connectors` (optional). Full walkthrough: [`stacks/oscar/README.md`](stacks/oscar/README.md).

After deploy, do the one-time `hermes setup` inside the `oscar-hermes` pod via `podman exec`.

## Debugging with Claude Code (MCP)

The repo ships a [`.mcp.json`](.mcp.json) wiring three MCP servers into Claude Code so debug sessions can query OSCAR's state directly:

| Server | Reads | When useful |
|---|---|---|
| `oscar-postgres` | `cloud_audit`, `system_settings` (read-only role recommended) | "Why was last night's cloud call so expensive?" |
| `oscar-servicebay` | container logs, health, services, diagnostics | "Why did oscar-voice crash-loop after the last deploy?" |
| `oscar-ha` | Home Assistant entities, areas, services | "Did the office light actually turn on after that voice command?" |

Setup: copy [`.env.example`](.env.example) to `~/.config/oscar.env`, fill in real values, source it before opening the repo.

## Language

Conversation with the maintainer is German. **All versioned artefacts — docs, READMEs, code identifiers, comments, issue titles, commit messages — are English.**

## Hardware

- Single host running ServiceBay on Fedora CoreOS.
- For real-time voice: an NVIDIA GPU (≥12 GB VRAM, e.g. RTX 4070) so Whisper-large + Gemma 12B Q4 + Piper stream under 500 ms.
- For testing: CPU-only works; latency 3–10 s.
- HA Voice PE devices on the same LAN (for the voice path; not needed for chat-only setups).

## Contributing

Single-maintainer for now. The open follow-ups across the templates are the cleanest places to chip in:

- HA Voice PE pairing path (firmware patch vs. HA-as-bridge) — see [`templates/oscar-voice/README.md`](templates/oscar-voice/README.md).
- GPU-passthrough validation under Podman Quadlet — open question whether ServiceBay translates `nvidia.com/gpu: "1"` cleanly.
- A `smart-home/home-assistant` skill in the [agentskills.io](https://agentskills.io) format — upstream-contribution candidate to Hermes' Skills Hub.

Issues with reproductions or design suggestions are welcome at [github.com/mdopp/oscar/issues](https://github.com/mdopp/oscar/issues).

## License

[MIT](LICENSE). Same intent declared in every OSCAR-owned `pyproject.toml`.
