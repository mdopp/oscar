# Stack `oscar`

End-to-end install walkthrough for OSCAR on top of a ServiceBay full-stack host. Deploys the four OSCAR pods in the right order with sensible defaults.

The actual templates live in [`../../templates/`](../../templates/):
- `oscar-brain` — HERMES + Ollama + Qdrant + Postgres + pg-backup + signal-cli (Phase 1)
- `oscar-voice` — Wyoming services + gatekeeper
- `oscar-connectors` — weather + cloud-llm connectors
- `oscar-ingestion` — material classification pipeline (Phase 3a, deferred)

A stack in ServiceBay is documentation-only — there's no programmatic "deploy these four" button. Walk the wizard four times, in this order:

## Prerequisites

- Fedora CoreOS host with [ServiceBay v3.16+](https://github.com/mdopp/servicebay) installed and the full-stack deployed (auth, nginx, home-assistant, …).
- [mdopp/servicebay#348](https://github.com/mdopp/servicebay/issues/348) merged — needed so the HA template can deploy with `VOICE_BUILTIN=disabled` and not collide with `oscar-voice` on Wyoming ports.
- HA-MCP enabled in your Home Assistant deployment (HA Core 2025.x integration `mcp_server`).
- For the **gpu-local** deployment mode: `nvidia-container-toolkit` + CDI configured (`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`). For **cpu-local** or **cloud** modes: no GPU required.

## Deploy order

### 1. Add the OSCAR registry

ServiceBay → Settings → Registries → add `https://github.com/mdopp/oscar.git`. The four OSCAR templates appear next to ServiceBay's built-in ones in the wizard.

### 2. Pick the deployment mode

Before anything else, decide:

| Mode | Voice latency | Hardware | Privacy |
|---|---|---|---|
| **gpu-local** (default) | <500 ms | GPU ≥12 GB VRAM | full |
| **cpu-local** | 3–10 s | 4-core CPU + 8 GB RAM | full |
| **cloud** | 1–3 s | any | **prompts to a third party** |

The choice drives variable defaults in `oscar-brain` (`OLLAMA_ENABLED`, `GPU_PASSTHROUGH`, `HERMES_MODEL`, `HERMES_API_KEY`) and in `oscar-voice` (`STT_GPU_PASSTHROUGH`, `WHISPER_MODEL`). Mixed modes work but are usually a mistake — pick one and stick with it across the two pods.

Trade-offs in detail: [`../../templates/oscar-brain/README.md`](../../templates/oscar-brain/README.md) → "Deployment modes".

### 3. Deploy `oscar-brain` first

It owns the Postgres schema everything else writes into. From the ServiceBay wizard:

1. Pick `oscar-brain`.
2. Fill in the variables for your chosen deployment mode.
3. Mint a Home Assistant long-lived token (Profile → Security → Long-lived access tokens) for `HA_MCP_TOKEN`.
4. Mint a ServiceBay-MCP bearer (Settings → Integrations → MCP → "Generate token", scope `read+lifecycle`) for `SERVICEBAY_MCP_TOKEN`.
5. For `cloud` mode: also provide `HERMES_API_KEY` from your Anthropic or Google API console. The brain template fans the value out to `HERMES_API_KEY`, `GOOGLE_API_KEY`, and `ANTHROPIC_API_KEY` inside the HERMES container — one key in the wizard covers all three SDKs. Full walkthrough: [`../../templates/oscar-brain/README.md`](../../templates/oscar-brain/README.md) → "Cloud-backend setup (Gemini / Anthropic)".
6. Deploy. The included `post-deploy.py` waits for the pod to come up, verifies the init schema, and prints the next-step checklist. First start takes 5–10 min while Ollama pulls models (skipped in `cloud` mode).

Verify with the `oscar-status` skill once the pod is up (or directly: `curl http://<host>:8000/health`).

### 4. Deploy `oscar-voice`

From the wizard, pick `oscar-voice`. Set the variables to match the mode you picked in step 2 (`STT_GPU_PASSTHROUGH=yes` for gpu-local, empty for cpu-local).

`HERMES_URL` defaults to `http://127.0.0.1:8000` — works because both pods use `hostNetwork`.

### 5. Deploy `oscar-connectors`

Generate a `CONNECTORS_BEARER` (ServiceBay auto-generates on first deploy; emit it via `__SB_CREDENTIAL__` so it shows up under Settings → Integrations). Then:

- `WEATHER_API_KEY` from openweathermap.org (free tier is fine)
- `ANTHROPIC_API_KEY` and/or `GOOGLE_API_KEY` for the cloud-LLM connector (leave empty to disable that vendor). If you're running `oscar-brain` in `cloud` mode, paste the *same* key you used there — ServiceBay doesn't (yet) share variables across templates.
- `POSTGRES_PASSWORD` must **match** the one used for oscar-brain — the cloud-llm connector writes audit rows into the same database

### 6. Wire HA Voice PE (optional, real-world voice)

This is the open piece — HA Voice PE devices speak HA's native WebSocket protocol, not raw Wyoming Satellite. Two options:

- **Option A:** flash custom ESPHome firmware on the device pointing `voice_assistant` at this host's `GATEKEEPER_PORT` instead of HA.
- **Option B:** keep HA in the loop. Configure HA's voice pipeline to use the `oscar-voice` Whisper/Piper endpoints and point its conversation step at this gatekeeper.

Pending validation at first deploy; the gatekeeper itself works against any wyoming-satellite-speaking client today.

### 7. (Phase 1) Pair Signal

Once oscar-brain is up and `SIGNAL_ACCOUNT` is set, follow the QR-scan flow in [`../../templates/oscar-brain/README.md`](../../templates/oscar-brain/README.md) → "Signal pairing (Phase 1)".

### 8. (For Claude-Code debugging) Set up `.mcp.json`

Copy `.env.example` to `~/.config/oscar.env` and fill in real values, including a dedicated `claude_ro` read-only Postgres role (SQL in `.env.example`). Claude Code then has live read access to OSCAR's state — see [`../../README.md`](../../README.md) → "Debugging with Claude Code (MCP)".

## After install

The `oscar-status` skill ("OSCAR, ist alles in Ordnung?") is the first thing to try. It auto-probes every dependency and tells you what's red. From there:

- "Welche Wecker hat michael scharf?" → `oscar-audit-query`
- "Verknüpfe Signal +49 … mit anna" → `oscar-identity-link`
- "Debug-Mode für eine Stunde an" → `oscar-debug-set`
- "Mach das Licht an" → `oscar-light`
- "Stell einen Pizza-Timer auf 12 Minuten" → `oscar-timer`

## Open follow-ups

Documented in each template README's "Open follow-ups" section. Big ones:
- GPU-passthrough validation under Podman Quadlet
- HA Voice PE pairing path
- Schema-migration tool (alembic)
- Voice-PE delivery for fired timers/alarms (gatekeeper push endpoint)
