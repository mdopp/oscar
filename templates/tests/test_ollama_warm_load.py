"""Tests for the post-deploy model warm-load: every (re)deploy restarts the
ollama unit and drops all residents — the first voice turn after a deploy
must not pay the cold reload (box-observed 2026-06-12: 9-66 s intent stage,
PE gives up)."""

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
    return _load("ollama_pd_warm", TEMPLATES / "ollama" / "post-deploy.py")


def test_warm_load_posts_one_token_generate(pd, monkeypatch):
    calls = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        calls.append((req.full_url, json.loads(req.data)))
        return _Resp()

    monkeypatch.setattr(pd.urllib.request, "urlopen", fake_urlopen)
    assert pd.warm_load_model("http://127.0.0.1:11434", "gemma4:e4b") is True
    url, body = calls[0]
    assert url.endswith("/api/generate")
    assert body["model"] == "gemma4:e4b"
    assert body["options"]["num_predict"] == 1


def test_warm_load_fails_soft(pd, monkeypatch):
    def boom(req, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(pd.urllib.request, "urlopen", boom)
    assert pd.warm_load_model("http://127.0.0.1:11434", "gemma4:e4b") is False


def test_main_warms_after_pulls(pd):
    src = (TEMPLATES / "ollama" / "post-deploy.py").read_text(encoding="utf-8")
    assert src.index("def main") < src.index(
        "warm_installed_models(ollama_url, fast_model, [*extra_models, model])"
    )


def _tags_resp(models):
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"models": models}).encode()

    return lambda req, timeout=0: _Resp()


def test_local_chat_tags_skips_embed_models(pd, monkeypatch):
    monkeypatch.setattr(
        pd.urllib.request,
        "urlopen",
        _tags_resp(
            [
                {"name": "gemma4:12b"},
                {"name": "gemma4:e4b"},
                {"name": "nomic-embed-text:latest"},
            ]
        ),
    )
    assert pd.local_chat_tags("http://x") == ["gemma4:12b", "gemma4:e4b"]


# The tags actually shipped today, with the sizes /api/tags really reports
# (box-measured, solarisbay#1217). They are MISLEADING: e4b's per-layer-
# embedding weights are the larger download (9.6 GB) but the smaller resident
# footprint (~3.3 GB vs 12b's ~8.4 GB). Any ordering derived from `size` —
# ascending or descending — gets one of the two cases below wrong; only
# ordering by the configured fast-model identity passes both.
_REAL_SIZES = [
    {"name": "gemma4:e4b", "size": 9608350718},
    {"name": "gemma4:12b", "size": 7556508396},
]
_INVERTED_SIZES = [
    {"name": "gemma4:e4b", "size": 7556508396},
    {"name": "gemma4:12b", "size": 9608350718},
]


@pytest.mark.parametrize("models", [_REAL_SIZES, _INVERTED_SIZES])
def test_fast_model_warms_first_whatever_api_tags_reports_as_size(
    pd, monkeypatch, models
):
    monkeypatch.setattr(pd.urllib.request, "urlopen", _tags_resp(models))
    tags = pd.local_chat_tags("http://x")
    assert pd.warm_load_order(tags, "gemma4:e4b") == ["gemma4:e4b", "gemma4:12b"]


def test_warm_load_order_puts_the_rest_after_the_fast_model(pd):
    order = pd.warm_load_order(
        ["gemma4:12b", "gemma4:e4b", "qwen3:8b", "gemma4:e4b"], "gemma4:e4b"
    )
    assert order == ["gemma4:e4b", "gemma4:12b", "qwen3:8b"]


def test_warm_load_order_matches_an_untagged_fast_model(pd):
    assert pd.warm_load_order(["gemma4:12b", "mymodel:latest"], "mymodel") == [
        "mymodel:latest",
        "gemma4:12b",
    ]


