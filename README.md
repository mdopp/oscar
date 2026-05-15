# O.S.C.A.R.

> A privacy-first, fully-local home assistant for a family household. The brain doesn't leave the house.

## What OSCAR is for

Five intents — short version:

1. **Sovereignty.** Use modern AI without exposing the family. Everything runs on a household server; cloud LLMs only on explicit, audited opt-in.
2. **Long memory.** Books, records, documents, photos, appointments, decisions — woven together so OSCAR remembers what the household remembers.
3. **One conversation.** Voice at home, chat (Signal/Telegram) on the road — same agent, same memory.
4. **Per-resident privacy.** Father, mother, child each have their own world; guests get a smaller, locked-down one. Voice is identity (Phase 2).
5. **Things actually happen.** Lights, heating, scenes, timers, reminders — OSCAR drives Home Assistant via its MCP server.

OSCAR is **not** a from-scratch agent and not a platform. It's a thin household-identity-and-memory layer on top of two upstream projects:

- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** (Nous Research) is the agent runtime — conversation, skills, gateways, cron, memory, self-improvement, MCP client.
- **[ServiceBay](https://github.com/mdopp/servicebay)** is the platform — LLDAP/Authelia identity, Home Assistant, Immich, Radicale, media, file-share, nginx, AdGuard, Vaultwarden, Podman-Quadlet runtime on Fedora CoreOS, MCP control surface.

Anything in OSCAR that *isn't* specifically about *this household* either gets contributed back to one of those projects or replaced by an upstream equivalent. Full rationale: [`oscar-architecture.md`](oscar-architecture.md).

## How it's put together today

```
              SIGNAL / TELEGRAM / DISCORD / …          HA Voice PE  (Phase 1)
                          │                                  │ Wyoming
                          │ Hermes-native gateway            │
                          ▼                                  ▼
                ┌─────────────────────┐            ┌────────────────────┐
                │   hermes            │            │   voice            │
                │   (ServiceBay)      │◄──HTTP─────│   (ServiceBay)     │
                │                     │            │   whisper + piper  │
                │  • skill registry   │            │   + openwakeword + │
                │  • cron / reminders │            │   gatekeeper       │
                │  • Honcho memory    │            │   (OSCAR sidecar)  │
                │  • MCP client       │            └────────────────────┘
                │  • self-improvement │
                └────────┬────────────┘
                         │ MCP
       ┌─────────────────┼─────────────────────────────────────┐
       ▼                 ▼                                     ▼
 ┌──────────┐    ┌────────────────┐                  ┌──────────────────┐
 │  HA-MCP  │    │ ServiceBay-MCP │                  │  oscar-household │
 │ (devices,│    │ (services,     │                  │     (OSCAR)      │
 │  scenes) │    │  health, logs) │                  │                  │
 └──────────┘    └────────────────┘                  │  • SQLite +      │
                                                     │    Alembic       │
                          ┌────── points provider to ┤    (oscar.db)    │
                          ▼                          │  • skill mount   │
              ┌──────────────────────┐               │  • MCP wiring    │
              │  ollama              │               │  • audit hook    │
              │  (ServiceBay,        │               └──────────────────┘
              │   ai-stack)          │
              │  local Gemma         │
              └──────────────────────┘
```

One OSCAR template (`oscar-household`), one OSCAR-published image (`gatekeeper`), three OSCAR skills (`oscar-status`, `oscar-audit-query`, `oscar-debug-set`), a small SQLite database for our tables. Everything else is upstream.

## What works today

| Capability | How it's done | Phase |
|---|---|---|
| Conversation via Signal/Telegram/Discord/Slack/WhatsApp/Email | Hermes' built-in gateway, paired interactively | 0 |
| Local LLM (Gemma family) | Ollama in ServiceBay's `ai-stack`, Hermes points its provider at the Ollama port | 0 |
| Cloud LLM (Anthropic, Google, OpenRouter, …) | Hermes' built-in providers, every call audited via `cloud_audit` | 0 |
| Light/heating/scenes via Home Assistant | Hermes consumes HA-MCP; the `smart-home/home-assistant` skill (Hermes Skills Hub, contributed from OSCAR) drives it | 0 |
| Timers / alarms / reminders / recurring tasks | Hermes' native cron scheduler | 0 (Hermes-native) |
| Health check ("Is OSCAR alive?") | `oscar-status` skill runs structured probes | 0 |
| Cloud-LLM audit ("Was kostete der gestrige Call?") | `oscar-audit-query` skill reads `cloud_audit` | 0 |
| Debug-mode toggle ("Verboser Log für eine Stunde") | `oscar-debug-set` skill flips `system_settings.debug_mode` | 0 |
| Voice in the house (Wyoming + HA Voice PE) | ServiceBay's extended `voice` template + OSCAR's `gatekeeper` sidecar | 1 |

## Phase plan

| Phase | Scope | Status |
|---|---|---|
| **0** | Chat on Hermes + lights via HA-MCP. Family talks to the assistant in Signal; the assistant turns on the lights. | code design complete; deploy pending the new ServiceBay `ai-stack` templates ([tracking](https://github.com/mdopp/oscar/issues)) |
| **1** | Voice path. HA Voice PE → ServiceBay's extended `voice` template → Hermes. Single uid. | designed |
| 2 | Speaker ID (SpeechBrain) → LLDAP-uid → per-resident memory namespace and tool scope. | designed |
| 3a | Ingestion pipeline. Books → records → audiobooks → documents → experience notes. | designed |
| 3b | Bulk import + MCP wrappers for Immich/Radicale/Audiobookshelf. | sketched |
| 4 | Multi-room voice, voice-tone analysis, custom "Oscar" wakeword, proactive memos. | sketched |

## Repo layout

```
oscar-architecture.md         # the architectural constitution
templates/
└── oscar-household/          # the one OSCAR ServiceBay template
gatekeeper/                   # Python source for the gatekeeper image
                              #   (published as ghcr.io/mdopp/oscar-gatekeeper,
                              #    consumed by ServiceBay's extended voice template)
schema/                       # Alembic migrations for the OSCAR tables
skills/                       # household-specific Hermes skills:
                              #   oscar-status, oscar-audit-query, oscar-debug-set
stacks/oscar/                 # ServiceBay stack walkthrough
docs/                         # rationale documents
```

OSCAR is intentionally small. Anything bigger has either moved upstream or hasn't been built yet — see [`oscar-architecture.md`](oscar-architecture.md) for the boundary.

## Install

OSCAR ships as a **ServiceBay external registry** — no install script. The walkthrough is in two steps:

1. **Prereqs**: ServiceBay v3.16+ with the full-stack deployed; [mdopp/servicebay#348](https://github.com/mdopp/servicebay/issues/348) merged (needed only once you add voice); [mdopp/servicebay#443](https://github.com/mdopp/servicebay/issues/443) merged (so the OSCAR registry can be cloned).
2. **Two stacks**: walk through ServiceBay's `ai-stack` first (Ollama + Hermes), then OSCAR's stack (just `oscar-household` — it ships its own SQLite). Optional: ServiceBay's extended `voice` template with `GATEKEEPER_IMAGE=ghcr.io/mdopp/oscar-gatekeeper` to add voice. (Phase 3a may add Postgres + Qdrant to `ai-stack` if the domain-collection scale calls for it — for Phase 0–2 the SQLite in `oscar-household` is enough.)

Full walkthrough: [`stacks/oscar/README.md`](stacks/oscar/README.md).

## Debugging with Claude Code (MCP)

The repo ships a [`.mcp.json`](.mcp.json) wiring three MCP servers into Claude Code so debug sessions can query OSCAR's state directly:

| Server | Reads | When useful |
|---|---|---|
| `oscar-sqlite` | `cloud_audit`, `system_settings` (read-only over `oscar.db`) | "Why was last night's cloud call so expensive?" |
| `oscar-servicebay` | container logs, health, services, diagnostics | "Why did the voice pod crash-loop after the last deploy?" |
| `oscar-ha` | Home Assistant entities, areas, services | "Did the office light actually turn on after that voice command?" |

Setup: copy [`.env.example`](.env.example) to `~/.config/oscar.env`, fill in real values, source it before opening the repo.

## Language

Conversation with the maintainer is German. **All versioned artefacts — docs, READMEs, code identifiers, comments, issue titles, commit messages — are English.**

## Hardware

- Single host running ServiceBay on Fedora CoreOS.
- For real-time voice: an NVIDIA GPU (≥12 GB VRAM, e.g. RTX 4070) so Whisper-large + Gemma 12B Q4 + Piper stream under 500 ms.
- For testing: CPU-only works for chat (Phase 0); not for live voice.
- HA Voice PE devices on the same LAN once you're at Phase 1.

## Contributing

Most of the open work is **not in this repo** — by design. The architecture pushes capabilities into ServiceBay and Hermes where they belong. Active upstream candidates are tracked from OSCAR's [tracking issue](https://github.com/mdopp/oscar/issues), with cross-links to:

- `mdopp/servicebay` — new `ollama` and `hermes` templates for Phase 0, `ai-stack` walkthrough, `voice` template extension, structured-logging and health-probe contracts. (`postgres` and `qdrant` are Phase-3a-conditional.)
- `NousResearch/hermes-agent` — voice-gateway PR (the gatekeeper's Phase-0 pass-through path)
- Hermes Skills Hub — `smart-home/home-assistant` skill

Inside OSCAR proper, the open follow-ups are the speaker-ID enrolment wizard (Phase 2), the ingestion pipeline (Phase 3a), and the material-store encryption decision. Issues with reproductions or design suggestions are welcome at [github.com/mdopp/oscar/issues](https://github.com/mdopp/oscar/issues).

## License

[MIT](LICENSE). Same intent declared in every OSCAR-owned `pyproject.toml`.
