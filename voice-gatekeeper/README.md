# Gatekeeper

Solaris-published Python image that bridges Wyoming-protocol satellites (HA Voice PE, `wyoming-satellite` CLI) to the Solaris Engine. Runs as a container inside Solaris's `solarisbay` pod; reaches ServiceBay's unchanged `voice` template (Whisper + Piper + openWakeWord) via host loopback. Both pods are `hostNetwork: true`, sharing the host netns. The `GATEKEEPER_IMAGE` variable on `solarisbay` picks which image tag to run.

## What it does

A Wyoming-protocol server. One inbound connection = one half-duplex pipeline turn:

```
Satellite (HA Voice PE / wyoming-satellite CLI)
  → AudioStart + AudioChunk* + AudioStop
Gatekeeper
  → Whisper (local, GPU): transcribe
  → the Solaris Engine (HTTP, same pod): converse(text, uid, endpoint, location, trace_id)
  → Piper (local): synthesize response
  → AudioStart + AudioChunk* + AudioStop back to the satellite
```

Plus an outbound `POST /push` endpoint (port 10750, pod-internal) so the engine's cron and proactive deliveries can address a specific Voice PE device by name.

The gatekeeper terminates the Wyoming connection after each turn. Multi-turn / barge-in / streaming responses are Phase 4 topics.

## Phase mapping

