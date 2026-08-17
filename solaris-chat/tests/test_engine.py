"""Tests for the Solaris Engine core: store, agent loop, tools, scheduler.

The loop tests run against a scripted fake Ollama (no network): each call
pops the next scripted result, so a tool-chain turn (tool_calls -> dispatch
-> final answer) exercises the real loop, store and trace paths.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from solaris_chat.engine import scheduler, store
from solaris_chat.engine.areas import AreaSnapshot
from solaris_chat.engine.client import (
    _TOOL_DISCIPLINE,
    EngineClient,
    EngineProfile,
    _is_fabricated_device_claim,
    _split_anchors,
    _split_followups,
    compact_history,
)
from solaris_chat.engine.ollama import ChatResult
from solaris_chat.engine.registry import EntityRegistry
from solaris_chat.engine.tools import Tool, Toolbox
from solaris_chat.engine.tools.ha import build_ha_tools
from solaris_chat.engine.trace import TraceRecorder

_SCHEMA = """
CREATE TABLE engine_sessions (
  id            TEXT PRIMARY KEY,
  owner_uid     TEXT NOT NULL,
  title         TEXT NOT NULL DEFAULT '',
  profile       TEXT NOT NULL DEFAULT 'household',
  system_prompt TEXT NOT NULL DEFAULT '',
  ephemeral     INTEGER NOT NULL DEFAULT 0,
  maintenance   INTEGER NOT NULL DEFAULT 0,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  last_activity TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE engine_messages (
  session_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL DEFAULT '',
  reasoning   TEXT,
  tool_calls  TEXT,
  images      TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  conversation_id TEXT,
  in_prompt   INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (session_id, seq)
);
CREATE TABLE engine_timers (
  id         TEXT PRIMARY KEY,
  owner_uid  TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'timer',
  label      TEXT NOT NULL DEFAULT '',
  fire_at    TEXT NOT NULL,
  rrule      TEXT,
  session_id TEXT,
  status     TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  room       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE voice_uid_stash (
  transcript TEXT PRIMARY KEY,
  uid        TEXT NOT NULL,
  matched    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE session_traces (
  session_id        TEXT NOT NULL,
  trace_id          TEXT NOT NULL,
  step_order        INTEGER NOT NULL,
  owner_uid         TEXT NOT NULL,
  model             TEXT,
  profile           TEXT,
  wall_s            REAL,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  context_free      INTEGER,
  finish_reason     TEXT,
  n_tools           INTEGER,
  detail_id         INTEGER,
  step_kind         TEXT,
  tool_name         TEXT,
  detail_json       TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (session_id, trace_id, step_order)
);
"""


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


class FakeOllama:
    """Pops one scripted ChatResult per call; records what it was sent."""

    def __init__(self, results: list[ChatResult]):
        self.results = list(results)
        self.calls: list[dict] = []

    async def stream(self, model, messages, tools=None, think=False, options=None):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
                "think": think,
                "options": options,
            }
        )
        result = self.results.pop(0)
        for chunk in result.content.split(" "):
            if chunk:
                yield "delta", chunk + " "
        if result.thinking:
            yield "thinking", result.thinking
        yield "done", result


def _client(db, soul, results, tools=None) -> tuple[EngineClient, FakeOllama]:
    fake = FakeOllama(results)
    client = EngineClient(
        EngineProfile(
            name="household",
            model="gemma4:e2b",
            soul_path=soul,
            toolbox=Toolbox(tools or []),
        ),
        db_path=db,
        ollama=fake,  # duck-typed
        recorder=TraceRecorder(),
        context_window=32768,
    )
    return client, fake


# -- store ---------------------------------------------------------------


def test_store_session_roundtrip(db):
    sid = store.create_session(db, "anna", title="Einkauf")
    assert store.session_owner(db, sid) == "anna"
    store.append_message(db, sid, "user", "Hallo")
    store.append_message(db, sid, "assistant", "Hi!")
    fetched = store.get_session(db, sid, "anna")
    assert fetched["title"] == "Einkauf"
    assert [m["role"] for m in fetched["messages"]] == ["user", "assistant"]
    # created_at rides each message so a reopened bubble gets its .meta line
    # (the anchor the persisted step-trace attaches to).
    assert all(m["created_at"] for m in fetched["messages"])
    # owner scope: a wrong uid sees nothing
    assert store.get_session(db, sid, "bert") is None
    listed = store.list_sessions(db, "anna")
    assert listed[0]["id"] == sid
    assert listed[0]["preview"] == "Hallo"


def test_store_ephemeral_not_listed(db):
    store.create_session(db, "anna", ephemeral=True)
    assert store.list_sessions(db, "anna") == []


def test_messages_since_returns_only_new_user_assistant_rows(db):
    import sqlite3

    sid = store.create_session(db, "anna")
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO engine_messages (session_id, seq, role, content, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (sid, 1, "user", "alt", "2026-07-05 09:00:00"),
            (sid, 2, "assistant", "alte antwort", "2026-07-05 09:00:01"),
            (sid, 3, "user", "neu", "2026-07-06 10:00:00"),
            (sid, 4, "assistant", "neue antwort", "2026-07-06 10:00:01"),
            (sid, 5, "assistant", "", "2026-07-06 10:00:02"),  # empty → skipped
            (sid, 6, "tool", "tool result", "2026-07-06 10:00:03"),  # non-chat
        ],
    )
    conn.commit()
    conn.close()
    got = store.messages_since(db, sid, "2026-07-06 00:00:00")
    assert got == [("user", "neu"), ("assistant", "neue antwort")]


def test_store_overlay_and_usage(db):
    sid = store.create_session(db, "anna")
    store.set_overlay(db, sid, "Fortsetzung: ...")
    assert store.get_overlay(db, sid) == "Fortsetzung: ..."
    store.add_usage(db, sid, 100, 20)
    store.add_usage(db, sid, 50, 10)
    session = store.get_session(db, sid, "anna")
    assert session["input_tokens"] == 150
    assert session["output_tokens"] == 30


def test_truncate_session_head_keeps_recent_at_user_boundary(db):
    sid = store.create_session(db, "anna")
    # 6 turns; each "user"(~40 tok) -> "assistant"(~40 tok). ~80 tok/turn.
    for i in range(6):
        store.append_message(db, sid, "user", f"frage {i} " + "x" * 160)
        store.append_message(db, sid, "assistant", f"antwort {i} " + "y" * 160)
    # Budget ~120 tokens -> keep ~last 1-2 turns, dropping the older ones.
    dropped = store.truncate_session_head(db, sid, 120)
    assert dropped > 0
    msgs = store.get_session(db, sid, "anna")["messages"]
    assert msgs[0]["role"] == "user"  # window starts cleanly at a user turn
    assert "frage 0" not in msgs[0]["content"]  # oldest turns are gone
    assert any("frage 5" in m["content"] for m in msgs)  # newest turn survives


def test_truncate_session_head_noop_when_within_budget(db):
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "user", "kurz")
    store.append_message(db, sid, "assistant", "ok")
    assert store.truncate_session_head(db, sid, 32768) == 0
    assert len(store.get_session(db, sid, "anna")["messages"]) == 2


def _add_trace(db, sid, trace_id, created_at, uid="anna"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO session_traces"
        " (session_id, trace_id, step_order, owner_uid, created_at)"
        " VALUES (?, ?, 0, ?, ?)",
        (sid, trace_id, uid, created_at),
    )
    conn.commit()
    conn.close()


def _trace_ids(db, sid):
    conn = sqlite3.connect(db)
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT trace_id FROM session_traces WHERE session_id = ?", (sid,)
        )
    ]
    conn.close()
    return set(ids)


def test_truncate_session_head_prunes_traces_of_cut_turns(db):
    sid = store.create_session(db, "anna")
    for i in range(6):
        store.append_message(db, sid, "user", f"frage {i} " + "x" * 160)
        store.append_message(db, sid, "assistant", f"antwort {i} " + "y" * 160)
    # A trace from a long-gone turn (before the kept window) and one from "now".
    _add_trace(db, sid, "old", "2000-01-01 00:00:00")
    _add_trace(db, sid, "fresh", "2999-01-01 00:00:00")
    assert store.truncate_session_head(db, sid, 120) > 0
    ids = _trace_ids(db, sid)
    assert "old" not in ids  # trace of a cut turn is pruned
    assert "fresh" in ids  # trace of a kept turn survives


def test_delete_session_purges_its_traces(db):
    sid = store.create_session(db, "anna")
    other = store.create_session(db, "anna")
    _add_trace(db, sid, "t1", "2026-01-01 00:00:00")
    _add_trace(db, other, "t2", "2026-01-01 00:00:00")
    assert store.delete_session(db, sid, "anna") is True
    assert _trace_ids(db, sid) == set()  # deleted chat's traces are gone
    assert _trace_ids(db, other) == {"t2"}  # another chat's traces untouched


# -- agent loop ----------------------------------------------------------


async def test_plain_turn_streams_and_persists(db, soul):
    client, fake = _client(
        db,
        soul,
        [ChatResult(content="Hallo zurück!", prompt_tokens=50, completion_tokens=5)],
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Hallo")]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "assistant.delta"
    assert kinds[-1] == "run.completed"
    final = events[-1]["data"]["messages"][-1]
    assert "Hallo" in final["content"]
    # system prompt = soul; history persisted
    assert fake.calls[0]["messages"][0]["role"] == "system"
    assert "Du bist Solaris." in fake.calls[0]["messages"][0]["content"]
    session = await client.get_session(sid, "anna")
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]
    assert session["input_tokens"] == 50


async def test_model_resolver_overrides_static_model(db, soul):
    # #366: a profile resolver re-points the model per turn; empty falls back.
    override = {"value": ""}
    fake = FakeOllama(
        [
            ChatResult(content="a"),
            ChatResult(content="b"),
        ]
    )
    client = EngineClient(
        EngineProfile(
            name="household",
            model="gemma4:e2b",
            soul_path=soul,
            model_resolver=lambda: override["value"],
        ),
        db_path=db,
        ollama=fake,
        recorder=TraceRecorder(),
        context_window=32768,
    )
    sid = await client.create_session("anna")
    [e async for e in client.chat_stream(sid, "x")]
    assert fake.calls[-1]["model"] == "gemma4:e2b"  # resolver empty -> default
    override["value"] = "gemma4:12b"
    [e async for e in client.chat_stream(sid, "y")]
    assert fake.calls[-1]["model"] == "gemma4:12b"  # resolver wins


async def test_tool_chain_turn(db, soul):
    seen = {}

    async def handler(args):
        seen.update(args)
        return '{"success": true}'

    tool = Tool(
        name="ha_call_service",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    results = [
        ChatResult(
            tool_calls=[
                {
                    "function": {
                        "name": "ha_call_service",
                        "arguments": {
                            "domain": "light",
                            "service": "turn_on",
                            "entity_id": "light.buero",
                        },
                    }
                }
            ],
            prompt_tokens=60,
            completion_tokens=8,
        ),
        ChatResult(
            content="Das Bürolicht ist an.", prompt_tokens=70, completion_tokens=6
        ),
    ]
    client, fake = _client(db, soul, results, tools=[tool])
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Licht im Büro an")]
    kinds = [e["type"] for e in events]
    assert "tool.started" in kinds and "tool.completed" in kinds
    assert seen["entity_id"] == "light.buero"
    # the second pass got the tool result fed back
    roles = [m["role"] for m in fake.calls[1]["messages"]]
    assert "tool" in roles
    # the turn's trace is the full interleaved step list: LLM call (tool_calls)
    # -> tool execution (with its own wall_s) -> final LLM call (#346).
    steps = client.recorder.for_session(sid, 0.0)
    assert [s["step_kind"] for s in steps] == ["llm", "tool", "llm"]
    assert steps[0]["finish_reason"] == "tool_calls"
    assert steps[1]["tool_name"] == "ha_call_service"
    assert "wall_s" in steps[1]
    assert steps[2]["finish_reason"] == "stop"


async def test_turn_emits_ha_cards_for_state_read(db, soul, monkeypatch):
    # A turn that reads HA state (#475) drains the per-turn card sink into a
    # single `ha_cards` event, emitted once just before run.completed.
    from solaris_chat.engine.tools import ha as ha_mod

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {
                "state": "21.4",
                "attributes": {
                    "friendly_name": "Küche",
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                },
            }

        def raise_for_status(self):
            pass

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)
    state_tool = next(
        t for t in build_ha_tools("http://ha", "tok") if t.name == "ha_get_state"
    )
    results = [
        ChatResult(
            tool_calls=[
                {
                    "function": {
                        "name": "ha_get_state",
                        "arguments": {"entity_id": "sensor.kueche"},
                    }
                }
            ],
            prompt_tokens=60,
            completion_tokens=8,
        ),
        ChatResult(content="21,4 °C.", prompt_tokens=70, completion_tokens=6),
    ]
    client, _ = _client(db, soul, results, tools=[state_tool])
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Wie warm ist die Küche?")]

    card_events = [e for e in events if e["type"] == "ha_cards"]
    assert len(card_events) == 1
    cards = card_events[0]["data"]["cards"]
    assert cards == [
        {
            "entity_id": "sensor.kueche",
            "name": "Küche",
            "domain": "sensor",
            "device_class": "temperature",
            "state": "21.4",
            "unit": "°C",
        }
    ]
    # the cards event precedes run.completed
    kinds = [e["type"] for e in events]
    assert kinds.index("ha_cards") < kinds.index("run.completed")


async def test_turn_without_state_read_emits_no_ha_cards(db, soul):
    # A plain turn with no HA state read emits no ha_cards event (#475).
    client, _ = _client(db, soul, [ChatResult(content="Hallo!", prompt_tokens=10)])
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Hi")]
    assert not any(e["type"] == "ha_cards" for e in events)


async def test_fabricated_device_claim_forces_tool_call(db, soul):
    """#356: clarify -> 'Ja.' -> model claims 'ist an' with empty tool_calls.

    The loop must reject the fabricated success and re-prompt so the tool
    actually fires, instead of persisting/returning a claim with no dispatch.
    """
    dispatched = []

    async def handler(args):
        dispatched.append(args)
        return '{"success": true}'

    tool = Tool(
        name="ha_call_service",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    results = [
        # Pass 1: the fabricated claim — no tool_calls.
        ChatResult(
            content="Das Sofa-Licht ist an.", prompt_tokens=60, completion_tokens=6
        ),
        # Pass 2 (after the correction nudge): the model now calls the tool.
        ChatResult(
            tool_calls=[
                {
                    "function": {
                        "name": "ha_call_service",
                        "arguments": {
                            "domain": "light",
                            "service": "turn_on",
                            "entity_id": "light.dimmer_2_5",
                        },
                    }
                }
            ],
            prompt_tokens=70,
            completion_tokens=8,
        ),
        # Pass 3: the truthful final answer, after the tool result.
        ChatResult(
            content="Das Sofa-Licht ist an.", prompt_tokens=80, completion_tokens=6
        ),
    ]
    client, fake = _client(db, soul, results, tools=[tool])
    sid = await client.create_session("household")
    events = [e async for e in client.chat_stream(sid, "Ja.")]
    kinds = [e["type"] for e in events]
    assert "tool.started" in kinds and "tool.completed" in kinds
    # the tool actually fired with the registry entity
    assert dispatched and dispatched[0]["entity_id"] == "light.dimmer_2_5"
    # the final answer returned to the caller is backed by a real tool call
    final = events[-1]["data"]["messages"][-1]["content"]
    assert final == "Das Sofa-Licht ist an."
    # the corrective nudge rode the in-memory messages, not the store: the
    # fabricated intermediate claim must NOT be persisted as a standalone
    # assistant message (it would poison future history — the bug's root). The
    # raw store holds only the tool-call assistant (empty content), the tool
    # result, and the final answer — never a content-bearing assistant turn
    # before the tool ran.
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT role, content, tool_calls FROM engine_messages"
            " WHERE session_id = ? ORDER BY seq",
            (sid,),
        ).fetchall()
    assert [r[0] for r in rows] == ["user", "assistant", "tool", "assistant"]
    tool_call_row, final_row = rows[1], rows[3]
    assert tool_call_row[1] == "" and tool_call_row[2]  # empty content, has tool_calls
    assert final_row[1] == "Das Sofa-Licht ist an." and final_row[2] is None


async def test_perfect_tense_action_claim_forces_tool_call(db, soul):
    """#360: 'Ich habe das Licht eingeschaltet' — the accusative-object perfect
    form #356's regex missed. With no tool_calls it must trip the guard and
    force the tool pass, same as the present-tense 'ist an' form."""
    dispatched = []

    async def handler(args):
        dispatched.append(args)
        return '{"success": true}'

    tool = Tool(
        name="ha_call_service",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    results = [
        ChatResult(content="Ich habe das Licht eingeschaltet.", completion_tokens=6),
        ChatResult(
            tool_calls=[
                {
                    "function": {
                        "name": "ha_call_service",
                        "arguments": {
                            "domain": "light",
                            "service": "turn_on",
                            "entity_id": "light.dimmer_2_5",
                        },
                    }
                }
            ],
            completion_tokens=8,
        ),
        ChatResult(content="Ich habe das Licht eingeschaltet.", completion_tokens=6),
    ]
    client, _ = _client(db, soul, results, tools=[tool])
    sid = await client.create_session("household")
    events = [e async for e in client.chat_stream(sid, "Ja.")]
    kinds = [e["type"] for e in events]
    assert "tool.started" in kinds and "tool.completed" in kinds
    assert dispatched and dispatched[0]["entity_id"] == "light.dimmer_2_5"


def test_device_claim_matches_perfect_and_passive_forms():
    """#360: the accusative-object perfect and the passive form trip the guard;
    an infinitive question and a future intent do not (no completed-action
    participle)."""
    assert _is_fabricated_device_claim("Ich habe das Licht eingeschaltet.")
    assert _is_fabricated_device_claim("habe das Sofalicht angeschaltet")
    assert _is_fabricated_device_claim("Ich habe den Fernseher ausgeschaltet.")
    assert _is_fabricated_device_claim("Das Licht wurde eingeschaltet.")
    assert not _is_fabricated_device_claim("Soll ich das Licht einschalten?")
    assert not _is_fabricated_device_claim("Ich schalte gleich das Licht an.")
    assert not _is_fabricated_device_claim("Welches Licht soll ich anschalten?")


def test_split_followups_strips_marker_and_caps_at_three():
    """#498: the trailing FOLLOWUPS line is parsed into <=3 chips and removed
    from the answer; no marker leaves the answer untouched with no chips."""
    answer, chips = _split_followups(
        "Der Verbrauch liegt bei 2,1 kW.\nFOLLOWUPS: Was zieht am meisten? | "
        "PV-Erzeugung? | Akku-Status? | Tagesverbrauch?"
    )
    assert answer == "Der Verbrauch liegt bei 2,1 kW."
    assert chips == ["Was zieht am meisten?", "PV-Erzeugung?", "Akku-Status?"]

    plain, none = _split_followups("Klar.")
    assert plain == "Klar."
    assert none == []


def test_split_anchors_keeps_prefixed_tokens_and_caps_at_three():
    """#501: the trailing ANCHORS line is parsed into <=3 prefixed anchors and
    removed; a bare token without #/@ is dropped; no marker leaves it untouched."""
    answer, anchors = _split_anchors(
        "Annas Garten-Projekt läuft gut.\n"
        "ANCHORS: @anna | #garten-projekt | muenchen | #frühling"
    )
    assert answer == "Annas Garten-Projekt läuft gut."
    assert anchors == ["@anna", "#garten-projekt", "#frühling"]

    plain, none = _split_anchors("Klar.")
    assert plain == "Klar."
    assert none == []


# -- history compaction (#623) -------------------------------------------


def _multi_tool_history() -> list[dict]:
    """A 2-turn convo: a PAST turn that read living-room + kitchen temps via two
    tools (verbose JSON), then the CURRENT turn ('und das Wohnzimmer?') whose own
    tool call + full result are appended."""
    big = json.dumps(
        {"state": "21.4", "attributes": {"unit": "°C", "friendly_name": "Küche"}}
        | {f"extra_{i}": "padding-value" for i in range(20)},
        ensure_ascii=False,
    )
    return [
        {"role": "system", "content": "Du bist Solaris."},
        {"role": "user", "content": "Wie warm ist die Küche?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "ha_get_state",
                        "arguments": {
                            "entity_id": "sensor.kueche_temp",
                            "verbose": True,
                        },
                    }
                }
            ],
        },
        {"role": "tool", "content": big, "tool_name": "ha_get_state"},
        {"role": "assistant", "content": "In der Küche sind es 21,4 °C."},
        # current turn:
        {"role": "user", "content": "Und das Wohnzimmer?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "ha_get_state",
                        "arguments": {"entity_id": "sensor.wohnzimmer_temp"},
                    }
                }
            ],
        },
        {
            "role": "tool",
            "content": json.dumps({"state": "22.8", "friendly_name": "Wohnzimmer"}),
            "tool_name": "ha_get_state",
        },
    ]