def test_warm_load_order_falls_back_alphabetically_and_logs(pd, capsys):
    order = pd.warm_load_order(["gemma4:12b", "qwen3:8b"], "gemma4:e4b")
    assert order == ["gemma4:12b", "qwen3:8b"]
    logged = json.loads(capsys.readouterr().out.strip())
    assert logged["level"] == "warn"
    assert logged["args"]["fast_model"] == "gemma4:e4b"


def test_local_chat_tags_fails_soft(pd, monkeypatch):
    def boom(req, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(pd.urllib.request, "urlopen", boom)
    assert pd.local_chat_tags("http://x") == []


# --- the re-warm unit: ANY ollama start warms, not just a deploy (#1236) ----


def test_warm_service_is_pulled_in_by_every_ollama_start(pd):
    unit = pd.render_warm_service("/mnt/data/x.py", "11434", "gemma4:e4b", 600)
    # WantedBy is what makes an auto-update / reboot / manual restart warm too.
    assert "WantedBy=ollama.service" in unit
    assert "After=ollama.service" in unit
    # A hanging or failing warm must never keep ollama from coming up.
    assert "Requires=" not in unit and "BindsTo=" not in unit and "PartOf=" not in unit
    assert "Type=oneshot" in unit
    assert "Environment=OLLAMA_WARM_ONLY=1" in unit
    assert "Environment=OLLAMA_FAST_MODEL=gemma4:e4b" in unit
    assert "ExecStart=/usr/bin/env python3 /mnt/data/x.py" in unit


def test_install_warm_unit_copies_self_and_enables(pd, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runs = []

    def fake_run(cmd, **kw):
        runs.append(cmd)

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    monkeypatch.setattr(pd.subprocess, "run", fake_run)
    assert pd.install_warm_unit(str(tmp_path / "data"), "11434", "gemma4:e4b", 600)

    script = tmp_path / "data" / "solarisbay" / pd.WARM_SCRIPT
    # The copy IS the post-deploy, so the warm can never drift from
    # warm_load_order() — there is only ever one implementation.
    assert "def warm_load_order" in script.read_text(encoding="utf-8")
    unit = tmp_path / "home" / ".config" / "systemd" / "user" / "ollama-warm.service"
    assert f"ExecStart=/usr/bin/env python3 {script}" in unit.read_text(
        encoding="utf-8"
    )
    assert ["systemctl", "--user", "enable", "ollama-warm.service"] in runs


def test_install_warm_unit_fails_soft(pd, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def boom(*a, **k):
        raise OSError("read-only fs")

    monkeypatch.setattr(pd.os, "makedirs", boom)
    assert (
        pd.install_warm_unit(str(tmp_path / "data"), "11434", "gemma4:e4b", 600)
        is False
    )


def test_warm_only_warms_the_fast_model_first(pd, monkeypatch):
    monkeypatch.setenv("OLLAMA_WARM_ONLY", "1")
    monkeypatch.setenv("OLLAMA_FAST_MODEL", "gemma4:e4b")
    monkeypatch.setattr(pd, "wait_for_ready", lambda url, deadline_sec: True)
    monkeypatch.setattr(pd, "local_chat_tags", lambda url: ["gemma4:12b", "gemma4:e4b"])
    warmed = []
    monkeypatch.setattr(
        pd, "warm_load_model", lambda url, m, **k: warmed.append(m) or True
    )
    assert pd.main() == 0
    assert warmed == ["gemma4:e4b", "gemma4:12b"]


def test_warm_only_exits_zero_when_ollama_never_answers(pd, monkeypatch):
    monkeypatch.setenv("OLLAMA_WARM_ONLY", "1")
    monkeypatch.setattr(pd, "wait_for_ready", lambda url, deadline_sec: False)

    def boom(*a, **k):
        raise AssertionError("must not warm against a dead API")

    monkeypatch.setattr(pd, "warm_load_model", boom)
    assert pd.main() == 0
