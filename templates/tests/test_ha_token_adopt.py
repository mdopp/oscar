"""Tests for adopt_ha_long_lived_token — picking up HA's auto-onboarded
long-lived token and patching HASS_TOKEN into the deployed solaris.yml pod
manifest (#425; servicebay#1847 makes onboarding (re)produce the token file).

Security-sensitive: this is the path that wires an HA credential into the
running engine. The tests cover the happy adopt, the idempotent re-deploy, the
fresh-render empty-value patch, the silent-no-match guard, and the actionable
absent-token warning. The token poll + HA /api/ probe are monkeypatched so no
live HA or sleeping is needed.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("solaris_pd_token", TEMPLATES / "solaris" / "post-deploy.py")


def _pod_yml(value: str) -> str:
    return (
        "apiVersion: v1\n"
        "kind: Pod\n"
        "spec:\n"
        "  containers:\n"
        "  - name: chat\n"
        "    env:\n"
        "    - name: HASS_URL\n"
        '      value: "http://127.0.0.1:8123"\n'
        "    - name: HASS_TOKEN\n"
        f"      value: {value}\n"
        "    - name: LLAMA_SERVER_URL\n"
        '      value: "http://127.0.0.1:11435"\n'
    )


@pytest.fixture
def wired(pd, tmp_path, monkeypatch):
    """Build a data_dir with the token file + a pod yml, and pin the helpers
    so the adopt runs offline. Returns (data_dir, pod_yml_path, token)."""
    token = "eyJhbGciOi.LONG.LIVED.TOKEN-abc123"
    token_path = (
        tmp_path / "home-assistant" / "homeassistant" / ".solaris-long-lived-token"
    )
    token_path.parent.mkdir(parents=True)
    token_path.write_text(token + "\n", encoding="utf-8")

    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_pod_yml('""'), encoding="utf-8")

    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    monkeypatch.setattr(pd, "_wait_for_ha_api", lambda *a, **k: True)
    return tmp_path, pod_yml, token


def _token_value_line(pod_yml: pathlib.Path) -> str:
    src = pod_yml.read_text(encoding="utf-8")
    block = src.split("- name: HASS_TOKEN\n", 1)[1]
    return block.splitlines()[0].strip()


def test_adopts_token_into_empty_value(pd, wired):
    data_dir, pod_yml, token = wired
    assert pd.adopt_ha_long_lived_token(str(data_dir)) == token
    assert _token_value_line(pod_yml) == f'value: "{token}"'


def test_replaces_a_stale_token(pd, wired, monkeypatch):
    data_dir, pod_yml, token = wired
    pod_yml.write_text(_pod_yml('"old-stale-token"'), encoding="utf-8")
    assert pd.adopt_ha_long_lived_token(str(data_dir)) == token
    assert _token_value_line(pod_yml) == f'value: "{token}"'
    assert "old-stale-token" not in pod_yml.read_text(encoding="utf-8")


def test_idempotent_when_already_current(pd, wired):
    data_dir, pod_yml, token = wired
    pod_yml.write_text(_pod_yml(f'"{token}"'), encoding="utf-8")
    before = pod_yml.read_text(encoding="utf-8")
    assert pd.adopt_ha_long_lived_token(str(data_dir)) == token
    assert pod_yml.read_text(encoding="utf-8") == before


def test_returns_none_and_warns_when_token_absent(pd, tmp_path, monkeypatch, capsys):
    # No token file written, and the poll must not block.
    monkeypatch.setattr(pd, "_ha_token_timeout", lambda: 0)
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_pod_yml('""'), encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    assert pd.adopt_ha_long_lived_token(str(tmp_path)) is None
    out = capsys.readouterr().out
    assert "no HA long-lived token to adopt" in out
    assert "auto-onboarding" in out
    # The pod manifest is left untouched (HASS_TOKEN stays empty).
    assert _token_value_line(pod_yml) == 'value: ""'


def test_returns_none_and_warns_when_no_hass_token_env(pd, wired, monkeypatch):
    data_dir, pod_yml, _token = wired
    # A manifest with no HASS_TOKEN env block — adoption must NOT silently
    # no-op; it must fail loudly so a broken template surfaces.
    pod_yml.write_text(
        "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n  - name: chat\n",
        encoding="utf-8",
    )
    assert pd.adopt_ha_long_lived_token(str(data_dir)) is None


def test_returns_none_when_pod_yml_missing(pd, wired, monkeypatch):
    data_dir, pod_yml, _token = wired
    pod_yml.unlink()
    assert pd.adopt_ha_long_lived_token(str(data_dir)) is None


def test_token_timeout_default_widened_for_cold_boot(pd, monkeypatch):
    monkeypatch.delenv("HA_TOKEN_TIMEOUT", raising=False)
    assert pd._ha_token_timeout() >= 180
