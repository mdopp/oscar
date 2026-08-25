"""Tests for the persisted per-message trace store + endpoint (#306).

Covers the `session_traces` store (persist/list, ordering, per-resident scope,
idempotent re-persist, missing-db degradation) and the turn-time correlation:
the server pulls the proxy's `/__traces__` calls in the turn window, assigns a
per-message trace_id, persists them, and serves them at
`GET /api/sessions/<id>/trace` reopen-consistently.
"""

from __future__ import annotations

import json
import sqlite3

from solaris_chat import trace_store
from solaris_chat.engine.trace import TraceRecorder
from solaris_chat.server import build_app

# The schema the 0007 migration creates, replayed locally so the store + endpoint
# tests run against a real sqlite db without alembic.
_SCHEMA = """
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
CREATE INDEX session_traces_session_idx
  ON session_traces (session_id, owner_uid);
CREATE TABLE engine_sessions (
  id        TEXT PRIMARY KEY,
  owner_uid TEXT NOT NULL
);
"""


def _db(tmp_path, sessions=()) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO engine_sessions (id, owner_uid) VALUES (?, ?)", sessions
    )
    conn.commit()
    conn.close()
    return path


def _step(model="gemma4:e2b", detail_id=0, **over):
    base = {
        "model": model,
        "profile": "household",
        "wall_s": 1.2,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "context_free": 30000,
        "finish_reason": "stop",
        "n_tools": 5,
        "detail_id": detail_id,
    }
    base.update(over)
    return base


def test_persist_then_list_keeps_step_order(tmp_path):
    db = _db(tmp_path)
    steps = [_step(detail_id=0, finish_reason="tool_calls"), _step(detail_id=1)]
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", steps)
    got = trace_store.list_session_trace(db, "sess-1", "mdopp")
    assert [s["step_order"] for s in got] == [0, 1]
    assert [s["detail_id"] for s in got] == [0, 1]
    assert got[0]["finish_reason"] == "tool_calls"
    assert got[1]["model"] == "gemma4:e2b"
    assert got[0]["profile"] == "household"


def test_multiple_turns_list_in_chronological_order(tmp_path):
    # Two turns (distinct trace_ids) persisted in order — the reopen reads them
    # back turn-then-step, matching the live order (#306 acceptance).
    db = _db(tmp_path)
    trace_store.persist_trace(db, "sess-1", "tr-1", "mdopp", [_step(detail_id=0)])
    trace_store.persist_trace(
        db, "sess-1", "tr-2", "mdopp", [_step(detail_id=1), _step(detail_id=2)]
    )
    got = trace_store.list_session_trace(db, "sess-1", "mdopp")
    assert [s["trace_id"] for s in got] == ["tr-1", "tr-2", "tr-2"]
    assert [s["detail_id"] for s in got] == [0, 1, 2]


def test_re_persist_same_trace_id_replaces(tmp_path):
    db = _db(tmp_path)
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", [_step(detail_id=9)])
    trace_store.persist_trace(
        db, "sess-1", "tr-a", "mdopp", [_step(detail_id=0), _step(detail_id=1)]
    )
    got = trace_store.list_session_trace(db, "sess-1", "mdopp")
    assert [s["detail_id"] for s in got] == [0, 1]


def test_interleaved_llm_and_tool_steps_round_trip(tmp_path):
    # A turn's trace is the full step list: an LLM call (tool_calls), a tool
    # execution, then the final LLM call — each persisted with its kind and, for
    # the tool step, the dispatched name + its own wall_s (#346).
    db = _db(tmp_path)
    steps = [
        _step(detail_id=0, finish_reason="tool_calls", step_kind="llm"),
        {"step_kind": "tool", "tool_name": "ha_call_service", "wall_s": 0.42},
        _step(detail_id=2, step_kind="llm"),
    ]
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", steps)
    got = trace_store.list_session_trace(db, "sess-1", "mdopp")
    assert [s["step_kind"] for s in got] == ["llm", "tool", "llm"]
    assert got[1]["tool_name"] == "ha_call_service"
    assert got[1]["wall_s"] == 0.42
    assert got[0]["tool_name"] is None


def test_list_inlines_ha_cards_detail_only(tmp_path):
    # The read-only HA cards (#475) ride the trace as an ha_cards step; its
    # detail_json IS the card payload, so the list endpoint inlines it — but an
    # LLM step's detail_json (the request/response body) must NOT leak into the
    # list (it's fetched on demand via detail_for).
    db = _db(tmp_path)
    cards = '[{"entity_id": "sensor.x", "domain": "sensor", "state": "21"}]'
    steps = [
        _step(detail_id="tr-a:0", detail_json='{"request": {"model": "m"}}'),
        {"step_kind": "ha_cards", "detail_json": cards},
    ]
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", steps)
    got = trace_store.list_session_trace(db, "sess-1", "mdopp")
    assert "detail_json" not in got[0]
    assert got[1]["step_kind"] == "ha_cards"
    assert got[1]["detail_json"] == cards


