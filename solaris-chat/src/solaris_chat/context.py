"""Derive the effective context window from the live llama-server (#235, #1332).

The compaction cap (#210) must match the window the model is *actually loaded
at*, not a hardcoded number. The box drifted once already: the chat had
`CONTEXT_WINDOW=131072` while the model was loaded at 32768, so compaction never
fired in time.

The runtime signal is llama-server's `GET /props`: `default_generation_settings.
n_ctx` is the window the loaded model is serving. Unlike Ollama's `/api/ps` it
needs no filtering — llama-server holds exactly one model, and the embeddings
server is a separate instance on its own port. It also adapts on its own to a
leased profile (#1319/#1325), which loads a different model at a different
window.

Fallback chain (first that resolves wins; never crash):
  1. explicit `CONTEXT_WINDOW` override — a positive integer means ops pinned it.
  2. live llama-server `/props` `n_ctx`.
  3. `LLAMA_CONTEXT_LENGTH` env (the value the template loads with) when set.
  4. a safe static default (32768) when llama-server is unreachable.
"""

from __future__ import annotations

import asyncio
import os

import aiohttp

from solaris_chat.logging import log

# Safe static window when nothing else resolves (matches the llama template's
# LLAMA_CONTEXT_LENGTH default) — never crash, never over-cap.
STATIC_DEFAULT = 32768


def parse_override(value: str | None) -> int | None:
    """An explicit operator override, or None to auto-derive.

    Empty / "auto" / unparsable / non-positive => auto (None). A positive int
    => the operator pinned the window and it wins over any derived value.
    """
    if value is None:
        return None
    text = value.strip().lower()
    if text in ("", "auto"):
        return None
    try:
        n = int(text)
    except ValueError:
        return None
    return n if n > 0 else None


async def _llama_loaded_context(llama_server_url: str) -> int | None:
    """The context window llama-server has the model loaded at, from `/props`.

    `default_generation_settings.n_ctx` is the per-slot window — what a turn
    can actually hold — which is the number compaction has to key off. Returns
    None when unreachable or the field is absent.
    """
    if not llama_server_url:
        return None
    url = f"{llama_server_url.rstrip('/')}/props"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.get(url) as resp:
            if resp.status >= 400:
                return None
            body = await resp.json()
    if not isinstance(body, dict):
        return None
    settings = body.get("default_generation_settings")
    n_ctx = settings.get("n_ctx") if isinstance(settings, dict) else None
    if not isinstance(n_ctx, int):
        n_ctx = body.get("n_ctx")
    return n_ctx if isinstance(n_ctx, int) and n_ctx > 0 else None


def _env_context_length() -> int | None:
    """`LLAMA_CONTEXT_LENGTH` — the window llama-server is started with."""
    raw = os.environ.get("LLAMA_CONTEXT_LENGTH")
    if not raw:
        return None
    try:
        n = int(raw.strip())
    except ValueError:
        return None
    return n if n > 0 else None


async def derive_context_window(
    llama_server_url: str, override: int | None
) -> tuple[int, str]:
    """Resolve the effective context window and the source it came from.

    `override` is the parsed `CONTEXT_WINDOW` operator pin (None => auto).
    Returns `(window, source)`; never raises — llama-server being down degrades
    to the env value or the static default.
    """
    if override is not None:
        return override, "override"

    try:
        loaded = await _llama_loaded_context(llama_server_url)
    except Exception as e:  # noqa: BLE001 — any backend failure must degrade, not crash
        log.warn("chat.context.llama_unreachable", error=str(e))
        loaded = None
    if loaded is not None:
        return loaded, "llama_server"

    env_len = _env_context_length()
    if env_len is not None:
        return env_len, "llama_context_length_env"

    return STATIC_DEFAULT, "static_default"


# How often to re-derive while running, so a model switch (a leased profile at a
# different window) takes effect without a restart.
REFRESH_INTERVAL_S = 300


class ContextWindow:
    """Live, refreshable effective context window the proxy reads.

    Holds a single int that `whoami` reports and compaction keys off. A
    background task re-derives it from llama-server so a model change adapts at
    runtime; an explicit override pins it (the refresh is a no-op then).
    """

    def __init__(self, llama_server_url: str, override: int | None, initial: int):
        self._llama_server_url = llama_server_url
        self._override = override
        self.value = initial

    @classmethod
    def static(cls, value: int) -> "ContextWindow":
        """A fixed, non-refreshing holder (tests + a pinned-int call site)."""
        return cls("", value, value)

    @property
    def is_override(self) -> bool:
        return self._override is not None

    async def refresh(self) -> None:
        window, source = await derive_context_window(
            self._llama_server_url, self._override
        )
        if window != self.value:
            log.info(
                "chat.context.changed", window=window, source=source, was=self.value
            )
        self.value = window

    async def refresh_loop(self) -> None:
        # Override never changes => no point polling llama-server.
        if self._override is not None:
            return
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_S)
            try:
                await self.refresh()
            except Exception as e:  # noqa: BLE001 — a refresh must never kill the loop
                log.warn("chat.context.refresh_failed", error=str(e))


async def build_context_window(
    llama_server_url: str, override: int | None
) -> ContextWindow:
    """Resolve the window once and return a refreshable holder, logging the source."""
    window, source = await derive_context_window(llama_server_url, override)
    log.info("chat.context.resolved", window=window, source=source)
    return ContextWindow(llama_server_url, override, window)