def test_compact_history_compacts_past_tool_message_to_named_gist():
    msgs = _multi_tool_history()
    out = compact_history(msgs)
    past_tool = out[3]
    assert past_tool["role"] == "tool"
    assert past_tool["content"].startswith("[tool ha_get_state] ")
    gist = past_tool["content"].removeprefix("[tool ha_get_state] ")
    assert gist  # non-empty: a follow-up can still resolve against it
    assert len(past_tool["content"]) < len(msgs[3]["content"])


def test_compact_history_keeps_current_turn_tool_result_full():
    msgs = _multi_tool_history()
    out = compact_history(msgs)
    # the current turn's tool result (last message) is untouched — the model
    # needs it to answer THIS turn.
    assert out[-1]["content"] == msgs[-1]["content"]
    assert "22.8" in out[-1]["content"]
    assert not out[-1]["content"].startswith("[tool")


def test_compact_history_preserves_user_and_assistant_text_verbatim():
    msgs = _multi_tool_history()
    out = compact_history(msgs)
    assert out[1]["content"] == "Wie warm ist die Küche?"
    assert out[4]["content"] == "In der Küche sind es 21,4 °C."
    assert out[5]["content"] == "Und das Wohnzimmer?"


def test_compact_history_preserves_past_assistant_tool_call_args():
    # #636: past tool_calls args MUST stay intact. Reducing them to empty
    # `arguments: {}` made e4b imitate the pattern and emit argument-less calls.
    msgs = _multi_tool_history()
    out = compact_history(msgs)
    past_call = out[2]["tool_calls"][0]["function"]
    assert past_call["name"] == "ha_get_state"
    assert past_call["arguments"] == {
        "entity_id": "sensor.kueche_temp",
        "verbose": True,
    }  # past args preserved, NOT emptied
    # the current turn's assistant tool_calls keep their full args too.
    cur_call = out[6]["tool_calls"][0]["function"]
    assert cur_call["arguments"] == {"entity_id": "sensor.wohnzimmer_temp"}


