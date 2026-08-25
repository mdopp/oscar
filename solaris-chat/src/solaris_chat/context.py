"""Derive the effective context window from the live Ollama active model (#235).

The compaction cap (#210) must match the window the model is *actually loaded
at*, not a hardcoded number. The box drifted: the chat had `CONTEXT_WINDOW=131072`
while Ollama loaded gemma4:12b at 32768, so compaction never fired in time.

The most reliable runtime signal is Ollama's `GET /api/ps`: each running model
carries `context_length`, the window it is currently loaded with (= the shared
`OLLAMA_CONTEXT_LENGTH`, bounded by the model's native context). That adapts per
model automatically and reflects exactly what the model can hold.

Fallback chain (first that resolves wins; never crash):
  1. explicit `CONTEXT_WINDOW` override — a positive integer means ops pinned it.
  2. live Ollama `/api/ps` `context_length` of the loaded CHAT model —
     embedding models are skipped, their 2048 is not a chat window (#1237).
  3. `OLLAMA_CONTEXT_LENGTH` env (the value Ollama loads with) when reachable.
  4. a safe static default (32768) when Ollama is unreachable.
"""

from __future__ import annotations

import asyncio
import os

import aiohttp

from solaris_chat.logging import log

# Safe static window when nothing else resolves (matches the ollama template's
# OLLAMA_CONTEXT_LENGTH default, #214) — never crash, never over-cap.
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


def _is_embed_entry(m: dict) -> bool:
    """An `/api/ps` entry for an embedding model, which never serves a turn.

    Embedding models carry a tiny native context (nomic-embed-text: 2048), so
    letting one set the window pins Solaris below its own 7,967-token base
    prompt (#1237). The ollama template's `local_chat_tags()` already excludes
    them by name; this is the same filter on the other endpoint.
    """
    name = str(m.get("name") or m.get("model") or "")
    return "embed" in name


async def _ollama_loaded_context(ollama_url: str) -> int | None:
    """The context window the active CHAT model is loaded with, from `/api/ps`.

    Returns the largest running chat model's `context_length` (the field Ollama
    exposes per loaded model), or None when no chat model is loaded /
    unreachable — the caller then falls back to the configured window rather
    than to whatever a resident embedding model happens to be loaded at.
    """
    url = f"{ollama_url.rstrip('/')}/api/ps"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.get(url) as resp:
            if resp.status >= 400:
                return None
            body = await resp.json()
    models = body.get("models") if isinstance(body, dict) else None
    if not isinstance(models, list):
        return None
    ctxs = [
        m["context_length"]
        for m in models
        if isinstance(m, dict)
        and isinstance(m.get("context_length"), int)
        and not _is_embed_entry(m)
    ]
    return max(ctxs) if ctxs else None


def _env_context_length() -> int | None:
    """`OLLAMA_CONTEXT_LENGTH` — the window Ollama loads models with."""
    raw = os.environ.get("OLLAMA_CONTEXT_LENGTH")
    if not raw:
        return None
    try:
        n = int(raw.strip())
    except ValueError:
        return None
    return n if n > 0 else None


async def derive_context_window(
    ollama_url: str, override: int | None
) -> tuple[int, str]:
    """Resolve the effective context window and the source it came from.

    `override` is the parsed `CONTEXT_WINDOW` operator pin (None => auto).
    Returns `(window, source)`; never raises — Ollama being down degrades to the
    env value or the static default.
    """
    if override is not None:
        return override, "override"

    try:
        loaded = await _ollama_loaded_context(ollama_url)
    except Exception as e:  # noqa: BLE001 — any Ollama failure must degrade, not crash
        log.warn("chat.context.ollama_unreachable", error=str(e))
        loaded = None
    if loaded is not None:
        return loaded, "ollama"

    env_len = _env_context_length()
    if env_len is not None:
        return env_len, "ollama_context_length_env"

    return STATIC_DEFAULT, "static_default"


# How often to re-derive while running, so a model switch (different native
# context / OLLAMA_CONTEXT_LENGTH) takes effect without a restart.
REFRESH_INTERVAL_S = 300


class ContextWindow:
    """Live, refreshable effective context window the proxy reads.

    Holds a single int that `whoami` reports and compaction keys off. A
    background task re-derives it from Ollama so a model change adapts at
    runtime; an explicit override pins it (the refresh is a no-op then).
    """

    def __init__(self, ollama_url: str, override: int | None, initial: int):
        self._ollama_url = ollama_url
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
        window, source = await derive_context_window(self._ollama_url, self._override)
        if window != self.value:
            log.info(
                "chat.context.changed", window=window, source=source, was=self.value
            )
        self.value = window

    async def refresh_loop(self) -> None:
        # Override never changes => no point polling Ollama.
        if self._override is not None:
            return
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_S)
            try:
                await self.refresh()
            except Exception as e:  # noqa: BLE001 — a refresh must never kill the loop
                log.warn("chat.context.refresh_failed", error=str(e))


async def build_context_window(ollama_url: str, override: int | None) -> ContextWindow:
    """Resolve the window once and return a refreshable holder, logging the source."""
    window, source = await derive_context_window(ollama_url, override)
    log.info("chat.context.resolved", window=window, source=source)
    return ContextWindow(ollama_url, override, window)
