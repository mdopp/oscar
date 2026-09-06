"""Streaming client for llama.cpp's `llama-server` OpenAI-compatible API.

The household hot path's backend (#1318). Box-measured 2026-09-04: the same
`gemma4:e4b` weights served by `llama-server` with the official Gemma-4 MTP
drafter answer in 0.30 s where Ollama takes 0.62 s — speculative decoding is
the reason, and Ollama has no draft-model knob.

`stream()` yields `("delta"|"thinking", str)` pairs and one final
`("done", ChatResult)`, so `EngineClient` never sees the wire shape.

Two things this translation layer has to get right (the engine's own history
is still Ollama-shaped — it is what the store persists):

* **`think` is not a request field here.** Ollama's `think: false` has no
  llama-server equivalent; the switch is `chat_template_kwargs`
  `{"enable_thinking": false}`, which stops the canonical Gemma-4 template from
  emitting the `<|think|>` token. llama.cpp otherwise renders the template with
  `enable_thinking = true`, overriding its `default(false)` — box-measured, and
  the whole reason #1317 read as "llama.cpp cannot turn Gemma's thinking off".
  With it on, six of every seven generated tokens are an invisible English
  reasoning trace the resident pays for and never sees.
* **The message shapes differ.** Ollama takes `tool_name` on a tool result,
  tool-call arguments as an object, and images as bare base64 on the message;
  the OpenAI schema wants `tool_call_id`/`name`, arguments as a JSON string,
  and images as `image_url` content parts.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from solaris_chat import gpu_lease
from solaris_chat.logging import log

# Base64 magic prefixes, so an attachment carried as bare base64 (the Ollama
# `images` shape the store persists) gets an honest data-URL media type.
_B64_MEDIA_TYPES = (
    ("iVBORw0KGgo", "image/png"),
    ("/9j/", "image/jpeg"),
    ("R0lGOD", "image/gif"),
    ("UklGR", "image/webp"),
)


class LlamaServerError(Exception):
    """Non-2xx from llama-server — the engine's one "model backend failed"."""


@dataclass
class ChatResult:
    """One completed model call, deltas folded together."""

    content: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_s: float = 0.0
    ttft_s: float = 0.0


def _media_type(b64: str) -> str:
    for prefix, media in _B64_MEDIA_TYPES:
        if b64.startswith(prefix):
            return media
    return "image/jpeg"


def _content_parts(text: str, images: list[str]) -> Any:
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for img in images:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{_media_type(img)};base64,{img}"},
            }
        )
    return parts


def to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the engine's Ollama-shaped history into OpenAI messages.

    Tool results carry no id in the engine's history, so they are paired
    positionally with the ids minted for the tool calls of the assistant
    message right above them — which is the order the loop appends them in.
    """
    out: list[dict[str, Any]] = []
    pending_ids: list[str] = []
    for index, msg in enumerate(messages):
        role = str(msg.get("role") or "")
        content = msg.get("content") or ""
        if role == "assistant" and msg.get("tool_calls"):
            calls = []
            pending_ids = []
            for slot, call in enumerate(msg["tool_calls"]):
                fn = call.get("function") or {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                call_id = str(call.get("id") or f"call_{index}_{slot}")
                pending_ids.append(call_id)
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": str(fn.get("name") or ""),
                            "arguments": args,
                        },
                    }
                )
            out.append({"role": "assistant", "content": content, "tool_calls": calls})
        elif role == "tool":
            entry: dict[str, Any] = {"role": "tool", "content": content}
            if msg.get("tool_name"):
                entry["name"] = str(msg["tool_name"])
            if pending_ids:
                entry["tool_call_id"] = pending_ids.pop(0)
            out.append(entry)
        else:
            images = [i for i in (msg.get("images") or []) if i]
            if images:
                out.append({"role": role, "content": _content_parts(content, images)})
            else:
                out.append({"role": role, "content": content})
    return out


def to_openai_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Ollama `options` → the OpenAI fields llama-server honours."""
    if not options:
        return {}
    body: dict[str, Any] = {}
    for src, dst in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("top_k", "top_k"),
        ("num_predict", "max_tokens"),
        ("seed", "seed"),
    ):
        if options.get(src) is not None:
            body[dst] = options[src]
    return body


class LlamaServerChat:
    def __init__(self, base_url: str, timeout: float = 300.0, lease_path: str = ""):
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout, sock_read=timeout)
        # Where the box writes the whole-card GPU lease (#1320). Empty = no
        # lease is possible, which is what a dev install has.
        self._lease_path = lease_path

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

        `tool_choice` is llama.cpp's string form ("auto"/"none"/"required");
        empty leaves the field off and the server defaults to "auto"."""
        if gpu_lease.mutes_chat(self._lease_path):
            # foundry holds the card, so llama.service is stopped and no model
            # can answer this turn (#1320) — or a coding lease is still loading
            # its model (#1319). Say so at once: the alternative is a
            # connection error the resident reads as Solaris being broken. A
            # coding lease that is up does not land here: llama-server answers
            # the turn from the coding model.
            log.info("engine.gpu_lease.busy", holder=gpu_lease.holder(self._lease_path))
            leased = ChatResult(content=gpu_lease.BUSY_REPLY)
            yield "delta", gpu_lease.BUSY_REPLY
            yield "done", leased
            return
        body: dict[str, Any] = {
            "model": model,
            "messages": to_openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": think},
        }
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
        body.update(to_openai_options(options))
        result = ChatResult()
        calls: dict[int, dict[str, str]] = {}
        t0 = time.monotonic()
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                f"{self._base_url}/v1/chat/completions", json=body
            ) as resp:
                if resp.status >= 400:
                    if tool_choice:
                        # A llama-server built without `--jinja` refuses the
                        # field outright. Drop the routing and let the turn run
                        # as an ordinary auto pass rather than fail it.
                        log.warn(
                            "engine.llama.tool_choice_rejected", status=resp.status
                        )
                        async for item in self.stream(
                            model, messages, tools, think, options
                        ):
                            yield item
                        return
                    detail = (await resp.text())[:500]
                    raise LlamaServerError(
                        f"llama-server /v1/chat/completions {resp.status}: {detail}"
                    )
                async for raw in resp.content:
                    line = raw.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == b"[DONE]":
                        break
                    chunk = json.loads(payload)
                    usage = chunk.get("usage") or {}
                    if usage:
                        result.prompt_tokens = usage.get("prompt_tokens") or 0
                        result.completion_tokens = usage.get("completion_tokens") or 0
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content") or ""
                    thinking = delta.get("reasoning_content") or ""
                    tool_deltas = delta.get("tool_calls") or []
                    if (content or thinking or tool_deltas) and not result.ttft_s:
                        result.ttft_s = time.monotonic() - t0
                    if content:
                        result.content += content
                        yield "delta", content
                    if thinking:
                        result.thinking += thinking
                        yield "thinking", thinking
                    for tc in tool_deltas:
                        slot = calls.setdefault(
                            int(tc.get("index") or 0),
                            {"id": "", "name": "", "args": ""},
                        )
                        fn = tc.get("function") or {}
                        if tc.get("id"):
                            slot["id"] = str(tc["id"])
                        if fn.get("name"):
                            slot["name"] = str(fn["name"])
                        if fn.get("arguments"):
                            slot["args"] += str(fn["arguments"])
        for _, slot in sorted(calls.items()):
            try:
                args: Any = json.loads(slot["args"] or "{}")
            except ValueError:
                args = slot["args"]
            result.tool_calls.append(
                {
                    "id": slot["id"],
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": args},
                }
            )
        result.wall_s = time.monotonic() - t0
        yield "done", result
