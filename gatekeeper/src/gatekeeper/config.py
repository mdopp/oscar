"""Env-driven configuration for the gatekeeper.

Phase 0 keeps the surface small. Phase 2 will add speaker-ID model paths
and the `gatekeeper_voice_embeddings` DSN.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    gatekeeper_uri: str
    whisper_uri: str
    piper_uri: str
    openwakeword_uri: str
    hermes_url: str
    hermes_token: str
    default_uid: str
    push_host: str
    push_port: int
    push_token: str
    voice_pe_devices: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        raw_devices = os.environ.get("VOICE_PE_DEVICES", "")
        devices: dict[str, str] = {}
        if raw_devices.strip():
            try:
                parsed = json.loads(raw_devices)
                if isinstance(parsed, dict):
                    devices = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                devices = {}
        return cls(
            gatekeeper_uri=os.environ.get("GATEKEEPER_URI", "tcp://0.0.0.0:10700"),
            whisper_uri=os.environ.get("WHISPER_URI", "tcp://127.0.0.1:10300"),
            piper_uri=os.environ.get("PIPER_URI", "tcp://127.0.0.1:10200"),
            openwakeword_uri=os.environ.get(
                "OPENWAKEWORD_URI", "tcp://127.0.0.1:10400"
            ),
            hermes_url=os.environ["HERMES_URL"],
            hermes_token=os.environ.get("HERMES_TOKEN", ""),
            default_uid=os.environ.get("DEFAULT_UID", "michael"),
            push_host=os.environ.get("PUSH_HOST", "0.0.0.0"),
            push_port=int(os.environ.get("PUSH_PORT", "10750")),
            push_token=os.environ.get("PUSH_TOKEN", ""),
            voice_pe_devices=devices,
        )


settings = Settings.from_env()