def test_compact_history_never_emits_empty_args_tool_call():
    # #636 regression: no assistant message in the compacted history may carry a
    # tool_call with empty `arguments: {}` — that pattern is the imitation hazard.
    msgs = _multi_tool_history()
    out = compact_history(msgs)
    for m in out:
        for tc in m.get("tool_calls") or []:
            args = (tc.get("function") or {}).get("arguments")
            assert args, f"empty-args tool_call leaked into compacted history: {tc}"


def test_compact_history_reduces_size_and_is_pure():
    msgs = _multi_tool_history()
    before = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)
    snapshot = json.dumps(msgs, ensure_ascii=False)
    out = compact_history(msgs)
    after = sum(len(json.dumps(m, ensure_ascii=False)) for m in out)
    assert after < before  # measurable char reduction
    assert json.dumps(msgs, ensure_ascii=False) == snapshot  # input not mutated


async def test_compact_history_applied_in_loop_before_model_call(db, soul):
    """End-to-end: a session with a verbose PAST tool turn sends a COMPACTED
    history to the model, while the new turn's user text rides full."""

    async def handler(args):
        return '{"state": "ok"}'

    tool = Tool(
        name="ha_get_state",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    results = [ChatResult(content="Klar.", prompt_tokens=10, completion_tokens=2)]
    client, fake = _client(db, soul, results, tools=[tool])
    sid = await client.create_session("anna")
    big = json.dumps({"state": "21.4"} | {f"p{i}": "x" * 10 for i in range(30)})
    store.append_message(db, sid, "user", "Wie warm ist die Küche?")
    store.append_message(
        db,
        sid,
        "assistant",
        "",
        tool_calls=[{"function": {"name": "ha_get_state", "arguments": {"e": 1}}}],
    )
    store.append_message(db, sid, "tool", big)
    store.append_message(db, sid, "assistant", "21,4 °C.")
    [_ async for _ in client.chat_stream(sid, "Und das Wohnzimmer?")]
    sent = fake.calls[0]["messages"]
    past_tool = next(m for m in sent if m["role"] == "tool")
    assert past_tool["content"].startswith("[tool ha_get_state] ")
    assert len(past_tool["content"]) < len(big)
    assert any(
        m["role"] == "user" and m["content"] == "Und das Wohnzimmer?" for m in sent
    )


async def test_claim_passes_through_without_tools(db, soul):
    """The guard is gated on the profile having tools: a tool-less Q&A profile
    that states 'das Licht ist an' has nothing to dispatch and must accept its
    answer on the first pass (no re-prompt, single Ollama call)."""
    client, fake = _client(
        db, soul, [ChatResult(content="Ja, das Licht ist an.", completion_tokens=4)]
    )
    sid = await client.create_session("anna")
    assert await client.chat(sid, "Ist das Licht an?") == "Ja, das Licht ist an."
    assert len(fake.calls) == 1  # accepted on pass 1, no correction


async def test_chat_returns_final_answer(db, soul):
    client, _ = _client(
        db, soul, [ChatResult(content="42", prompt_tokens=10, completion_tokens=1)]
    )
    sid = await client.create_session("anna")
    assert await client.chat(sid, "Antwort?") == "42"


async def test_overlay_rides_system_prompt(db, soul):
    client, fake = _client(db, soul, [ChatResult(content="ok")])
    sid = await client.create_session("anna", "Fortsetzung einer früheren Unterhaltung")
    await client.chat(sid, "weiter")
    system = fake.calls[0]["messages"][0]["content"]
    assert "Fortsetzung einer früheren" in system


async def test_resident_uid_personalizes_prompt(db, soul):
    """A turn owned by an enrolled resident names them in the system prompt (#352)."""
    client, fake = _client(db, soul, [ChatResult(content="ok")])
    sid = await client.create_session("anna")
    await client.chat(sid, "Hallo")
    system = fake.calls[0]["messages"][0]["content"]
    assert "anna" in system
    assert "persönlich" in system


async def test_household_uid_prompt_unchanged(db, soul):
    """The shared household/default uid carries NO personalization block (#352)."""
    client, fake = _client(db, soul, [ChatResult(content="ok")])
    sid = await client.create_session("household")
    await client.chat(sid, "Hallo")
    system = fake.calls[0]["messages"][0]["content"]
    assert "persönlich" not in system
    assert system == "Du bist Solaris."


async def test_prefill_composition_logged_per_shape(db, soul, capsys):
    """The prefill is attributable to its parts at assembly time (#1138), and
    a stable prompt logs the breakdown once, not once per turn."""

    async def handler(_args):
        return "{}"

    tool = Tool(
        name="ha_call_service",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    client, _ = _client(
        db, soul, [ChatResult(content="ok"), ChatResult(content="ok")], tools=[tool]
    )
    sid = await client.create_session("anna")
    await client.chat(sid, "Hallo")
    await client.chat(sid, "Nochmal")

    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if "engine.prompt.composition" in line
    ]
    assert len(lines) == 1
    args = lines[0]["args"]
    assert args["profile"] == "household"
    assert args["tool_count"] == 1
    assert args["tools"] > 0
    assert args["soul"] > 0
    assert args["registry"] == 0  # no HA registry on this profile
    assert args["scaffold"] > 0  # identity block + tool discipline
    parts = args["tools"] + args["soul"] + args["registry"] + args["scaffold"]
    assert abs(args["total"] - parts) <= 3  # per-part rounding only


def test_identity_block_only_for_real_residents():
    from solaris_chat.engine.residents import identity_block

    assert identity_block("anna") != ""
    assert "anna" in identity_block("anna")
    # non-residents: empty/default/guest/anonymous and the configured default_uid
    assert identity_block("") == ""
    assert identity_block("household") == ""
    assert identity_block("guest") == ""
    assert identity_block("default") == ""
    assert identity_block("papa", default_uid="papa") == ""


def test_wer_bin_ich_names_resident_only_when_identified():
    """#384: the "für mich klingst du wie {name}" framing lives inside the
    resident block, so it can name the person only when there IS one. An
    unidentified speaker (no block) leaks no resident name."""
    from solaris_chat.engine.residents import identity_block

    block = identity_block("anna")
    assert "klingst du wie anna" in block
    # Privacy: no block at all for guest/household/off -> no name to reveal.
    for non_resident in ("", "household", "guest", "default"):
        assert identity_block(non_resident) == ""
        assert "anna" not in identity_block(non_resident)


# -- HA tools ------------------------------------------------------------


async def test_ha_blocked_domain_rejected():
    tools = {t.name: t for t in build_ha_tools("http://ha", "token")}
    out = await tools["ha_call_service"].handler(
        {"domain": "shell_command", "service": "run", "entity_id": "x.y"}
    )
    assert "not allowed" in out
    out = await tools["ha_call_service"].handler(
        {"domain": "../../api", "service": "turn_on", "entity_id": "x.y"}
    )
    assert "invalid" in out


# -- registry ------------------------------------------------------------


async def test_registry_prompt_block(monkeypatch):
    reg = EntityRegistry("http://ha", "token")

    async def fake_states():
        return [
            {
                "entity_id": "light.buero",
                "attributes": {"friendly_name": "Bürolicht", "area": "Büro"},
            },
            {"entity_id": "sensor.temp", "attributes": {"friendly_name": "Temp"}},
            {
                "entity_id": "sensor.multisensor_6_air_temperature",
                "attributes": {
                    "friendly_name": "Küchensensor Air temperature",
                    "device_class": "temperature",
                },
            },
            {
                "entity_id": "sensor.waschmaschine_power",
                "attributes": {
                    "friendly_name": "Waschmaschine",
                    "device_class": "power",
                },
            },
        ]

    monkeypatch.setattr(reg, "_fetch_states", fake_states)
    block = await reg.prompt_block()
    assert "light.buero | Bürolicht | Büro" in block
    # No sensor entity is PACKED into the prompt — they're discovered on demand.
    assert "sensor.multisensor_6_air_temperature" not in block
    assert "sensor.waschmaschine_power" not in block
    assert "sensor.temp" not in block
    # ...but the discovery legend tells the model which classes/domains it can
    # fetch with a targeted ha_list_entities query (power + temperature, sorted).
    assert "ha_list_entities" in block
    assert "Sensor-device_class: power, temperature" in block
    assert "read-only domains: sensor" in block
    # sensors are never advertised as a ha_call_service action
    assert "sensor:" not in block


async def test_registry_actions_legend(monkeypatch):
    """#381: each present domain gets its real HA services so the model emits
    cover.open_cover (not the guessed cover.open that 400'd, #379). The legend
    only lists domains actually present, and set_cover_position rides only when
    a cover advertises SUPPORT_SET_POSITION (supported_features bit 2)."""
    reg = EntityRegistry("http://ha", "token")

    async def fake_states():
        return [
            {
                "entity_id": "cover.garage",
                "attributes": {"friendly_name": "Garage", "supported_features": 15},
            },
            {"entity_id": "light.buero", "attributes": {"friendly_name": "Büro"}},
            {"entity_id": "lock.tuer", "attributes": {"friendly_name": "Tür"}},
        ]

    monkeypatch.setattr(reg, "_fetch_states", fake_states)
    block = await reg.prompt_block()
    assert "cover: open_cover/close_cover/stop_cover/set_cover_position" in block
    assert "light: turn_on/turn_off" in block
    assert "lock: lock/unlock" in block
    # absent domains are not listed (keeps the legend tight)
    assert "switch:" not in block and "vacuum:" not in block
    # stable: the legend follows CONTROLLABLE_DOMAINS order, refetch is identical
    reg._fetched_at = 0.0
    assert await reg.prompt_block() == block
    assert block.index("light:") < block.index("cover:") < block.index("lock:")


async def test_registry_set_position_omitted_without_feature(monkeypatch):
    """A cover lacking SUPPORT_SET_POSITION (bit 2) gets no set_cover_position."""
    reg = EntityRegistry("http://ha", "token")

    async def fake_states():
        return [
            {
                "entity_id": "cover.tor",
                "attributes": {"friendly_name": "Tor", "supported_features": 3},
            }
        ]

    monkeypatch.setattr(reg, "_fetch_states", fake_states)
    block = await reg.prompt_block()
    assert "cover: open_cover/close_cover/stop_cover\n" in block + "\n"
    assert "set_cover_position" not in block


async def test_registry_surfaces_cover_device_class(monkeypatch):
    """#382: a garage cover and a blind are both domain=cover; the confirm-first
    safety rule can only tell them apart if device_class rides the cover line."""
    reg = EntityRegistry("http://ha", "token")

    async def fake_states():
        return [
            {
                "entity_id": "cover.garage",
                "attributes": {"friendly_name": "Garage", "device_class": "garage"},
            },
            {
                "entity_id": "cover.rollo",
                "attributes": {"friendly_name": "Rollo", "device_class": "shade"},
            },
        ]

    monkeypatch.setattr(reg, "_fetch_states", fake_states)
    block = await reg.prompt_block()
    assert "cover.garage | Garage | garage" in block
    # the blind keeps its non-safety device_class, distinguishable from garage
    assert "cover.rollo | Rollo | shade" in block


async def test_registry_no_device_class_no_trailing_column(monkeypatch):
    """A cover without a device_class keeps the 3-column shape (cache-stable)."""
    reg = EntityRegistry("http://ha", "token")

    async def fake_states():
        return [
            {"entity_id": "cover.tor", "attributes": {"friendly_name": "Tor"}},
        ]

    monkeypatch.setattr(reg, "_fetch_states", fake_states)
    block = await reg.prompt_block()
    assert "cover.tor | Tor\n" in block


async def test_registry_media_player_legend_includes_play_media(monkeypatch):
    """#511/#512: a media_player advertises play_media so the model can stream
    Jellyfin / radio in one call instead of guessing the service and 400'ing."""
    reg = EntityRegistry("http://ha", "token")

    async def fake_states():
        return [
            {
                "entity_id": "media_player.wohnzimmer",
                "attributes": {"friendly_name": "Wohnzimmer"},
            },
        ]

    monkeypatch.setattr(reg, "_fetch_states", fake_states)
    block = await reg.prompt_block()
    assert "media_player: play_media/" in block
    assert "media_next_track/media_previous_track" in block


def test_tool_discipline_confirms_safety_actions():
    """#382/#558: one crisp confirm-first rule for home-securing actions (locks,
    alarm disarm, garage covers) and act-decisively for everything else. The
    confirm uses offer_choices(ja/nein) AND must NOT run the action this turn —
    it waits for the reply (the #558 bug was asking and executing in one turn)."""
    rule = _TOOL_DISCIPLINE
    assert "Soll ich" in rule
    assert "unlock" in rule and "garage" in rule and "alarm_control_panel" in rule
    # #558: the confirm goes through offer_choices(ja/nein) so chips appear ...
    assert "offer_choices" in rule
    # ... and the same turn dispatches no action tool — it stops and waits.
    assert "KEIN" in rule and "wartest" in rule
    # act decisively on the ordinary domains — no confirmation nag
    assert "ohne Rückfrage" in rule
    for direct in ("Licht", "media_player", "Rollos"):
        assert direct in rule


# -- scheduler -----------------------------------------------------------


def test_timer_crud(db):
    timer = scheduler.add_timer(db, "anna", duration_s=600, label="Pizza")
    listed = scheduler.list_timers(db, "anna")
    assert listed[0]["label"] == "Pizza"
    assert scheduler.list_timers(db, "bert") == []
    assert scheduler.cancel_timer(db, "anna", timer["id"]) is True
    assert scheduler.list_timers(db, "anna") == []


async def test_timer_fires_and_announces(db, monkeypatch):
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    scheduler.add_timer(db, "anna", fire_at=past, label="Tee")
    sched = scheduler.TimerScheduler(db, "http://ha", "token")
    announced = []

    async def fake_announce(timer):
        announced.append(timer["label"])
        return True

    monkeypatch.setattr(sched, "_announce", fake_announce)
    await sched._fire_due()
    assert announced == ["Tee"]
    with sqlite3.connect(db) as conn:
        status = conn.execute("SELECT status FROM engine_timers").fetchone()[0]
    assert status == "fired"


def _capture_announce(monkeypatch, states=None, entity_area=None):
    """Stub aiohttp so _announce sees `states` (default: one fake satellite) and
    records the POST body; `entity_area` stubs the HA area registry the room
    scoping reads. Returns the list the announce payload lands in."""
    posted: list[dict] = []
    states = (
        states if states is not None else [{"entity_id": "assist_satellite.kitchen"}]
    )

    async def fake_snapshot(_self):
        return AreaSnapshot(entity_area=dict(entity_area or {}))

    monkeypatch.setattr(scheduler.AreaRegistry, "snapshot", fake_snapshot)

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return states

        def raise_for_status(self):
            return None

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, _url, **k):
            return _Resp()

        def post(self, _url, *, json, **k):
            posted.append(json)
            return _Resp()

    monkeypatch.setattr(scheduler.aiohttp, "ClientSession", _Session)
    return posted


