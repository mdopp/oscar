"""Tests for runtime context-window derivation (#235): override-wins, the live
Ollama-derived value, and the fallback chain when Ollama is unreachable."""

from __future__ import annotations

import pytest

from solaris_chat import context


# --- parse_override --------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "  ", "auto", "AUTO", "nan", "0", "-5"])
def test_parse_override_means_auto(raw):
    # Empty / "auto" / unparsable / non-positive => auto-derive (None).
    assert context.parse_override(raw) is None


@pytest.mark.parametrize(("raw", "want"), [("32768", 32768), (" 131072 ", 131072)])
def test_parse_override_positive_int(raw, want):
    assert context.parse_override(raw) == want


# --- derive_context_window fallback chain ----------------------------------


async def test_override_wins_over_everything(monkeypatch):
    # If ops pinned a value it must win — Ollama is not even consulted.
    called = False

    async def _boom(_url):
        nonlocal called
        called = True
        return 999999

    monkeypatch.setattr(context, "_ollama_loaded_context", _boom)
    window, source = await context.derive_context_window("http://x", override=131072)
    assert (window, source) == (131072, "override")
    assert called is False


async def test_derives_live_ollama_loaded_context(monkeypatch):
    async def _loaded(_url):
        return 32768

    monkeypatch.setattr(context, "_ollama_loaded_context", _loaded)
    window, source = await context.derive_context_window("http://x", override=None)
    assert (window, source) == (32768, "ollama")


async def test_fallback_to_ollama_context_length_env(monkeypatch):
    async def _none(_url):
        return None

    monkeypatch.setattr(context, "_ollama_loaded_context", _none)
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "16384")
    window, source = await context.derive_context_window("http://x", override=None)
    assert (window, source) == (16384, "ollama_context_length_env")


async def test_fallback_to_static_default_when_ollama_unreachable(monkeypatch):
    async def _raises(_url):
        raise OSError("connection refused")

    monkeypatch.setattr(context, "_ollama_loaded_context", _raises)
    monkeypatch.delenv("OLLAMA_CONTEXT_LENGTH", raising=False)
    window, source = await context.derive_context_window("http://x", override=None)
    assert (window, source) == (context.STATIC_DEFAULT, "static_default")
    assert window == 32768


# --- _ollama_loaded_context picks the field off /api/ps --------------------


async def test_ollama_loaded_context_reads_running_model(monkeypatch):
    # /api/ps exposes context_length per loaded model — the signal we key off.
    payload = {"models": [{"name": "gemma4:12b", "context_length": 32768}]}
    _patch_ollama_get(monkeypatch, payload)
    assert await context._ollama_loaded_context("http://x") == 32768


async def test_ollama_loaded_context_no_model_loaded(monkeypatch):
    _patch_ollama_get(monkeypatch, {"models": []})
    assert await context._ollama_loaded_context("http://x") is None


async def test_embed_model_alone_does_not_set_the_window(monkeypatch):
    # Box-observed #1237: with only nomic-embed-text resident the window
    # resolved to its 2048 native context — below Solaris's own 7,967-token
    # base prompt. "No chat model loaded" must fall through, not pin 2048.
    _patch_ollama_get(
        monkeypatch,
        {"models": [{"name": "nomic-embed-text:latest", "context_length": 2048}]},
    )
    assert await context._ollama_loaded_context("http://x") is None


async def test_embed_model_never_outvotes_a_resident_chat_model(monkeypatch):
    _patch_ollama_get(
        monkeypatch,
        {
            "models": [
                {"name": "nomic-embed-text:latest", "context_length": 2048},
                {"name": "gemma4:e4b", "context_length": 32768},
            ]
        },
    )
    assert await context._ollama_loaded_context("http://x") == 32768


async def test_only_embed_resident_falls_back_to_the_configured_window(monkeypatch):
    _patch_ollama_get(
        monkeypatch, {"models": [{"model": "nomic-embed-text", "context_length": 2048}]}
    )
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "32768")
    window, source = await context.derive_context_window("http://x", override=None)
    assert (window, source) == (32768, "ollama_context_length_env")


# --- ContextWindow holder refresh ------------------------------------------


async def test_refresh_updates_live_value(monkeypatch):
    async def _loaded(_url):
        return 65536

    monkeypatch.setattr(context, "_ollama_loaded_context", _loaded)
    cw = context.ContextWindow("http://x", override=None, initial=32768)
    await cw.refresh()
    assert cw.value == 65536


async def test_refresh_noop_under_override(monkeypatch):
    called = False

    async def _boom(_url):
        nonlocal called
        called = True
        return 1

    monkeypatch.setattr(context, "_ollama_loaded_context", _boom)
    cw = context.ContextWindow("http://x", override=131072, initial=131072)
    await cw.refresh()
    assert cw.value == 131072 and called is False
    assert cw.is_override is True


# --- helpers ---------------------------------------------------------------


def _patch_ollama_get(monkeypatch, payload):
    """Stub aiohttp so /api/ps returns `payload` without a real socket."""

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return payload

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, _url):
            return _Resp()

    monkeypatch.setattr(context.aiohttp, "ClientSession", _Session)
