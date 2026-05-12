# oscar-voice

ServiceBay Pod-YAML template: Rhasspy 3 + faster-whisper-large-v3 + Piper + openWakeWord + gatekeeper.

Wyoming endpoints (10300/10200/10400) for HA Voice PE devices. The gatekeeper (code in `gatekeeper/`) handles pipeline orchestration + conversation handoff to HERMES.

Phase 0: pass-through (no speaker ID). Phase 2: speaker ID + embedding table. Architecture: [`oscar-architecture.md`](../../oscar-architecture.md).
