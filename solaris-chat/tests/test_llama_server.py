"""The llama-server chat backend (#1318): the request it builds and the SSE it
folds back into a ChatResult.

The translation is the whole module — `EngineClient` speaks Ollama's message
shape and must not learn a second one — so these tests pin the four things that
silently change an answer if they drift: `enable_thinking`, tool-call ids, tool
results, and image parts.
"""

from __future__ import annotations

import json

import pytest

from solaris_chat.engine import llama_server
from solaris_chat.engine.llama_server import (
    LlamaServerChat,
    LlamaServerError,
    to_openai_messages,
    to_openai_options,
)


def _patch_post(monkeypatch, events, status=200):
    """Stub aiohttp so POST streams `events` as SSE `data:` lines."""

    class _Content:
        def __aiter__(self):
            async def gen():
                for obj in events:
                    payload = obj if isinstance(obj, str) else json.dumps(obj)
                    yield f"data: {payload}\n".encode()
                yield b"data: [DONE]\n"

            return gen()

    class _Resp:
        def __init__(self):
            self.status = status
            self.content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return "boom"

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None):
            _Session.last = {"url": url, "json": json}
            return _Resp()

    monkeypatch.setattr(llama_server.aiohttp, "ClientSession", _Session)
    return _Session


def _chunk(**delta):
    return {"choices": [{"index": 0, "delta": delta}]}


# --- the request ----------------------------------------------------------


async def test_thinking_is_off_via_chat_template_kwargs(monkeypatch):
    """Ollama's `think: false` has no llama-server equivalent — this is it.

    llama.cpp renders the Gemma template with `enable_thinking = true`,
    overriding the template's own `default(false)`; without this key the
    resident waits for an invisible English reasoning trace.
    """
    sess = _patch_post(monkeypatch, [_chunk(content="ja")])
    client = LlamaServerChat("http://x:11435")

    [c async for c in client.stream("gemma", [{"role": "user", "content": "hi"}])]

    assert sess.last["url"] == "http://x:11435/v1/chat/completions"
    assert sess.last["json"]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_think_true_asks_for_the_reasoning_trace(monkeypatch):
    sess = _patch_post(monkeypatch, [_chunk(content="ja")])
    client = LlamaServerChat("http://x:11435")

    [
        c
        async for c in client.stream(
            "gemma", [{"role": "user", "content": "hi"}], think=True
        )
    ]

    assert sess.last["json"]["chat_template_kwargs"] == {"enable_thinking": True}


async def test_options_are_translated(monkeypatch):
    sess = _patch_post(monkeypatch, [_chunk(content="x")])
    client = LlamaServerChat("http://x:11435")

    [
        c
        async for c in client.stream(
            "gemma",
            [{"role": "user", "content": "hi"}],
            options={"temperature": 0.2, "num_predict": 64},
        )
    ]

    body = sess.last["json"]
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 64
    assert "num_predict" not in body


async def test_non_2xx_raises(monkeypatch):
    _patch_post(monkeypatch, [], status=503)
    client = LlamaServerChat("http://x:11435")
    with pytest.raises(LlamaServerError):
        [c async for c in client.stream("gemma", [{"role": "user", "content": "hi"}])]


def test_options_none_is_empty():
    assert to_openai_options(None) == {}


# --- message translation --------------------------------------------------


def test_tool_call_arguments_become_a_json_string_with_an_id():
    out = to_openai_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "ha_call_service", "arguments": {"a": 1}}}
                ],
            }
        ]
    )
    call = out[0]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["id"]
    assert json.loads(call["function"]["arguments"]) == {"a": 1}


def test_tool_result_is_paired_with_the_call_above_it():
    """The engine's history carries `tool_name` and no id at all; the OpenAI
    schema needs `tool_call_id`, and the loop appends results in call order."""
    out = to_openai_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "one", "arguments": {}}},
                    {"function": {"name": "two", "arguments": {}}},
                ],
            },
            {"role": "tool", "content": "{}", "tool_name": "one"},
            {"role": "tool", "content": "{}", "tool_name": "two"},
        ]
    )
    ids = [c["id"] for c in out[0]["tool_calls"]]
    assert [out[1]["tool_call_id"], out[2]["tool_call_id"]] == ids
    assert out[1]["name"] == "one"


def test_images_become_data_url_parts():
    """Attachments are persisted as bare base64 (Ollama's `images` shape); the
    media type has to come back or the projector gets a broken data URL."""
    out = to_openai_messages(
        [{"role": "user", "content": "was ist das?", "images": ["iVBORw0KGgoAAA"]}]
    )
    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "was ist das?"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")


# --- the stream -----------------------------------------------------------


async def test_stream_folds_deltas_reasoning_and_usage(monkeypatch):
    _patch_post(
        monkeypatch,
        [
            _chunk(reasoning_content="denk"),
            _chunk(content="Hallo"),
            _chunk(content=" du"),
            {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        ],
    )
    client = LlamaServerChat("http://x:11435")

    events = [c async for c in client.stream("gemma", [])]

    assert [e for e in events if e[0] == "delta"] == [
        ("delta", "Hallo"),
        ("delta", " du"),
    ]
    assert ("thinking", "denk") in events
    kind, result = events[-1]
    assert kind == "done"
    assert result.content == "Hallo du"
    assert result.thinking == "denk"
    assert (result.prompt_tokens, result.completion_tokens) == (7, 3)
    assert result.ttft_s > 0


async def test_tool_call_fragments_are_reassembled(monkeypatch):
    """llama-server streams a tool call's arguments in pieces; the engine
    expects one whole call with a parsed argument object, like Ollama's."""
    _patch_post(
        monkeypatch,
        [
            _chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": "call_a",
                        "function": {"name": "ha_call_service", "arguments": '{"dom'},
                    }
                ]
            ),
            _chunk(
                tool_calls=[{"index": 0, "function": {"arguments": 'ain": "light"}'}}]
            ),
        ],
    )
    client = LlamaServerChat("http://x:11435")

    events = [c async for c in client.stream("gemma", [])]

    _, result = events[-1]
    assert result.tool_calls == [
        {
            "id": "call_a",
            "type": "function",
            "function": {
                "name": "ha_call_service",
                "arguments": {"domain": "light"},
            },
        }
    ]
