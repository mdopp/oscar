# oscar-voice

ServiceBay Pod-YAML-Template: Rhasspy 3 + faster-whisper-large-v3 + Piper + openWakeWord + Türsteher.

Wyoming-Endpoints (10300/10200/10400) für HA Voice PE Devices. Türsteher (Code in `tuersteher/`) macht Pipeline-Orchestrierung + Conversation-Handoff an HERMES.

Phase 0: Pass-through (kein Speaker-ID). Phase 2: Speaker-ID + Embedding-Tabelle. Architektur: [`oscar-architecture.md`](../../oscar-architecture.md).
