"""Per-resident scope on the chat endpoints (#1168/#1169).

`/api/chat` and `/api/chat/stream` used to take the body's `session_id` on
trust, so any resident could read and append to another resident's session by
supplying its (deterministic, computable) id — and `effective_uid` mapped every
caller onto the owner of the shared "Wartung" admin-ops session. Both are gated
here: a session whose owner is somebody else is refused, and the shared Wartung
row is only the caller's when they are an Authelia admin.
"""

from __future__ import annotations

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
