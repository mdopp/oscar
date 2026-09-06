"""Tests for the ollama residency config, wired on both the variables.json
defaults and the GPU `.container` Quadlet render path: OLLAMA_MAX_LOADED_MODELS
(#268) and the service-wide OLLAMA_KEEP_ALIVE fallback, which is short since
#1264 because it governs every consumer's models, not only the household's.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
OLLAMA = TEMPLATES / "ollama"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def variables():
    return json.loads((OLLAMA / "variables.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def ollama_pd():
    return _load("ollama_post_deploy", OLLAMA / "post-deploy.py")


# ── variables.json defaults ───────────────────────────────────────────────


def test_max_loaded_models_default_is_two_for_e4b(variables):
    # The household fast model is now gemma4:e4b (box-measured 3.3 GB @ 32k, vs
    # e2b's 2.0 GB). e4b + 12b + nomic ≈ 16.2 GB resident — past the 16 GB GPU's
    # safe edge — so the cap drops to 2: e4b + embed stay hot, 12b loads on
    # demand for the Gründlich mode and crons (#396). The cap is GLOBAL — the
    # embed model IS counted (box-measured 2026-06-10).
    assert variables["OLLAMA_MAX_LOADED_MODELS"]["default"] == "2"


def test_context_length_default_is_32k(variables):
    # The 131k window existed only because the Hermes-era base prompt grew to
    # ~25k tokens; the Solaris Engine prompt is ≤3k, and the KV saving (12b ≈8.95
    # vs ≈10.3 GB) is what fits the three-model trio on the 16 GB GPU.
    assert variables["OLLAMA_CONTEXT_LENGTH"]["default"] == "32768"


def test_keep_alive_default_is_short(variables):
    # #1264 — the service-wide default governs EVERY model any consumer on the
    # box loads, so it is short: forgetting a per-request keep_alive costs
    # minutes of squatted VRAM, not a day. The long hold on our own fast model
    # is asked for explicitly by the warm call (see FAST_MODEL_KEEP_ALIVE).
    assert variables["OLLAMA_KEEP_ALIVE"]["default"] == "15m"


# ── template.yml env wiring ───────────────────────────────────────────────


def test_template_yml_wires_max_loaded_models():
    tmpl = (OLLAMA / "template.yml").read_text(encoding="utf-8")
    assert "name: OLLAMA_MAX_LOADED_MODELS" in tmpl
    assert "{{OLLAMA_MAX_LOADED_MODELS}}" in tmpl
    assert "name: OLLAMA_KEEP_ALIVE" in tmpl


# ── GPU .container render path parity ─────────────────────────────────────


def test_gpu_unit_carries_max_loaded_models_and_the_short_keep_alive(
    ollama_pd, variables, monkeypatch
):
    # No env set → the render path falls back to the new defaults, and the GPU
    # path must fall back to the SAME value variables.json declares — the .kube
    # env never reaches the GPU runtime, so a drift here is invisible on the box.
    monkeypatch.delenv("OLLAMA_MAX_LOADED_MODELS", raising=False)
    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    unit = ollama_pd.render_gpu_container_unit("11434", "/mnt/data/stacks")
    assert "Environment=OLLAMA_MAX_LOADED_MODELS=2" in unit
    assert "Environment=OLLAMA_KEEP_ALIVE=15m" in unit
    assert (
        f"Environment=OLLAMA_KEEP_ALIVE={variables['OLLAMA_KEEP_ALIVE']['default']}\n"
        in unit
    )
    assert "Environment=OLLAMA_CONTEXT_LENGTH=32768" in unit


def test_gpu_unit_honors_env_overrides(ollama_pd, monkeypatch):
    monkeypatch.setenv("OLLAMA_MAX_LOADED_MODELS", "2")
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "60m")
    unit = ollama_pd.render_gpu_container_unit("11434", "/mnt/data/stacks")
    assert "Environment=OLLAMA_MAX_LOADED_MODELS=2" in unit
    assert "Environment=OLLAMA_KEEP_ALIVE=60m" in unit


# ── #322: activation idempotency — content match must NOT skip activation ──


@pytest.fixture
def fallback_box(ollama_pd, tmp_path, monkeypatch):
    """Drive install_gpu_quadlet_fallback against a fake on-disk systemd
    dir + recorded systemctl calls. Returns a handle exposing the
    rendered unit, file paths, the recorded systemctl argv list, and
    knobs for the SourcePath probe so a test can simulate "service is
    sourced from .kube vs .container"."""
    systemd_dir = tmp_path / ".config" / "containers" / "systemd"
    systemd_dir.mkdir(parents=True)
    kube_path = systemd_dir / "ollama.kube"
    container_path = systemd_dir / "ollama.container"

    real_exists = os.path.exists
    monkeypatch.setattr(
        ollama_pd.os.path,
        "expanduser",
        lambda p: p.replace("~", str(tmp_path)),
    )
    # CDI registered so we clear the early gate; everything else is real.
    monkeypatch.setattr(
        ollama_pd.os.path,
        "exists",
        lambda p: (p == "/etc/cdi/nvidia.yaml") or real_exists(p),
    )

    calls: list[list[str]] = []
    state = {"source": "ollama.kube"}  # SourcePath the probe reports

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        out = ""
        if argv[:4] == ["systemctl", "--user", "show", "-p"]:
            out = f"SourcePath=/x/y/{state['source']}"

        class _R:
            returncode = 0
            stdout = out
            stderr = ""

        return _R()

    monkeypatch.setattr(ollama_pd.subprocess, "run", fake_run)

    class Handle:
        def __init__(self):
            self.kube_path = kube_path
            self.container_path = container_path
            self.calls = calls
            self.state = state
            self.unit = ollama_pd.render_gpu_container_unit("11434", "/mnt/data/stacks")

        def run(self):
            return ollama_pd.install_gpu_quadlet_fallback("11434", "/mnt/data/stacks")

    return Handle()