| Phase | What this code does |
|---|---|
| **0 / 1 (default)** | Pass-through. `uid` hardcoded to `DEFAULT_UID`, `endpoint = voice-pe:<connection-id>`. No speaker ID. |
| **2 (framework landed, #937)** | Resolver, k-NN cosine, embeddings store, `POST /enrol` HTTP endpoint, handler wiring all live. ECAPA-TDNN itself is opt-in — see "Enabling Speaker-ID" below. |
| **4** | Multi-room routing (response goes to the satellite the user is closest to), voice-tone sensor parallel to STT, custom "Solaris" wakeword. |

Long-term target: fold the Phase 0/1 pass-through path into the Solaris Engine core as a generic voice gateway. The Phase 2+ logic (speaker ID, multi-room, voice-tone) stays here.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `GATEKEEPER_URI` | `tcp://0.0.0.0:10700` | Wyoming endpoint for satellite connections |
| `WHISPER_URI` | `tcp://127.0.0.1:10300` | Wyoming Whisper service (provided by ServiceBay's `voice` template) |
| `PIPER_URI` | `tcp://127.0.0.1:10200` | Wyoming Piper service (same pod) |
| `OPENWAKEWORD_URI` | `tcp://127.0.0.1:10400` | openWakeWord (advertised in Info; Phase 0 lets the satellite do wakeword on-device) |
| `SOLARIS_ENGINE_URL` | `http://127.0.0.1:8787/ollama` | Base URL of the Solaris Engine's Ollama-compatible facade (the `solaris-chat` server's loopback port + `/ollama` prefix; both pods use hostNetwork) |
| `SOLARIS_API_KEY` | empty | Bearer token for the Solaris Engine facade |
| `DEFAULT_UID` | `michael` | Fallback uid when speaker-ID is off or doesn't match |
| `SOLARIS_DB_PATH` | `/var/lib/solaris/solaris.db` | SQLite file (Phase 2: `voice_embeddings` read/write) |
| `SOLARIS_SPEAKER_ID_ENABLED` | empty | Set to `1`/`true` to turn on Phase-2 speaker resolution. Off by default — the stock image has no ECAPA model anyway. |
| `SOLARIS_SPEAKER_ID_THRESHOLD` | `0.55` | Cosine similarity threshold above which a k-NN match is accepted. Below it, the resolver falls back to `DEFAULT_UID` (and logs the score). |
| `SOLARIS_SPEAKER_COLLISION_THRESHOLD` | `0.65` | Enrolment self-test only: cosine similarity at which a newly captured profile counts as an already-enrolled resident, so enrolment is refused rather than merging two residents into one identity. Higher = only a near-identical profile is refused. Deliberately stricter than `SOLARIS_SPEAKER_ID_THRESHOLD`; the default is a starting point, not a measured value. |
| `SOLARIS_SPEAKER_MATCH_MARGIN` | `0.10` | Recognition only: how far the best-matching resident must beat the best row of any *other* resident before the turn is attributed. A closer race is a coin toss and routes to the guest profile instead. This is the counterweight to storing several fingerprints per resident, which raises recall and false-match surface together. `0` disables it; the default is a starting point, not a measured value. |
| `SOLARIS_SPEAKER_MODEL_CACHE` | `/var/lib/solaris/models/spkrec-ecapa` | Where SpeechBrain caches the pretrained ECAPA weights. ~80 MB on first start. |
| `SOLARIS_DEBUG_MODE` | `false` | Initial verbose-mode default (runtime override comes from `system_settings.debug_mode` in `solaris.db`) |

## Enabling Speaker-ID (Phase 2)

The framework (resolver, store, enrolment endpoint, handler wiring)
ships in the default image. The ECAPA-TDNN model and its dependencies
(SpeechBrain + torch, ~1 GB) do **not** ship in the default image —
adding them would balloon a sidecar that's otherwise ~100 MB.

To activate Phase-2 speaker resolution:

1. Build a custom gatekeeper image that installs the extras:

   ```dockerfile
   FROM ghcr.io/mdopp/solaris-gatekeeper:latest
   RUN pip install --no-cache-dir 'solaris-gatekeeper[speaker-id]'
   ```

2. Point `GATEKEEPER_IMAGE` (the `solarisbay` template variable)
   at your image tag.
3. Set `SOLARIS_SPEAKER_ID_ENABLED=1` in the wizard.
4. Enrol each resident through the gatekeeper's HTTP API:

   ```bash
   curl -X POST http://127.0.0.1:10750/enrol \
        -H "Authorization: Bearer $PUSH_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
              "uid": "alice",
              "sample_rate": 16000,
              "sample_width": 2,
              "channels": 1,
              "samples": [
                "<base64 of 16kHz mono int16 PCM clip 1>",
                "<base64 of clip 2>",
                "<base64 of clip 3>"
              ]
            }'
   ```

   3–10 clips. The endpoint averages the per-clip ECAPA embeddings
   and upserts the result. `GET /enrolments` lists enrolled uids;
   `DELETE /enrolments/<uid>` removes one.

When the env flag is off, deps are missing, or no enrolments exist,
the handler short-circuits the resolver and uses `DEFAULT_UID`. The
conversation pipeline is unaffected.

## Room mapping (location)

Each turn, the gatekeeper attaches a `location` (room) to the Solaris Engine
`converse` payload so room-dependent commands ("turn on the light")
resolve to the right area. The room is looked up by the originating
satellite's id (the socket peer host, `voice-pe:<host>`) in the
`voice_pe_rooms` table of `solaris.db`. When unknown, `location` is `null`
and the Solaris Engine prompts the resident to name the room, then persists it — the
spoken enrolment ("which room am I in?" / "this is the bath" remap) lives
in the engine (see #94).

Rooms are managed over the pod-internal HTTP endpoint (shares `PUSH_TOKEN`):

```bash
curl -X POST http://127.0.0.1:10750/room \
     -H "Authorization: Bearer $PUSH_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"satellite_id": "192.168.178.42", "room": "kitchen"}'
# also accepts {"endpoint": "voice-pe:192.168.178.42", "room": "kitchen"}
# GET /rooms lists mappings; DELETE /rooms/<satellite_id> removes one.
```

**Interim store.** `solaris.db` holds the mapping for now. The longer-term
goal is to source device→area from Home Assistant as the single source of
truth (HA already owns areas), dropping the local table once that lands.

## Local development

```bash
pip install -e ./gatekeeper

# Pretend Whisper / Piper / the Solaris Engine are running on the expected URIs
SOLARIS_ENGINE_URL=http://localhost:8787/ollama SOLARIS_DEBUG_MODE=true gatekeeper
```

Test from another shell with a tiny Wyoming client (`wyoming-satellite` CLI or the `example_event_client.py` shipped with that package). For pure protocol smoke-testing without audio hardware, feed a WAV file through `python -m wyoming.tools.wav` → the gatekeeper.

## Image

Built from this directory by [`.github/workflows/build-images.yml`](../.github/workflows/build-images.yml) on every push to `main` and on tags. Published as `ghcr.io/mdopp/solaris-gatekeeper:latest`. To rebuild locally: `podman build -t ghcr.io/mdopp/solaris-gatekeeper:latest voice-gatekeeper/` (from the repo root).

## Open points

- **HA Voice PE pairing** — HA Voice PE devices speak HA's WebSocket protocol natively, not the Wyoming-satellite protocol. Either patch the device firmware to use wyoming-satellite + point its `--event-uri` at this gatekeeper, or run HA's voice pipeline as a thin bridge with the conversation step pointing here. Validation needed at first deploy.
- **Server-side wakeword orchestration** — Phase 0 trusts the satellite to do wakeword (HA Voice PE does it locally). For software clients without VAD, the gatekeeper needs an extra event flow that connects to `OPENWAKEWORD_URI`.
- **Logging contract** — the inlined `gatekeeper.logging` helper is a placeholder until [`mdopp/servicebay`](https://github.com/mdopp/servicebay) ships a platform-wide structured-logging contract every template can follow.

Architecture: [`../../../../solaris-architecture.md`](../../../../solaris-architecture.md) → "gatekeeper (Solaris-published image)".
