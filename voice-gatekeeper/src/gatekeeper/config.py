"""Env-driven configuration for the gatekeeper.

Phase 0 (initial release) kept this tiny. Phase 2 (#937) adds the
SpeechBrain ECAPA model toggle + threshold and the solaris.db path for
the voice_embeddings table.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# Falsy values that turn speaker-ID + live enrolment OFF. Everything else
# (including unset/empty) leaves them ON — a household box wants residents
# recognised out of the box; deployments opt out via the template variable.
_SPEAKER_ID_DISABLED = {"0", "false", "no", "off"}


def speaker_id_enabled_from_env() -> bool:
    """Single source of truth for "is speaker-ID on": enabled unless
    SOLARIS_SPEAKER_ID_ENABLED is set to an explicit falsy value. Shared with
    `gatekeeper.speaker.get_extractor` so the config flag and the extractor
    loader can never disagree."""
    raw = os.environ.get("SOLARIS_SPEAKER_ID_ENABLED", "").strip().lower()
    return raw not in _SPEAKER_ID_DISABLED


@dataclass(frozen=True)
class Settings:
    gatekeeper_uri: str
    whisper_uri: str
    piper_uri: str
    openwakeword_uri: str
    engine_url: str
    engine_token: str
    default_uid: str
    push_host: str
    push_port: int
    push_token: str
    mcp_host: str
    mcp_port: int
    mcp_token: str
    solaris_db_path: str
    speaker_id_enabled: bool
    speaker_id_threshold: float
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
        try:
            threshold = float(os.environ.get("SOLARIS_SPEAKER_ID_THRESHOLD", "0.55"))
        except ValueError:
            threshold = 0.55
        return cls(
            gatekeeper_uri=os.environ.get("GATEKEEPER_URI", "tcp://0.0.0.0:10700"),
            whisper_uri=os.environ.get("WHISPER_URI", "tcp://127.0.0.1:10300"),
            piper_uri=os.environ.get("PIPER_URI", "tcp://127.0.0.1:10200"),
            openwakeword_uri=os.environ.get(
                "OPENWAKEWORD_URI", "tcp://127.0.0.1:10400"
            ),
            # The Solaris Engine's Ollama-compatible facade (the chat server's
            # loopback port, /ollama prefix). Voice always speaks to the
            # household profile — there is deliberately no admin URL here.
            engine_url=os.environ.get(
                "SOLARIS_ENGINE_URL", "http://127.0.0.1:8787/ollama"
            ),
            engine_token=os.environ.get("SOLARIS_API_KEY", ""),
            default_uid=os.environ.get("DEFAULT_UID", "michael"),
            # Loopback by default: the push + MCP listeners only ever serve
            # in-pod callers, which share the host netns (hostNetwork) and
            # reach them over 127.0.0.1. Binding 0.0.0.0 under hostNetwork
            # would expose them on the host's LAN interface, where an empty
            # token leaves them unauthenticated (#116). Only the Wyoming port
            # (GATEKEEPER_URI) needs the LAN, for satellites.
            push_host=os.environ.get("PUSH_HOST", "127.0.0.1"),
            push_port=int(os.environ.get("PUSH_PORT", "10750")),
            push_token=os.environ.get("PUSH_TOKEN", ""),
            mcp_host=os.environ.get("MCP_HOST", "127.0.0.1"),
            mcp_port=int(os.environ.get("MCP_PORT", "10760")),
            mcp_token=os.environ.get("GATEKEEPER_MCP_TOKEN", ""),
            solaris_db_path=os.environ.get(
                "SOLARIS_DB_PATH", "/var/lib/solaris/solaris.db"
            ),
            speaker_id_enabled=speaker_id_enabled_from_env(),
            speaker_id_threshold=threshold,
            voice_pe_devices=devices,
        )


settings = Settings.from_env()