async def test_alarm_rings_media_when_sound_present(monkeypatch, tmp_path):
    sound = tmp_path / "alarm.ogg"
    sound.write_bytes(b"OggS")
    sched = scheduler.TimerScheduler(
        ":memory:", "http://ha", "token", "media-source://x/alarm.ogg", str(sound)
    )
    posted = _capture_announce(
        monkeypatch, entity_area={"assist_satellite.kitchen": "Küche"}
    )
    assert (
        await sched._announce({"kind": "alarm", "label": "Aufstehen", "room": "Küche"})
        is True
    )
    assert posted == [
        {
            "entity_id": ["assist_satellite.kitchen"],
            "media_id": "media-source://x/alarm.ogg",
        }
    ]


async def test_alarm_falls_back_to_tts_when_sound_missing(monkeypatch, tmp_path):
    sched = scheduler.TimerScheduler(
        ":memory:",
        "http://ha",
        "token",
        "media-source://x/alarm.ogg",
        str(tmp_path / "absent.ogg"),
    )
    posted = _capture_announce(monkeypatch)
    assert await sched._announce({"kind": "alarm", "label": "", "room": ""}) is True
    assert posted[0]["message"] == "Es ist Zeit aufzustehen."
    assert "media_id" not in posted[0]


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("timer", "Der Timer Tee ist abgelaufen."),
        ("reminder", "Erinnerung: Tee"),
    ],
)
async def test_timer_and_reminder_keep_tts(monkeypatch, tmp_path, kind, message):
    sound = tmp_path / "alarm.ogg"
    sound.write_bytes(b"OggS")
    sched = scheduler.TimerScheduler(
        ":memory:", "http://ha", "token", "media-source://x/alarm.ogg", str(sound)
    )
    posted = _capture_announce(
        monkeypatch, entity_area={"assist_satellite.kitchen": "Küche"}
    )
    assert (
        await sched._announce({"kind": kind, "label": "Tee", "room": "Küche"}) is True
    )
    assert posted[0]["message"] == message
    assert "media_id" not in posted[0]