def test_detail_for_round_trips_persisted_body(tmp_path):
    # The detail body is stored next to the step and read back by detail_id,
    # per-resident scoped (#451). A step without a body returns None.
    db = _db(tmp_path)
    steps = [
        _step(detail_id="tr-a:0", detail_json='{"request": {"model": "m"}}'),
        {"step_kind": "tool", "tool_name": "ha", "wall_s": 0.4},
    ]
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", steps)
    assert (
        trace_store.detail_for(db, "mdopp", "tr-a:0") == '{"request": {"model": "m"}}'
    )
    # Wrong resident, unknown id, missing db all return None.
    assert trace_store.detail_for(db, "lena", "tr-a:0") is None
    assert trace_store.detail_for(db, "mdopp", "tr-a:9") is None
    assert (
        trace_store.detail_for(str(tmp_path / "absent.db"), "mdopp", "tr-a:0") is None
    )


def test_scopes_to_resident(tmp_path):
    db = _db(tmp_path)
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", [_step(detail_id=0)])
    assert trace_store.list_session_trace(db, "sess-1", "lena") == []
    assert len(trace_store.list_session_trace(db, "sess-1", "mdopp")) == 1


def test_missing_db_degrades(tmp_path):
    missing = str(tmp_path / "absent.db")
    # No-op write, empty read — never raises.
    trace_store.persist_trace(missing, "sess-1", "tr-a", "mdopp", [_step()])
    assert trace_store.list_session_trace(missing, "sess-1", "mdopp") == []


def test_empty_steps_is_noop(tmp_path):
    db = _db(tmp_path)
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", [])
    assert trace_store.list_session_trace(db, "sess-1", "mdopp") == []


class _FakeEngine:
    # Client-config flag the server reads (real EngineClient.ephemeral).
    ephemeral = False

    async def create_session(self, uid, system_prompt=None, **kw):
        return "sess-1"

    async def set_title(self, *a, **k):
        pass

    async def chat(
        self, session_id, text, images=None, reasoning_effort="none", turn_uid=""
    ):
        return f"echo: {text}"


def _recorder_with(session_id: str, recorder: TraceRecorder, n: int = 2) -> None:
    """Record `n` engine calls for `session_id` (mimics the engine loop)."""
    for i in range(n):
        recorder.record(
            session_id=session_id,
            profile="household",
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": f"t{i}"}}],
            content="ok",
            thinking="",
            tool_calls=[] if i == n - 1 else [{"function": {"name": "t0"}}],
            prompt_tokens=80 + i,
            completion_tokens=4,
            wall_s=0.5,
            context_window=4096,
        )


class _RecordingEngine(_FakeEngine):
    """Records trace entries during the turn, the way the engine loop does."""

    def __init__(self, recorder):
        self._recorder = recorder

    async def chat(
        self, session_id, text, images=None, reasoning_effort="none", turn_uid=""
    ):
        _recorder_with(session_id, self._recorder)
        return f"echo: {text}"


