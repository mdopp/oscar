"""Tests for runtime context-window derivation (#235, #1332): override-wins,
the live value read off llama-server's `/props`, and the fallback chain when
llama-server is unreachable."""

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
    # If ops pinned a value it must win — llama-server is not even consulted.
    called = False

    async def _boom(_url):
        nonlocal called
        called = True
        return 999999

    monkeypatch.setattr(context, "_llama_loaded_context", _boom)
    window, source = await context.derive_context_window("http://x", override=131072)
    assert (window, source) == (131072, "override")
    assert called is False


async def test_derives_the_live_loaded_context(monkeypatch):
    async def _loaded(_url):
        return 32768

    monkeypatch.setattr(context, "_llama_loaded_context", _loaded)
    window, source = await context.derive_context_window("http://x", override=None)
    assert (window, source) == (32768, "llama_server")


async def test_fallback_to_llama_context_length_env(monkeypatch):
    async def _none(_url):
        return None

    monkeypatch.setattr(context, "_llama_loaded_context", _none)
    monkeypatch.setenv("LLAMA_CONTEXT_LENGTH", "16384")
    window, source = await context.derive_context_window("http://x", override=None)
    assert (window, source) == (16384, "llama_context_length_env")


async def test_fallback_to_static_default_when_the_server_is_unreachable(monkeypatch):
    async def _raises(_url):
        raise OSError("connection refused")

    monkeypatch.setattr(context, "_llama_loaded_context", _raises)
    monkeypatch.delenv("LLAMA_CONTEXT_LENGTH", raising=False)
    window, source = await context.derive_context_window("http://x", override=None)
    assert (window, source) == (context.STATIC_DEFAULT, "static_default")
    assert window == 32768


# --- _llama_loaded_context picks n_ctx off /props --------------------------


async def test_loaded_context_reads_the_per_slot_window(monkeypatch):
    """`default_generation_settings.n_ctx` is what a single turn can hold —
    the number compaction has to key off. The top-level `n_ctx` on some builds
    is the whole KV cache across slots, which would over-cap."""
    _patch_props(
        monkeypatch,
        {"default_generation_settings": {"n_ctx": 32768}, "n_ctx": 131072},
    )
    assert await context._llama_loaded_context("http://x") == 32768


async def test_loaded_context_falls_back_to_the_top_level_field(monkeypatch):
    _patch_props(monkeypatch, {"n_ctx": 65536})
    assert await context._llama_loaded_context("http://x") == 65536


async def test_loaded_context_none_when_the_field_is_missing(monkeypatch):
    _patch_props(monkeypatch, {"model_path": "/models/gemma-4-E4B-it-Q4_0.gguf"})
    assert await context._llama_loaded_context("http://x") is None


async def test_no_llama_url_is_not_a_socket_call(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not open a session without a url")

    monkeypatch.setattr(context.aiohttp, "ClientSession", _boom)
    assert await context._llama_loaded_context("") is None


async def test_a_leased_profile_moves_the_window(monkeypatch):
    """A foundry/coding lease reloads llama-server on a different model at a
    different window (#1319/#1325); the derived cap has to follow it without a
    restart, which is the whole point of re-deriving from the live server."""
    _patch_props(monkeypatch, {"default_generation_settings": {"n_ctx": 81920}})
    window, source = await context.derive_context_window("http://x", override=None)
    assert (window, source) == (81920, "llama_server")


# --- ContextWindow holder refresh ------------------------------------------


async def test_refresh_updates_live_value(monkeypatch):
    async def _loaded(_url):
        return 65536

    monkeypatch.setattr(context, "_llama_loaded_context", _loaded)
    cw = context.ContextWindow("http://x", override=None, initial=32768)
    await cw.refresh()
    assert cw.value == 65536


async def test_refresh_noop_under_override(monkeypatch):
    called = False

    async def _boom(_url):
        nonlocal called
        called = True
        return 1

    monkeypatch.setattr(context, "_llama_loaded_context", _boom)
    cw = context.ContextWindow("http://x", override=131072, initial=131072)
    await cw.refresh()
    assert cw.value == 131072 and called is False
    assert cw.is_override is True


# --- helpers ---------------------------------------------------------------


def _patch_props(monkeypatch, payload):
    """Stub aiohttp so /props returns `payload` without a real socket."""

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
