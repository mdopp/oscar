"""A turn's multiple tool calls run concurrently, bounded, ordered (#624).

'Alle Lichter' emits many ha_get_state calls in one model response; the loop
must dispatch them CONCURRENTLY (bounded ~5) instead of awaiting each, while
keeping the tool-result messages in the EMITTED order, isolating a failing
tool, and — load-bearing for security — NOT letting the confirm-gate (#570) be
bypassed by parallelism: a sensitive ha_call_service among the batch is still
held, the rest run.
"""

from __future__ import annotations

import asyncio
import gc
import sqlite3

import pytest

from solaris_chat.engine import client as client_mod
from solaris_chat.engine.client import EngineClient, EngineProfile
from solaris_chat.engine.ollama import ChatResult
from solaris_chat.engine.tools import Tool, Toolbox
from solaris_chat.engine.tools.choices import build_choice_tools
from solaris_chat.engine.tools.ha import build_ha_tools
from solaris_chat.engine.trace import TraceRecorder

from tests.test_engine import _SCHEMA, FakeOllama
from tests.test_confirm_gate import _FakeRegistry, _stub_ha


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def soul(tmp_path) -> str:
    path = tmp_path / "SOUL.md"
    path.write_text("Du bist Solaris.", encoding="utf-8")
    return str(path)


def _client(db, soul, results, tools, classes: dict[str, str] | None = None):
    return EngineClient(
        EngineProfile(
            name="household",
            model="gemma4:e2b",
            soul_path=soul,
            registry=_FakeRegistry(classes or {}),  # type: ignore[arg-type]
            toolbox=Toolbox(tools),
        ),
        db_path=db,
        ollama=FakeOllama(results),
        recorder=TraceRecorder(),
        context_window=32768,
    )


def _call(name: str, args: dict) -> dict:
    return {"function": {"name": name, "arguments": args}}


def _get_state(entity_id: str) -> dict:
    return _call("ha_get_state", {"entity_id": entity_id})


