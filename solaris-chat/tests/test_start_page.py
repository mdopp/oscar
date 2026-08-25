"""Start-page favorites API — aggregator + CRUD + run gate (#646).

Covers the `/api/portal/start` aggregator (personal-vs-household scoping,
live-card enrichment, frequent excluded from the curated lists), the per-id
favorites CRUD, and `/api/favorites/{id}/run` (dispatch on a sensitive/unlisted
tool is refused with 403; a routine action dispatches on the gateway toolbox).
Tables are created with raw SQL copied from migration 0019 — a chat test must
NOT import alembic (CI runs solaris-chat in a clean env without it).
"""

from __future__ import annotations

import json
import re
import sqlite3

from solaris_chat import favorites_store
from solaris_chat.engine.tools import ha
from solaris_chat.server import build_app

# The two tables migration 0019 creates, replayed locally (no alembic).
_SCHEMA = """
CREATE TABLE favorites (
  id        TEXT PRIMARY KEY,
  owner_uid TEXT NOT NULL,
  kind      TEXT NOT NULL CHECK (kind IN ('action','entity','link')),
  label     TEXT NOT NULL,
  payload   TEXT NOT NULL,
  position  INTEGER NOT NULL DEFAULT 0,
  created   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE favorite_usage (
  owner_uid    TEXT NOT NULL,
  kind         TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload      TEXT NOT NULL,
  count        INTEGER NOT NULL DEFAULT 0,
  last_used    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (owner_uid, payload_hash)
);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


class _FakeEngine:
    """Records the one tool dispatch a favorite run makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def dispatch_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps({"ok": True, "say": "erledigt"})


def _app(tmp_path, engine=None, **kw):
    return build_app(
        engine=engine or _FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
        notes_dir=str(tmp_path),
        **kw,
    )


async def test_start_scopes_personal_and_household(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Mein Radio",
        {"tool": "play_radio", "args": {"station": "NDR"}},
    )
    favorites_store.add_favorite(
        db,
        "household",
        "action",
        "Haus Radio",
        {"tool": "play_radio", "args": {"station": "WDR"}},
    )
    favorites_store.add_favorite(
        db, "anna", "action", "Annas", {"tool": "play_radio", "args": {}}
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    assert [f["label"] for f in j["personal"]] == ["Mein Radio"]
    assert [f["label"] for f in j["household"]] == ["Haus Radio"]
    # Anna's private favorite is invisible to mdopp.
    assert all("Annas" not in f["label"] for f in j["personal"] + j["household"])


async def test_start_enriches_entity_with_live_card(
    aiohttp_client, tmp_path, monkeypatch
):
    async def _fake_fetch(url, token, entity_id):
        return ha.card_spec(entity_id, "on", {"friendly_name": "Bürolicht"})

    monkeypatch.setattr("solaris_chat.server.fetch_card", _fake_fetch)
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "buerolicht", {"entity_id": "light.buero"}
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    card = j["personal"][0]["card"]
    assert card["domain"] == "light" and card["state"] == "on"
    # HA reachable → the happy-path signal stays "ok" (#729).
    assert j["ha"] == "ok"


async def test_start_reports_ha_ok_when_reachable(
    aiohttp_client, tmp_path, monkeypatch
):
    async def _fake_fetch(url, token, entity_id):
        return ha.card_spec(entity_id, "on", {"friendly_name": "Bürolicht"})

    monkeypatch.setattr("solaris_chat.server.fetch_card", _fake_fetch)
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "buerolicht", {"entity_id": "light.buero"}
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    assert j["ha"] == "ok"
    assert j["personal"][0].get("card") is not None
    assert "card_unavailable" not in j["personal"][0]


async def test_start_flags_unreachable_when_fetch_returns_none(
    aiohttp_client, tmp_path, monkeypatch
):
    """HA is configured (token present) but every fetch_card returns None (HA
    down / bad token): ha == "unreachable" and the entity item is flagged
    card_unavailable so the client greys it instead of a bare name (#729)."""

    async def _fake_fetch(url, token, entity_id):
        return None

    monkeypatch.setattr("solaris_chat.server.fetch_card", _fake_fetch)
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "buerolicht", {"entity_id": "light.buero"}
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    assert j["ha"] == "unreachable"
    item = j["personal"][0]
    assert item.get("card") is None
    assert item["card_unavailable"] is True


async def test_start_reports_unconfigured_without_ha(aiohttp_client, tmp_path):
    """No hass_url/hass_token → ha == "unconfigured" (a calmer notice) and no
    entity is fetched or flagged unavailable (#729)."""
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "buerolicht", {"entity_id": "light.buero"}
    )
    app = build_app(  # no hass_url/hass_token → HA unconfigured
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    assert j["ha"] == "unconfigured"
    assert "card" not in j["personal"][0]
    assert "card_unavailable" not in j["personal"][0]


async def test_start_ha_status_prefers_watcher(aiohttp_client, tmp_path, monkeypatch):
    """When the HA-WS watcher is wired in, its live status is authoritative: a
    successful fetch but a disconnected watcher still reports "unreachable"."""

    async def _fake_fetch(url, token, entity_id):
        return ha.card_spec(entity_id, "on", {"friendly_name": "Bürolicht"})

    monkeypatch.setattr("solaris_chat.server.fetch_card", _fake_fetch)

    class _Watcher:
        status = "disconnected"

    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "buerolicht", {"entity_id": "light.buero"}
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
        ha_watcher=_Watcher(),
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    assert j["ha"] == "unreachable"


async def test_state_only_flags_unavailable_on_ha_drop(
    aiohttp_client, tmp_path, monkeypatch
):
    """A mid-session HA drop is reflected on the poll branch: ha == "unreachable"
    and the pinned entity id is listed under `unavailable` (#729)."""

    async def _fake_fetch(url, token, entity_id):
        return None

    monkeypatch.setattr("solaris_chat.server.fetch_card", _fake_fetch)
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "garage", {"entity_id": "cover.garage"}
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get(
            "/api/portal/start?state_only=1", headers={"Remote-User": "mdopp"}
        )
    ).json()
    assert j["ha"] == "unreachable"
    assert j["unavailable"] == ["cover.garage"]
    assert j["states"] == {}


