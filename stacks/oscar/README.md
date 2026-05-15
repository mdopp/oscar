# OSCAR stack

End-to-end install for OSCAR on top of a ServiceBay full-stack host.

A ServiceBay stack is **documentation-only** — there's no programmatic "deploy these N" button. The OSCAR stack walks through **two ServiceBay stacks** plus, optionally, the extended `voice` template:

1. ServiceBay's `ai-stack` — Ollama + Hermes (Postgres + Qdrant only when Phase 3a calls for them)
2. OSCAR's `oscar-household` template — the household-specific glue, ships its own SQLite
3. (Optional, Phase 1) ServiceBay's extended `voice` template with OSCAR's `gatekeeper` sidecar

> **Status**: the `ai-stack` templates and the extended `voice` template are upstream work in [`mdopp/servicebay`](https://github.com/mdopp/servicebay). Until they land, parts of this walkthrough are aspirational — marked **TODO ServiceBay** below.

## Prerequisites

- **ServiceBay v3.16+** on a Fedora CoreOS host with the **full-stack** deployed (auth, nginx, home-assistant, …).
- **[mdopp/servicebay#443](https://github.com/mdopp/servicebay/issues/443)** merged so ServiceBay can sync external git registries.
- **[mdopp/servicebay#348](https://github.com/mdopp/servicebay/issues/348)** merged — only needed once you add voice (Phase 1); lets you deploy HA with `VOICE_BUILTIN=disabled` so Wyoming ports don't collide with the extended `voice` template.
- **HA-MCP** integration enabled in your Home Assistant (Settings → Devices & Services → Add Integration → "Model Context Protocol Server").
- A **ServiceBay-MCP** bearer token (Settings → Integrations → MCP → Generate token, scope `read+lifecycle`).
- For **gpu-local** mode: `nvidia-container-toolkit` + CDI on the host. For **cpu-local** / **cloud-only** modes: nothing extra.

## Step 0 — Add the OSCAR registry

ServiceBay → Settings → Registries → Add:

- Name: `oscar`
- URL: `https://github.com/mdopp/oscar.git`

After save, the `oscar-household` template appears in the wizard.

## Step 1 — Walk through ServiceBay's `ai-stack`

**TODO ServiceBay** — the `ai-stack` is upstream work. Until it lands, you'll need to deploy its parts individually as ServiceBay grows them.

The end-state walkthrough:

1. **`ollama`** — choose model (`gemma-12b-q4` by default), enable GPU passthrough if you have a CDI-registered NVIDIA GPU. First start pulls the model (5–10 min).
2. **`hermes`** — wraps `docker.io/nousresearch/hermes-agent`. Wizard prompts: `API_SERVER_KEY` (auto-generated), `LLM_PROVIDER_URL` (defaults to the `ollama` template's Ollama port). Hermes ships its own SQLite for Honcho — no external Postgres needed for Phase 0.

(`postgres` and `qdrant` enter the picture only if Phase 3a decides to migrate off SQLite — see [`oscar-architecture.md`](../../oscar-architecture.md) → "Phase 3a — Streaming ingestion". For Phase 0–2, neither is needed.)

After deploy, do the one-time setup:

```bash
ssh <oscar-host>
podman exec -it hermes hermes setup
# (wizard pairs the messaging gateway, registers MCP servers)
podman exec -it hermes hermes gateway setup signal
```

## Step 2 — Deploy OSCAR's `oscar-household`

ServiceBay wizard → `oscar-household` → fill in:

- `HERMES_API_URL` — defaults to `http://127.0.0.1:8642` (hostNetwork)
- `HERMES_TOKEN` — Hermes' `API_SERVER_KEY` from Step 1
- `HA_MCP_TOKEN` — long-lived access token from HA (Profile → Long-lived access tokens) **or** Authelia OIDC client credentials
- `SERVICEBAY_MCP_TOKEN` — ServiceBay-MCP bearer token from prerequisites
- `LLDAP_GROUP` — defaults to `family`
- `GATEKEEPER_IMAGE` — leave empty for chat-only; set later for voice

The template doesn't ask for a database DSN — it ships its own SQLite (`oscar.db`) in the pod's volume.

Deploy. The template:

- Runs Alembic against the local `oscar.db` SQLite file, creating `cloud_audit`, `system_settings`, `voice_embeddings` (idempotent)
- Bind-mounts OSCAR's `skills/` directory into Hermes at `/opt/data/skills/oscar`, plus the same volume so the skills can read `oscar.db` directly
- Calls `hermes mcp add` for HA-MCP and ServiceBay-MCP with the tokens you provided
- Configures Hermes' cloud-audit hook so every cloud-LLM call writes a `cloud_audit` row

Restart Hermes once so it picks up the new skills + MCP servers:

```bash
systemctl --user restart hermes.service
```

## Step 3 — Smoke-test

Talk to OSCAR through Signal (or whichever gateway you paired):

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

If anything answers "nein", run `oscar-status` first — it returns a structured probe of `oscar.db`, Ollama, Hermes, HA-MCP, ServiceBay-MCP.

## Step 4 — Add voice (Phase 1, optional)

**TODO ServiceBay** — requires the extended `voice` template with the `GATEKEEPER_IMAGE` variable. Until it lands, voice runs from the legacy OSCAR template path (`templates/oscar-voice/`), which the repo carries during the transition.

The end-state walkthrough:

1. Make sure HA was redeployed with `VOICE_BUILTIN=disabled` (otherwise HA's bundled Wyoming containers collide with the extended `voice` template on ports 10300/10200/10400).
2. Walk through ServiceBay's extended `voice` template:
   - `STT_GPU_PASSTHROUGH=yes` for `large-v3`; `WHISPER_MODEL=large-v3` (or `small` / `base` on CPU)
   - `WHISPER_LANGUAGE=de`, `PIPER_VOICE=de_DE-thorsten-medium`
   - `GATEKEEPER_IMAGE=ghcr.io/mdopp/oscar-gatekeeper:latest`
   - `HERMES_URL` + `HERMES_TOKEN` (same as `oscar-household`)
3. Point HA Voice PE devices at the host's `:10700` (Wyoming).

The gatekeeper starts in pass-through mode (`uid = DEFAULT_UID`). Phase 2 enables speaker ID.

## Step 5 — Connectors (optional)

If you want weather, news, or other external information, add a third-party MCP server via:

```bash
podman exec -it hermes hermes mcp add <url> --token <token>
```

OSCAR no longer ships its own weather/news connectors — Hermes' MCP ecosystem is rich enough that we consume third-party MCPs instead of duplicating them.

The one exception is the **cloud-LLM audit proxy** (planned separate repo `mcp-audit-proxy`). Once published, add it the same way:

```bash
podman exec -it hermes hermes mcp add http://localhost:8801 --token <token>
```

`oscar-household` already configures Hermes to route cloud calls through it.

## After install — smoke-test checklist

The full observe-first matrix lives in the OSCAR tracking issue. Work through it after install and file gaps as separate issues:

- [ ] `oscar-status` returns green for all probes
- [ ] Signal/Telegram round-trip → Hermes responds in German
- [ ] Light command via HA-MCP turns the light on/off
- [ ] Timer command creates a Hermes cron job and fires on schedule
- [ ] Cloud-LLM call (e.g. complex question) writes a `cloud_audit` row
- [ ] `oscar-audit-query` returns the row when asked
- [ ] (Phase 1) Voice round-trip via HA Voice PE works under 1.5 s end-to-end
