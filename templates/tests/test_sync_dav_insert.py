"""Tests for the DAV-credential wiring into the engine pod env (#997 / #1010).

On the box the rendered `solaris.yml` does NOT contain the SYNC_DAV_* /
DEADLINES_SYNC_URL_BASE env entries at all (the template.yml block isn't
reaching the rendered pod), so the old PATCH-only pass found nothing (`n==0`)
and the DAV sync stayed dormant. `_patched_sync_dav_yaml` now INSERTS the three
entries after the `- name: CALDAV_URL` anchor when they're absent, and PATCHES
them in place when present. Both paths must produce valid YAML (a bad insert
crash-loops the pod) and be idempotent.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

# PyYAML isn't installed in the templates CI env; the yaml-validity assertions
# run where it IS present (locally + the pod image at runtime). Skip the module
# when yaml is absent rather than fail collection.
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
    return _load("solaris_pd_sync_dav", TEMPLATES / "solaris" / "post-deploy.py")


# A pod snippet with the CALDAV_URL anchor but NO SYNC_DAV_* entries — the shape
# seen on the box. Two-space list indent, value-line indent = name + 2.
_POD_NO_SYNC = (
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

# A pod snippet where SYNC_DAV_PASSWORD already exists (with empty values) — the
# render-with-block case, to exercise the patch-in-place path.
_POD_WITH_SYNC = (
    "apiVersion: v1\n"
    "kind: Pod\n"
    "spec:\n"
    "  containers:\n"
    "  - name: chat\n"
    "    env:\n"
    "    - name: CALDAV_URL\n"
    '      value: "http://127.0.0.1:5232/dav/"\n'
    "    - name: SYNC_DAV_USERNAME\n"
    '      value: ""\n'
    "    - name: SYNC_DAV_PASSWORD\n"
    '      value: ""\n'
    "    - name: DEADLINES_SYNC_URL_BASE\n"
    '      value: ""\n'
)


def _env_map(text: str) -> dict[str, str]:
    doc = yaml.safe_load(text)
    env = doc["spec"]["containers"][0]["env"]
    return {e["name"]: e["value"] for e in env}


def test_insert_when_absent(pd, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    new, n = pd._patched_sync_dav_yaml(_POD_NO_SYNC, "the-managed-pw")
    assert n == 1
    assert new != _POD_NO_SYNC
    # Valid YAML (a bad insert crash-loops the pod).
    env = _env_map(new)
    assert env["SYNC_DAV_USERNAME"] == "solaris"
    assert env["SYNC_DAV_PASSWORD"] == "the-managed-pw"
    assert env["DEADLINES_SYNC_URL_BASE"] == pd.DEADLINES_SYNC_URL_BASE_DEFAULT
    # Inserted immediately after the CALDAV_URL anchor, same indentation.
    assert (
        "    - name: CALDAV_URL\n"
        '      value: "http://127.0.0.1:5232/dav/"\n'
        "    - name: SYNC_DAV_USERNAME\n"
        '      value: "solaris"\n'
        "    - name: SYNC_DAV_PASSWORD\n"
        '      value: "the-managed-pw"\n'
        "    - name: DEADLINES_SYNC_URL_BASE\n"
        f'      value: "{pd.DEADLINES_SYNC_URL_BASE_DEFAULT}"\n'
    ) in new


def test_insert_is_idempotent(pd, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    once, _ = pd._patched_sync_dav_yaml(_POD_NO_SYNC, "pw")
    twice, n = pd._patched_sync_dav_yaml(once, "pw")
    assert twice == once  # a re-deploy re-patches to the same values, no drift
    assert n == 1


def test_patch_in_place_when_present(pd, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    new, n = pd._patched_sync_dav_yaml(_POD_WITH_SYNC, "new-pw")
    assert n == 1
    env = _env_map(new)
    assert env["SYNC_DAV_USERNAME"] == "solaris"
    assert env["SYNC_DAV_PASSWORD"] == "new-pw"
    assert env["DEADLINES_SYNC_URL_BASE"] == pd.DEADLINES_SYNC_URL_BASE_DEFAULT
    # No duplicate entries were inserted.
    assert new.count("- name: SYNC_DAV_PASSWORD") == 1
    assert new.count("- name: DEADLINES_SYNC_URL_BASE") == 1


def test_url_base_env_override(pd, monkeypatch):
    monkeypatch.setenv("DEADLINES_SYNC_URL_BASE", "http://radicale.local:5232/")
    new, _ = pd._patched_sync_dav_yaml(_POD_NO_SYNC, "pw")
    assert _env_map(new)["DEADLINES_SYNC_URL_BASE"] == "http://radicale.local:5232/"


def test_no_anchor_leaves_file_untouched(pd, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    no_anchor = "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n  - name: chat\n"
    new, n = pd._patched_sync_dav_yaml(no_anchor, "pw")
    assert n == 0
    assert new == no_anchor


# ── CardDAV write targets (#996) ─────────────────────────────────────────────


def test_contacts_urls_inserted_when_absent(pd, monkeypatch):
    monkeypatch.delenv("CONTACTS_SYNC_URL_BASE", raising=False)
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    new, n = pd._patched_contacts_sync_yaml(_POD_NO_SYNC, "mdopp")
    assert n == 1
    env = _env_map(new)
    root = pd.DEADLINES_SYNC_URL_BASE_DEFAULT.rstrip("/")
    assert env["CONTACTS_SYNC_URL_BASE"] == root + "/"
    # The provider book is the primary resident's OWN address book, not a
    # service-account collection the resident cannot read.
    assert env["CONTACTS_SYNC_URL"] == f"{root}/mdopp/solaris-contacts/"


def test_contacts_urls_are_idempotent(pd, monkeypatch):
    monkeypatch.delenv("CONTACTS_SYNC_URL_BASE", raising=False)
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    once, _ = pd._patched_contacts_sync_yaml(_POD_NO_SYNC, "mdopp")
    twice, n = pd._patched_contacts_sync_yaml(once, "mdopp")
    assert twice == once and n == 1  # a re-deploy re-stamps the same values
    assert once.count("- name: CONTACTS_SYNC_URL\n") == 1


def test_contacts_urls_report_failure_without_an_anchor(pd, monkeypatch):
    monkeypatch.delenv("CONTACTS_SYNC_URL_BASE", raising=False)
    no_anchor = "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n  - name: chat\n"
    new, n = pd._patched_contacts_sync_yaml(no_anchor, "mdopp")
    assert n == 0
    assert new == no_anchor


def test_apply_writes_and_returns_true_on_insert(pd, tmp_path, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_POD_NO_SYNC, encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    assert pd.apply_sync_dav_credential_to_engine("mp") is True
    env = _env_map(pod_yml.read_text(encoding="utf-8"))
    assert env["SYNC_DAV_PASSWORD"] == "mp"
