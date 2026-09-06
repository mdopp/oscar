"""Who may reach llama-server (#1344).

llama-server ships no authentication, so the endpoint is on-box only — but
"on-box" is two addresses, not one. A pod on host networking (the Solaris
Engine, post-deploy, the health check) uses `127.0.0.1`; an isolated pod
(claude-dev's `pi`) can only use `host.containers.internal`, which rootless
podman/pasta maps to the host's LAN address rather than to loopback. A
loopback bind therefore serves the first and silently starves the second.

ADR-0007 Decision 3 resolves that by binding wider and closing the LAN one
layer down: `LLAMA_PORT` carries `blockLanAccess: true`, and ServiceBay drops
the port on physical interfaces while leaving `lo` — where the pasta-proxied
pod path lands — alone. Both halves are load-bearing and neither shows up as
a failure when it is missing: without the wide bind `pi`'s model picker is
merely empty, and without the flag an unauthenticated model server answers
the whole LAN. Hence these assertions.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
LLAMA = TEMPLATES / "llama"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("llama_pd_pod_access", LLAMA / "post-deploy.py")


@pytest.fixture(scope="module")
def template_text() -> str:
    return (LLAMA / "template.yml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def variables() -> dict:
    return json.loads((LLAMA / "variables.json").read_text(encoding="utf-8"))


def test_template_binds_every_interface(template_text):
    assert '- "--host"\n    - "0.0.0.0"' in template_text
    assert '- "127.0.0.1"' not in template_text


def test_post_deploy_bind_mirrors_the_template(pd):
    args = pd.server_args("11435", "/models")
    assert args[:2] == ["--host", "0.0.0.0"]


def test_every_leased_profile_keeps_the_bind(pd, monkeypatch):
    """A coding/foundry lease re-renders the unit from the same argv builder —
    a profile that dropped back to loopback would take `pi` away for the window.
    """
    monkeypatch.setattr(pd, "env", lambda name, default="": default)
    for name in pd.LEASE_PROFILES:
        args = pd.server_args("11435", "/models", pd.LEASE_PROFILES[name])
        assert args[:2] == ["--host", "0.0.0.0"], name


def test_port_blocks_lan_access(variables):
    assert variables["LLAMA_PORT"]["blockLanAccess"] is True


def test_port_is_declared_so_the_firewall_rule_has_a_target(template_text):
    assert 'servicebay.ports: "{{LLAMA_PORT}}/tcp"' in template_text


def test_health_check_stays_on_loopback(template_text):
    """post-deploy and the check run in the host netns (ADR 0007), and loopback
    is the path the Engine uses — probing the LAN address would test the
    firewall rule instead of the server."""
    assert "url: http://127.0.0.1:{{LLAMA_PORT}}/health" in template_text


def test_schema_version_bumped_for_the_new_bind(template_text):
    assert 'servicebay.schema-version: "2"' in template_text


def test_readme_names_all_three_audiences():
    readme = (LLAMA / "README.md").read_text(encoding="utf-8")
    assert "host.containers.internal" in readme
    assert "blockLanAccess" in readme
