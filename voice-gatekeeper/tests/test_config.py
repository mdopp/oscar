"""Tests for the env-driven Settings dataclass."""

from __future__ import annotations


def _fresh_settings(monkeypatch, env: dict[str, str]):
    """Build Settings.from_env() against a controlled env."""
    import gatekeeper.config as cfg_mod

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return cfg_mod.Settings.from_env()


def test_voice_pe_devices_parses_json_map(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        {
            "VOICE_PE_DEVICES": '{"office": "tcp://10.0.0.1:10700", "bedroom": "tcp://10.0.0.2:10700"}',
        },
    )
    assert s.voice_pe_devices == {
        "office": "tcp://10.0.0.1:10700",
        "bedroom": "tcp://10.0.0.2:10700",
    }


def test_voice_pe_devices_invalid_json_is_empty(monkeypatch):
    s = _fresh_settings(monkeypatch, {"VOICE_PE_DEVICES": "not-json"})
    assert s.voice_pe_devices == {}


def test_voice_pe_devices_empty_when_unset(monkeypatch):
    monkeypatch.delenv("VOICE_PE_DEVICES", raising=False)
    s = _fresh_settings(monkeypatch, {})
    assert s.voice_pe_devices == {}


def test_push_port_default(monkeypatch):
    monkeypatch.delenv("PUSH_PORT", raising=False)
    s = _fresh_settings(monkeypatch, {})
    assert s.push_port == 10750


def test_push_and_mcp_hosts_default_to_loopback(monkeypatch):
    # #116: under hostNetwork a 0.0.0.0 bind exposes these on the LAN
    # where a blank token is unauthenticated. They only ever serve in-pod
    # callers over loopback, so the default must stay 127.0.0.1.
    monkeypatch.delenv("PUSH_HOST", raising=False)
    monkeypatch.delenv("MCP_HOST", raising=False)
    s = _fresh_settings(monkeypatch, {})
    assert s.push_host == "127.0.0.1"
    assert s.mcp_host == "127.0.0.1"


def test_push_and_mcp_hosts_overridable(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        {"PUSH_HOST": "0.0.0.0", "MCP_HOST": "0.0.0.0"},
    )
    assert s.push_host == "0.0.0.0"
    assert s.mcp_host == "0.0.0.0"


def test_engine_url_defaults_to_facade(monkeypatch):
    monkeypatch.delenv("SOLARIS_ENGINE_URL", raising=False)
    s = _fresh_settings(monkeypatch, {})
    assert s.engine_url == "http://127.0.0.1:8787/ollama"


def test_engine_url_and_token_read(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        {"SOLARIS_ENGINE_URL": "http://127.0.0.1:9999/ollama", "SOLARIS_API_KEY": "k"},
    )
    assert s.engine_url == "http://127.0.0.1:9999/ollama"
    assert s.engine_token == "k"


def test_collision_threshold_defaults_stricter_than_recognition(monkeypatch):
    """The enrolment self-test's profile-vs-profile bar is its own number (#1083).
    Cosine similarity, so stricter = higher = fewer enrolments refused: at the
    recognition threshold a merely similar-sounding household member could not
    enrol at all."""
    monkeypatch.delenv("SOLARIS_SPEAKER_ID_THRESHOLD", raising=False)
    monkeypatch.delenv("SOLARIS_SPEAKER_COLLISION_THRESHOLD", raising=False)
    s = _fresh_settings(monkeypatch, {})
    assert s.speaker_id_threshold == 0.55
    assert s.speaker_collision_threshold == 0.65
    assert s.speaker_collision_threshold > s.speaker_id_threshold


def test_collision_threshold_is_env_overridable(monkeypatch):
    # Tunable on the box without a rebuild — the default is a starting point,
    # not a measured value.
    s = _fresh_settings(monkeypatch, {"SOLARIS_SPEAKER_COLLISION_THRESHOLD": "0.8"})
    assert s.speaker_collision_threshold == 0.8
    # Garbage must not silently become 0.0, which would refuse every enrolment.
    s = _fresh_settings(monkeypatch, {"SOLARIS_SPEAKER_COLLISION_THRESHOLD": "nope"})
    assert s.speaker_collision_threshold == 0.65


def test_collision_threshold_moves_independently(monkeypatch):
    """Tuning the recognition threshold must not drag the collision bar with it —
    they answer different questions."""
    s = _fresh_settings(monkeypatch, {"SOLARIS_SPEAKER_ID_THRESHOLD": "0.7"})
    assert s.speaker_id_threshold == 0.7
    assert s.speaker_collision_threshold == 0.65


def test_match_margin_is_its_own_knob(monkeypatch):
    """A third, independent number (#1084): how far ahead of the runner-up
    resident a match must be before a turn is attributed. Tunable on the box —
    the default is a chosen starting point, not a measured one."""
    monkeypatch.delenv("SOLARIS_SPEAKER_MATCH_MARGIN", raising=False)
    assert _fresh_settings(monkeypatch, {}).speaker_match_margin == 0.10

    s = _fresh_settings(monkeypatch, {"SOLARIS_SPEAKER_MATCH_MARGIN": "0.2"})
    assert s.speaker_match_margin == 0.2
    assert s.speaker_id_threshold == 0.55
    assert s.speaker_collision_threshold == 0.65

    # Garbage must not silently become 0.0 and switch the rule off.
    s = _fresh_settings(monkeypatch, {"SOLARIS_SPEAKER_MATCH_MARGIN": "nope"})
    assert s.speaker_match_margin == 0.10


def test_settings_has_single_engine_url_no_admin_gateway(monkeypatch):
    # Voice routes to the household profile only: residents speak to Solaris,
    # never the admin persona. The gatekeeper carries exactly one engine URL
    # and has no admin field, so a voice turn can never reach the admin
    # profile.
    s = _fresh_settings(monkeypatch, {})
    fields = set(type(s).__dataclass_fields__)
    assert not any("admin" in name for name in fields)
