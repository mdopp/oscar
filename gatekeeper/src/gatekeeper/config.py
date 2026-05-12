"""Env-driven configuration for the gatekeeper.

Phase 0 keeps the surface small. Phase 2 will add speaker-ID model paths
and the `gatekeeper_voice_embeddings` DSN.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    gatekeeper_uri: str
    whisper_uri: str
    piper_uri: str
    openwakeword_uri: str
    hermes_url: str
    hermes_token: str
    default_uid: str

    @classmethod
    def from_env(cls) -> "Settings":
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
        )


settings = Settings.from_env()
