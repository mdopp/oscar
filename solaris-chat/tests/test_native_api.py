"""The `/napi/` native-API prefix — strict device-token auth, fail-closed (#757).

The Android widgets reach solaris-chat through an Authelia BYPASS on this prefix,
so `/napi/*` is device-token-ONLY: a request without a valid `sol_device_` bearer
is 401 and must NEVER inherit the household `default_uid` (an unauthenticated
internet caller could otherwise control the house) nor trust a `Remote-User`
header. This mirrors the `/api/` handlers but with the strict native gate; the
`/api/` routes keep their unchanged loopback/browser behaviour.

Reuses the #748 device-token store; replays the migration-0021 schema with raw
SQL (a chat test must NOT import alembic).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from solaris_chat import device_token_store
from solaris_chat.engine.notify import EventBus
from solaris_chat.server import build_app, native_uid

# The table migration 0021 creates, replayed locally (no alembic).
_SCHEMA = """
CREATE TABLE device_tokens (
  id         TEXT PRIMARY KEY,
  owner_uid  TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  label      TEXT,
  created    TEXT NOT NULL DEFAULT (datetime('now')),
  last_used  TEXT,
  revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX device_tokens_hash_idx ON device_tokens (token_hash);
CREATE INDEX device_tokens_owner_idx ON device_tokens (owner_uid);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


class _FakeRequest:
    def __init__(self, headers, path):
        self.headers = headers
        self.path = path


def _app(tmp_path, db):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )


def _ha_app(tmp_path, db):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )


# ---- native_uid helper (fail-closed, device-token-only) -------------------


def test_native_uid_none_without_bearer(tmp_path):
    db = _db(tmp_path)
    # A Remote-User header must NOT authenticate a `/napi/` request.
    req = _FakeRequest({"Remote-User": "mdopp"}, "/napi/whoami")
    assert native_uid(req, db) is None


def test_native_uid_none_for_service_key(tmp_path):
    db = _db(tmp_path)
    req = _FakeRequest({"Authorization": "Bearer SOLARIS_API_KEY"}, "/napi/whoami")
    assert native_uid(req, db) is None


def test_native_uid_none_for_revoked_token(tmp_path):
    db = _db(tmp_path)
    tid, token = device_token_store.create(db, "lena")
    device_token_store.revoke(db, "lena", tid)
    req = _FakeRequest({"Authorization": f"Bearer {token}"}, "/napi/whoami")
    assert native_uid(req, db) is None


