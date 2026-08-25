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
    assert src.index("def main") < src.index("warm_load_order(warm_tags, fast_model)")


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