# #1187: a fired timer speaks only where it was set — reminder text is often
# private, so it must not be read out on every satellite in the house.


def _room_sched() -> scheduler.TimerScheduler:
    return scheduler.TimerScheduler(":memory:", "http://ha", "token")


_TWO_SATS = [
    {"entity_id": "assist_satellite.kitchen"},
    {"entity_id": "assist_satellite.bedroom"},
]
_TWO_AREAS = {
    "assist_satellite.kitchen": "Küche",
    "assist_satellite.bedroom": "Schlafzimmer",
}


async def test_announce_rings_only_the_originating_room(monkeypatch):
    posted = _capture_announce(monkeypatch, states=_TWO_SATS, entity_area=_TWO_AREAS)
    ok = await _room_sched()._announce(
        {"kind": "reminder", "label": "Arzttermin", "room": "Küche"}
    )
    assert ok is True
    assert posted[0]["entity_id"] == ["assist_satellite.kitchen"]
    assert posted[0]["message"] == "Erinnerung: Arzttermin"


async def test_announce_without_a_room_rings_everywhere_without_the_label(monkeypatch):
    # App/browser-set timer: no originating device, so it is never silently
    # dropped — it rings house-wide, but the private label stays off the air.
    posted = _capture_announce(monkeypatch, states=_TWO_SATS, entity_area=_TWO_AREAS)
    ok = await _room_sched()._announce(
        {"kind": "reminder", "label": "Arzttermin", "room": ""}
    )
    assert ok is True
    assert sorted(posted[0]["entity_id"]) == [
        "assist_satellite.bedroom",
        "assist_satellite.kitchen",
    ]
    assert posted[0]["message"] == "Erinnerung."


