"""Thin streaming client for Ollama's native `/api/chat`.

The native endpoint (not `/v1/chat/completions`) is deliberate: it honors
`think` for reasoning control and streams `message.thinking` separately from
`message.content`, so the engine never parses `<thinking>` tags out of the
answer. No `num_ctx` is sent — the box-wide OLLAMA_CONTEXT_LENGTH rules, and
a per-request value would reload the model with a different KV size.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

# How long a warm call asks Ollama to hold the household fast model. Mirrors
# `FAST_MODEL_KEEP_ALIVE` in templates/ollama/post-deploy.py — the two warms
# share a shape, so they share the hold; a test pins the value on both sides.
FAST_MODEL_KEEP_ALIVE = "24h"


class OllamaError(Exception):
    """Raised when Ollama returns a non-2xx response."""


@dataclass
class ChatResult:
    """One completed `/api/chat` call, deltas folded together."""

    content: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_s: float = 0.0
    ttft_s: float = 0.0


class OllamaChat:
    def __init__(self, base_url: str, timeout: float = 300.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout, sock_read=timeout)

    async def tags(self) -> list[dict[str, Any]]:
        """`GET /api/tags` — the locally pulled models with their on-disk
        `size` (bytes). Returns the `models` list (empty when unreachable)."""
        return await self._get_models("/api/tags")

    async def ps(self) -> list[dict[str, Any]]:
        """`GET /api/ps` — the loaded models with `size`/`size_vram` (bytes).
        Returns the `models` list (empty when nothing is loaded/unreachable)."""
        return await self._get_models("/api/ps")

    async def _get_models(self, path: str) -> list[dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(f"{self._base_url}{path}") as resp:
                if resp.status >= 400:
                    raise OllamaError(f"ollama {path} {resp.status}")
                body = await resp.json()
        models = body.get("models") if isinstance(body, dict) else None
        return models if isinstance(models, list) else []

    async def pull(self, model: str):
        """Stream `POST /api/pull` progress for a model tag/URL.

        `model` is whatever `ollama pull` accepts — a registry tag
        (`gemma4:12b`) or a Hugging Face GGUF repo (`hf.co/<repo>:<quant>`),
        which Ollama resolves natively, so no separate HF download path is
        needed. Yields each progress chunk as a dict (`status`/`completed`/
        `total`/`digest`) verbatim; the caller relays it to the panel.
        """
        body = {"model": model, "stream": True}
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(f"{self._base_url}/api/pull", json=body) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:500]
                    raise OllamaError(f"ollama /api/pull {resp.status}: {detail}")
                async for raw in resp.content:
                    line = raw.strip()
                    if not line:
                        continue
                    yield json.loads(line)

    async def warm(self, model: str) -> bool:
        """Load `model` into VRAM with a 1-token generate — the same warm shape
        `ollama-warm` uses on the box (#1236), so there is one warm, not two.

        Carries an explicit `keep_alive`: the service-wide default is short so a
        neighbour that sends none can't squat the GPU for a day (#1264), which
        makes the long hold on our fast model this call's job to ask for.

        Best-effort: a warm that fails leaves the next real turn cold, which is
        slow, not broken, so nothing here raises.
        """
        body = {
            "model": model,
            "prompt": "Hi",
            "stream": False,
            "keep_alive": FAST_MODEL_KEEP_ALIVE,
            "options": {"num_predict": 1},
        }
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as client:
                async with client.post(
                    f"{self._base_url}/api/generate", json=body
                ) as resp:
                    return resp.status < 400
        except Exception:  # noqa: BLE001 — any transport failure is a cold model
            return False

    async def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        """`POST /api/embed` — embed a batch of texts, one vector per input.

        Returns the `embeddings` list (a `list[float]` per input, in order).
        Non-2xx raises `OllamaError` with truncated detail, like `pull()`.
        """
        body = {"model": model, "input": inputs}
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(f"{self._base_url}/api/embed", json=body) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:500]
                    raise OllamaError(f"ollama /api/embed {resp.status}: {detail}")
                data = await resp.json()
        return data.get("embeddings") or []

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        think: bool = False,
        options: dict[str, Any] | None = None,
        tool_choice: str = "",
    ):
        """Yield `("delta", str)` / `("thinking", str)` per chunk, then one
        final `("done", ChatResult)`. Closing the generator aborts the HTTP
        request — that is what actually interrupts the model's generation.

        `tool_choice` is accepted for signature parity with the llama-server
        backend and ignored: `/api/chat` has no forced-tool field, so a routed
        turn on this backend is carried by the code-enforced store instead.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": think,
        }
        if tools:
            body["tools"] = tools
        if options:
            body["options"] = options
        result = ChatResult()
        t0 = time.monotonic()
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(f"{self._base_url}/api/chat", json=body) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:500]
                    raise OllamaError(f"ollama /api/chat {resp.status}: {detail}")
                async for raw in resp.content:
                    line = raw.strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    msg = chunk.get("message") or {}
                    delta = msg.get("content") or ""
                    thinking = msg.get("thinking") or ""
                    if (
                        delta or thinking or msg.get("tool_calls")
                    ) and not result.ttft_s:
                        result.ttft_s = time.monotonic() - t0
                    if delta:
                        result.content += delta
                        yield "delta", delta
                    if thinking:
                        result.thinking += thinking
                        yield "thinking", thinking
                    for tc in msg.get("tool_calls") or []:
                        result.tool_calls.append(tc)
                    if chunk.get("done"):
                        result.prompt_tokens = chunk.get("prompt_eval_count") or 0
                        result.completion_tokens = chunk.get("eval_count") or 0
        result.wall_s = time.monotonic() - t0
        yield "done", result
