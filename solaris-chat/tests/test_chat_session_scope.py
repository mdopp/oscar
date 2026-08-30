"""Per-resident scope on the chat endpoints (#1168/#1169/#1287).

`/api/chat` and `/api/chat/stream` used to take the body's `session_id` on
trust, so any resident could read and append to another resident's session by
supplying its (deterministic, computable) id — and `effective_uid` mapped every
caller onto the owner of the shared "Wartung" admin-ops session. Both are gated
here: a session whose owner is somebody else is refused, and the shared Wartung
row is only the caller's when they are an Authelia admin.

`/api/chat/cancel` was the same hole one endpoint over (#1287): it set the
session's cancel event without asking who was calling, so any resident could
abort another session's in-flight turn — including the admin ops turn, whose id
is computable. It now runs the identical gate.
"""

from __future__ import annotations

import asyncio
import sqlite3

from solaris_chat.engine import store
from solaris_chat.server import build_app

from .test_server import _FakeEngine

# engine_sessions as migration 0009 creates it (subset the server reads).
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
"""

SHARED_ZUHAUSE = store.household_session_id("household")
SHARED_WARTUNG = store.wartung_session_id("household")


def _db(tmp_path, sessions) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO engine_sessions (id, owner_uid) VALUES (?, ?)", sessions
    )
    conn.commit()
    conn.close()
    return path


def _app(tmp_path, fake, sessions, admin=None):
    return build_app(
        engine=fake,
        engine_admin=admin,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solaris_db_path=_db(tmp_path, sessions),
    )


async def test_chat_refuses_another_residents_session(aiohttp_client, tmp_path):
    fake = _FakeEngine()
    client = await aiohttp_client(_app(tmp_path, fake, [("anna-1", "anna")]))

    r = await client.post(
        "/api/chat",
        json={"input": "was steht in meinem kalender?", "session_id": "anna-1"},
        headers={"Remote-User": "cdopp"},
    )
    assert r.status == 403
    assert (await r.json())["reason"] == "forbidden"
    # No turn ran: neither anna's history read nor cdopp's turn appended.
    assert fake.turns == []


async def test_chat_stream_refuses_another_residents_session(aiohttp_client, tmp_path):
    fake = _FakeEngine()
    client = await aiohttp_client(_app(tmp_path, fake, [("anna-1", "anna")]))

    r = await client.post(
        "/api/chat/stream",
        json={"input": "weiter", "session_id": "anna-1"},
        headers={"Remote-User": "cdopp"},
    )
    assert r.status == 403
    assert fake.turns == []


async def test_chat_allows_the_owner_and_the_shared_zuhause(aiohttp_client, tmp_path):
    fake = _FakeEngine()
    client = await aiohttp_client(
        _app(tmp_path, fake, [("anna-1", "anna"), (SHARED_ZUHAUSE, "household")])
    )

    r = await client.post(
        "/api/chat",
        json={"input": "weiter", "session_id": "anna-1"},
        headers={"Remote-User": "anna"},
    )
    assert r.status == 200

    # The shared "Zuhause" row (#649) is owned by default_uid but every resident
    # acts in it — the owner check must not close that door.
    r = await client.post(
        "/api/chat",
        json={"input": "wer ist da?", "session_id": SHARED_ZUHAUSE},
        headers={"Remote-User": "cdopp"},
    )
    assert r.status == 200
    assert [sid for sid, _ in fake.turns] == ["anna-1", SHARED_ZUHAUSE]


async def test_unknown_caller_gets_a_fresh_session_but_not_someone_elses(
    aiohttp_client, tmp_path
):
    # Speaker-ID is off in production, so a voice/loopback caller carries no
    # identity header and resolves to `default_uid`. Such a caller must still get
    # its own new session, and must not inherit an existing resident's one.
    fake = _FakeEngine()
    client = await aiohttp_client(_app(tmp_path, fake, [("anna-1", "anna")]))

    r = await client.post("/api/chat", json={"input": "hallo"})
    assert r.status == 200
    assert (await r.json())["session_id"] == "sess-1"

    r = await client.post("/api/chat", json={"input": "hallo", "session_id": "anna-1"})
    assert r.status == 403


async def test_non_admin_cannot_chat_into_the_shared_wartung_session(
    aiohttp_client, tmp_path
):
    # The Wartung id is a fixed uuid5 anyone can compute (#1169), so knowing it
    # must not admit a non-admin to the admin ops conversation.
    fake, admin_gw = _FakeEngine(), _FakeEngine()
    client = await aiohttp_client(
        _app(tmp_path, fake, [(SHARED_WARTUNG, "household")], admin=admin_gw)
    )

    r = await client.post(
        "/api/chat",
        json={"input": "deploy status", "session_id": SHARED_WARTUNG},
        headers={"Remote-User": "cdopp", "Remote-Groups": "family"},
    )
    assert r.status == 403
    assert fake.turns == [] and admin_gw.turns == []


async def test_admin_still_chats_into_the_shared_wartung_session(
    aiohttp_client, tmp_path
):
    fake, admin_gw = _FakeEngine(), _FakeEngine()
    client = await aiohttp_client(
        _app(tmp_path, fake, [(SHARED_WARTUNG, "household")], admin=admin_gw)
    )

    r = await client.post(
        "/api/chat",
        json={"input": "deploy status", "session_id": SHARED_WARTUNG},
        headers={"Remote-User": "mdopp", "Remote-Groups": "admins"},
    )
    assert r.status == 200
    assert [sid for sid, _ in admin_gw.turns] == [SHARED_WARTUNG]
    assert fake.turns == []


class _SlowEngine(_FakeEngine):
    """Streams forever until the turn's cancel event fires."""

    async def chat_stream(
        self,
        session_id,
        text,
        images=None,
        reasoning_effort="none",
        suggest_answers=False,
        turn_uid="",
    ):
        self.turns.append((session_id, text))
        while True:
            yield {"type": "assistant.delta", "data": {"delta": "x"}}
            await asyncio.sleep(0.01)


