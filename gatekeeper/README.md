# Gatekeeper

Python container code, runs inside the `oscar-voice` pod alongside Rhasspy 3.

Responsibilities:
- Receive Wyoming audio from HA Voice PE, drive the pipeline (openWakeWord → STT → conversation → TTS)
- **Phase 0:** pass-through mode — `uid` hardcoded to the single family account, `endpoint=voice-pe:<device>`
- **Phase 2:** SpeechBrain ECAPA-TDNN for speaker ID, embedding lookup against `gatekeeper_voice_embeddings` in `oscar-brain.postgres`
- HERMES conversation call with `(text, uid, endpoint, audio_features)`; response → Piper → Wyoming stream back to the Voice PE
- `trace_id` generation per conversation turn (spec: [`../docs/logging.md`](../docs/logging.md))

Architecture: [`../oscar-architecture.md`](../oscar-architecture.md) → "2. Gatekeeper".
