# OSCAR skills

Household-specific skills consumed by [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Hermes provides the agent loop, skill registry, cron, messaging gateways, and the self-improvement loop natively. OSCAR contributes only the **household-specific** procedures that aren't in Hermes' bundled Skills Hub — anything tied to *our* data plane (oscar-brain Postgres, oscar-connectors MCP servers) or *our* hardware (HA-MCP for lights, voice-PE for audio).

The install path (`scripts/install.sh`) symlinks this directory into `~/.hermes/skills/oscar` so Hermes loads everything here on next restart.

## Currently registered skills

| Directory | `name:` | Phase | One-liner |
|---|---|---|---|
| `light/` | `oscar-light` | 0 | Lights on/off/dim via HA-MCP. Tool-name-agnostic — Hermes picks the right HA tool from `tools/list` at boot. |
| `status/` | `oscar-status` | 1 | `python -m oscar_health doctor` — pings every OSCAR dependency, returns per-component status. Read-only. |
| `audit-query/` | `oscar-audit-query` | 1 | Read-only query over `cloud_audit` (and future Phase-3 household-domain tables). |
| `debug-set/` | `oscar-debug-set` | 1 | Admin: toggle `system_settings.debug_mode` in oscar-brain's Postgres (verbose logging on demand). |

## Adding a new skill

1. `mkdir skills/<short>/` and write `SKILL.md` with the standard frontmatter (`name`, `description`, `version`, `author`, `license`).
2. If the skill needs a CLI, put the code under `shared/oscar_<short>/` and have Hermes shell out via `python -m oscar_<short> …`. For Hermes to pick up the Python, install the OSCAR shared libs into Hermes' venv (or rely on the symlinked workspace).
3. Add a row to the table above.
4. Restart Hermes to pick up the new skill.

## What's *not* a skill

Removed during the May 2026 architecture reset because Hermes does it natively:

| Removed | Hermes equivalent |
|---|---|
| `oscar-help` | `/skills` |
| `oscar-timer` / `oscar-alarm` | Hermes cron |
| `oscar-skill-author` / `-reviewer` / `-revert` | Hermes' built-in skill management + self-improvement loop |
| `oscar-identity-link` | Hermes' messaging-gateway pairing |

Context: [`../docs/architecture/oscar-on-hermes.md`](../docs/architecture/oscar-on-hermes.md).
