"""Tests for stamp_vapid_public_key — re-asserting VAPID_PUBLIC_KEY when a
template reinstall blanks it (#1147).

A plain `solaris` reinstall resets the non-secret VAPID_PUBLIC_KEY text var to
its blank default while the secret VAPID_PRIVATE_KEY survives (servicebay#2531),
so the browser can no longer subscribe to Web Push. The public half is the
private half's own public point, so the post-deploy re-derives it and stamps it
back into the deployed solaris.yml. Never a generated keypair: a rotated key
silently unsubscribes every device the household already registered.

The EC maths runs inside the chat container (the host python has no
`cryptography`), so the probe is faked here and the box-real keypair below is a
genuine P-256 pair — PUBLIC_KEY is the uncompressed point of PRIVATE_PEM.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
REPO = TEMPLATES.parent

PRIVATE_PEM = """-----BEGIN EC PRIVATE KEY-----
MHcCAQEEIIsXUontHy8U6szIjr3mAkcizidKzlFpvBmSCmjXV+rNoAoGCCqGSM49
AwEHoUQDQgAE/4Gu4vq8/3wJT3DM3FjPt+bsw1k2kJ2U5ioeqLb8A+lnsAaxjhri
9YmOh2pR5r0eF12zR8E7/QTfohOG6Dyung==
-----END EC PRIVATE KEY-----
"""
PUBLIC_KEY = "BP-BruL6vP98CU9wzNxYz7fm7MNZNpCdlOYqHqi2_APpZ7AGsY4a4vWJjodqUea9Hhdds0fBO_0E36IThug8rp4"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("solaris_pd_vapid", TEMPLATES / "solaris" / "post-deploy.py")


def _pod_yml(public_value: str) -> str:
    return (
        "apiVersion: v1\n"
        "kind: Pod\n"
        "spec:\n"
        "  containers:\n"
        "  - name: chat\n"
        "    env:\n"
        "    - name: VAPID_PUBLIC_KEY\n"
        f"      value: {public_value}\n"
        "    - name: VAPID_PRIVATE_KEY\n"
        '      value: "-----BEGIN EC PRIVATE KEY-----\\nMHc…\\n"\n'
        "    - name: VAPID_SUBJECT\n"
        '      value: ""\n'
    )


@pytest.fixture
def wired(pd, tmp_path, monkeypatch):
    """A pod yml with an empty VAPID_PUBLIC_KEY, expanduser pinned at it."""
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_pod_yml('""'), encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    return pod_yml


def _fake_probe(pd, monkeypatch, stdout: str) -> list[list[str]]:
    """Replace the podman call with one returning `stdout`; record the argvs."""
    calls: list[list[str]] = []

    def fake(args, timeout=15):
        calls.append(args)
        return stdout

    monkeypatch.setattr(pd, "_podman_out", fake)
    return calls


def test_empty_public_key_is_stamped_from_the_private_half(pd, wired, monkeypatch):
    calls = _fake_probe(pd, monkeypatch, PUBLIC_KEY)

    assert pd.stamp_vapid_public_key() == PUBLIC_KEY

    assert f'- name: VAPID_PUBLIC_KEY\n      value: "{PUBLIC_KEY}"' in wired.read_text()
    # Derived inside the chat container from ITS OWN private key — nothing about
    # the value comes from the post-deploy's own environment.
    assert calls == [["exec", pd.CHAT_CONTAINER, "python", "-c", pd.VAPID_DERIVE_PROBE]]
    assert "VAPID_PRIVATE_KEY" in pd.VAPID_DERIVE_PROBE


def test_an_already_set_public_key_is_left_alone(pd, wired, monkeypatch):
    wired.write_text(_pod_yml('"BExistingKeyFromTheOperator"'), encoding="utf-8")
    before = wired.read_text()
    calls = _fake_probe(pd, monkeypatch, PUBLIC_KEY)

    assert pd.stamp_vapid_public_key() is None

    # Untouched file AND no probe: nothing to converge, so nothing to restart for.
    assert wired.read_text() == before
    assert calls == []


def test_a_stamped_key_reconverges_without_rewriting(pd, wired, monkeypatch):
    _fake_probe(pd, monkeypatch, PUBLIC_KEY)
    assert pd.stamp_vapid_public_key() == PUBLIC_KEY
    stamped = wired.read_text()

    # Second deploy: the key it would derive is already in place.
    assert pd.stamp_vapid_public_key() is None
    assert wired.read_text() == stamped


@pytest.mark.parametrize(
    "probe_output",
    [
        "",  # no VAPID_PRIVATE_KEY, or the chat container is down
        "Traceback (most recent call last):",  # the probe itself blew up
        "not-a-key",  # garbage private key -> the engine derive returns ""
    ],
)
def test_an_unreadable_private_key_stamps_nothing(pd, wired, monkeypatch, probe_output):
    before = wired.read_text()
    _fake_probe(pd, monkeypatch, probe_output)

    assert pd.stamp_vapid_public_key() is None

    # Never a blank, never an invented key — an empty value keeps Web Push off,
    # a wrong one would unsubscribe every registered device.
    assert wired.read_text() == before


def test_a_pruned_env_entry_is_reported_not_invented(pd, tmp_path, monkeypatch):
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text("spec:\n  containers:\n  - name: chat\n", encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    _fake_probe(pd, monkeypatch, PUBLIC_KEY)

    assert pd.stamp_vapid_public_key() is None
    assert pod_yml.read_text() == "spec:\n  containers:\n  - name: chat\n"


def test_the_probe_targets_the_engine_helper_that_actually_exists():
    """The probe calls into the engine's own derivation (#801); a rename there
    would otherwise only surface as an empty key on the box."""
    config = REPO / "solaris-chat" / "src" / "solaris_chat" / "config.py"
    assert "def _derive_vapid_public_key(" in config.read_text(encoding="utf-8")