def test_native_uid_resolves_owner_for_valid_token(tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    req = _FakeRequest({"Authorization": f"Bearer {token}"}, "/napi/whoami")
    assert native_uid(req, db) == "lena"


# ---- /napi/whoami ----------------------------------------------------------


async def test_napi_whoami_without_bearer_is_401_not_household(
    aiohttp_client, tmp_path
):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get("/napi/whoami")
    assert r.status == 401
    j = await r.json()
    # Fail-closed: NOT the household default_uid.
    assert j == {"ok": False, "error": "unauthorized"}


async def test_napi_whoami_remote_user_header_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    # Authelia bypasses this prefix, so a spoofable Remote-User must NOT count.
    r = await client.get("/napi/whoami", headers={"Remote-User": "mdopp"})
    assert r.status == 401


async def test_napi_whoami_service_key_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get(
        "/napi/whoami", headers={"Authorization": "Bearer SOLARIS_API_KEY"}
    )
    assert r.status == 401


async def test_napi_whoami_revoked_token_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    tid, token = device_token_store.create(db, "lena")
    device_token_store.revoke(db, "lena", tid)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get("/napi/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 401


async def test_napi_whoami_valid_token_resolves_owner(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get("/napi/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 200
    j = await r.json()
    assert j["ok"] is True and j["uid"] == "lena"
    # whoami still carries the VAPID key the widget subscribes with.
    assert "vapid_public_key" in j


# ---- a data endpoint: /napi/portal/entity-history --------------------------


async def test_napi_entity_history_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get("/napi/portal/entity-history?entity_id=sensor.temp&range=24h")
    assert r.status == 401


async def test_napi_entity_history_valid_token_is_200(
    aiohttp_client, tmp_path, monkeypatch
):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")

    async def _fake(url, tok, entity_id, rng):
        return [{"t": "t0", "state": "21.2"}]

    monkeypatch.setattr("solaris_chat.server.fetch_entity_history", _fake)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/entity-history?entity_id=sensor.temp&range=24h",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 200
    assert (await r.json())["history"] == [{"t": "t0", "state": "21.2"}]


# ---- /napi/portal/camera/{entity_id}/snapshot (privacy-sensitive, #770) ----


async def test_napi_camera_snapshot_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    # Fail-closed: a live camera must NEVER be served to a token-less caller.
    r = await client.get("/napi/portal/camera/camera.front/snapshot")
    assert r.status == 401


async def test_napi_camera_snapshot_service_key_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/camera/camera.front/snapshot",
        headers={"Authorization": "Bearer SOLARIS_API_KEY"},
    )
    assert r.status == 401


async def test_napi_camera_snapshot_valid_token_returns_image(
    aiohttp_client, tmp_path, monkeypatch
):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")

    async def _fake(url, tok, entity_id):
        return b"\xff\xd8\xff-jpeg-bytes", "image/jpeg"

    monkeypatch.setattr("solaris_chat.server.fetch_camera_snapshot", _fake)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/camera/camera.front/snapshot",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 200
    assert r.headers["Content-Type"] == "image/jpeg"
    assert await r.read() == b"\xff\xd8\xff-jpeg-bytes"


async def test_napi_camera_snapshot_non_camera_entity_is_400(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/camera/light.x/snapshot",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 400


async def test_napi_camera_snapshot_ha_unconfigured_is_503(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/camera/camera.front/snapshot",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 503


# ---- /napi/device-tokens: scoped by the device-token owner, not Remote-User


async def test_napi_device_tokens_list_scoped_to_token_owner(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    device_token_store.create(db, "lena", "a")
    device_token_store.create(db, "mdopp", "b")
    _, token = device_token_store.create(db, "lena", "widget")
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get(
        "/napi/device-tokens", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 200
    labels = {t["label"] for t in (await r.json())["tokens"]}
    # Only lena's tokens, never mdopp's.
    assert labels == {"a", "widget"}


async def test_napi_device_tokens_list_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get("/napi/device-tokens", headers={"Remote-User": "mdopp"})
    assert r.status == 401


async def test_napi_device_tokens_revoke_owner_checked(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    other_id, other = device_token_store.create(db, "mdopp")
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    # lena's device token cannot revoke mdopp's.
    r = await client.delete(
        f"/napi/device-tokens/{other_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 404
    assert device_token_store.resolve(db, other) == "mdopp"


# ---- minting is NOT reachable under /napi/ ---------------------------------


async def test_napi_does_not_expose_token_minting(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    # POST /napi/device-tokens is not routed at all — minting stays interactive-
    # Authelia-only (#748/#751), so a device token can't mint another.
    r = await client.post(
        "/napi/device-tokens",
        json={"label": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status in (404, 405)
    # /pair-device likewise is not on the native prefix.
    r2 = await client.post(
        "/napi/pair-device",
        data={"label": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status in (404, 405)
    assert len(device_token_store.list_for_uid(db, "lena")) == 1


# ---- /napi/portal/start/addable: the device picker (#762) ------------------


def _patch_addable(monkeypatch):
    """Stub the HA reads `portal_start_addable` fans out to (device picker)."""

    async def _cards(url, tok, entity_area):
        return [
            {"entity_id": "light.k", "name": "Küche", "room": "Küche", "state": "on"}
        ]

    async def _runnables(url, tok):
        return []

    class _Snap:
        entity_area = {"light.k": "Küche"}

    async def _snapshot(self):
        return _Snap()

    monkeypatch.setattr("solaris_chat.server.fetch_addable_cards", _cards)
    monkeypatch.setattr("solaris_chat.server.fetch_addable_runnables", _runnables)
    monkeypatch.setattr("solaris_chat.server.AreaRegistry.snapshot", _snapshot)


async def test_napi_addable_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get("/napi/portal/start/addable")
    assert r.status == 401
    # Fail-closed: NOT the household default_uid.
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}


async def test_napi_addable_service_key_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/start/addable",
        headers={"Authorization": "Bearer SOLARIS_API_KEY"},
    )
    assert r.status == 401


async def test_napi_addable_valid_token_is_200(aiohttp_client, tmp_path, monkeypatch):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    _patch_addable(monkeypatch)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/start/addable",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 200
    j = await r.json()
    assert j["ok"] is True
    assert j["rooms"][0]["room"] == "Küche"


# ---- /napi/portal/active: active-devices collection, no N+1 (#773) ---------


def _patch_active(monkeypatch):
    """Stub the single bulk actuator+state fetch `portal_active` filters over.

    An on light and an open cover are active; an off switch and an unavailable
    device must be excluded."""

    async def _cards(url, tok, entity_area):
        return [
            {
                "entity_id": "light.k",
                "name": "Küche",
                "room": "Küche",
                "domain": "light",
                "state": "on",
            },
            {
                "entity_id": "cover.g",
                "name": "Garage",
                "room": "Garage",
                "domain": "cover",
                "state": "open",
            },
            {
                "entity_id": "switch.b",
                "name": "Boiler",
                "room": "Bad",
                "domain": "switch",
                "state": "off",
            },
            {
                "entity_id": "light.f",
                "name": "Flur",
                "room": "Flur",
                "domain": "light",
                "state": "unavailable",
            },
        ]

    class _Snap:
        entity_area = {}

    async def _snapshot(self):
        return _Snap()

    monkeypatch.setattr("solaris_chat.server.fetch_addable_cards", _cards)
    monkeypatch.setattr("solaris_chat.server.AreaRegistry.snapshot", _snapshot)


async def test_napi_active_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get("/napi/portal/active")
    assert r.status == 401
    # Fail-closed: NOT the household default_uid.
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}


async def test_napi_active_service_key_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/active",
        headers={"Authorization": "Bearer SOLARIS_API_KEY"},
    )
    assert r.status == 401


async def test_napi_active_valid_token_returns_only_active(
    aiohttp_client, tmp_path, monkeypatch
):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    _patch_active(monkeypatch)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/active",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 200
    j = await r.json()
    assert j["ok"] is True
    ids = {e["entity_id"] for e in j["active"]}
    # on light + open cover in; off switch + unavailable device out.
    assert ids == {"light.k", "cover.g"}
    item = next(e for e in j["active"] if e["entity_id"] == "light.k")
    assert set(item) == {"entity_id", "name", "room", "domain", "state"}
    assert item["name"] == "Küche" and item["room"] == "Küche"
    assert item["domain"] == "light" and item["state"] == "on"


async def test_napi_active_ha_unconfigured_is_503(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    # _app has no hass_url/hass_token → HA unconfigured.
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/active",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 503
    assert (await r.json()) == {"ok": False, "error": "ha_unconfigured"}


# ---- /napi/portal/cameras: camera list for the Android widget picker (#779) -


class _FakeStatesResponse:
    """Minimal async-context-manager stand-in for an aiohttp `/api/states` GET,
    so `fetch_cameras`' real camera-domain filter runs over faked states."""

    status = 200

    def __init__(self, states):
        self._states = states

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._states


class _FakeClientSession:
    def __init__(self, states):
        self._states = states

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None):
        return _FakeStatesResponse(self._states)


def _patch_camera_states(monkeypatch):
    """Fake the single bulk `/api/states` read: 2 cameras + a light, so the
    real `camera.*` filter must yield exactly the two cameras."""
    states = [
        {
            "entity_id": "camera.front",
            "state": "idle",
            "attributes": {"friendly_name": "Haustür"},
        },
        {
            "entity_id": "camera.garden",
            "state": "idle",
            "attributes": {"friendly_name": "Garten"},
        },
        {
            "entity_id": "light.k",
            "state": "on",
            "attributes": {"friendly_name": "Küche"},
        },
    ]

    def _session(*args, **kwargs):
        return _FakeClientSession(states)

    class _Snap:
        entity_area = {"camera.front": "Eingang"}

    async def _snapshot(self):
        return _Snap()

    monkeypatch.setattr("solaris_chat.engine.tools.ha.aiohttp.ClientSession", _session)
    monkeypatch.setattr("solaris_chat.server.AreaRegistry.snapshot", _snapshot)


async def test_napi_cameras_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get("/napi/portal/cameras")
    assert r.status == 401
    # Fail-closed: NOT the household default_uid.
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}


async def test_napi_cameras_service_key_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/cameras",
        headers={"Authorization": "Bearer SOLARIS_API_KEY"},
    )
    assert r.status == 401


async def test_napi_cameras_valid_token_returns_only_cameras(
    aiohttp_client, tmp_path, monkeypatch
):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    _patch_camera_states(monkeypatch)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/cameras",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 200
    j = await r.json()
    assert j["ok"] is True
    ids = {c["entity_id"] for c in j["cameras"]}
    # only the two camera.* entities; the light is excluded.
    assert ids == {"camera.front", "camera.garden"}
    front = next(c for c in j["cameras"] if c["entity_id"] == "camera.front")
    assert set(front) == {"entity_id", "name", "room"}
    assert front["name"] == "Haustür" and front["room"] == "Eingang"
    garden = next(c for c in j["cameras"] if c["entity_id"] == "camera.garden")
    # no area → room "".
    assert garden["name"] == "Garten" and garden["room"] == ""


async def test_napi_cameras_ha_unconfigured_is_503(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    # _app has no hass_url/hass_token → HA unconfigured.
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/cameras",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 503
    assert (await r.json()) == {"ok": False, "error": "ha_unconfigured"}


# ---- /napi/portal/state: lean per-entity card-spec (#762) ------------------


async def test_napi_state_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get("/napi/portal/state?entity_id=light.k")
    assert r.status == 401
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}


async def test_napi_state_service_key_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/state?entity_id=light.k",
        headers={"Authorization": "Bearer SOLARIS_API_KEY"},
    )
    assert r.status == 401


async def test_napi_state_valid_token_is_200(aiohttp_client, tmp_path, monkeypatch):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")

    async def _card(url, tok, entity_id):
        return {"entity_id": entity_id, "name": "Küche", "state": "on"}

    monkeypatch.setattr("solaris_chat.server.fetch_card", _card)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/state?entity_id=light.k",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 200
    j = await r.json()
    assert j["ok"] is True and j["card"]["entity_id"] == "light.k"


async def test_napi_state_bad_entity_id_is_400(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/state?entity_id=not-an-entity",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 400
    assert (await r.json())["error"] == "invalid entity_id"


async def test_napi_state_ha_unconfigured_is_503(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    # `_app` builds without HA url/token.
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/state?entity_id=light.k",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 503
    assert (await r.json())["error"] == "ha_unconfigured"


# ---- /napi/portal/energy: the energy widget (#762) -------------------------


async def test_napi_energy_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get("/napi/portal/energy")
    assert r.status == 401
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}


async def test_napi_energy_service_key_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/energy", headers={"Authorization": "Bearer SOLARIS_API_KEY"}
    )
    assert r.status == 401


async def test_napi_energy_valid_token_is_200(aiohttp_client, tmp_path, monkeypatch):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")

    async def _energy(url, tok):
        return {"grid": 42}

    monkeypatch.setattr("solaris_chat.server.fetch_energy", _energy)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/energy", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 200
    j = await r.json()
    assert j["ok"] is True and j["energy"] == {"grid": 42}


# ---- /api/* stays unchanged: falls back to default_uid ---------------------


async def test_api_whoami_still_falls_back_to_default_uid(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    # The browser/loopback path is untouched: no bearer ⇒ household default_uid.
    r = await client.get("/api/whoami")
    assert r.status == 200
    assert (await r.json())["uid"] == "household"


async def test_api_addable_still_falls_back_to_default_uid(
    aiohttp_client, tmp_path, monkeypatch
):
    db = _db(tmp_path)
    _patch_addable(monkeypatch)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    # No bearer ⇒ the `/api/` path resolves the household default_uid (200), unlike
    # the fail-closed `/napi/` mirror.
    r = await client.get("/api/portal/start/addable")
    assert r.status == 200
    assert (await r.json())["ok"] is True


async def test_api_energy_still_falls_back_to_default_uid(
    aiohttp_client, tmp_path, monkeypatch
):
    db = _db(tmp_path)

    async def _energy(url, tok):
        return {"grid": 42}

    monkeypatch.setattr("solaris_chat.server.fetch_energy", _energy)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get("/api/portal/energy")
    assert r.status == 200
    assert (await r.json())["ok"] is True


# ---- /napi/portal/events (SSE card_state stream, #806) ---------------------


def _bus_app(tmp_path, db, bus):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        event_bus=bus,
    )


async def _read_card_state(resp) -> dict:
    event = None
    while True:
        line = (await asyncio.wait_for(resp.content.readline(), 2)).decode().strip()
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event == "card_state":
            return json.loads(line.split(":", 1)[1].strip())


async def test_napi_portal_events_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_bus_app(tmp_path, db, EventBus()))
    r = await client.get("/napi/portal/events")
    assert r.status == 401
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}


async def test_napi_portal_events_remote_user_header_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_bus_app(tmp_path, db, EventBus()))
    r = await client.get("/napi/portal/events", headers={"Remote-User": "mdopp"})
    assert r.status == 401


async def test_napi_portal_events_valid_token_streams_owner_scoped(
    aiohttp_client, tmp_path
):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    bus = EventBus()
    client = await aiohttp_client(_bus_app(tmp_path, db, bus))
    resp = await client.get(
        "/napi/portal/events", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/event-stream"
    await asyncio.sleep(0.05)  # let the subscription register
    # Another resident's event must NOT reach lena's device-scoped stream.
    bus.publish("mdopp", "card_state", {"entity_id": "light.buero", "card": {}})
    bus.publish("lena", "card_state", {"entity_id": "cover.garage", "card": {"x": 1}})
    data = await _read_card_state(resp)
    assert data["entity_id"] == "cover.garage"
    resp.close()


# ---- /napi/portal/watch — per-device native watch-set (#810) ---------------


def _watch_app(tmp_path, db, store):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        native_watch=store,
    )


async def test_napi_watch_without_bearer_is_401(aiohttp_client, tmp_path):
    from solaris_chat.engine.native_watch import NativeWatchStore

    db = _db(tmp_path)
    store = NativeWatchStore()
    client = await aiohttp_client(_watch_app(tmp_path, db, store))
    r = await client.post("/napi/portal/watch", json={"entity_ids": ["light.buero"]})
    assert r.status == 401
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}
    # Fail-closed: nothing stored for an unauthenticated caller.
    assert store.native_watch_owners() == {}


async def test_napi_watch_service_key_bearer_is_401(aiohttp_client, tmp_path):
    from solaris_chat.engine.native_watch import NativeWatchStore

    db = _db(tmp_path)
    store = NativeWatchStore()
    client = await aiohttp_client(_watch_app(tmp_path, db, store))
    r = await client.post(
        "/napi/portal/watch",
        json={"entity_ids": ["light.buero"]},
        headers={"Authorization": "Bearer SOLARIS_API_KEY"},
    )
    assert r.status == 401
    assert store.native_watch_owners() == {}


async def test_napi_watch_valid_token_stores_and_returns_ok(aiohttp_client, tmp_path):
    from solaris_chat.engine.native_watch import NativeWatchStore

    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    store = NativeWatchStore()
    client = await aiohttp_client(_watch_app(tmp_path, db, store))
    r = await client.post(
        "/napi/portal/watch",
        json={"entity_ids": ["light.buero", "cover.garage"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 200
    assert (await r.json()) == {"ok": True}
    owners = store.native_watch_owners()
    assert owners == {"light.buero": {"lena"}, "cover.garage": {"lena"}}


async def test_napi_watch_replaces_the_devices_set(aiohttp_client, tmp_path):
    from solaris_chat.engine.native_watch import NativeWatchStore

    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    store = NativeWatchStore()
    client = await aiohttp_client(_watch_app(tmp_path, db, store))
    hdr = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/napi/portal/watch", json={"entity_ids": ["light.a"]}, headers=hdr
    )
    await client.post(
        "/napi/portal/watch", json={"entity_ids": ["cover.b"]}, headers=hdr
    )
    # The second POST REPLACES the set, not appends.
    assert store.native_watch_owners() == {"cover.b": {"lena"}}


def test_native_watch_store_ttl_expiry_drops_the_set():
    from solaris_chat.engine.native_watch import NativeWatchStore

    store = NativeWatchStore(ttl_s=0.0)
    store.set("dev-1", "lena", {"light.buero"})
    # A zero TTL means the set is already expired on the next read.
    assert store.native_watch_owners() == {}


def test_native_watch_store_per_device_union():
    from solaris_chat.engine.native_watch import NativeWatchStore

    store = NativeWatchStore()
    store.set("dev-1", "lena", {"light.buero"})
    store.set("dev-2", "mdopp", {"light.buero", "cover.garage"})
    owners = store.native_watch_owners()
    assert owners == {
        "light.buero": {"lena", "mdopp"},
        "cover.garage": {"mdopp"},
    }


# ---- /napi/upload — camera/PDF capture into the notes vault (#826) ---------


def _find_upload_file(root, uid, ext):
    """The single uploaded file of `ext` under the resident's uploads subtree."""
    base = root / "users" / uid / "uploads"
    return next(base.glob(f"*{ext}"), None)


async def _search_notes(root, uid, query):
    """Run the real notes_search tool for `uid` over the vault at `root`."""
    from solaris_chat.engine.tools.notes import build_notes_tools

    tools = build_notes_tools(str(root), lambda: uid)
    search = next(t for t in tools if t.name == "notes_search")
    return json.loads(await search.handler({"query": query}))


async def test_napi_upload_without_bearer_is_401(aiohttp_client, tmp_path):
    import aiohttp

    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    fd = aiohttp.FormData()
    fd.add_field(
        "file", b"\xff\xd8\xff-jpeg", filename="shot.jpg", content_type="image/jpeg"
    )
    r = await client.post("/napi/upload", data=fd)
    assert r.status == 401
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}
    # Fail-closed: nothing written for a token-less caller.
    assert not (tmp_path / "users").exists()


async def test_napi_upload_jpeg_lands_in_vault_as_extraction_scratch(
    aiohttp_client, tmp_path
):
    import aiohttp

    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    fd = aiohttp.FormData()
    fd.add_field("filename", "2026-07-14_0900_Rechnung")
    fd.add_field(
        "file",
        b"\xff\xd8\xff-jpeg-bytes",
        filename="cam.jpg",
        content_type="image/jpeg",
    )
    r = await client.post(
        "/napi/upload", data=fd, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 200
    j = await r.json()
    assert j["ok"] is True and j["id"].endswith(".jpg")
    # The raw file landed in lena's uploads subtree.
    stored = _find_upload_file(tmp_path, "lena", ".jpg")
    assert stored is not None and stored.read_bytes() == b"\xff\xd8\xff-jpeg-bytes"
    # A companion markdown embeds it and carries the chosen name.
    note = stored.with_suffix(".md")
    assert note.is_file()
    note_text = note.read_text()
    assert f"![[{stored.name}]]" in note_text
    assert "added_by: lena" in note_text
    # The companion is extraction scratch, not a note (#998): notes_search never
    # returns the uploads/ companion — only the derived OKF note is a note.
    hits = await _search_notes(tmp_path, "lena", "Rechnung")
    assert not any(h["path"] == str(note.relative_to(tmp_path)) for h in hits)


async def test_napi_upload_pdf_works(aiohttp_client, tmp_path):
    import aiohttp

    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    fd = aiohttp.FormData()
    fd.add_field("filename", "Vertrag")
    fd.add_field(
        "file",
        b"%PDF-1.4 bytes",
        filename="doc.pdf",
        content_type="application/pdf",
    )
    r = await client.post(
        "/napi/upload", data=fd, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 200
    assert (await r.json())["id"].endswith(".pdf")
    assert _find_upload_file(tmp_path, "lena", ".pdf") is not None


async def test_napi_upload_unsupported_mime_is_415(aiohttp_client, tmp_path):
    import aiohttp

    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    fd = aiohttp.FormData()
    fd.add_field(
        "file",
        b"MZ-exe",
        filename="x.exe",
        content_type="application/octet-stream",
    )
    r = await client.post(
        "/napi/upload", data=fd, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 415
    # Nothing was written to the vault.
    assert _find_upload_file(tmp_path, "lena", ".exe") is None


async def test_napi_upload_oversize_is_413(aiohttp_client, tmp_path):
    import aiohttp

    from solaris_chat.server import _UPLOAD_MAX_BYTES

    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    fd = aiohttp.FormData()
    fd.add_field(
        "file",
        b"\xff" * (_UPLOAD_MAX_BYTES + 1),
        filename="big.jpg",
        content_type="image/jpeg",
    )
    r = await client.post(
        "/napi/upload", data=fd, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 413


async def test_napi_upload_sanitizes_traversal_filename(aiohttp_client, tmp_path):
    import aiohttp

    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    fd = aiohttp.FormData()
    fd.add_field("filename", "../../etc/passwd")
    fd.add_field(
        "file",
        b"\xff\xd8\xff",
        filename="../../evil.jpg",
        content_type="image/jpeg",
    )
    r = await client.post(
        "/napi/upload", data=fd, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 200
    stored = _find_upload_file(tmp_path, "lena", ".jpg")
    # The `../` is neutralised: the file stays inside lena's uploads subtree.
    assert stored is not None
    uploads = (tmp_path / "users" / "lena" / "uploads").resolve()
    assert str(stored.resolve()).startswith(str(uploads) + "/")
    assert "/" not in stored.name and ".." not in stored.name


async def test_napi_upload_series_returns_a_list(aiohttp_client, tmp_path):
    import aiohttp

    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    fd = aiohttp.FormData()
    fd.add_field("filename", "Serie")
    fd.add_field("file", b"\xff\xd8\xff-a", filename="a.jpg", content_type="image/jpeg")
    fd.add_field("file", b"\xff\xd8\xff-b", filename="b.jpg", content_type="image/jpeg")
    r = await client.post(
        "/napi/upload", data=fd, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 200
    j = await r.json()
    assert j["ok"] is True and len(j["files"]) == 2
    # Two distinct files, collision-suffixed.
    names = {Path(f["id"]).name for f in j["files"]}
    assert len(names) == 2


def test_ha_watch_unions_native_watch_into_pinned_owners(tmp_path):
    from solaris_chat.engine.ha_watch import HaStateWatcher
    from solaris_chat.engine.native_watch import NativeWatchStore

    # No favorites DB → pinned_entity_owners is empty; the native set is the only
    # source of watched entities.
    store = NativeWatchStore()
    store.set("dev-1", "lena", {"light.buero"})
    watcher = HaStateWatcher(
        "http://ha",
        "t",
        EventBus(),
        str(tmp_path / "missing.db"),
        native_watch=store,
    )
    watcher._refresh_pins()
    assert watcher._owners == {"light.buero": {"lena"}}


# ---- /api/import/status — Notizen import section live status (#869 P4b) -----


class _StubJobs:
    """Minimal JobRunner stand-in — the status route only calls latest_for."""

    def __init__(self, by_owner):
        self._by_owner = by_owner

    def latest_for(self, owner):
        return self._by_owner.get(owner)


def _import_app(tmp_path, db, jobs):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        import_jobs=jobs,
    )


async def test_import_status_without_session_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_import_app(tmp_path, db, _StubJobs({})))
    r = await client.get("/api/import/status")
    assert r.status == 401
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}


async def test_import_status_no_job_returns_null(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_import_app(tmp_path, db, _StubJobs({})))
    r = await client.get("/api/import/status", headers={"Remote-User": "mdopp"})
    assert r.status == 200
    assert (await r.json()) == {"ok": True, "job": None}


async def test_import_status_reflects_latest_job_owner_scoped(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    snap = {
        "jobId": "abc123",
        "status": "running",
        "progress": {"stage": "keep", "pct": 40, "message": "Notizen 210/340…"},
        "result": None,
        "error": None,
    }
    jobs = _StubJobs({"mdopp": snap})
    client = await aiohttp_client(_import_app(tmp_path, db, jobs))
    # mdopp sees their running import.
    r = await client.get("/api/import/status", headers={"Remote-User": "mdopp"})
    assert (await r.json()) == {"ok": True, "job": snap}
    # lena has no job → null (owner-scoped via latest_for).
    r = await client.get("/api/import/status", headers={"Remote-User": "lena"})
    assert (await r.json()) == {"ok": True, "job": None}


async def test_import_status_reattaches_pending_plan(aiohttp_client, tmp_path):
    # An unconfirmed plan persisted beside the archive re-attaches on reload
    # (the actionable card is otherwise a transient live inject), owner-scoped,
    # and takes precedence over a null job.
    import json as _json

    db = _db(tmp_path)
    imp = tmp_path / "users" / "mdopp" / "imports"
    imp.mkdir(parents=True)
    plan = {
        "archive_id": "users/mdopp/imports/takeout-x.zip",
        "card": {"kind": "action", "title": "Import aus Google Takeout", "buttons": []},
    }
    (imp / "takeout-x.zip.plan.json").write_text(_json.dumps(plan), encoding="utf-8")
    client = await aiohttp_client(_import_app(tmp_path, db, _StubJobs({})))
    r = await client.get("/api/import/status", headers={"Remote-User": "mdopp"})
    assert (await r.json()) == {"ok": True, "plan": plan}
    # lena has no pending plan → falls through to the (null) job.
    r = await client.get("/api/import/status", headers={"Remote-User": "lena"})
    assert (await r.json()) == {"ok": True, "job": None}


async def test_import_status_running_job_beats_stale_plan(aiohttp_client, tmp_path):
    # A stale, never-confirmed plan from an earlier upload must NOT mask a running
    # import — otherwise the live progress never surfaces (Importieren looked dead).
    import json as _json

    db = _db(tmp_path)
    imp = tmp_path / "users" / "mdopp" / "imports"
    imp.mkdir(parents=True)
    (imp / "takeout-stale.zip.plan.json").write_text(
        _json.dumps(
            {"archive_id": "users/mdopp/imports/takeout-stale.zip", "card": {}}
        ),
        encoding="utf-8",
    )
    running = {
        "jobId": "j1",
        "status": "running",
        "progress": {"pct": 32},
        "result": None,
    }
    client = await aiohttp_client(
        _import_app(tmp_path, db, _StubJobs({"mdopp": running}))
    )
    r = await client.get("/api/import/status", headers={"Remote-User": "mdopp"})
    assert (await r.json()) == {"ok": True, "job": running}  # job wins, not the plan


# ---- person.update: the EDIT leg of the create·find·edit pattern (#967) -----

_PERSON_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (entity_id, alias));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
"""


def _person_db(tmp_path):
    """A DB with a `.contacts`-created person (name + email/phone under source
    `contact`), for exercising the person.update correction path."""
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    conn.executescript(_PERSON_SCHEMA)
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES ('p-mike', 'person', 'Mike', 'mdopp', 'contact', 'h')"
    )
    conn.execute(
        "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate, value,"
        " confidence, source) VALUES ('f1', 'p-mike', 'mdopp', 'email',"
        " 'old@x.de', 1.0, 'contact')"
    )
    conn.commit()
    conn.close()
    return path


async def test_person_update_correction_wins_and_persists(aiohttp_client, tmp_path):
    from solaris_chat import documents_portal_db

    db = _person_db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.post(
        "/api/action-callback",
        headers={"Remote-User": "mdopp"},
        json={
            "action_id": "person.update",
            "confirmed": True,
            "params": {
                "entity_id": "p-mike",
                "name": "Michael",
                "email": "michael@x.de",
                "phone": "0151 123",
            },
        },
    )
    assert r.status == 200 and (await r.json())["ok"] is True
    # The correction replaced the created `contact` facts and renamed the entity.
    rows = documents_portal_db.person_contacts(db, "mdopp")
    row = next(x for x in rows if x["id"] == "p-mike")
    assert row["name"] == "Michael"
    assert row["email"] == "michael@x.de" and row["phone"] == "0151 123"


async def test_person_update_owner_scoped(aiohttp_client, tmp_path):
    from solaris_chat import documents_portal_db

    db = _person_db(tmp_path)  # person owned by mdopp
    client = await aiohttp_client(_app(tmp_path, db))
    # lena neither owns nor shares the person → no-op False, facts untouched.
    r = await client.post(
        "/api/action-callback",
        headers={"Remote-User": "lena"},
        json={
            "action_id": "person.update",
            "confirmed": True,
            "params": {"entity_id": "p-mike", "name": "Hijack", "email": "e@x.de"},
        },
    )
    assert r.status == 200 and (await r.json())["ok"] is False
    row = next(
        x
        for x in documents_portal_db.person_contacts(db, "mdopp")
        if x["id"] == "p-mike"
    )
    assert row["name"] == "Mike" and row["email"] == "old@x.de"


# ---- /api/photo: .photo dot-command upload + search (#961) -----------------
# Interactive (Remote-User) surface — no device token; a request that proves no
# resident is 401. Immich is mocked (no live instance).


def _photo_app(tmp_path, db, *, configured=True):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        immich_base_url="http://immich" if configured else "",
        immich_api_key="k" if configured else "",
    )


async def test_photo_search_without_session_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_photo_app(tmp_path, db))
    r = await client.get("/api/photo?q=anna")
    assert r.status == 401
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}


async def test_photo_upload_without_session_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_photo_app(tmp_path, db))
    r = await client.post("/api/photo", data={"file": "x"})
    assert r.status == 401


async def test_photo_search_unconfigured_immich_is_clear_message(
    aiohttp_client, tmp_path
):
    db = _db(tmp_path)
    client = await aiohttp_client(_photo_app(tmp_path, db, configured=False))
    r = await client.get("/api/photo?q=anna", headers={"Remote-User": "mdopp"})
    assert r.status == 503
    assert "nicht konfiguriert" in (await r.json())["error"]


async def test_photo_search_filters_by_person_and_returns_rows(
    aiohttp_client, tmp_path, monkeypatch
):
    from solaris_chat import server as srv
    from solaris_chat.engine.ingest.immich_client import ImmichAsset, ImmichPerson

    assets = [
        ImmichAsset(
            id="a1",
            file_name="strand.jpg",
            when="2026-06-01T00:00:00",
            checksum="c1",
            people=[ImmichPerson(id="p1", name="Anna")],
        ),
        ImmichAsset(
            id="a2",
            file_name="auto.jpg",
            when="2026-06-02T00:00:00",
            checksum="c2",
        ),
    ]

    class _FakeImmich:
        def __init__(self, *a, **k):
            pass

        async def iter_assets(self, *, updated_after=""):
            for a in assets:
                yield a

        def asset_uri(self, asset_id):
            return f"http://immich/api/assets/{asset_id}"

    monkeypatch.setattr(srv, "RestImmichClient", _FakeImmich)
    db = _db(tmp_path)
    client = await aiohttp_client(_photo_app(tmp_path, db))
    r = await client.get("/api/photo?q=anna", headers={"Remote-User": "mdopp"})
    assert r.status == 200
    j = await r.json()
    assert j["ok"] is True
    ids = [p["id"] for p in j["photos"]]
    # Only the asset tagged with Anna matches the person filter.
    assert ids == ["a1"]
    assert j["photos"][0]["people"] == ["Anna"]


# ---- /napi/defs/tool — the .tool catalog on the device-token surface (#1021) --


def _write_tool_def(skills_dir: Path) -> None:
    d = skills_dir / "task-tool"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\n"
        "name: solaris-task-tool\n"
        "description: The .task tool.\n"
        "kind: tool\n"
        "tool-id: task\n"
        "tool-label: Aufgabe\n"
        "command: .task\n"
        "tool-api-path: /api/portal/tasks?done=1\n"
        "tool-actions: task.set_status, task.add\n"
        'tool-cell-schema: {"title": "title", "meta": ["due"]}\n'
        "---\n\n# Task\n",
        encoding="utf-8",
    )


def _defs_app(tmp_path, db):
    skills_dir = tmp_path / "skills"
    _write_tool_def(skills_dir)
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        skills_dir=str(skills_dir),
    )


async def test_napi_defs_tool_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_defs_app(tmp_path, db))
    # Proxy-bypassed: a spoofable Remote-User must NOT reach the catalog.
    r = await client.get("/napi/defs/tool", headers={"Remote-User": "mdopp"})
    assert r.status == 401


async def test_napi_defs_tool_valid_token_serves_catalog(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_defs_app(tmp_path, db))
    r = await client.get(
        "/napi/defs/tool", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 200
    j = await r.json()
    # Same payload as /api/defs/tool: the declarative plugin surface (#1021).
    assert j["ok"] is True and j["kind"] == "tool"
    tool = j["defs"][0]
    assert tool["tool-id"] == "task"
    assert tool["tool-api-path"] == "/api/portal/tasks?done=1"
    assert tool["tool-actions"] == ["task.set_status", "task.add"]
    assert tool["tool-cell-schema"] == {"title": "title", "meta": ["due"]}
