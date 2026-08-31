# Solaris skills

Household-specific skill packs the **Solaris Engine** (`solaris-chat`) folds into its prompts.

The engine provides the agent loop, skill loading, and the night crons natively. These packs contribute only the **household-specific** procedures tied to *our* SQLite schema (`solaris.db`) or *our* policy choices.

The `solaris` ServiceBay template bind-mounts this directory into the `solaris-chat` container at `/data/skills` (env `SKILLS_DIR`), alongside the path to `solaris.db`. The engine reads the packs here live.

## Currently registered skills

| Directory | `name:` | Phase | One-liner |
|---|---|---|---|
| `status/` | `solaris-status` | 0 | Pings every Solaris dependency (`solaris.db`, Ollama, Home Assistant, ServiceBay-MCP; voice probes once Phase 1 voice is deployed) and returns per-component status. Read-only. |
| `problem-summarizer/` | `solaris-problem-summarizer` | 0 | Distils resolved problem→indicators→solution sequences from system logs + past diagnostic chats into a structured Markdown KB at `/opt/data/notes/knowledge-base/troubleshooting.md`. On-request + weekly cron. |

Both operate through ServiceBay-MCP (`get_health_checks`/`diagnose`) and the engine's own tools — no external `solaris_*` libraries or separate companion scripts.

## What's *not* a skill in Solaris

| Capability | Lives in |
|---|---|
| Lights / heating / scenes | The engine's native `ha_*` tools (Home Assistant) |
| Help (`/skills`, `/help`) | Engine native |
| Timers / alarms / reminders / recurring tasks | The engine's scheduler + night crons |
| Skill management (list / read / edit) | The engine's skills API + chat skills panel |

Context: [`../solaris-architecture.md`](../solaris-architecture.md).
