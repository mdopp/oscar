# Gatekeeper

Python container that runs inside the `oscar-voice` pod and orchestrates the voice pipeline.

## What it does

A Wyoming-protocol server. One inbound connection = one pipeline turn:

```
Satellite (HA Voice PE / wyoming-satellite CLI)
  → AudioStart + AudioChunk* + AudioStop
Gatekeeper
  → Whisper (local, GPU): transcribe
  → Hermes (oscar-hermes pod over HTTP): converse(text, uid, endpoint, trace_id)
  → Piper (local): synthesize response
  → AudioStart + AudioChunk* + AudioStop back to the satellite
```

Plus an outbound `POST /push` endpoint (port 10750, pod-internal) so Hermes' cron / proactive deliveries can address a specific Voice PE device by name.

The gatekeeper terminates the Wyoming connection after each turn (half-duplex). Multi-turn / barge-in / streaming responses are Phase 4 topics.

## Phase mapping

| Phase | What this code does |
|---|---|
| **0 (now)** | Pass-through. `uid` hardcoded to `DEFAULT_UID` (= `michael` in `oscar-voice/variables.json`), `endpoint = voice-pe:<connection-id>`. No speaker ID. |
| **2** | SpeechBrain ECAPA-TDNN extracts a 256-d embedding from the audio buffer, lookup against `gatekeeper_voice_embeddings` in `oscar-brain.postgres`, real `uid` per turn. |
| **4** | Multi-room routing (response goes to the speaker the user is closest to, not necessarily the originating session), voice-tone sensor parallel to STT. |

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `GATEKEEPER_URI` | `tcp://0.0.0.0:10700` | Wyoming endpoint for satellite connections |
| `WHISPER_URI` | `tcp://127.0.0.1:10300` | Internal Whisper service |
| `PIPER_URI` | `tcp://127.0.0.1:10200` | Internal Piper service |
| `OPENWAKEWORD_URI` | `tcp://127.0.0.1:10400` | openWakeWord (advertised in Info; Phase 0 doesn't yet orchestrate wakeword server-side because satellites already do it on-device) |
| `HERMES_URL` | `http://127.0.0.1:8642` | Base URL of the Hermes Agent HTTP API (the `oscar-hermes` pod; both pods use hostNetwork) |
| `HERMES_TOKEN` | empty | Bearer for Hermes (matches its `API_SERVER_KEY`) |
| `DEFAULT_UID` | `michael` | Hardcoded harness uid until Phase 2 |
| `OSCAR_DEBUG_MODE` | `false` | Set to `true` to log full transcripts and bodies |

## Local development

```bash
# From the repo root
pip install -e ./shared/oscar_logging
pip install -e ./gatekeeper

# Pretend Whisper / Piper / Hermes are running on the expected URIs
HERMES_URL=http://localhost:8642 OSCAR_DEBUG_MODE=true gatekeeper
```

Test from another shell with a tiny Wyoming client (`wyoming-satellite` CLI or the `example_event_client.py` shipped with that package). For pure protocol smoke-testing without audio hardware, feed a WAV file through `python -m wyoming.tools.wav` → the gatekeeper.

## Image

Built from this directory; the `oscar-voice` template references `ghcr.io/mdopp/oscar-gatekeeper:latest`. CI publishes on every push to `main` and on tags (see `.github/workflows/build-images.yml`). To rebuild locally: `podman build -t ghcr.io/mdopp/oscar-gatekeeper:latest -f gatekeeper/Dockerfile .` (from the repo root so the `shared/oscar_logging` copy works).

## Open points

- **HA Voice PE pairing** — HA Voice PE devices speak HA's WebSocket protocol natively, not the Wyoming-satellite protocol. Either patch the device firmware to use wyoming-satellite + point its `--event-uri` at this gatekeeper, or run HA's voice pipeline as a thin bridge with the conversation step pointing here. Validation needed at first deploy.
- **Server-side wakeword orchestration** — Phase 0 trusts the satellite to do wakeword (the HA Voice PE does it locally). If we want centralised wakeword (e.g. for software clients without VAD), the gatekeeper needs an extra event flow that connects to `OPENWAKEWORD_URI`.

Architecture: [`../oscar-architecture.md`](../oscar-architecture.md) → "2. Gatekeeper / voice pipeline".