async def test_start_state_only_returns_pinned_card_state(
    aiohttp_client, tmp_path, monkeypatch
):
    """The live-refresh tick (#711): `?state_only=1` re-fetches just the pinned
    entities' card state (keyed by entity_id), no frequent/usage lists — so the
    client updates each card in place while #/p/start is the active view."""

    async def _fake_fetch(url, token, entity_id):
        return ha.card_spec(
            entity_id, "open", {"friendly_name": "Garagentor", "device_class": "garage"}
        )

    monkeypatch.setattr("solaris_chat.server.fetch_card", _fake_fetch)
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "garage", {"entity_id": "cover.garage"}
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get(
            "/api/portal/start?state_only=1", headers={"Remote-User": "mdopp"}
        )
    ).json()
    assert j["ok"] is True
    assert "personal" not in j and "frequent" not in j
    card = j["states"]["cover.garage"]
    assert card["state"] == "open" and card["sensitive"] is True


async def test_frequent_excluded_from_curated(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    favorites_store.record_usage(db, "mdopp", "play_radio", {"station": "NDR"})
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    assert j["personal"] == [] and j["household"] == []
    assert j["frequent"] and j["frequent"][0]["kind"] == "action"


async def test_addable_groups_by_room_and_marks_state(
    aiohttp_client, tmp_path, monkeypatch
):
    """The picker aggregator (#669/#702) groups controllable actuators by room,
    marks the already-pinned one, and flags a garage cover as sensitive. The
    garage stays ADDABLE (sensitive flag, not excluded) so a guarded card can be
    pinned; scenes/scripts/automations come back as an Automationen group."""
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "Bürolicht", {"entity_id": "light.buero"}
    )

    async def _fake_addable(url, token, entity_area):
        return [
            ha.card_spec("light.buero", "on", {"friendly_name": "Bürolicht"})
            | {"room": "Büro"},
            ha.card_spec("light.kueche", "off", {"friendly_name": "Küchenlicht"})
            | {"room": "Küche"},
            ha.card_spec(
                "cover.garage",
                "closed",
                {"friendly_name": "Garagentor", "device_class": "garage"},
            )
            | {"room": "Garage"},
            ha.card_spec("lock.zuhause", "locked", {"friendly_name": "Haustür"})
            | {"room": "Flur"},
        ]

    async def _fake_runnables(url, token):
        return [
            {"entity_id": "scene.abend", "name": "Abendszene", "domain": "scene"},
            {
                "entity_id": "automation.gute_nacht",
                "name": "Gute Nacht",
                "domain": "automation",
            },
        ]

    async def _fake_snapshot(self):
        from solaris_chat.engine.areas import AreaSnapshot

        return AreaSnapshot(rooms=[], entity_area={})

    monkeypatch.setattr("solaris_chat.server.fetch_addable_cards", _fake_addable)
    monkeypatch.setattr("solaris_chat.server.fetch_addable_runnables", _fake_runnables)
    monkeypatch.setattr(
        "solaris_chat.engine.areas.AreaRegistry.snapshot", _fake_snapshot
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start/addable", headers={"Remote-User": "mdopp"})
    ).json()
    rooms = {g["room"]: g["cards"] for g in j["rooms"]}
    assert rooms["Büro"][0]["pinned"] is True
    assert rooms["Küche"][0]["pinned"] is False
    # The garage is still OFFERED (present in the payload) but flagged sensitive.
    assert rooms["Garage"][0]["sensitive"] is True
    assert rooms["Garage"][0]["pinned"] is False
    assert rooms["Küche"][0]["sensitive"] is False
    # #1212: a lock is offered too, flagged sensitive (the whole domain is), and
    # keeps the bolt state — never an open/closed door claim.
    assert rooms["Flur"][0]["entity_id"] == "lock.zuhause"
    assert rooms["Flur"][0]["sensitive"] is True
    assert rooms["Flur"][0]["state"] == "locked"
    # Automationen group: scenes/scripts/automations as pinnable action cards.
    autos = {a["entity_id"]: a for a in j["automations"]}
    assert autos["scene.abend"]["kind"] == "action"
    assert autos["scene.abend"]["tool"] == "ha_run_scene_script"
    assert autos["scene.abend"]["args"] == {"entity": "scene.abend"}
    assert autos["scene.abend"]["sensitive"] is False
    assert autos["automation.gute_nacht"]["pinned"] is False