async def test_turn_persists_engine_steps_and_endpoint_serves_them(
    aiohttp_client, tmp_path
):
    db = _db(tmp_path)
    recorder = TraceRecorder()
    # A pre-turn record for the same session must NOT land in the turn's trace
    # (the t0 bound keeps the steps per-turn).
    _recorder_with("sess-1", recorder, n=1)
    recorder.list_traces()[0]["ts"] -= 3600.0

    app = build_app(
        engine=_RecordingEngine(recorder),
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solaris_db_path=db,
        trace_recorder=recorder,
    )
    client = await aiohttp_client(app)

    r = await client.post(
        "/api/chat",
        json={"input": "Welche Lichter sind an?"},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200

    r = await client.get("/api/sessions/sess-1/trace", headers={"Remote-User": "mdopp"})
    body = await r.json()
    assert body["ok"] is True
    # Only the two in-turn calls (the pre-turn record is dropped), in step
    # order. detail_id is now the stable per-step key (`<trace_id>:<order>`),
    # not the recorder ring id, so it survives a restart (#451).
    assert len(body["steps"]) == 2
    assert all(":" in s["detail_id"] for s in body["steps"])
    assert [s["detail_id"].rsplit(":", 1)[1] for s in body["steps"]] == ["0", "1"]
    assert body["steps"][0]["finish_reason"] == "tool_calls"
    assert body["steps"][0]["profile"] == "household"


async def test_persisted_detail_survives_recorder_restart(aiohttp_client, tmp_path):
    # The crux of #451: run a turn (recorder records the bodies), then build a
    # FRESH app with an EMPTY recorder (simulating an engine restart / reload).
    # The persisted detail body must still serve at /__traces__/<detail_id>.
    db = _db(tmp_path)
    recorder = TraceRecorder()
    app = build_app(
        engine=_RecordingEngine(recorder),
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solaris_db_path=db,
        trace_recorder=recorder,
    )
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/chat", json={"input": "hallo"}, headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200

    # Fresh recorder = no in-process detail ring (ids would restart at 0).
    fresh = TraceRecorder()
    app2 = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solaris_db_path=db,
        trace_recorder=fresh,
    )
    client2 = await aiohttp_client(app2)

    r = await client2.get(
        "/api/sessions/sess-1/trace", headers={"Remote-User": "mdopp"}
    )
    steps = (await r.json())["steps"]
    detail_id = steps[0]["detail_id"]
    assert detail_id and ":" in detail_id

    # The persisted body resolves even though the recorder ring is empty —
    # using the URL-encoded form the browser sends (encodeURIComponent of the
    # `<trace_id>:<order>` key, so the `:` arrives as %3A).
    from urllib.parse import quote

    r = await client2.get(
        "/__traces__/" + quote(detail_id, safe=""), headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    d = await r.json()
    assert d["request"]["model"] == "m"
    assert "messages" in d["request"]
    assert d["response"]["final"] == "ok"

    # Per-resident scope: another resident can't read it.
    r = await client2.get("/__traces__/" + detail_id, headers={"Remote-User": "lena"})
    assert r.status == 404


async def test_trace_detail_served_in_process(aiohttp_client, tmp_path):
    # /__traces__/<id> serves the engine recorder's detail ring directly; an
    # evicted/unknown id is a 404 the panel degrades on. The ring is
    # process-wide, so the read is owner-scoped like the persisted path (#1171):
    # only the owner of the session the call ran in gets the body.
    recorder = TraceRecorder()
    _recorder_with("sess-1", recorder, n=1)

    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solaris_db_path=_db(tmp_path, sessions=[("sess-1", "mdopp")]),
        trace_recorder=recorder,
    )
    client = await aiohttp_client(app)

    r = await client.get("/__traces__/0", headers={"Remote-User": "mdopp"})
    assert r.status == 200
    body = await r.json()
    assert body["response"]["final"] == "ok"
    assert body["request"]["model"] == "m"

    r = await client.get("/__traces__/999", headers={"Remote-User": "mdopp"})
    assert r.status == 404

    # Another resident walking the ring ids sees nothing, not mdopp's prompt.
    r = await client.get("/__traces__/0", headers={"Remote-User": "lena"})
    assert r.status == 404


async def test_trace_ring_detail_of_unknown_session_is_never_served(
    aiohttp_client, tmp_path
):
    # Fail closed (#1171): a ring record whose session has no row (so no owner
    # to compare against) is nobody's to read — not even the household default.
    recorder = TraceRecorder()
    _recorder_with("sess-orphan", recorder, n=1)

    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solaris_db_path=_db(tmp_path),
        trace_recorder=recorder,
    )
    client = await aiohttp_client(app)

    r = await client.get("/__traces__/0", headers={"Remote-User": "mdopp"})
    assert r.status == 404


async def test_endpoint_empty_when_no_trace(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solaris_db_path=db,
    )
    client = await aiohttp_client(app)
    r = await client.get("/api/sessions/none/trace", headers={"Remote-User": "mdopp"})
    body = await r.json()
    assert body == {"ok": True, "steps": []}


def test_step_row_persists_tool_arguments_and_result(tmp_path):
    # A tool step used to persist nothing but its name, so a failed tool call
    # could not be diagnosed from the trace at all (#1241) — the args and the
    # result had to be reconstructed from HA history. They now ride the same
    # detail_json channel as the LLM bodies: fetched on demand by detail_id,
    # per-resident scoped, never inlined into the panel list.
    db = _db(tmp_path)
    rec = {
        "step_kind": "tool",
        "tool_name": "ha_call_service",
        "wall_s": 0.42,
        "arguments": {"domain": "light", "service": "turn_on", "entity_id": "light.x"},
        "output": '{"error": "unknown entity_id: light.x"}',
    }
    steps = [trace_store.step_row(rec, None, "tr-a:0")]
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", steps)

    got = trace_store.list_session_trace(db, "sess-1", "mdopp")
    assert got[0]["step_kind"] == "tool"
    assert got[0]["detail_id"] == "tr-a:0"
    assert "detail_json" not in got[0]  # resident content stays out of the list

    body = json.loads(trace_store.detail_for(db, "mdopp", "tr-a:0"))
    assert body["tool_name"] == "ha_call_service"
    assert body["arguments"]["entity_id"] == "light.x"
    assert "unknown entity_id" in body["result"]
    assert trace_store.detail_for(db, "other", "tr-a:0") is None


def test_step_row_keeps_llm_detail_body(tmp_path):
    # The LLM step is unchanged: its recorded body is what gets persisted, and a
    # step with no body keeps a null detail_id.
    detail = {"request": {"model": "m"}, "response": {"final": "ok"}}
    row = trace_store.step_row({"step_kind": "llm", "model": "m"}, detail, "tr-a:1")
    assert row["detail_id"] == "tr-a:1"
    assert json.loads(row["detail_json"]) == detail
    bodyless = trace_store.step_row({"step_kind": "llm", "model": "m"}, None, "tr-a:2")
    assert bodyless["detail_id"] is None
    assert bodyless["detail_json"] is None