async def test_announce_falls_back_when_the_originating_satellite_is_offline(
    monkeypatch,
):
    posted = _capture_announce(
        monkeypatch,
        states=[
            {"entity_id": "assist_satellite.kitchen", "state": "unavailable"},
            {"entity_id": "assist_satellite.bedroom", "state": "idle"},
        ],
        entity_area=_TWO_AREAS,
    )
    ok = await _room_sched()._announce(
        {"kind": "reminder", "label": "Arzttermin", "room": "Küche"}
    )
    assert ok is True
    assert posted[0]["entity_id"] == ["assist_satellite.bedroom"]
    assert posted[0]["message"] == "Erinnerung."


def test_add_timer_records_the_originating_room(db):
    scheduler.add_timer(db, "anna", duration_s=60, label="Tee", room="Küche")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT room FROM engine_timers").fetchone()[0] == "Küche"


# -- trace shape ---------------------------------------------------------


def test_trace_record_shape():
    rec = TraceRecorder()
    record = rec.record(
        session_id="s1",
        profile="household",
        model="gemma4:e2b",
        messages=[
            {"role": "system", "content": "x" * 400},
            {"role": "user", "content": "y" * 100},
        ],
        tools=[{"type": "function", "function": {"name": "t1"}}],
        content="answer",
        thinking="",
        tool_calls=[],
        prompt_tokens=125,
        completion_tokens=10,
        wall_s=1.5,
        context_window=32768,
    )
    assert record["prompt_tokens"] == 125
    assert record["context_free"] == 32768 - 125
    assert record["tools"][0]["name"] == "t1"
    # block split sums to the ground-truth total
    assert sum(record["blocks_tok"].values()) + record["tools_tok"] == 125
    detail = rec.detail(record["id"])
    assert detail["response"]["final"] == "answer"
    assert json.dumps(detail)  # JSON-serialisable end to end