async def test_addable_marks_pinned_automation(aiohttp_client, tmp_path, monkeypatch):
    """A scene/script already pinned as an ha_run_scene_script action is marked
    `pinned` in the Automationen group so it isn't offered twice (#702)."""
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Abendszene",
        {"tool": "ha_run_scene_script", "args": {"entity": "scene.abend"}},
    )

    async def _fake_addable(url, token, entity_area):
        return []

    async def _fake_runnables(url, token):
        return [{"entity_id": "scene.abend", "name": "Abendszene", "domain": "scene"}]

    async def _fake_snapshot(self):
        from solaris_chat.engine.areas import AreaSnapshot

        return AreaSnapshot(rooms=[], entity_area={})

    monkeypatch.setattr("solaris_chat.server.fetch_addable_cards", _fake_addable)
    monkeypatch.setattr("solaris_chat.server.fetch_addable_runnables", _fake_runnables)
    monkeypatch.setattr(
        "solaris_chat.engine.areas.AreaRegistry.snapshot", _fake_snapshot
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start/addable", headers={"Remote-User": "mdopp"})
    ).json()
    assert j["automations"][0]["pinned"] is True


async def test_run_dispatches_scene_script_action(aiohttp_client, tmp_path):
    """A pinned Automationen card (ha_run_scene_script) dispatches on tap like
    any non-sensitive action favorite (#702)."""
    db = _db(tmp_path)
    engine = _FakeEngine()
    fav_id = favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Abendszene",
        {"tool": "ha_run_scene_script", "args": {"entity": "scene.abend"}},
    )
    app = build_app(
        engine=engine,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        f"/api/favorites/{fav_id}/run", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    assert engine.calls == [("ha_run_scene_script", {"entity": "scene.abend"})]


async def test_addable_503_without_ha(aiohttp_client, tmp_path):
    app = _app(tmp_path)  # no hass_url/hass_token
    client = await aiohttp_client(app)
    r = await client.get("/api/portal/start/addable", headers={"Remote-User": "mdopp"})
    assert r.status == 503


async def test_create_delete_reorder(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/favorites",
        json={
            "kind": "entity",
            "label": "Sofalicht",
            "payload": {"entity_id": "light.sofa"},
        },
        headers={"Remote-User": "mdopp"},
    )
    fav_id = (await r.json())["id"]
    # Reorder.
    r = await client.put(
        f"/api/favorites/{fav_id}",
        json={"position": 4},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200
    assert favorites_store.list_favorites(db, "mdopp")[0]["position"] == 4
    # Delete.
    r = await client.delete(
        f"/api/favorites/{fav_id}", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    assert favorites_store.list_favorites(db, "mdopp") == []


async def test_start_dedups_both_scoped_entity_into_personal(aiohttp_client, tmp_path):
    """A device pinned in BOTH scopes renders once, in the PERSONAL list (#745)."""
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "Büro", {"entity_id": "light.buro"}
    )
    favorites_store.add_favorite(
        db, "household", "entity", "Büro", {"entity_id": "light.buro"}
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    personal_ids = [f["payload"].get("entity_id") for f in j["personal"]]
    household_ids = [f["payload"].get("entity_id") for f in j["household"]]
    assert personal_ids == ["light.buro"]  # kept in personal (preferred)
    assert household_ids == []  # household duplicate dropped from display
    assert j["pinned_entities"] == ["light.buro"]


async def test_start_single_scope_when_uid_is_household(aiohttp_client, tmp_path):
    """uid==household collapses the two buckets → single_scope true (#745)."""
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "household", "entity", "Flur", {"entity_id": "light.flur"}
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "household"})
    ).json()
    assert j["single_scope"] is True
    # A distinct resident is NOT collapsed.
    j2 = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    assert j2["single_scope"] is False