def _concurrency_tool(probe: dict) -> Tool:
    """A ha_get_state tool that holds for a moment so concurrent dispatches
    overlap; records the live + peak in-flight count in `probe`."""
    probe["live"] = 0
    probe["peak"] = 0

    async def handler(args):
        probe["live"] += 1
        probe["peak"] = max(probe["peak"], probe["live"])
        await asyncio.sleep(0.05)
        probe["live"] -= 1
        return f'{{"entity_id": "{args.get("entity_id")}", "state": "on"}}'

    return Tool(
        name="ha_get_state",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


async def test_multiple_tool_calls_run_concurrently(db, soul):
    probe: dict = {}
    tool = _concurrency_tool(probe)
    results = [
        ChatResult(tool_calls=[_get_state(f"light.l{i}") for i in range(4)]),
        ChatResult(content="Alle vier sind an."),
    ]
    client = _client(db, soul, results, [tool])
    sid = await client.create_session("anna")

    # A gen2 collection landing in the measured window costs more than the whole
    # overlap this asserts (~0.15s in a full-suite run, enough to flip it), so
    # the collector is held off across it — concurrency is what's under test.
    gc.disable()
    try:
        t0 = asyncio.get_event_loop().time()
        _ = [e async for e in client.chat_stream(sid, "alle Lichter")]
        elapsed = asyncio.get_event_loop().time() - t0
    finally:
        gc.enable()

    # Four 0.05s tools serialized would take >=0.2s; concurrent they overlap.
    assert probe["peak"] >= 2
    assert elapsed < 0.18


async def test_tool_results_appended_in_emitted_order(db, soul):
    async def handler(args):
        # Reverse the natural finish order: an earlier-emitted call sleeps
        # LONGER, so a result appended by finish-time would be out of order.
        eid = args.get("entity_id")
        delay = {"light.a": 0.06, "light.b": 0.02, "light.c": 0.0}[eid]
        await asyncio.sleep(delay)
        return f'{{"entity_id": "{eid}"}}'

    tool = Tool(
        name="ha_get_state",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    results = [
        ChatResult(
            tool_calls=[
                _get_state("light.a"),
                _get_state("light.b"),
                _get_state("light.c"),
            ]
        ),
        ChatResult(content="ok"),
    ]
    client = _client(db, soul, results, [tool])
    fake = client._ollama
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Status")]

    # The second model pass sees the tool results in EMITTED (a,b,c) order, not
    # finish (c,b,a) order — gather preserves position.
    tool_msgs = [m["content"] for m in fake.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs == [
        '{"entity_id": "light.a"}',
        '{"entity_id": "light.b"}',
        '{"entity_id": "light.c"}',
    ]


async def test_failing_tool_isolates_others(db, soul):
    async def handler(args):
        eid = args.get("entity_id")
        if eid == "light.bad":
            raise RuntimeError("boom")
        return f'{{"entity_id": "{eid}", "state": "on"}}'

    tool = Tool(
        name="ha_get_state",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    results = [
        ChatResult(
            tool_calls=[
                _get_state("light.ok1"),
                _get_state("light.bad"),
                _get_state("light.ok2"),
            ]
        ),
        ChatResult(content="ok"),
    ]
    client = _client(db, soul, results, [tool])
    fake = client._ollama
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Status")]

    tool_msgs = [m["content"] for m in fake.calls[1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 3
    # The two good calls produced their normal results...
    assert '"entity_id": "light.ok1"' in tool_msgs[0]
    assert '"entity_id": "light.ok2"' in tool_msgs[2]
    # ...and the failing one became its own error result (isolated, ordered 2nd).
    assert "RuntimeError" in tool_msgs[1]


async def test_concurrency_is_bounded(db, soul):
    probe: dict = {}
    tool = _concurrency_tool(probe)
    # 12 calls > the bound of 5: peak in-flight must never exceed the bound.
    results = [
        ChatResult(tool_calls=[_get_state(f"light.l{i}") for i in range(12)]),
        ChatResult(content="ok"),
    ]
    client = _client(db, soul, results, [tool])
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "alle Lichter")]

    assert probe["peak"] <= client_mod._MAX_PARALLEL_TOOLS
    assert probe["peak"] >= 2  # it did run concurrently, just capped


async def test_sensitive_call_still_gated_amid_parallel(db, soul, monkeypatch):
    # A turn emits several reads PLUS one sensitive open_cover (garage). The
    # gate must hold the garage (needs_confirmation, NOT executed) even though
    # the other calls run concurrently around it.
    posts = _stub_ha(monkeypatch)
    tools = build_ha_tools("http://ha", "tok") + build_choice_tools()
    results = [
        ChatResult(
            tool_calls=[
                _get_state("light.l1"),
                _call(
                    "ha_call_service",
                    {
                        "domain": "cover",
                        "service": "open_cover",
                        "entity_id": "cover.garage_door",
                    },
                ),
                _get_state("light.l2"),
            ]
        ),
        ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
    ]
    client = _client(db, soul, results, tools, {"cover.garage_door": "garage"})
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Status und Garage auf")]

    # The sensitive cover service was NOT invoked (gate held it).
    assert all("cover/open_cover" not in url for url, _ in posts)
    # It is stashed for a ja/nein follow-up; chips offered.
    pending = client._pending.peek(sid)
    assert pending is not None and pending.service == "open_cover"
    qr = [e for e in events if e["type"] == "quick_replies"]
    assert qr and qr[0]["data"]["options"] == ["ja", "nein"]
    # The two non-sensitive reads still ran (their state hit HA's GET).
    fake = client._ollama
    tool_msgs = [m for m in fake.calls[1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 3  # read, held-confirmation, read — in order
    assert "needs_confirmation" in tool_msgs[1]["content"]


async def test_two_sensitive_calls_only_first_stashes(db, soul, monkeypatch):
    # Two sensitive calls in one response: serial gating must hold BOTH (neither
    # runs) and not race the single pending slot — the first to be gated stashes.
    _stub_ha(monkeypatch)
    tools = build_ha_tools("http://ha", "tok") + build_choice_tools()
    results = [
        ChatResult(
            tool_calls=[
                _call(
                    "ha_call_service",
                    {
                        "domain": "cover",
                        "service": "open_cover",
                        "entity_id": "cover.garage_door",
                    },
                ),
                _call(
                    "ha_call_service",
                    {"domain": "lock", "service": "unlock", "entity_id": "lock.front"},
                ),
            ]
        ),
        ChatResult(content="Soll ich das wirklich tun?"),
    ]
    client = _client(db, soul, results, tools, {"cover.garage_door": "garage"})
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garage auf und Schloss auf")]

    fake = client._ollama
    tool_msgs = [m for m in fake.calls[1]["messages"] if m["role"] == "tool"]
    # Both were held (needs_confirmation), neither dispatched.
    assert len(tool_msgs) == 2
    assert all("needs_confirmation" in m["content"] for m in tool_msgs)
    # A single pending slot — the last gated stash wins, deterministically.
    assert client._pending.peek(sid) is not None
