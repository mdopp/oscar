# oscar-voice

ServiceBay Pod-YAML template: faster-whisper + Piper + openWakeWord + Gatekeeper.

Phase 0 target. HA Voice PE devices speak Wyoming directly to this pod; HERMES handles conversation; HA stays in the loop only as an MCP-tool source for device control.

## Containers

| Container | Image | Purpose |
|---|---|---|
| `faster-whisper` | `docker.io/rhasspy/wyoming-whisper:latest` | Speech-to-text, large-v3 on GPU. ~50 ms for 3 s audio when GPU is wired up. |
| `piper` | `docker.io/rhasspy/wyoming-piper:latest` | Text-to-speech. CPU is fine. |
| `openwakeword` | `docker.io/rhasspy/wyoming-openwakeword:latest` | Wake-word detection. Reserved for software clients in Phase 0; HA Voice PE does wakeword on-device. |
| `gatekeeper` | `ghcr.io/mdopp/oscar-gatekeeper:latest` | OSCAR pipeline orchestrator. Source in [`../../gatekeeper/`](../../gatekeeper/). |

## Deployment modes

Mirrors the `oscar-brain` axis on the STT side:

| Mode | `STT_GPU_PASSTHROUGH` | `WHISPER_MODEL` | STT latency for 3 s audio |
|---|---|---|---|
| **gpu** (default) | `yes` | `large-v3` | ~50 ms |
| **cpu** | empty | `small` or `base` | 0.5–2 s |

There is **no cloud-STT mode** here — STT stays local even when `oscar-brain` runs in cloud mode. Whisper-`small` on CPU is fast enough to keep voice usable; replacing it with a cloud STT API would add another egress channel that doesn't pay off for a household.

Pair with `oscar-brain`'s mode: GPU brain + GPU STT, or CPU brain + CPU STT, or cloud brain + CPU STT.

## Host prerequisites

- **gpu mode:** `nvidia-container-toolkit` + CDI same as `oscar-brain`. Whisper-large-v3 on CPU is unusable for live voice (~5–20 s for 3 s audio).
- **cpu mode:** any host with 4+ cores and ≥4 GB RAM.
- **mdopp/servicebay#348 merged** so the HA pod can deploy with `VOICE_BUILTIN=disabled` and not collide on Wyoming ports (all modes).
- **HA Voice PE device** (or a wyoming-satellite-speaking software client) on the LAN.
- `oscar-brain` deployed and HERMES reachable at `HERMES_URL`.

## Deploy steps

1. Deploy `oscar-brain` first (issue #1) and mint a HERMES token if HERMES enforces auth.
2. In ServiceBay, pick `oscar-voice` from the wizard.
3. Fill in:
   - `GATEKEEPER_PORT` (default 10700) — the host port satellites connect to
   - `WHISPER_MODEL`, `PIPER_VOICE`, `WAKEWORD_MODEL` — defaults sensible for German household
   - `HERMES_URL` = `http://127.0.0.1:8000` (works because both pods use `hostNetwork`)
   - `HERMES_TOKEN`
4. Deploy. Whisper takes ~5–10 minutes to download the model on first start.
5. Smoke test from a laptop on the same LAN:
   ```bash
   pip install wyoming-satellite
   python -m wyoming_satellite \
     --uri tcp://0.0.0.0:0 \
     --asr-uri tcp://<oscar-host>:{{GATEKEEPER_PORT}} \
     --mic-command 'arecord -r 16000 -c 1 -f S16_LE -t raw -' \
     --snd-command 'aplay -r 22050 -c 1 -f S16_LE -t raw'
   # Speak a sentence. The gatekeeper should transcribe, call HERMES, and play
   # back HERMES's response.
   ```
6. Check logs via ServiceBay-MCP: `get_container_logs(id="oscar-voice-gatekeeper")` should show structured JSON with `trace_id`, `event=gatekeeper.transcript`, `gatekeeper.response`.

## Storage paths

All under `{{DATA_DIR}}/oscar-voice/` on the host:

| Subdir | Contents |
|---|---|
| `whisper/` | Downloaded Whisper model blobs |
| `piper/` | Downloaded Piper voice model |

openWakeWord and the gatekeeper need no host state.

## HA Voice PE pairing

HA Voice PE devices speak HA's native WebSocket protocol, not raw Wyoming Satellite. To make them reach this gatekeeper directly, the device firmware needs adjustment — open question, deferred:

- **Option A (likely):** flash the device with custom ESPHome firmware whose `voice_assistant` component points at this pod instead of HA. Requires re-flashing each device once.
- **Option B (fallback):** keep HA in the loop. Configure HA's voice pipeline to use `oscar-voice` Whisper/Piper services *and* point its conversation step at this gatekeeper via a custom HA conversation agent.

Both paths need validation at first deploy. Phase-0 acceptance for issue #2 ships the server-side infrastructure and tests it with the software-client smoke test above; HA Voice PE wiring is a follow-up.

## Logging

Same convention as the rest of OSCAR — stdout JSON, ServiceBay-MCP reads via `get_container_logs`. Spec: [`../../docs/logging.md`](../../docs/logging.md). `OSCAR_DEBUG_MODE=true` is set by default in Phase 0.

Architecture: [`../../oscar-architecture.md`](../../oscar-architecture.md) → "2. Gatekeeper / voice pipeline".