async def test_favorites_scope_move_reowns_both_directions(aiohttp_client, tmp_path):
    """POST /api/favorites/{id}/scope re-owns the row, owner-checked (#745)."""
    db = _db(tmp_path)
    fav_id = favorites_store.add_favorite(
        db, "mdopp", "entity", "Büro", {"entity_id": "light.buro"}
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        f"/api/favorites/{fav_id}/scope",
        json={"scope": "household"},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200
    assert favorites_store.list_favorites(db, "mdopp")[0]["owner_uid"] == "household"
    r = await client.post(
        f"/api/favorites/{fav_id}/scope",
        json={"scope": "personal"},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200
    assert favorites_store.list_favorites(db, "mdopp")[0]["owner_uid"] == "mdopp"
    # A resident who doesn't own it gets 404.
    r = await client.post(
        f"/api/favorites/{fav_id}/scope",
        json={"scope": "household"},
        headers={"Remote-User": "lena"},
    )
    assert r.status == 404


async def test_create_action_rejects_unlisted_tool(aiohttp_client, tmp_path):
    app = _app(tmp_path)
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/favorites",
        json={
            "kind": "action",
            "label": "x",
            "payload": {"tool": "ha_list_entities", "args": {}},
        },
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 403


async def test_create_action_accepts_scene_script(aiohttp_client, tmp_path):
    """An Automationen pick pins as an ha_run_scene_script action — a pinnable,
    non-sensitive tool, so the create is accepted (#702)."""
    db = _db(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/favorites",
        json={
            "kind": "action",
            "label": "Abendszene",
            "payload": {
                "tool": "ha_run_scene_script",
                "args": {"entity": "scene.abend"},
            },
        },
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200


async def test_run_dispatches_routine_action(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    engine = _FakeEngine()
    fav_id = favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Radio",
        {"tool": "play_radio", "args": {"station": "NDR"}},
    )
    app = build_app(
        engine=engine,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        f"/api/favorites/{fav_id}/run", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    assert engine.calls == [("play_radio", {"station": "NDR"})]
    # Usage counter bumped for the frequent list.
    assert favorites_store.top_usage(db, "mdopp")[0]["payload"]["tool"] == "play_radio"


async def test_run_403_on_sensitive_action(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    engine = _FakeEngine()
    # A lock unlock is sensitive → must not dispatch from a one-tap start page.
    fav_id = favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Tür auf",
        {
            "tool": "ha_call_service",
            "args": {"domain": "lock", "service": "unlock", "entity_id": "lock.front"},
        },
    )
    app = build_app(
        engine=engine,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        f"/api/favorites/{fav_id}/run", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 403
    assert engine.calls == []


async def test_run_403_on_unlisted_tool(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    engine = _FakeEngine()
    fav_id = favorites_store.add_favorite(
        db, "mdopp", "action", "x", {"tool": "ha_list_entities", "args": {}}
    )
    app = build_app(
        engine=engine,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        f"/api/favorites/{fav_id}/run", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 403
    assert engine.calls == []


# --- Frontend-contract checks for the #702 picker (real check = box-verify) ---

from solaris_chat.server import STATIC_DIR  # noqa: E402

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_picker_renders_cards_inert():
    # The picker tiles render in inert mode so a tile click can never fire a
    # device action — selection only.
    assert "renderHaCard(c, false, { picker: true })" in _HTML
    assert "var inert = !!(opts && opts.picker);" in _HTML
    assert ".hc-inert, .hc-inert * { pointer-events: none; }" in _HTML


def test_picker_offers_sensitive_and_automations():
    # Sensitive tiles stay selectable (only pinned ones are disabled); the
    # Automationen group is rendered from j.automations.
    assert 'if (c.pinned) tile.classList.add("taken");' in _HTML
    assert 'ah.textContent = "Automationen";' in _HTML
    assert "j.automations" in _HTML


def test_sensitive_card_tap_confirms_and_sends_confirmed():
    # A sensitive cover asks an explicit confirm and sends confirmed=true; the
    # server re-checks the gate, so haCall must forward the flag.
    assert 'var sensitive = c.sensitive || c.device_class === "garage";' in _HTML
    assert 'haCall(card, c, "cover." + b[1], {}, sensitive);' in _HTML
    assert "confirmed: confirmed === true," in _HTML


# --- #729: HA-unreachable notice + unavailable cards (real check = box-verify) ---


def test_start_page_renders_ha_notice_and_unavailable_card():
    # The start page renders the connection banner from data.ha and a greyed
    # "nicht verfügbar" tile for a card_unavailable entity favorite.
    assert "function renderHaBanner(ha)" in _HTML
    assert "function renderUnavailableCard(f)" in _HTML
    assert "nicht erreichbar" in _HTML  # unreachable copy
    assert "nicht eingerichtet" in _HTML  # unconfigured copy
    assert '"nicht verfügbar"' in _HTML  # the disabled-card label
    assert "f.card_unavailable" in _HTML
    assert "updateHaBanner(page, j.ha" in _HTML  # banner lifts on the live poll


def test_play_favorite_shows_device_and_volume():
    # #972: a play_radio/play_music action favorite renders subtitle lines with
    # device + volume (+ station/query only when it isn't already the label),
    # built from the stored payload.args — not just the bare button label.
    assert "function playFavoriteSubtitle(f)" in _HTML
    assert 'p.tool !== "play_radio" && p.tool !== "play_music"' in _HTML
    # station/query is only re-shown when it differs from the label:
    assert 'if (what && String(what) !== String(f.label || "")) parts.push' in _HTML
    # device falls back across the keys that may exist:
    assert "a.target || a.area || a.media_player || a.entity_id" in _HTML
    # volume_level renders as a percentage:
    assert 'Math.round(Number(a.volume_level) * 100) + "%"' in _HTML
    # the action branch renders the subtitle via the helper:
    assert "var sub = playFavoriteSubtitle(f);" in _HTML


def test_generic_card_renders_unavailable_entity_as_inactive():
    # #732: an offline entity (unavailable/unknown/empty state) must render as an
    # explicit inactive .hc-unavailable tile with the offline label — NOT a normal
    # .off switch — and must not wire a toggle (the branch adds no handler; the
    # .hc-unavailable CSS also kills pointer-events).
    fn = re.search(
        r"function renderHaCard\(c, row, opts\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert fn, "renderHaCard not found"
    body = fn.group(1)
    assert 'st === "unavailable" || st === "unknown" || st === ""' in body
    assert 'card.classList.add("hc-unavailable")' in body
    assert 'ulbl.className = "hc-unavail-label"' in body
    # the unavailable branch precedes the generic on/off branch, so an offline
    # entity never reaches the haToggle wiring:
    unavail_at = body.index('st === "unavailable"')
    toggle_at = body.index("haToggle(card, badge, c)")
    assert unavail_at < toggle_at


# --- #736: colour-picker overlay survives live card re-render (box-verify visual) ---


def test_colour_picker_suspends_live_rerender():
    # The colour <input type=color> sets hcColorPicking on focus and clears it on
    # blur/change, and the busy guard honours the flag — so an open native overlay
    # isn't destroyed by an SSE/poll in-place card re-render. #980: the guard moved
    # into liveHostBusy (shared across every live host, not just the start page).
    assert "var hcColorPicking = false;" in _HTML
    focus = re.search(
        r'picker\.addEventListener\("focus", function \(\) \{(.*?)\}\);', _HTML, re.S
    )
    assert focus and "hcColorPicking = true;" in focus.group(1)
    blur = re.search(
        r'picker\.addEventListener\("blur", function \(\) \{(.*?)\n          \}\);',
        _HTML,
        re.S,
    )
    assert blur and "hcColorPicking = false;" in blur.group(1)
    fn = re.search(r"function liveHostBusy\(h\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "liveHostBusy not found"
    assert "hcColorPicking ||" in fn.group(1)
    # change must also clear the flag (belt-and-braces if blur didn't fire first)
    change = re.search(
        r'picker\.addEventListener\("change", function \(\) \{(.*?)\n          \}\);',
        _HTML,
        re.S,
    )
    assert change and "hcColorPicking = false;" in change.group(1)


# --- #738: colour picker previews live, reverts on cancel, keeps on confirm ---
# (live preview / cancel-revert behaviour is device-verified — source contract here)


def test_colour_picker_wires_live_preview_and_cancel_revert():
    # <input type=color> has no native cancel event: focus records the original
    # rgb + a committed flag, input previews live (debounced), change commits,
    # and blur without a commit reverts to the recorded original.
    for evt in ("focus", "input", "change", "blur"):
        assert 'picker.addEventListener("%s"' % evt in _HTML
    # focus captures the original colour + clears the committed flag
    focus = re.search(
        r'picker\.addEventListener\("focus", function \(\) \{(.*?)\}\);', _HTML, re.S
    )
    assert focus
    assert "pickOriginal = rgbToHex(c.rgb_color);" in focus.group(1)
    assert "pickCommitted = false;" in focus.group(1)
    # input previews live via the debounce helper
    inp = re.search(
        r'picker\.addEventListener\("input", function \(\) \{(.*?)\}\);', _HTML, re.S
    )
    assert inp and "previewColour(picker.value);" in inp.group(1)
    # change commits — sets the flag and keeps the picked value
    change = re.search(
        r'picker\.addEventListener\("change", function \(\) \{(.*?)\n          \}\);',
        _HTML,
        re.S,
    )
    assert change and "pickCommitted = true;" in change.group(1)
    # blur without a commit reverts to the recorded original
    blur = re.search(
        r'picker\.addEventListener\("blur", function \(\) \{(.*?)\n          \}\);',
        _HTML,
        re.S,
    )
    assert blur
    assert "if (!pickCommitted && pickOriginal != null) sendColour(pickOriginal);" in (
        blur.group(1)
    )
    # the debounce coalesces a rapid drag to the latest value
    assert "function previewColour(hex) {" in _HTML
    assert "clearTimeout(pickTimer)" in _HTML
    assert "setTimeout(function () {" in _HTML


def test_ha_watch_status_getter():
    from solaris_chat.engine.ha_watch import HaStateWatcher
    from solaris_chat.engine.notify import EventBus

    disabled = HaStateWatcher("", "", EventBus(), ":memory:")
    assert disabled.status == "disabled"
    configured = HaStateWatcher("http://ha", "t", EventBus(), ":memory:")
    assert configured.status == "disconnected"
    configured._connected = True
    assert configured.status == "connected"


def test_frequent_section_removed():
    # #973: the auto "Häufig genutzt" section is gone — the Geräte page is one
    # grid of the resident's pinned favourites, no frequent auto-list.
    assert "Häufig genutzt" not in _HTML
    assert "renderFrequent" not in _HTML
    assert "data.frequent" not in _HTML


def test_device_pin_defaults_to_household_scope():
    # #745 source contract: the ☆ posts scope:"household" for a device pin — the
    # default lives in one flippable constant.
    assert 'var DEVICE_PIN_SCOPE = "household";' in _HTML
    assert "scope: DEVICE_PIN_SCOPE," in _HTML


def test_single_grid_merges_favourites_and_hides_move_on_single_scope():
    # #973 source contract: the Geräte page is ONE grid — personal + household
    # favourites merge into a single .start-favs list (no household sub-section).
    # #745: the scope-move affordance is still hidden when single_scope.
    assert "card.singleScope = !!data.single_scope;" in _HTML
    assert "var favs = (data.personal || []).concat(data.household || []);" in _HTML
    assert 'wrap.className = "ha-cards start-favs";' in _HTML
    assert "if (!page.singleScope) {" in _HTML


# --- #980: generalized live-update engine (registry + SSE-gated poll) ---


def test_live_host_registry_functions_exist():
    # The single-startPageCard assumption is replaced by a registry of live hosts;
    # register/unregister keep it, and apply/unavailable iterate ALL hosts.
    assert "var liveHosts = [];" in _HTML
    assert "function registerLiveHost(hostEl, entries, apply, unavail)" in _HTML
    assert "function unregisterLiveHost(hostEl, keep)" in _HTML
    assert "function applyCardState(entityId, card)" in _HTML
    assert "function applyCardUnavailable(entityId)" in _HTML
    # applyCardState loops every registered host and applies via the host's own
    # render closure — no start-page-only branch.
    body = re.search(
        r"function applyCardState\(entityId, card\) \{(.*?)\n      \}", _HTML, re.S
    ).group(1)
    assert "liveHosts.forEach" in body
    assert "h.apply(rec, card)" in body
    # The stale-echo guard is kept for every host.
    assert "if (hcPendingContradicts(entityId, card)) return;" in body


def test_start_page_registers_itself_as_a_live_host():
    # The start page registers with its startFavs as the entries, re-rendering a
    # favorite in place via renderFavorite — it IS a live host now.
    assert re.search(r"registerLiveHost\(\s*card,\s*card\.startFavs,", _HTML)
    # Leaving the portal unregisters it (which tears down SSE when last).
    assert "if (startPageCard) unregisterLiveHost(startPageCard);" in _HTML


def test_periodic_poll_is_sse_down_fallback_only():
    # The 12s poll is no longer armed unconditionally in loadStartPage; SSE
    # open/error gate it. On open → mark connected + stop the poll; on error →
    # mark disconnected + (re)start the poll.
    opened = re.search(
        r'es\.addEventListener\("open", function \(\) \{(.*?)\}\);', _HTML, re.S
    ).group(1)
    assert "sseConnected = true;" in opened
    assert "stopPoll();" in opened
    err = re.search(
        r'es\.addEventListener\("error", function \(\) \{(.*?)\}\);', _HTML, re.S
    ).group(1)
    assert "sseConnected = false;" in err
    assert "startPoll();" in err
    # startPoll refuses to arm while SSE is healthy.
    poll = re.search(r"function startPoll\(\) \{(.*?)\n      \}", _HTML, re.S).group(1)
    assert "if (sseConnected || startTimer || !startRefreshActive()) return;" in poll
    assert "startTimer = setInterval(refreshStartPage, START_REFRESH_MS);" in poll
    # loadStartPage no longer arms the timer directly.
    load = re.search(
        r"function loadStartPage\(card\) \{(.*?)\n      \}", _HTML, re.S
    ).group(1)
    assert "setInterval(refreshStartPage" not in load


def test_sse_reconnect_and_visibility_reconcile():
    # #714 SSE reliability: the connection can silently die (nginx 60s idle
    # timeout); EventSource reopens it but nothing caught up the gap. Every
    # (re)connect and every return-to-visible must reconcile the live hosts —
    # else a switch flipped while backgrounded leaves the card stale.
    opened = re.search(
        r'es\.addEventListener\("open", function \(\) \{(.*?)\}\);', _HTML, re.S
    ).group(1)
    assert "reconcileLiveHosts();" in opened  # reconcile on EVERY (re)connect
    # reconcileLiveHosts catches up the start-page host via refreshStartPage and
    # each .home host's entries via the per-entity state endpoint; it is
    # debounced and skips while hidden / with no hosts.
    rec = re.search(
        r"function reconcileLiveHosts\(\) \{(.*?)\n      \}\n", _HTML, re.S
    ).group(1)
    assert "if (reconcileTimer) return;" in rec  # debounce
    assert "if (document.hidden) return;" in rec
    assert "if (h.el === startPageCard) { refreshStartPage(); return; }" in rec
    assert '"/api/portal/state?entity_id=" + encodeURIComponent(rec.entityId)' in rec
    # The visibilitychange handler resubscribes a dropped SSE and reconciles.
    vis = re.search(
        r'addEventListener\("visibilitychange", function \(\) \{(.*?)\n      \}\);',
        _HTML,
        re.S,
    ).group(1)
    assert "if (!sseConnected && liveHosts.length) startEventsSubscribe();" in vis
    assert "reconcileLiveHosts();" in vis