async def _start_stream(client, session_id, headers):
    resp = await client.post(
        "/api/chat/stream",
        json={"input": "erzähl mir was langes", "session_id": session_id},
        headers=headers,
    )
    assert resp.status == 200
    await resp.content.readuntil(b"event: session")
    await resp.content.readuntil(b"event: delta")
    return resp


async def test_cancel_refuses_another_residents_stream(aiohttp_client, tmp_path):
    # /api/chat/cancel used to go straight to the process-wide cancels dict, so
    # any resident could abort a turn in a session they don't own (#1287).
    fake = _SlowEngine()
    client = await aiohttp_client(_app(tmp_path, fake, [("anna-1", "anna")]))
    stream = await _start_stream(client, "anna-1", {"Remote-User": "anna"})

    r = await client.post(
        "/api/chat/cancel",
        json={"session_id": "anna-1"},
        headers={"Remote-User": "cdopp"},
    )
    assert r.status == 403
    assert (await r.json())["reason"] == "forbidden"
    # Anna's turn is still running: the stream keeps producing deltas.
    await stream.content.readuntil(b"event: delta")

    # The owner still cancels her own turn.
    r = await client.post(
        "/api/chat/cancel",
        json={"session_id": "anna-1"},
        headers={"Remote-User": "anna"},
    )
    assert await r.json() == {"ok": True, "cancelled": True}
    assert "event: cancelled" in await stream.text()


async def test_non_admin_cannot_cancel_the_shared_wartung_turn(
    aiohttp_client, tmp_path
):
    # The Wartung id is a fixed uuid5 anyone can compute (#1169), so knowing it
    # must not admit a non-admin to killing the admin ops turn.
    fake, admin_gw = _FakeEngine(), _SlowEngine()
    client = await aiohttp_client(
        _app(tmp_path, fake, [(SHARED_WARTUNG, "household")], admin=admin_gw)
    )
    stream = await _start_stream(
        client, SHARED_WARTUNG, {"Remote-User": "mdopp", "Remote-Groups": "admins"}
    )

    r = await client.post(
        "/api/chat/cancel",
        json={"session_id": SHARED_WARTUNG},
        headers={"Remote-User": "cdopp", "Remote-Groups": "family"},
    )
    assert r.status == 403
    await stream.content.readuntil(b"event: delta")

    r = await client.post(
        "/api/chat/cancel",
        json={"session_id": SHARED_WARTUNG},
        headers={"Remote-User": "mdopp", "Remote-Groups": "admins"},
    )
    assert await r.json() == {"ok": True, "cancelled": True}
    assert "event: cancelled" in await stream.text()


async def test_any_resident_still_cancels_the_shared_zuhause(aiohttp_client, tmp_path):
    # The shared Zuhause (#649) is one family conversation: whoever is looking
    # at it may stop the turn, exactly as before the owner check.
    fake = _SlowEngine()
    client = await aiohttp_client(_app(tmp_path, fake, [(SHARED_ZUHAUSE, "household")]))
    stream = await _start_stream(client, SHARED_ZUHAUSE, {"Remote-User": "anna"})

    r = await client.post(
        "/api/chat/cancel",
        json={"session_id": SHARED_ZUHAUSE},
        headers={"Remote-User": "cdopp"},
    )
    assert await r.json() == {"ok": True, "cancelled": True}
    assert "event: cancelled" in await stream.text()


async def test_cancel_of_an_unknown_session_is_still_a_noop(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path, _FakeEngine(), [("anna-1", "anna")]))

    r = await client.post(
        "/api/chat/cancel",
        json={"session_id": "no-such-session"},
        headers={"Remote-User": "cdopp"},
    )
    assert r.status == 200
    assert await r.json() == {"ok": True, "cancelled": False}
