# Türsteher

Python-Container-Code, läuft im `oscar-voice`-Pod als zusätzlicher Container neben Rhasspy 3.

Verantwortlichkeiten:
- Wyoming-Audio von HA Voice PE empfangen, Pipeline-Steuerung (openWakeWord → STT → conversation → TTS)
- **Phase 0:** Pass-through-Modus — `uid` immer auf einzigen Familien-Account, `endpoint=voice-pe:<device>`
- **Phase 2:** SpeechBrain ECAPA-TDNN für Speaker-ID, Embedding-Lookup gegen `tuersteher_voice_embeddings` in `oscar-brain.postgres`
- HERMES-Conversation-Call mit `(text, uid, endpoint, audio_features)`; Antwort → Piper → Wyoming-Stream zurück zum Voice-PE
- `trace_id`-Generation pro Conversation-Turn (Spec: [`../docs/logging.md`](../docs/logging.md))

Architektur: [`../oscar-architecture.md`](../oscar-architecture.md) → „2. Türsteher".