# --- conversation-scoped prompt history (#1067) ---------------------------


def test_prompt_history_returns_only_the_requested_conversation(db):
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "user", "alt", conversation_id="conv-a")
    store.append_message(db, sid, "assistant", "alte Antwort", conversation_id="conv-a")
    store.append_message(db, sid, "user", "neu", conversation_id="conv-b")

    msgs = store.prompt_history(db, sid, "conv-b")
    assert [m["content"] for m in msgs] == ["neu"]
    assert len(store.history(db, sid)) == 3  # full reader untouched


def test_prompt_history_skips_legacy_rows_without_a_conversation(db):
    """The rows already on the box carry no conversation id. They must drop out
    of every future prompt without being rewritten — and stay in the UI."""
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "assistant", "Alex wurde als A - L - E - X erkannt!")
    store.append_message(db, sid, "user", "frisch", conversation_id="conv-new")

    msgs = store.prompt_history(db, sid, "conv-new")
    assert [m["content"] for m in msgs] == ["frisch"]
    assert any("erkannt" in m["content"] for m in store.history(db, sid))


def test_prompt_history_excludes_hidden_rows(db):
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "user", "frage", conversation_id="c1")
    store.append_message(
        db, sid, "assistant", "Skriptzeile", conversation_id="c1", in_prompt=False
    )

    assert [m["content"] for m in store.prompt_history(db, sid, "c1")] == ["frage"]
    assert len(store.get_session(db, sid, "anna")["messages"]) == 2


