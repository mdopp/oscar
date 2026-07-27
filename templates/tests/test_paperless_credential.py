"""Tests for wiring PAPERLESS_URL/PAPERLESS_TOKEN into the engine pod env and the
DRF token mint (#1034, option A).

Paperless is SSO-only on the box (Authelia remote-user, no native login), so the
#931 ingest handoff no-op'd: the engine carried no PAPERLESS_URL/TOKEN. The
managed-admin converge mints the admin's DRF API token and stamps the two env
entries into the deployed solaris.yml — inserting after the `- name: CALDAV_URL`
anchor when absent (the renderer prunes empty env) and patching in place when
present. Both paths must produce valid YAML (a bad insert crash-loops the pod)
and be idempotent. The token mint is exercised with a mocked HTTP POST.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

yaml = pytest.importorskip("yaml")

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
    return _load("solaris_pd_paperless", TEMPLATES / "solaris" / "post-deploy.py")


# A pod snippet with the CALDAV_URL anchor but no PAPERLESS_* entries.
_POD = (
    "apiVersion: v1\n"
    "kind: Pod\n"
    "spec:\n"
    "  containers:\n"
    "  - name: chat\n"
    "    env:\n"
    "    - name: CALDAV_URL\n"
    '      value: "http://127.0.0.1:5232/dav/"\n'
    "    - name: CALDAV_USERNAME\n"
    '      value: "resident"\n'
)

# A pod snippet where PAPERLESS_URL/TOKEN already exist (empty) — patch path.
_POD_WITH_PAPERLESS = (
    "apiVersion: v1\n"
    "kind: Pod\n"
    "spec:\n"
    "  containers:\n"
    "  - name: chat\n"
    "    env:\n"
    "    - name: CALDAV_URL\n"
    '      value: "http://127.0.0.1:5232/dav/"\n'
    "    - name: PAPERLESS_URL\n"
    '      value: ""\n'
    "    - name: PAPERLESS_TOKEN\n"
    '      value: ""\n'
)


def _env_map(text: str) -> dict[str, str]:
    doc = yaml.safe_load(text)
    env = doc["spec"]["containers"][0]["env"]
    return {e["name"]: e["value"] for e in env}


def _setup(pd, tmp_path, monkeypatch):
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_POD, encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    return pod_yml


def test_insert_when_absent(pd):
    new, n = pd._patch_or_insert_paperless_env(_POD, "http://127.0.0.1:8000", "tok-1")
    assert n == 1
    env = _env_map(new)  # valid YAML — a bad insert crash-loops the pod
    assert env["PAPERLESS_URL"] == "http://127.0.0.1:8000"
    assert env["PAPERLESS_TOKEN"] == "tok-1"


def test_insert_is_idempotent(pd):
    once, _ = pd._patch_or_insert_paperless_env(_POD, "http://127.0.0.1:8000", "tok")
    twice, n = pd._patch_or_insert_paperless_env(once, "http://127.0.0.1:8000", "tok")
    assert twice == once
    assert n == 1
    assert twice.count("- name: PAPERLESS_TOKEN") == 1


def test_patch_in_place_when_present(pd):
    new, n = pd._patch_or_insert_paperless_env(
        _POD_WITH_PAPERLESS, "http://127.0.0.1:8000", "new-tok"
    )
    assert n == 1
    env = _env_map(new)
    assert env["PAPERLESS_URL"] == "http://127.0.0.1:8000"
    assert env["PAPERLESS_TOKEN"] == "new-tok"
    assert new.count("- name: PAPERLESS_TOKEN") == 1
    assert new.count("- name: PAPERLESS_URL") == 1


def test_no_anchor_leaves_file_untouched(pd):
    no_anchor = "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n  - name: chat\n"
    new, n = pd._patch_or_insert_paperless_env(no_anchor, "http://127.0.0.1:8000", "t")
    assert n == 0
    assert new == no_anchor


def test_apply_writes_and_returns_true_on_insert(pd, tmp_path, monkeypatch):
    pod_yml = _setup(pd, tmp_path, monkeypatch)
    assert (
        pd.apply_paperless_credential_to_engine(
            str(tmp_path), "http://127.0.0.1:8000", "tok-x"
        )
        is True
    )
    env = _env_map(pod_yml.read_text(encoding="utf-8"))
    assert env["PAPERLESS_TOKEN"] == "tok-x"
    assert env["PAPERLESS_URL"] == "http://127.0.0.1:8000"


def test_mint_token_success(pd, monkeypatch):
    calls = {}

    def fake_post(url, payload, timeout=10.0):
        calls["url"] = url
        calls["payload"] = payload
        return 200, {"token": "minted-abc123"}

    monkeypatch.setattr(pd, "post_json", fake_post)
    token = pd.mint_paperless_token("http://127.0.0.1:8000", "solaris", "pw")
    assert token == "minted-abc123"
    assert calls["url"] == "http://127.0.0.1:8000/api/token/"
    assert calls["payload"] == {"username": "solaris", "password": "pw"}


def test_mint_token_unreachable_returns_none(pd, monkeypatch):
    monkeypatch.setattr(pd, "post_json", lambda *a, **k: (0, None))
    assert pd.mint_paperless_token("http://127.0.0.1:8000", "solaris", "pw") is None


def test_converge_mints_persists_and_stamps(pd, tmp_path, monkeypatch):
    monkeypatch.setattr(pd, "post_json", lambda *a, **k: (200, {"token": "T0K"}))
    monkeypatch.delenv("PAPERLESS_URL", raising=False)
    monkeypatch.delenv("PAPERLESS_ADMIN_USER", raising=False)
    pod_yml = _setup(pd, tmp_path, monkeypatch)
    assert pd.converge_paperless_credential(str(tmp_path)) is True
    # Password + token persisted host-side so a pruning render is survivable.
    pw = tmp_path / "solarisbay" / pd.PAPERLESS_ADMIN_PASSWORD_FILE
    tok = tmp_path / "solarisbay" / pd.PAPERLESS_TOKEN_FILE
    assert pw.read_text(encoding="utf-8").strip()
    assert tok.read_text(encoding="utf-8").strip() == "T0K"
    env = _env_map(pod_yml.read_text(encoding="utf-8"))
    assert env["PAPERLESS_TOKEN"] == "T0K"
    assert env["PAPERLESS_URL"] == pd.PAPERLESS_URL_DEFAULT


def test_converge_falls_back_to_persisted_token_when_mint_fails(
    pd, tmp_path, monkeypatch
):
    # A prior deploy persisted a token; paperless is momentarily unreachable.
    (tmp_path / "solarisbay").mkdir()
    (tmp_path / "solarisbay" / pd.PAPERLESS_TOKEN_FILE).write_text(
        "old-tok\n", encoding="utf-8"
    )
    monkeypatch.setattr(pd, "post_json", lambda *a, **k: (0, None))
    monkeypatch.delenv("PAPERLESS_URL", raising=False)
    pod_yml = _setup(pd, tmp_path, monkeypatch)
    assert pd.converge_paperless_credential(str(tmp_path)) is True
    assert _env_map(pod_yml.read_text(encoding="utf-8"))["PAPERLESS_TOKEN"] == "old-tok"


def test_converge_noop_when_paperless_absent(pd, tmp_path, monkeypatch):
    # No persisted token + mint fails (paperless not installed) → clean no-op.
    monkeypatch.setattr(pd, "post_json", lambda *a, **k: (0, None))
    monkeypatch.delenv("PAPERLESS_URL", raising=False)
    pod_yml = _setup(pd, tmp_path, monkeypatch)
    before = pod_yml.read_text(encoding="utf-8")
    assert pd.converge_paperless_credential(str(tmp_path)) is False
    assert pod_yml.read_text(encoding="utf-8") == before
