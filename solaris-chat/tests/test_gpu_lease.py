"""The whole-card GPU lease seen from the Engine (#1320).

While foundry or the coding run holds the card, `llama.service` is stopped.
The turn must end in one honest German sentence instead of a timeout against
a dead socket — and it must not reach the network at all, because there is
nothing there to answer.
"""

from __future__ import annotations

import json
import pathlib

from solaris_chat import gpu_lease
from solaris_chat.engine.llama_server import LlamaServerChat


def _held(tmp_path, holder="foundry"):
    path = tmp_path / gpu_lease.LEASE_FILENAME
    path.write_text(json.dumps({"holder": holder, "since": 1.0}), "utf-8")
    return path


def test_lease_path_sits_beside_the_database():
    assert gpu_lease.lease_path("/var/lib/solaris/solaris.db") == pathlib.Path(
        "/var/lib/solaris/gpu_lease.json"
    )


def test_no_file_is_no_lease(tmp_path):
    assert gpu_lease.is_leased(tmp_path / gpu_lease.LEASE_FILENAME) is False
    assert gpu_lease.is_leased("") is False


def test_a_half_written_lease_still_counts_as_held(tmp_path):
    """The units are stopped either way; reading a truncated file as "free"
    would send the turn into a dead socket."""
    path = tmp_path / gpu_lease.LEASE_FILENAME
    path.write_text('{"holder": "found', "utf-8")
    assert gpu_lease.is_leased(path) is True
    assert gpu_lease.holder(path) == ""


def test_holder_names_the_job(tmp_path):
    assert gpu_lease.holder(_held(tmp_path, "coder")) == "coder"


async def test_a_leased_turn_answers_the_fixed_sentence_without_a_request(
    tmp_path, monkeypatch
):
    def explode(*a, **k):  # pragma: no cover - only runs on a regression
        raise AssertionError("talked to llama-server while the card was leased")

    monkeypatch.setattr("aiohttp.ClientSession", explode)
    chat = LlamaServerChat("http://127.0.0.1:11435", lease_path=str(_held(tmp_path)))

    events = [ev async for ev in chat.stream("m", [{"role": "user", "content": "hi"}])]

    assert events[0] == ("delta", gpu_lease.BUSY_REPLY)
    kind, result = events[-1]
    assert kind == "done"
    assert result.content == gpu_lease.BUSY_REPLY
    assert result.tool_calls == []


async def test_a_leased_turn_calls_no_tool(tmp_path, monkeypatch):
    """ "Licht an" during a lease must be refused honestly, not half-executed:
    the model that would decide is the one that is unloaded."""
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: 1 / 0)
    chat = LlamaServerChat("http://127.0.0.1:11435", lease_path=str(_held(tmp_path)))
    tools = [{"type": "function", "function": {"name": "ha_turn_on"}}]

    events = [
        ev
        async for ev in chat.stream(
            "m", [{"role": "user", "content": "Licht an"}], tools=tools
        )
    ]

    assert [k for k, _ in events] == ["delta", "done"]
    assert events[-1][1].tool_calls == []
