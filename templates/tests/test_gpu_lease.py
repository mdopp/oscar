"""The whole-card GPU lease (#1320): what `acquire` stops and what `release`
gives back.

The unit list is the load-bearing part — a missing `llama.service` leaves
Solaris' own 3.9 GB server loaded and the 26B/Qwen run then OOMs on a card
measured full at 15.0 of 16.4 GB. The ordering matters just as much: the lease
file is written before the stop and removed after the model answers again, so
there is no moment when the card is gone and nothing knows it.
"""

from __future__ import annotations

import importlib.util
import json
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
    return _load("llama_pd_lease", TEMPLATES / "llama" / "post-deploy.py")


@pytest.fixture
def systemctl_calls(pd, monkeypatch):
    """Record `(verb, units)` instead of touching the box's units."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        pd, "systemctl", lambda verb, units: bool(calls.append((verb, units))) or True
    )
    return calls


def _lease(tmp_path, pd) -> pathlib.Path:
    return tmp_path / "solarisbay" / pd.LEASE_FILE


def test_leased_units_cover_voice_ollama_and_solaris_own_server(pd):
    assert set(pd.LEASED_UNITS) == {
        "ollama.service",
        "solaris-whisper.service",
        "solaris-whisper-batch.service",
        "solaris-tts.service",
        "solaris-wakeword-trainer.service",
        "llama.service",
    }


def test_acquire_stops_exactly_the_leased_units(pd, tmp_path, systemctl_calls):
    assert pd.lease_acquire(str(tmp_path), "coder") == 0
    assert systemctl_calls == [("stop", pd.LEASED_UNITS)]
    written = json.loads(_lease(tmp_path, pd).read_text())
    assert written["holder"] == "coder"
    assert written["since"] > 0


def test_acquire_claims_before_it_stops(pd, tmp_path, monkeypatch):
    held_when_stopping: list[bool] = []
    monkeypatch.setattr(
        pd,
        "systemctl",
        lambda verb, units: (
            bool(held_when_stopping.append(_lease(tmp_path, pd).exists())) or True
        ),
    )
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0
    assert held_when_stopping == [True]


def test_acquire_refuses_a_card_someone_else_holds(pd, tmp_path, systemctl_calls):
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0
    systemctl_calls.clear()
    assert pd.lease_acquire(str(tmp_path), "coder") == 1
    assert systemctl_calls == []
    assert pd.read_lease(str(tmp_path))["holder"] == "foundry"


def test_acquire_is_idempotent_for_the_same_holder(pd, tmp_path, systemctl_calls):
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0


def test_release_starts_everything_and_clears_only_once_warm(
    pd, tmp_path, monkeypatch, systemctl_calls
):
    pd.lease_acquire(str(tmp_path), "foundry")
    systemctl_calls.clear()
    held_while_loading: list[bool] = []
    monkeypatch.setattr(
        pd,
        "wait_for_ready",
        lambda url, deadline_sec: (
            bool(held_while_loading.append(_lease(tmp_path, pd).exists())) or True
        ),
    )
    monkeypatch.setattr(pd, "speculative_active", lambda url: True)

    assert pd.lease_release(str(tmp_path), "11435") == 0
    assert systemctl_calls == [("start", pd.LEASED_UNITS)]
    # The lease still stood while the model was loading — a resident asking
    # during those ~38 s gets the honest sentence, not a connection error.
    assert held_while_loading == [True]
    assert not _lease(tmp_path, pd).exists()


def test_release_clears_the_lease_even_when_the_model_never_comes_back(
    pd, tmp_path, monkeypatch, systemctl_calls
):
    """A stuck llama-server must not mute Solaris forever — staying "busy"
    after the holder has left is the worse lie."""
    pd.lease_acquire(str(tmp_path), "foundry")
    monkeypatch.setattr(pd, "wait_for_ready", lambda url, deadline_sec: False)
    assert pd.lease_release(str(tmp_path), "11435") == 1
    assert not _lease(tmp_path, pd).exists()


def test_lease_file_sits_where_the_chat_pod_mounts_it(pd):
    # templates/solaris/template.yml mounts {{DATA_DIR}}/solarisbay at
    # /var/lib/solaris, which is where solaris_chat.gpu_lease looks for it.
    assert (
        pd.lease_file("/mnt/data/stacks")
        == "/mnt/data/stacks/solarisbay/gpu_lease.json"
    )
