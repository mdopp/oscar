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
    assert pd.warm_load_model("http://127.0.0.1:11434", "gemma4:e2b") is True
    url, body = calls[0]
    assert url.endswith("/api/generate")
    assert body["model"] == "gemma4:e2b"
    assert body["options"]["num_predict"] == 1


def test_warm_load_fails_soft(pd, monkeypatch):
    def boom(req, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(pd.urllib.request, "urlopen", boom)
    assert pd.warm_load_model("http://127.0.0.1:11434", "gemma4:e2b") is False


def test_main_warms_after_pulls(pd):
    src = (TEMPLATES / "ollama" / "post-deploy.py").read_text(encoding="utf-8")
    assert src.index("def main") < src.index("warm_load_model(ollama_url, warm)")
    # Ground truth = locally installed tags (solarisbay#339); env list only as
    # fallback. Small-first is load-bearing (solarisbay#340): with e2b resident
    # a 12b load co-exists; the reverse order evicts 12b.
    assert "local_chat_tags(ollama_url)" in src
    assert "(*extra_models, model)" in src
    assert '"e2b" not in t' in src


def test_local_chat_tags_skips_embed_models(pd, monkeypatch):
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {
                    "models": [
                        {"name": "gemma4:12b"},
                        {"name": "gemma4:e2b"},
                        {"name": "nomic-embed-text:latest"},
                    ]
                }
            ).encode()

    monkeypatch.setattr(pd.urllib.request, "urlopen", lambda req, timeout=0: _Resp())
    assert pd.local_chat_tags("http://x") == ["gemma4:12b", "gemma4:e2b"]


def test_local_chat_tags_fails_soft(pd, monkeypatch):
    def boom(req, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(pd.urllib.request, "urlopen", boom)
    assert pd.local_chat_tags("http://x") == []
