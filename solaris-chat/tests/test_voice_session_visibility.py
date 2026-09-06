"""Voice turns land in the shared "Zuhause" every resident opens (#649).

With speaker-ID off, all voice is anonymous-household: the facade persists
each spoken turn via `respond_session(uid="household")` into the deterministic
`household_session_id("household")` row (owner_uid=`household`). Before #649 the
browser's Zuhause opened a DIFFERENT row per resident (`household_session_id(
<Remote-User>)`), so the spoken history was invisible to everyone logged in.

The fix re-points `whoami` at the ONE shared row and admits any resident to it
via `effective_uid`, while a typed turn keeps its caller's identity (turn_uid)
so timers/facts stay theirs. A future speaker-ID hit still lands in the
resident's own session — the regression guard here proves that path is intact.
"""

from __future__ import annotations

import sqlite3

import pytest
from solaris_chat.engine import client as engine_client
from solaris_chat.engine import store
from solaris_chat.engine.llama_server import ChatResult
from solaris_chat.engine.tools import Tool

from tests.test_engine import _SCHEMA
from tests.test_facade import _app, _engine

pytestmark = pytest.mark.asyncio


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


def _read_session(client, session_id: str, remote_user: str):
    return client.get(
        f"/api/sessions/{session_id}",
        headers={"Remote-User": remote_user},
    )


# ---- (a) a voice turn (uid -> household) persists into the shared row ---------


async def test_voice_turn_persists_into_shared_household_session(db, soul):
    # The facade's respond_session(uid="household") — what a stash-MISS voice
    # turn runs — writes into household_session_id("household"), the shared row.
    client, _ = _engine(
        db, soul, [ChatResult(content="21 Grad.", prompt_tokens=5, completion_tokens=2)]
    )
    events = [
        e async for e in client.respond_session("Wie warm ist es?", uid="household")
    ]
    assert events[-1]["type"] == "run.completed"
    shared = store.household_session_id("household")
    msgs = store.get_session(db, shared, "household")["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"].endswith("Wie warm ist es?")


# ---- (b) whoami returns the SAME shared id for any Remote-User ----------------


async def test_whoami_household_id_is_shared_for_every_resident(
    aiohttp_client, db, soul
):
    app, _ = _app(db, soul, [])
    http = await aiohttp_client(app)
    shared = store.household_session_id("household")
    for remote_user in ("mdopp", "anna", "lena"):
        body = await (
            await http.get("/api/whoami", headers={"Remote-User": remote_user})
        ).json()
        assert body["uid"] == remote_user
        # One shared row, not household_session_id(<remote_user>).
        assert body["household_session_id"] == shared
        assert body["household_session_id"] != store.household_session_id(remote_user)


# ---- (c) a resident GET on the shared session sees the voice turns ------------


async def test_resident_get_shared_session_sees_voice_turns(aiohttp_client, db, soul):
    # Seed the shared row as the voice facade would, then read it as a resident.
    shared = store.ensure_household_session(db, "household")
    store.append_message(db, shared, "user", "Ist die Haustür zu?")
    store.append_message(db, shared, "assistant", "Ja, verriegelt.")
    app, _ = _app(db, soul, [])
    http = await aiohttp_client(app)
    resp = await _read_session(http, shared, "mdopp")
    assert resp.status == 200
    body = await resp.json()
    contents = [m["content"] for m in body["session"]["messages"]]
    assert any("Haustür" in c for c in contents)
    # A resident's OWN personal session stays private to them (scope unchanged).
    mine = store.create_session(db, "anna", title="Privat")
    store.append_message(db, mine, "user", "geheim")
    other = await _read_session(http, mine, "mdopp")
    assert (await other.json())["ok"] is False  # not_found for a non-owner


# ---- (d) a resident subscribing to the shared session is not 403 -------------


async def test_resident_session_events_shared_not_forbidden(aiohttp_client, db, soul):
    store.ensure_household_session(db, "household")
    shared = store.household_session_id("household")
    app, _ = _app(db, soul, [])  # bus is None -> stream ends immediately
    http = await aiohttp_client(app)
    resp = await http.get(
        f"/api/sessions/{shared}/events", headers={"Remote-User": "lena"}
    )
    assert resp.status == 200  # admitted (would be 403 pre-#649)
    # A resident's OWN foreign session stays 403 (owner-scoped mirror intact).
    mine = store.create_session(db, "anna", title="Privat")
    forbidden = await http.get(
        f"/api/sessions/{mine}/events", headers={"Remote-User": "lena"}
    )
    assert forbidden.status == 403


# ---- (e) a typed turn into the shared session runs under the CALLER's uid ----


async def test_typed_turn_into_shared_session_keeps_caller_uid(
    aiohttp_client, db, soul
):
    # A resident types into the shared Zuhause: the session OWNER is household,
    # but the turn (timers/facts) must run under the typing resident — proven by
    # a tool observing current_uid.
    seen: list[str] = []

    async def handler(_args):
        seen.append(engine_client.current_uid.get())
        return "{}"

    tool = Tool(
        name="timer_set",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    household, _ = _engine(
        db,
        soul,
        [
            ChatResult(
                content="",
                tool_calls=[{"function": {"name": "timer_set", "arguments": {}}}],
                prompt_tokens=5,
            ),
            ChatResult(content="Gestellt.", prompt_tokens=6, completion_tokens=1),
        ],
        tools=[tool],
    )
    from solaris_chat.server import build_app

    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    http = await aiohttp_client(app)
    shared = store.ensure_household_session(db, "household")
    resp = await http.post(
        "/api/chat/stream",
        headers={"Remote-User": "mdopp"},
        json={"input": "Stell einen Timer", "session_id": shared, "topic": "household"},
    )
    assert resp.status == 200
    await resp.text()  # drain the SSE stream
    assert seen == ["mdopp"]  # the turn ran as the typing resident, not household
    # The session itself stays owned by household (the shared row is unchanged).
    assert store.session_owner(db, shared) == "household"


# ---- (f) speaker-ID stash HIT still lands in the resident's own session ------


async def test_speaker_id_hit_lands_in_resident_session(db, soul):
    # Regression guard for the future speaker-ID re-enable: an identified voice
    # turn runs respond_session(uid=<resident>) and MUST persist into that
    # resident's own household session, NOT the shared household one.
    client, _ = _engine(
        db, soul, [ChatResult(content="Ok.", prompt_tokens=5, completion_tokens=1)]
    )
    events = [e async for e in client.respond_session("Meine Notiz", uid="mdopp")]
    assert events[-1]["type"] == "run.completed"
    resident_row = store.household_session_id("mdopp")
    shared = store.household_session_id("household")
    assert resident_row != shared
    # The turn is in mdopp's own row …
    assert store.get_session(db, resident_row, "mdopp")["messages"]
    # … and the shared household row got nothing from this identified turn.
    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM engine_messages WHERE session_id = ?", (shared,)
        ).fetchone()[0]
    assert count == 0
