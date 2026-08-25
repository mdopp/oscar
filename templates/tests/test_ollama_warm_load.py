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

# The tags the household actually ships today (templates/ollama/variables.json
# OLLAMA_DEFAULT_MODEL + OLLAMA_EXTRA_MODELS, templates/solaris/variables.json
# FAST_MODEL), with their box-measured sizes. The warm-load order must be
# proven against THESE, not a retired tag set (#1217).
FAST_TAG = "gemma4:e4b"
THOROUGH_TAG = "gemma4:12b"
TAGS_PAYLOAD = {
    "models": [
        {"name": THOROUGH_TAG, "size": 8_400_000_000},
        {"name": FAST_TAG, "size": 3_300_000_000},
        {"name": "nomic-embed-text:latest", "size": 274_000_000},
    ]
}


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


class _Resp:
    status = 200

    def __init__(self, payload: object = None):
        self._payload = payload if payload is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_warm_load_posts_one_token_generate(pd, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append((req.full_url, json.loads(req.data)))
        return _Resp()

    monkeypatch.setattr(pd.urllib.request, "urlopen", fake_urlopen)
    assert pd.warm_load_model("http://127.0.0.1:11434", FAST_TAG) is True
    url, body = calls[0]
    assert url.endswith("/api/generate")
    assert body["model"] == FAST_TAG
    assert body["options"]["num_predict"] == 1


def test_warm_load_fails_soft(pd, monkeypatch):
    def boom(req, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(pd.urllib.request, "urlopen", boom)
    assert pd.warm_load_model("http://127.0.0.1:11434", FAST_TAG) is False


def _fake_ollama(monkeypatch, pd, tags_payload, warmed):
    def fake_urlopen(req, timeout=0):
        if req.full_url.endswith("/api/tags"):
            if tags_payload is None:
                raise OSError("down")
            return _Resp(tags_payload)
        warmed.append(json.loads(req.data)["model"])
        return _Resp()

    monkeypatch.setattr(pd.urllib.request, "urlopen", fake_urlopen)


def test_warm_load_order_is_fast_model_first(pd, monkeypatch):
    """The load-bearing one (solarisbay#340, box-measured): with the small
    fast model resident the 12b load co-exists, but 12b-first makes the
    subsequent small load evict it."""
    warmed: list[str] = []
    _fake_ollama(monkeypatch, pd, TAGS_PAYLOAD, warmed)
    order = pd.warm_load_chat_models("http://x", [THOROUGH_TAG])
    assert warmed == [FAST_TAG, THOROUGH_TAG]
    assert order == warmed


def test_warm_load_falls_back_to_env_list_fast_first(pd, monkeypatch):
    """/api/tags unreachable → the env list is used verbatim, and main passes
    it extras-before-primary (OLLAMA_EXTRA_MODELS carries FAST_MODEL)."""
    warmed: list[str] = []
    _fake_ollama(monkeypatch, pd, None, warmed)
    assert pd.warm_load_chat_models("http://x", [FAST_TAG, THOROUGH_TAG, FAST_TAG]) == [
        FAST_TAG,
        THOROUGH_TAG,
    ]
    assert warmed == [FAST_TAG, THOROUGH_TAG]


def test_main_warms_after_pulls(pd):
    src = (TEMPLATES / "ollama" / "post-deploy.py").read_text(encoding="utf-8")
    assert src.index("def main") < src.index("warm_load_chat_models(ollama_url,")
    # Ground truth = locally installed tags (solarisbay#339); env list only as
    # fallback, extras (FAST_MODEL) before the primary tag.
    assert "(*extra_models, model)" in src


def test_local_chat_tags_skips_embed_models_and_sorts_small_first(pd, monkeypatch):
    monkeypatch.setattr(
        pd.urllib.request, "urlopen", lambda req, timeout=0: _Resp(TAGS_PAYLOAD)
    )
    assert pd.local_chat_tags("http://x") == [FAST_TAG, THOROUGH_TAG]


def test_local_chat_tags_fails_soft(pd, monkeypatch):
    def boom(req, timeout=0):
        raise OSError("down")

    monkeypatch.setattr(pd.urllib.request, "urlopen", boom)
    assert pd.local_chat_tags("http://x") == []
