# Gatekeeper

OSCAR-published Python image that bridges Wyoming-protocol satellites (HA Voice PE, `wyoming-satellite` CLI) to Hermes. Runs as a container inside OSCAR's `oscar-household` pod; reaches ServiceBay's unchanged `voice` template (Whisper + Piper + openWakeWord) via host loopback. Both pods are `hostNetwork: true`, sharing the host netns. The `GATEKEEPER_IMAGE` variable on `oscar-household` picks which image tag to run.

## What it does

A Wyoming-protocol server. One inbound connection = one half-duplex pipeline turn:

```
Satellite (HA Voice PE / wyoming-satellite CLI)
  → AudioStart + AudioChunk* + AudioStop
Gatekeeper
  → Whisper (local, GPU): transcribe
  → Hermes (HTTP, oscar-household neighbour pod): converse(text, uid, endpoint, trace_id)
  → Piper (local): synthesize response
  → AudioStart + AudioChunk* + AudioStop back to the satellite
```

Plus an outbound `POST /push` endpoint (port 10750, pod-internal) so Hermes' cron and proactive deliveries can address a specific Voice PE device by name.

The gatekeeper terminates the Wyoming connection after each turn. Multi-turn / barge-in / streaming responses are Phase 4 topics.

## Phase mapping

| Phase | What this code does |
|---|---|
| **0 / 1 (now)** | Pass-through. `uid` hardcoded to `DEFAULT_UID`, `endpoint = voice-pe:<connection-id>`. No speaker ID. |
| **2** | SpeechBrain ECAPA-TDNN extracts a 256-d embedding from the audio buffer; brute-force cosine k-NN against `voice_embeddings` in `oscar.db` (3–10 rows) resolves the LLDAP `uid`. |
| **4** | Multi-room routing (response goes to the satellite the user is closest to), voice-tone sensor parallel to STT, custom "Oscar" wakeword. |

Long-term target: contribute the Phase 0/1 pass-through path to Hermes as a generic `hermes gateway voice`. The Phase 2+ logic (speaker ID, multi-room, voice-tone) stays here.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `GATEKEEPER_URI` | `tcp://0.0.0.0:10700` | Wyoming endpoint for satellite connections |
| `WHISPER_URI` | `tcp://127.0.0.1:10300` | Wyoming Whisper service (provided by ServiceBay's `voice` template) |
| `PIPER_URI` | `tcp://127.0.0.1:10200` | Wyoming Piper service (same pod) |
| `OPENWAKEWORD_URI` | `tcp://127.0.0.1:10400` | openWakeWord (advertised in Info; Phase 0 lets the satellite do wakeword on-device) |
| `HERMES_URL` | `http://127.0.0.1:8642` | Base URL of Hermes' HTTP API (matches ServiceBay `hermes` template default; both pods use hostNetwork) |
| `HERMES_TOKEN` | empty | Bearer for Hermes (matches its `API_SERVER_KEY` / surfaced by ServiceBay's `hermes` template as `HERMES_API_KEY`) |
| `DEFAULT_UID` | `michael` | Hardcoded uid until Phase 2 speaker ID lands |
| `OSCAR_DB_PATH` | `/var/lib/oscar/oscar.db` | SQLite file (Phase 2: `voice_embeddings` lookup) |
| `OSCAR_DEBUG_MODE` | `false` | Initial verbose-mode default (runtime override comes from `system_settings.debug_mode` in `oscar.db`) |

## Local development

```bash
pip install -e ./gatekeeper

# Pretend Whisper / Piper / Hermes are running on the expected URIs
HERMES_URL=http://localhost:8642 OSCAR_DEBUG_MODE=true gatekeeper
```

Test from another shell with a tiny Wyoming client (`wyoming-satellite` CLI or the `example_event_client.py` shipped with that package). For pure protocol smoke-testing without audio hardware, feed a WAV file through `python -m wyoming.tools.wav` → the gatekeeper.

## Image

Built from this directory by [`.github/workflows/build-images.yml`](../.github/workflows/build-images.yml) on every push to `main` and on tags. Published as `ghcr.io/mdopp/oscar-gatekeeper:latest`. To rebuild locally: `podman build -t ghcr.io/mdopp/oscar-gatekeeper:latest -f gatekeeper/Dockerfile .` (from the repo root).

## Open points

- **HA Voice PE pairing** — HA Voice PE devices speak HA's WebSocket protocol natively, not the Wyoming-satellite protocol. Either patch the device firmware to use wyoming-satellite + point its `--event-uri` at this gatekeeper, or run HA's voice pipeline as a thin bridge with the conversation step pointing here. Validation needed at first deploy.
- **Server-side wakeword orchestration** — Phase 0 trusts the satellite to do wakeword (HA Voice PE does it locally). For software clients without VAD, the gatekeeper needs an extra event flow that connects to `OPENWAKEWORD_URI`.
- **Logging contract** — the inlined `gatekeeper.logging` helper is a placeholder until [`mdopp/servicebay`](https://github.com/mdopp/servicebay) ships a platform-wide structured-logging contract every template can follow.

Architecture: [`../oscar-architecture.md`](../oscar-architecture.md) → "gatekeeper (OSCAR-published image)".
