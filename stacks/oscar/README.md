# OSCAR stack

End-to-end install for OSCAR on top of a ServiceBay full-stack host.

A ServiceBay stack is **documentation-only** — there's no programmatic "deploy these N" button. The OSCAR stack walks through:

1. ServiceBay's `ai-stack` walkthrough — `ollama` + `hermes`
2. OSCAR's `oscar-household` template — the household-specific glue (schema init + skill mount + voice gatekeeper + non-interactive MCP wiring)
3. (Optional, Phase 1) ServiceBay's unchanged `voice` template, deployed alongside `oscar-household`

> **Status**: the `ai-stack` templates are upstream work in `mdopp/servicebay` ([#538](https://github.com/mdopp/servicebay/issues/538), [#539](https://github.com/mdopp/servicebay/issues/539), [#540](https://github.com/mdopp/servicebay/issues/540)). Until they land, Step 1 below is aspirational — marked **TODO ServiceBay**.

## Prerequisites

- **ServiceBay v3.16+** on a Fedora CoreOS host with the **full-stack** deployed (auth, nginx, home-assistant, …).
- **[mdopp/servicebay#443](https://github.com/mdopp/servicebay/issues/443)** merged so ServiceBay can sync external git registries.
- **[mdopp/servicebay#348](https://github.com/mdopp/servicebay/issues/348)** merged — only needed once you add voice (Phase 1); lets you deploy HA with `VOICE_BUILTIN=disabled` so Wyoming ports don't collide with ServiceBay's `voice` template.
- **HA-MCP** integration enabled in your Home Assistant (Settings → Devices & Services → Add Integration → "Model Context Protocol Server"). Generate a long-lived access token for it.
- A **ServiceBay-MCP** bearer token (Settings → Integrations → MCP → Generate token, scope `read+lifecycle`).
- For **gpu-local** mode: `nvidia-container-toolkit` + CDI on the host. For **cpu-local** / **cloud-only** modes: nothing extra.

## Step 0 — Add the OSCAR registry

ServiceBay → Settings → Registries → Add:

- Name: `oscar`
- URL: `https://github.com/mdopp/oscar.git`

After save, the `oscar-household` template appears in the wizard.

## Step 1 — Walk through ServiceBay's `ai-stack`

**TODO ServiceBay** — Phase 0 depends on these landing.

The end-state walkthrough:

1. **`ollama`** ([mdopp/servicebay#538](https://github.com/mdopp/servicebay/issues/538)) — choose model (`gemma-12b-q4` by default), enable GPU passthrough if you have a CDI-registered NVIDIA GPU. Defaults to `OLLAMA_HOST=127.0.0.1` — remote access goes through NPM + Authelia. First start pulls the model (5–10 min).
2. **`hermes`** ([mdopp/servicebay#539](https://github.com/mdopp/servicebay/issues/539)) — wraps `docker.io/nousresearch/hermes-agent`. Wizard prompts: `API_SERVER_KEY` (auto-generated), `LLM_PROVIDER_URL` (defaults to the `ollama` template's port). Hermes ships its own SQLite for Honcho — no external Postgres needed for Phase 0. **Setup runs non-interactively** from the wizard variables; no `podman exec hermes setup` step.

(`postgres` and `qdrant` enter the picture only if Phase 3a decides to migrate off SQLite — see [`oscar-architecture.md`](../../oscar-architecture.md) → "Phase 3a — Streaming ingestion". For Phase 0–2, neither is needed.)

## Step 2 — Pair the messaging gateway

Hermes' messaging gateways (Signal, Telegram, Discord, Slack, WhatsApp, Email) need an interactive pairing because the underlying messenger protocols require it (e.g. Signal pairs as a linked device, Telegram needs a Bot API key, etc.). This is genuine human-input territory, not a `podman exec` anti-pattern — ServiceBay's `hermes` template will surface it as a `diagnose` probe with structured `actions[]` once paired credentials are missing.

For Phase-0 testing, walk the operator through it in `hermes`'s setup UI. The exact flow depends on `mdopp/servicebay#539` shipping.

## Step 3 — Deploy OSCAR's `oscar-household`

ServiceBay wizard → `oscar-household` → fill in:

- `DEFAULT_UID` — household admin's LLDAP uid (default `michael`)
- `HERMES_API_URL` — defaults to `http://127.0.0.1:8642` (both pods hostNetwork)
- `HERMES_TOKEN` — Hermes' `API_SERVER_KEY` from Step 1
- `HA_MCP_URL` + `HA_MCP_TOKEN` — Home Assistant's MCP endpoint and access token
- `SERVICEBAY_MCP_URL` + `SERVICEBAY_MCP_TOKEN` — ServiceBay-MCP bearer
- `GATEKEEPER_PORT` — default `10700`, host port for HA Voice PE
- `GATEKEEPER_IMAGE` — leave default (`ghcr.io/mdopp/oscar-gatekeeper:latest`)
- `WHISPER_URI` / `PIPER_URI` — default to `127.0.0.1:10300` / `10200` (matches ServiceBay's `voice` template's published ports; only relevant once voice is added)
- `VOICE_PE_DEVICES` — `{}` for now (populate once devices are paired)
- `LLDAP_GROUP` — defaults to `family`
- `OSCAR_DEBUG_MODE` — `true` while building, `false` for productive household

The template doesn't ask for a database DSN — it ships its own SQLite (`oscar.db`) in the pod's volume.

Deploy. The template:

- Runs Alembic against the local `oscar.db`, creating `cloud_audit`, `system_settings`, `voice_embeddings` (idempotent)
- Bind-mounts OSCAR's `skills/` and `oscar.db` into the Hermes container at `/opt/data/skills/oscar` and `/opt/data/oscar/oscar.db` so the skills can read both
- Starts the gatekeeper container (long-running, idle until you point a Voice PE at it)
- Non-interactive post-deploy: calls Hermes' HTTP API to register HA-MCP and ServiceBay-MCP using the tokens you provided. No `podman exec` needed.

## Step 4 — Smoke-test

Talk to OSCAR through whichever gateway you paired:

```
You (Signal):    bist du da?
OSCAR:           ja, alles ok. ollama, oscar.db, ha-mcp und servicebay-mcp antworten.
You:             mach das wohnzimmerlicht an
OSCAR:           ok, wohnzimmer ist an.
You:             stell einen timer auf 5 minuten
OSCAR:           ok, fünf minuten.
You:             was kostete der gestrige cloud-call?
OSCAR:           gestern abend einer um 21:14, anthropic claude-haiku, 0,0023 €.
```

If anything answers "nein", run `oscar-status` first — it calls ServiceBay-MCP's `get_health_checks` / `diagnose` and returns the per-component state.

## Step 5 — Add voice (Phase 1, optional)

Once Phase 0 works, add voice by deploying ServiceBay's **unchanged** `voice` template alongside `oscar-household`. Both pods are `hostNetwork: true`, so the gatekeeper container in `oscar-household` reaches the `voice` template's Whisper/Piper via `127.0.0.1`.

1. Make sure HA was redeployed with `VOICE_BUILTIN=disabled` (otherwise HA's bundled Wyoming containers collide with the `voice` template on ports 10300/10200/10400).
2. Walk through ServiceBay's `voice` template:
   - `STT_GPU_PASSTHROUGH=yes` for `large-v3`; `WHISPER_MODEL=large-v3` (or `small` / `base` on CPU)
   - `WHISPER_LANGUAGE=de`, `PIPER_VOICE=de_DE-thorsten-medium`
3. Point HA Voice PE devices at the host's `:10700` (Wyoming, the port `oscar-household` exposes).

The gatekeeper in `oscar-household` immediately picks it up — no re-deploy of `oscar-household` needed.

## Step 6 — Connectors (optional)

If you want weather, news, or other external information, register a third-party MCP server with Hermes. Same non-interactive pattern as Step 3 (the `hermes` template's UI surfaces an "add MCP server" form; behind the scenes it's an HTTP call to Hermes' API).

OSCAR no longer ships its own weather/news connectors — Hermes' MCP ecosystem is rich enough that we consume third-party MCPs instead of duplicating them.

The one exception is the **cloud-LLM audit proxy** (planned separate repo `mcp-audit-proxy`). Once published, register it the same way; `oscar-household`'s post-deploy will then route Hermes' cloud calls through it so every call writes a `cloud_audit` row.

## After install — smoke-test checklist

The full observe-first matrix lives in [`mdopp/oscar#70`](https://github.com/mdopp/oscar/issues/70). Work through it after install and file gaps as separate issues:

- [ ] `oscar-status` returns green for all probes
- [ ] Signal/Telegram round-trip → Hermes responds in German
- [ ] Light command via HA-MCP turns the light on/off
- [ ] Timer command creates a Hermes cron job and fires on schedule
- [ ] Cloud-LLM call (e.g. complex question) writes a `cloud_audit` row
- [ ] `oscar-audit-query` returns the row when asked
- [ ] (Phase 1) Voice round-trip via HA Voice PE works under 1.5 s end-to-end
