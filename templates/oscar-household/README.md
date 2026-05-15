# oscar-household

The one OSCAR-owned ServiceBay template. It is the household-specific overlay on top of ServiceBay's `ai-stack` (Hermes + Ollama) — see [`../../oscar-architecture.md`](../../oscar-architecture.md) for the full architecture.

## What it does

1. **Schema init** — runs the migration sidecar (`ghcr.io/mdopp/oscar-household-init`, built from [`../../schema/`](../../schema/)) against `/var/lib/oscar/oscar.db` on every pod start. Creates `system_settings`, `cloud_audit`, `voice_embeddings`. Idempotent.
2. **Skill mount** — bind-mounts OSCAR's [`../../skills/`](../../skills/) into the Hermes container at `/opt/data/skills/oscar`. Hermes picks them up alongside the built-in Skills Hub.
3. **MCP wiring** — `post-deploy.py` calls `hermes mcp add` for HA-MCP and ServiceBay-MCP using the tokens collected by the wizard.
4. **Audit hook** — sets the cloud-LLM audit proxy URL in Hermes' env so every cloud call writes a `cloud_audit` row. (The audit-proxy MCP itself lives in a separate repo — see the architecture's "Upstream work" section.)

This template does **not** deploy Postgres, Qdrant, Ollama, Hermes, Whisper, Piper or any other generic infrastructure. Those come from ServiceBay's `ai-stack` (planned in `mdopp/servicebay`). Until those templates ship, this template's deploy is gated on them.

## Phase status

- **Phase 0**: schema init + skill mount + MCP wiring + audit hook
- **Phase 1**: same template; voice path adds via ServiceBay's extended `voice` template (separate template, sets `GATEKEEPER_IMAGE=ghcr.io/mdopp/oscar-gatekeeper`)
- **Phase 2**: voice_embeddings starts being populated by the gatekeeper after the enrolment wizard runs
- **Phase 3a**: additional migrations land for the domain-collection tables (`books`, `records`, `documents`, `audiobooks`, `experiences`). The storage choice is re-opened at that point — SQLite likely still fits.

## Variables

| Variable | Type | Purpose |
|---|---|---|
| `HERMES_API_URL` | text | Base URL of Hermes' HTTP API (default `http://127.0.0.1:8642`, hostNetwork) |
| `HERMES_TOKEN` | secret | Bearer token for Hermes — matches its `API_SERVER_KEY` |
| `HA_MCP_TOKEN` | secret | Long-lived access token from Home Assistant **or** Authelia OIDC client credentials, for HA's native MCP server |
| `SERVICEBAY_MCP_TOKEN` | secret | ServiceBay-MCP bearer token (`read+lifecycle` scope) |
| `LLDAP_GROUP` | text | LLDAP group whose members are considered family (default `family`) |
| `GATEKEEPER_IMAGE` | text | Image tag for the gatekeeper sidecar in ServiceBay's extended `voice` template — leave default unless you're running a fork PoC |
| `TZ` | text | IANA time zone for log timestamps (default `Europe/Berlin`) |

The template does **not** ask for a database DSN — `oscar.db` lives in the bind-mounted volume, no external Postgres for Phase 0–2.

## Volumes

| Mount | Purpose |
|---|---|
| `/var/lib/oscar` | Owns `oscar.db` (the SQLite database). Also bind-mounted into the Hermes container so the OSCAR skills can read it directly. |
| `/opt/data/skills/oscar` (in Hermes container) | OSCAR's `skills/` checked out by the registry sync, read-only. |

## Deploy prerequisites

This template assumes:

- ServiceBay's `hermes` template is deployed (or Hermes runs as a hostNetwork container reachable at `HERMES_API_URL`)
- ServiceBay's `ollama` template is deployed (Hermes' LLM provider points at it)
- Home Assistant is reachable with its native MCP server enabled
- ServiceBay-MCP is reachable with a bearer token

Until ServiceBay grows the `hermes` and `ollama` templates, deploying this template is blocked. See [`../../stacks/oscar/README.md`](../../stacks/oscar/README.md) for the end-to-end walkthrough.