def test_prompt_history_trims_a_leading_orphan_tool_row(db):
    """A head cut by truncation (or a hidden pass) can leave a dangling tool
    result — Ollama rejects that shape, so open on a standalone turn."""
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "tool", "verwaistes Ergebnis", conversation_id="c1")
    store.append_message(db, sid, "user", "frage", conversation_id="c1")

    assert [m["role"] for m in store.prompt_history(db, sid, "c1")] == ["user"]


def test_prompt_history_empty_without_a_conversation_id(db):
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "user", "x", conversation_id="c1")
    assert store.prompt_history(db, sid, "") == []


def test_resolve_conversation_reuses_within_the_idle_gap(db):
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "user", "erste", conversation_id="c1")
    assert store.resolve_conversation_id(db, sid) == "c1"


def test_resolve_conversation_starts_fresh_after_the_idle_gap(db):
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "user", "gestern", conversation_id="c1")
    _age_last_message(db, sid, seconds=3600)

    assert store.resolve_conversation_id(db, sid) not in ("", "c1")


def test_resolve_conversation_honours_a_requested_id(db):
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "user", "erste", conversation_id="c1")
    assert store.resolve_conversation_id(db, sid, requested="ha-42") == "ha-42"


def test_resolve_conversation_rejects_a_stale_requested_id(db):
    """HA re-sending yesterday's conversation id must not resurrect it."""
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "user", "gestern", conversation_id="ha-42")
    _age_last_message(db, sid, seconds=3600)

    assert store.resolve_conversation_id(db, sid, requested="ha-42") != "ha-42"


def test_resolve_conversation_ignores_hidden_rows_for_the_boundary(db):
    """A hidden enrolment turn must not split the resident's conversation."""
    sid = store.create_session(db, "anna")
    store.append_message(db, sid, "user", "frage", conversation_id="c1")
    store.append_message(
        db, sid, "assistant", "Skriptzeile", conversation_id="c1", in_prompt=False
    )
    assert store.resolve_conversation_id(db, sid) == "c1"


def test_truncated_head_still_yields_a_valid_prompt(db):
    sid = store.create_session(db, "anna")
    for i in range(6):
        store.append_message(db, sid, "user", f"frage {i} " * 40, conversation_id="c1")
        store.append_message(db, sid, "assistant", f"antwort {i}", conversation_id="c1")
    store.truncate_session_head(db, sid, 40)

    msgs = store.prompt_history(db, sid, "c1")
    assert msgs and msgs[0]["role"] == "user"


def _age_last_message(db_path: str, session_id: str, *, seconds: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE engine_messages SET created_at = datetime('now', ?)"
        " WHERE session_id = ? AND seq = (SELECT MAX(seq) FROM engine_messages"
        "                                  WHERE session_id = ?)",
        (f"-{seconds} seconds", session_id, session_id),
    )
    conn.commit()
    conn.close()
