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

# The enrolment self-test (#1083) compares the profile it is about to store
# against every already-enrolled resident. That comparison gets its own bar,
# deliberately NOT the recognition threshold above: at 0.55 a merely
# similar-sounding household member — a sibling, a parent and child — is
# unenrollable, which is a real household failing honestly but pointlessly.
#
# Direction matters, so spelling it out: these are cosine similarities, higher =
# more similar. A HIGHER collision bar is STRICTER about what counts as "already
# enrolled" and therefore refuses FEWER enrolments; only a near-identical profile
# is turned away. A genuine collision still fails closed — two residents must
# never be silently merged into one identity.
#
# 0.65 is a chosen starting point, not a measured one: this repo has no
# distance data from real household voices yet. `gatekeeper.enroll.selftest`
# logs the score each verdict turned on, so the number can be tuned on the box
# via SOLARIS_SPEAKER_COLLISION_THRESHOLD without a rebuild.
DEFAULT_COLLISION_THRESHOLD = 0.65

# Storing several fingerprints per resident (#1084) raises recall — and with it
# the chance that one of those extra rows happens to sit close to a *different*
# resident's voice. The counterweight is a margin: a turn is only attributed
# when the best row beats the best row of any OTHER resident by this much. An
# absolute threshold alone cannot express "0.62 for Anna and 0.60 for Ben is not
# a recognition, it is a coin toss".
#
# Applies to recognition only. The enrolment self-test (#1083) keeps its own
# profile-vs-profile collision bar and does not use this.
#
# 0.10 is a chosen starting point, not a measured one — this repo has no
# distance data from real household voices. `gatekeeper.speaker.match` logs the
# score and the runner-up every turn, so the number can be tuned on the box via
# SOLARIS_SPEAKER_MATCH_MARGIN without a rebuild. 0 disables the rule.
DEFAULT_MATCH_MARGIN = 0.10


def system_language_from_env() -> str:
    """The language the voice stack runs in (#1057).

    Whisper, the TTS bridge and the HA assist pipeline all take it from the
    template's SOLARIS_LANGUAGE; the gatekeeper advertises the same value to
    satellites instead of a hardcoded "de" that may no longer match what the
    pipeline actually transcribes.
    """
    return (os.environ.get("SOLARIS_LANGUAGE", "").strip() or "de").lower()


def speaker_collision_threshold_from_env() -> float:
    """The profile-vs-profile collision bar for the enrolment self-test —
    separate from SOLARIS_SPEAKER_ID_THRESHOLD (see DEFAULT_COLLISION_THRESHOLD
    for why, and for which direction is stricter)."""
    try:
        return float(
            os.environ.get(
                "SOLARIS_SPEAKER_COLLISION_THRESHOLD", str(DEFAULT_COLLISION_THRESHOLD)
            )
        )
    except ValueError:
        return DEFAULT_COLLISION_THRESHOLD


def speaker_match_margin_from_env() -> float:
    """How far the best-matching resident must beat the runner-up resident
    before a turn is attributed (see DEFAULT_MATCH_MARGIN)."""
    try:
        return float(
            os.environ.get("SOLARIS_SPEAKER_MATCH_MARGIN", str(DEFAULT_MATCH_MARGIN))
        )
    except ValueError:
        return DEFAULT_MATCH_MARGIN


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
    speaker_collision_threshold: float
    speaker_match_margin: float
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
            # The Solaris Engine's Ollama-PROTOCOL facade (the chat server's
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
            speaker_collision_threshold=speaker_collision_threshold_from_env(),
            speaker_match_margin=speaker_match_margin_from_env(),
            voice_pe_devices=devices,
        )


settings = Settings.from_env()
