"""Chat-proxy routing across the two engine gateways (#293).

The household gateway (`engine`, :8642) serves every resident session; the admin
gateway (`engine_admin`, :8643) serves only the admin-gated servicebay-maintenance
path. These tests pin the routing contract: a household turn lands on household, a
maintenance turn lands on admin, a non-admin never reaches admin, and the #278
dropdown's admin persona selects the admin gateway — all server-enforced.
"""

from __future__ import annotations

import sqlite3

from solaris_chat import personalities, settings_store, topics_store
from solaris_chat.engine import store
from solaris_chat.server import build_app

from .test_server import _FakeEngine

ADMIN_HDRS = {"Remote-User": "mdopp", "Remote-Groups": "admins"}
RESIDENT_HDRS = {"Remote-User": "cdopp", "Remote-Groups": "family"}

# Minimal session_topics schema (migration 0005) so create can persist a primary
# topic and follow-up routing can read it back, plus engine_sessions so the
# durable household session (#345/#419) can be created on a household first turn.
_SCHEMA = """
CREATE TABLE topics (
  slug TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'resident',
  owner_uid TEXT
);
CREATE TABLE session_topics (
  session_id TEXT NOT NULL,
  topic_slug TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'secondary',
  owner_uid TEXT NOT NULL,
  PRIMARY KEY (session_id, topic_slug)
);
CREATE UNIQUE INDEX session_topics_one_primary_idx
  ON session_topics (session_id) WHERE role = 'primary';
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


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO topics (slug, display_name, scope) VALUES (?, ?, ?)",
        ("household", "Zuhause", "system"),
    )
    conn.commit()
    conn.close()
    return path


def _app(household, admin, tmp_path):
    return build_app(
        engine=household,
        engine_admin=admin,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
    )


def _pref_app(household, tmp_path, *, pref="thorough"):
    db = _db(tmp_path)
    settings_store.set_other_model_pref(db, pref)
    return build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )


async def test_household_chat_routes_to_household_gateway(aiohttp_client, tmp_path):
    household, admin = _FakeEngine(), _FakeEngine()
    client = await aiohttp_client(_app(household, admin, tmp_path))

    resp = await client.post(
        "/api/chat", json={"input": "wie spät ist es?"}, headers=RESIDENT_HDRS
    )
    assert resp.status == 200
    # A first-turn resident chat creates + turns on household; admin untouched.
    assert household.created == ["cdopp"]
    assert household.turns and household.turns[0][0] == "sess-1"
    assert admin.created == []
    assert admin.turns == []


async def test_resident_followup_turn_routes_to_household(aiohttp_client, tmp_path):
    # A resident reusing an existing (household) session id keeps every follow-up
    # turn on the household gateway — the pinned "Zuhause" chat (#237) is a normal
    # resident session and never leaks onto admin.
    household, admin = _FakeEngine(), _FakeEngine()
    client = await aiohttp_client(_app(household, admin, tmp_path))

    resp = await client.post(
        "/api/chat",
        json={"input": "mach das licht an", "session_id": "sess-9"},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert household.turns and household.turns[0][0] == "sess-9"
    assert admin.turns == []


async def test_maintenance_session_create_and_turns_route_to_admin(
    aiohttp_client, tmp_path
):
    household, admin = _FakeEngine(), _FakeEngine()
    client = await aiohttp_client(_app(household, admin, tmp_path))

    # Admin opens the servicebay-maintenance session: created on the ADMIN
    # gateway with the live soul + maintenance marker, household untouched.
    resp = await client.post(
        "/api/sessions?persona=servicebay-maintenance", headers=ADMIN_HDRS
    )
    body = await resp.json()
    assert resp.status == 200
    sid = body["session_id"]
    assert admin.created == ["mdopp"]
    assert admin.maintenance == [True]
    assert household.created == []

    # A follow-up turn carrying that session id routes back to the SAME (admin)
    # gateway — engine session state is per-gateway, so the session must stay put.
    resp = await client.post(
        "/api/chat", json={"input": "status", "session_id": sid}, headers=ADMIN_HDRS
    )
    assert resp.status == 200
    assert admin.turns and admin.turns[0][0] == sid
    assert household.turns == []


async def test_non_admin_maintenance_create_forbidden_no_admin_gateway(
    aiohttp_client,
    tmp_path,
):
    household, admin = _FakeEngine(), _FakeEngine()
    client = await aiohttp_client(_app(household, admin, tmp_path))

    resp = await client.post(
        "/api/sessions?persona=servicebay-maintenance", headers=RESIDENT_HDRS
    )
    assert resp.status == 403
    # Neither gateway created a session.
    assert admin.created == [] and household.created == []


async def test_non_admin_admin_persona_turn_never_reaches_admin(
    aiohttp_client, tmp_path
):
    # A non-admin sending the admin/maintenance persona on a chat turn is routed
    # to household, never admin — the Remote-Groups gate holds at the router.
    household, admin = _FakeEngine(), _FakeEngine()
    client = await aiohttp_client(_app(household, admin, tmp_path))

    resp = await client.post(
        "/api/chat",
        json={"input": "status", "personality": personalities.MAINTENANCE_ID},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert household.turns and admin.turns == []
    assert admin.created == []


async def test_non_admin_with_known_admin_session_id_stays_household(
    aiohttp_client, tmp_path
):
    # Even presenting a session id that lives on the admin gateway, a non-admin
    # is routed to household — knowing an id can't escalate the gateway choice.
    household, admin = _FakeEngine(), _FakeEngine()
    client = await aiohttp_client(_app(household, admin, tmp_path))

    resp = await client.post(
        "/api/chat",
        json={"input": "status", "session_id": "maint-1"},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert household.turns and household.turns[0][0] == "maint-1"
    assert admin.turns == []


async def test_admin_dropdown_persona_routes_new_chat_to_admin(
    aiohttp_client, tmp_path
):
    # The #278 dropdown's "Admin" option sends personality=servicebay-maintenance
    # on a fresh chat; an admin caller routes that create + turn to the admin
    # gateway (the dropdown selects the profile/gateway, server re-checks the gate).
    household, admin = _FakeEngine(), _FakeEngine()
    client = await aiohttp_client(_app(household, admin, tmp_path))

    resp = await client.post(
        "/api/chat",
        json={"input": "deploy status", "personality": personalities.MAINTENANCE_ID},
        headers=ADMIN_HDRS,
    )
    assert resp.status == 200
    assert admin.created == ["mdopp"]
    assert admin.turns and admin.turns[0][0] == "sess-1"
    assert household.created == [] and household.turns == []


async def test_admin_household_persona_still_routes_to_household(
    aiohttp_client, tmp_path
):
    # An admin choosing a normal household persona (e.g. technical) is a resident
    # chat — it must stay on the household gateway, not leak onto admin.
    household, admin = _FakeEngine(), _FakeEngine()
    client = await aiohttp_client(_app(household, admin, tmp_path))

    resp = await client.post(
        "/api/chat",
        json={"input": "erklär mir das", "personality": "technical"},
        headers=ADMIN_HDRS,
    )
    assert resp.status == 200
    assert household.turns and admin.turns == []


async def test_stream_maintenance_session_routes_to_admin(aiohttp_client, tmp_path):
    household = _FakeEngine()
    admin = _FakeEngine(events=[{"type": "assistant.delta", "data": {"delta": "ok"}}])
    client = await aiohttp_client(_app(household, admin, tmp_path))

    resp = await client.post(
        "/api/sessions?persona=servicebay-maintenance", headers=ADMIN_HDRS
    )
    sid = (await resp.json())["session_id"]

    resp = await client.post(
        "/api/chat/stream",
        json={"input": "logs", "session_id": sid},
        headers=ADMIN_HDRS,
    )
    assert resp.status == 200
    await resp.text()
    assert admin.turns and admin.turns[0][0] == sid
    assert household.turns == []


# ---- pinned "Wartung" admin ops chat (#786) ----


async def test_whoami_exposes_wartung_id_only_for_admin(aiohttp_client, tmp_path):
    # The pinned Wartung row is admin-only: whoami hands its deterministic
    # session id to an admin (and ensures the row exists), but NOT to a
    # household user — so a resident's UI never learns the id and can't render
    # or open the row.
    household = _FakeEngine()
    db = _db(tmp_path)
    app = build_app(
        engine=household,
        engine_admin=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/whoami", headers=ADMIN_HDRS)
    body = await resp.json()
    assert body["is_admin"] is True
    assert body["wartung_session_id"] == store.wartung_session_id("household")

    resp = await client.get("/api/whoami", headers=RESIDENT_HDRS)
    body = await resp.json()
    assert body["is_admin"] is False
    assert body["wartung_session_id"] == ""


async def test_wartung_session_turn_routes_to_admin_gateway(aiohttp_client, tmp_path):
    # A turn into the deterministic Wartung session lands on the admin gateway
    # (its ops soul + SB-MCP toolset), household untouched — the admin acts in
    # the one shared ops row, materialized lazily (profile=admin, maintenance=1)
    # on this first turn.
    household, admin = _FakeEngine(), _FakeEngine()
    db = _db(tmp_path)
    wartung = store.wartung_session_id("household")
    app = build_app(
        engine=household,
        engine_admin=admin,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat",
        json={"input": "restart the media pod", "session_id": wartung},
        headers=ADMIN_HDRS,
    )
    assert resp.status == 200
    assert admin.turns and admin.turns[0][0] == wartung
    assert household.turns == []
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT profile, maintenance FROM engine_sessions WHERE id = ?",
        (wartung,),
    ).fetchone()
    conn.close()
    assert row == ("admin", 1)


async def test_non_admin_wartung_turn_never_reaches_admin(aiohttp_client, tmp_path):
    # Even presenting the Wartung session id, a non-admin is routed to household,
    # never the admin gateway — the Remote-Groups gate holds at the router, so
    # the ops toolset (SB-MCP) is unreachable without admin group membership.
    household, admin = _FakeEngine(), _FakeEngine()
    db = _db(tmp_path)
    wartung = store.wartung_session_id("household")
    app = build_app(
        engine=household,
        engine_admin=admin,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat",
        json={"input": "restart the media pod", "session_id": wartung},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert household.turns and household.turns[0][0] == wartung
    assert admin.turns == []
    # The non-admin turn never materializes the admin ops row.
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM engine_sessions WHERE id = ?", (wartung,)
    ).fetchone()[0]
    conn.close()
    assert n == 0


async def test_falls_back_to_household_when_no_admin_gateway(aiohttp_client):
    # No admin gateway configured (single-instance/offline): admin routing is a
    # no-op — everything stays on household and nothing breaks.
    household = _FakeEngine()
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat",
        json={"input": "status", "personality": personalities.MAINTENANCE_ID},
        headers=ADMIN_HDRS,
    )
    assert resp.status == 200
    assert household.turns


# ---- everyday-chat reasoning preference (#332-followup / #809) ----
# 12b retired 2026-07-13: fast+thorough both run the e4b household gateway; the
# preference sets the per-turn reasoning effort, not a separate gateway.


async def test_household_topic_chat_routes_to_household_even_when_thorough(
    aiohttp_client, tmp_path
):
    # The pinned "Zuhause" chat (primary topic = household) is ALWAYS the fast
    # e4b household gateway, even though the everyday-chat preference is thorough.
    household = _FakeEngine()
    client = await aiohttp_client(_pref_app(household, tmp_path, pref="thorough"))

    resp = await client.post(
        "/api/chat",
        json={"input": "mach das licht an", "topic": "household"},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    # A household first turn lands in the ONE SHARED household session (#649),
    # not a freshly minted `sess-1` — so it never forks per click.
    durable = store.household_session_id("household")
    assert household.turns and household.turns[0][0] == durable
    # Household turns are fast-only regardless of any selector.
    assert household.efforts == ["none"]


async def test_household_first_turn_lands_in_durable_session_once(
    aiohttp_client, tmp_path
):
    # The pinned "Zuhause" first turn (topic=household) must land in the ONE
    # SHARED household session (#649) — owned by default_uid, the same row voice
    # persists to — and never mint a fresh row per click (#419): two separate
    # first turns (e.g. two clicks, even by different residents) reuse the SAME
    # id and leave exactly one engine_sessions row, stamped household primary.
    db = _db(tmp_path)
    durable = store.household_session_id("household")
    for _ in range(2):
        household = _FakeEngine()
        app = build_app(
            engine=household,
            remote_user_header="Remote-User",
            default_uid="household",
            solaris_db_path=db,
            attachments_dir=str(tmp_path / "att"),
        )
        client = await aiohttp_client(app)
        resp = await client.post(
            "/api/chat",
            json={"input": "wie spät ist es?", "topic": "household"},
            headers=RESIDENT_HDRS,
        )
        assert resp.status == 200
        assert (await resp.json())["session_id"] == durable
        # The durable session is created in the store, not via the fake gateway.
        assert household.created == []
        assert household.turns and household.turns[0][0] == durable
        # The turn runs under the typing resident (turn_uid), not the owner.
        assert household.turn_uids[-1] == "cdopp"

    import sqlite3 as _sqlite3

    conn = _sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM engine_sessions WHERE owner_uid = ?", ("household",)
    ).fetchone()[0]
    conn.close()
    assert n == 1
    assert (
        topics_store.get_session_topics(db, durable, "household").get("primary")
        == "household"
    )


async def test_durable_household_session_is_never_compacted(aiohttp_client, tmp_path):
    # Compaction must NOT fork the durable household session into a `Fortsetzung`
    # continuation (it would surface as a second "Zuhause" row, #419). Even with
    # an over-threshold session, a follow-up turn into the durable id stays
    # in-place: the compaction path (get_session/create) is never entered.
    db = _db(tmp_path)
    durable = store.household_session_id("cdopp")
    store.ensure_household_session(db, "cdopp")

    class _CompactingEngine(_FakeEngine):
        def __init__(self):
            super().__init__()
            self.gets: list[str] = []

        async def get_session(self, session_id, uid):
            self.gets.append(session_id)
            # Always over the cap, so a non-guarded path WOULD compact.
            return {"id": session_id, "input_tokens": 10**9, "output_tokens": 0}

    household = _CompactingEngine()
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
        context_window=32768,
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={"input": "und weiter", "session_id": durable},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    # The household guard short-circuits before the compaction path runs.
    assert household.gets == []
    assert household.created == []
    assert household.turns and household.turns[0][0] == durable


async def test_household_followup_reads_persisted_primary_topic(
    aiohttp_client, tmp_path
):
    # A follow-up turn (different in-memory app state) routes to household by the
    # persisted primary topic, not just the first-turn topic hint.
    household = _FakeEngine()
    db = _db(tmp_path)
    settings_store.set_other_model_pref(db, "thorough")
    topics_store.set_primary(db, "sess-42", "household", "cdopp")
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat",
        json={"input": "und im flur?", "session_id": "sess-42"},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert household.turns and household.turns[0][0] == "sess-42"


async def test_other_chat_thorough_reasons_on_household(aiohttp_client, tmp_path):
    # A normal (non-household) chat with the thorough preference stays on the e4b
    # household gateway (no second profile) but runs WITH reasoning — thorough is
    # the effort knob, not a bigger model (#809). A plain turn (no selector, no
    # cue) escalates to "high" purely from the pref.
    household = _FakeEngine()
    client = await aiohttp_client(_pref_app(household, tmp_path, pref="thorough"))

    resp = await client.post(
        "/api/chat", json={"input": "erklär mir das"}, headers=RESIDENT_HDRS
    )
    assert resp.status == 200
    assert household.turns and household.turns[0][0] == "sess-1"
    assert household.efforts == ["high"]


async def test_other_chat_fast_runs_none_but_escalates_on_cue(aiohttp_client, tmp_path):
    # The same normal chat with the fast preference runs "none" on the household
    # gateway, but an explicit "think harder" cue still escalates it to "high".
    household = _FakeEngine()
    client = await aiohttp_client(_pref_app(household, tmp_path, pref="fast"))

    resp = await client.post(
        "/api/chat", json={"input": "erklär mir das"}, headers=RESIDENT_HDRS
    )
    assert resp.status == 200
    assert household.turns
    assert household.efforts == ["none"]

    resp = await client.post(
        "/api/chat", json={"input": "denk mal scharf nach"}, headers=RESIDENT_HDRS
    )
    assert resp.status == 200
    assert household.efforts == ["none", "high"]


async def test_model_put_toggles_effort_not_gateway(aiohttp_client, tmp_path):
    # The admin Model setting is a live effort toggle: after switching to fast, a
    # fresh normal plain turn runs "none" instead of the thorough "high" — both on
    # the household gateway, no restart.
    household = _FakeEngine()
    client = await aiohttp_client(_pref_app(household, tmp_path, pref="thorough"))

    resp = await client.put("/api/model", json={"value": "fast"}, headers=ADMIN_HDRS)
    assert resp.status == 200
    assert (await resp.json())["current"] == "fast"

    resp = await client.post(
        "/api/chat", json={"input": "noch eine frage"}, headers=RESIDENT_HDRS
    )
    assert resp.status == 200
    assert household.turns
    assert household.efforts == ["none"]


async def test_model_get_returns_options_and_current(aiohttp_client, tmp_path):
    household = _FakeEngine()
    client = await aiohttp_client(_pref_app(household, tmp_path, pref="fast"))

    resp = await client.get("/api/model", headers=ADMIN_HDRS)
    assert resp.status == 200
    body = await resp.json()
    assert body["current"] == "fast"
    assert [o["value"] for o in body["options"]] == ["fast", "thorough"]


async def test_model_put_rejects_unknown_value(aiohttp_client, tmp_path):
    household = _FakeEngine()
    client = await aiohttp_client(_pref_app(household, tmp_path))

    resp = await client.put("/api/model", json={"value": "12b"}, headers=ADMIN_HDRS)
    assert resp.status == 400


async def test_model_get_forbidden_for_non_admin(aiohttp_client, tmp_path):
    household = _FakeEngine()
    client = await aiohttp_client(_pref_app(household, tmp_path))

    resp = await client.get("/api/model", headers=RESIDENT_HDRS)
    assert resp.status == 403
