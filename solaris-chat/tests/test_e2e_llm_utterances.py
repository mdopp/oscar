"""End-to-End LLM Integration Test Suite (#1056).

Tests full E2E inference turns with local Ollama (gemma4:e4b) + solaris-chat
engine tools to ensure the LLM correctly generates tool calls and the engine
resolves streams without false matches.

Runs locally when Ollama is active. Skipped automatically if Ollama is unreachable.
"""

from __future__ annotations

import os
import json
import urllib.request
import pytest

from solaris_chat.engine.ollama import OllamaChat
from solaris_chat.engine.tools.radio import build_radio_tools


def _is_ollama_available() -> bool:
    try:
        req = urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5)
        return req.status == 200
    except Exception:
        return False


OLLAMA_AVAILABLE = _is_ollama_available()


@pytest.mark.skipif(not OLLAMA_AVAILABLE, reason="Local Ollama endpoint (127.0.0.1:11434) unavailable")
@pytest.mark.asyncio
async def test_e2e_llm_1live_resolution(tmp_path):
    notes_dir = str(tmp_path / "notes")
    os.makedirs(notes_dir, exist_ok=True)

    ollama = OllamaChat("http://127.0.0.1:11434")

    radio_tools = build_radio_tools(
        hass_url="http://127.0.0.1:8123",
        hass_token="mock_token",
        notes_dir=notes_dir,
        uid_getter=lambda: "household",
        room_getter=lambda: "Wohnzimmer"
    )

    tool_schemas = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters
            }
        }
        for t in radio_tools
    ]

    messages = [
        {
            "role": "system",
            "content": "Du bist Solaris, der HA Voice Assistent. Für Radio rufe play_radio auf."
        },
        {
            "role": "user",
            "content": "Spiele den Radiosender 1live im Wohnzimmer"
        }
    ]

    chat_res = None
    async for kind, res in ollama.stream("gemma4:e4b", messages, tools=tool_schemas):
        if kind == "done":
            chat_res = res

    assert chat_res is not None, "Ollama returned no response"
    assert chat_res.tool_calls, f"Ollama failed to generate a tool call: {chat_res}"

    tc = chat_res.tool_calls[0]
    t_name = tc["function"]["name"]
    t_args = tc["function"]["arguments"]

    assert t_name in ("play_radio", "play_music"), f"Unexpected tool generated: {t_name}"

    target_tool = next(t for t in radio_tools if t.name == t_name)
    import unittest.mock
    async def mock_call_service(*args, **kwargs):
        return {'ok': True}
    with unittest.mock.patch('solaris_chat.engine.tools.radio.call_service_scoped', side_effect=mock_call_service):
        out_raw = await target_tool.handler(t_args)
        out = json.loads(out_raw)

    assert out.get("ok") is True, f"Tool execution failed: {out}"
    assert out.get("title") != "Live Alone", "Matched Live Alone!"
    assert out.get("title") != "1+1=3", "Matched ...But Alive!"
    assert str(out.get("station")).lower() in ("1live", "1 live"), f"Unexpected station: {out}"
