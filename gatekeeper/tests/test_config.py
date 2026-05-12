"""Tests for the env-driven Settings dataclass."""

from __future__ import annotations

import importlib

import pytest


def _fresh_settings(monkeypatch, env: dict[str, str]):
    """Reload the config module so Settings.from_env() picks up new env."""
    import gatekeeper.config as cfg_mod

    for key in list(env.keys()):
        monkeypatch.setenv(key, env[key])
    importlib.reload(cfg_mod)
    return cfg_mod.Settings.from_env()


def test_voice_pe_devices_parses_json_map(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        {
            "HERMES_URL": "http://hermes:8000",
            "VOICE_PE_DEVICES": '{"office": "tcp://10.0.0.1:10700", "bedroom": "tcp://10.0.0.2:10700"}',
        },
    )
    assert s.voice_pe_devices == {
        "office": "tcp://10.0.0.1:10700",
        "bedroom": "tcp://10.0.0.2:10700",
    }


def test_voice_pe_devices_invalid_json_is_empty(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        {"HERMES_URL": "http://hermes:8000", "VOICE_PE_DEVICES": "not-json"},
    )
    assert s.voice_pe_devices == {}


def test_voice_pe_devices_empty_when_unset(monkeypatch):
    monkeypatch.delenv("VOICE_PE_DEVICES", raising=False)
    s = _fresh_settings(monkeypatch, {"HERMES_URL": "http://hermes:8000"})
    assert s.voice_pe_devices == {}


def test_push_port_default(monkeypatch):
    monkeypatch.delenv("PUSH_PORT", raising=False)
    s = _fresh_settings(monkeypatch, {"HERMES_URL": "http://hermes:8000"})
    assert s.push_port == 10750


def test_hermes_url_is_required(monkeypatch):
    monkeypatch.delenv("HERMES_URL", raising=False)
    import gatekeeper.config as cfg_mod

    with pytest.raises(KeyError):
        cfg_mod.Settings.from_env()