def _did_activate(calls):
    return ["systemctl", "--user", "start", "ollama.service"] in calls


def test_content_match_but_kube_recreated_reactivates(fallback_box):
    """Regression #322: `.container` byte-identical to what we'd render,
    but `podman kube play` re-created `ollama.kube` and the live service
    is sourced from `.kube` → the function MUST stop→remove-kube→
    daemon-reload→start, not take the skip path."""
    h = fallback_box
    h.container_path.write_text(h.unit)  # matches rendered exactly
    h.kube_path.write_text("[KubeUnit]\nYaml=ollama.yml\n")  # re-created on redeploy
    h.state["source"] = "ollama.kube"  # live service is CPU-sourced

    assert h.run() is True
    assert _did_activate(h.calls), "must re-activate the GPU unit, not skip"
    assert not h.kube_path.exists(), "CPU ollama.kube must be removed"
    assert ["systemctl", "--user", "daemon-reload"] in h.calls
    # File already matched → no redundant rewrite, but content is correct.
    assert h.container_path.read_text() == h.unit


def test_gpu_already_live_is_noop(fallback_box):
    """The genuinely-correct path stays a no-op: `.container` matches, it is
    the live source, and no `.kube` lingers → do NOT flap Ollama."""
    h = fallback_box
    h.container_path.write_text(h.unit)
    # no ollama.kube on disk
    h.state["source"] = "ollama.container"

    assert h.run() is True
    assert not _did_activate(h.calls), "already GPU-live → must not restart"
    assert ["systemctl", "--user", "stop", "ollama.service"] not in h.calls


def test_container_source_but_kube_lingers_reactivates(fallback_box):
    """Even when the live source already reads `.container`, a lingering
    `ollama.kube` on disk must be cleaned up + re-activated (Quadlet would
    otherwise regenerate a conflicting CPU service on the next reload)."""
    h = fallback_box
    h.container_path.write_text(h.unit)
    h.kube_path.write_text("[KubeUnit]\nYaml=ollama.yml\n")
    h.state["source"] = "ollama.container"

    assert h.run() is True
    assert _did_activate(h.calls)
    assert not h.kube_path.exists()
