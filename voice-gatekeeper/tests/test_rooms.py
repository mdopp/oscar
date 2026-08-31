"""Tests for the /room HTTP endpoint."""

from __future__ import annotations

import re
import sqlite3

import pytest
from aiohttp import web

from gatekeeper.push import build_combined_app
from gatekeeper.rooms import add_routes

_DDL = (
    "CREATE TABLE voice_pe_rooms ("
    "satellite_id TEXT PRIMARY KEY, room TEXT NOT NULL, "
    "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
)


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(p)
    conn.execute(_DDL)
    conn.commit()
    conn.close()
    return p


@pytest.fixture
async def client(aiohttp_client, db_path):
    app = web.Application()
    add_routes(app, db_path=db_path, push_token="")
    return await aiohttp_client(app)


async def test_set_room_happy(client):
    resp = await client.post(
        "/room", json={"satellite_id": "192.168.178.42", "room": "kitchen"}
    )
    assert resp.status == 200
    assert await resp.json() == {
        "ok": True,
        "satellite_id": "192.168.178.42",
        "room": "kitchen",
    }


async def test_set_room_accepts_endpoint_form(client):
    resp = await client.post(
        "/room", json={"endpoint": "voice-pe:10.0.0.5", "room": "office"}
    )
    assert resp.status == 200
    assert (await resp.json())["satellite_id"] == "10.0.0.5"


async def test_set_room_missing_room(client):
    resp = await client.post("/room", json={"satellite_id": "x"})
    assert resp.status == 400
    assert (await resp.json())["reason"] == "invalid_room"


async def test_set_room_missing_satellite(client):
    resp = await client.post("/room", json={"room": "kitchen"})
    assert resp.status == 400
    assert (await resp.json())["reason"] == "invalid_satellite_id"


async def test_set_room_invalid_json(client):
    resp = await client.post("/room", data="not json")
    assert resp.status == 400


async def test_list_and_delete_rooms(client):
    await client.post("/room", json={"satellite_id": "a", "room": "kitchen"})
    listed = await client.get("/rooms")
    assert await listed.json() == {"rooms": {"a": "kitchen"}}
    deleted = await client.delete("/rooms/a")
    assert await deleted.json() == {"ok": True, "removed": True}


async def test_set_room_requires_token_when_set(aiohttp_client, db_path):
    app = web.Application()
    add_routes(app, db_path=db_path, push_token="secret")
    client = await aiohttp_client(app)
    bad = await client.post(
        "/room",
        json={"satellite_id": "a", "room": "k"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert bad.status == 401
    good = await client.post(
        "/room",
        json={"satellite_id": "a", "room": "k"},
        headers={"Authorization": "Bearer secret"},
    )
    assert good.status == 200


async def test_list_rooms_requires_token_when_set(aiohttp_client, db_path):
    """GET /rooms leaks the whole satellite->room map without this (#1290)."""
    app = web.Application()
    add_routes(app, db_path=db_path, push_token="secret")
    client = await aiohttp_client(app)
    anon = await client.get("/rooms")
    assert anon.status == 401
    assert (await anon.json())["reason"] == "unauthorized"
    bad = await client.get("/rooms", headers={"Authorization": "Bearer wrong"})
    assert bad.status == 401
    good = await client.get("/rooms", headers={"Authorization": "Bearer secret"})
    assert good.status == 200
    assert "rooms" in await good.json()


# Routes that may answer an unauthenticated request, with the reason each is
# safe. A new route is NOT on this list, so it must return 401 or the
# enumeration below fails — adding one here is a deliberate, reviewable line
# (#1300: `GET /rooms` shipped with no check at all and nothing noticed).
_UNAUTHENTICATED_ROUTES = {
    "GET /health": "liveness probe; returns only device names, polled tokenless",
}


async def test_every_route_requires_the_push_token(aiohttp_client, db_path):
    """Every registered route, driven with a token set, must refuse anonymous.

    This is the guard the two forgotten checks (#1290, #1287) needed: it walks
    the router instead of trusting that a new handler copied its neighbours.
    """
    app = build_combined_app(
        piper_uri="tcp://127.0.0.1:10200",
        devices={},
        push_token="secret",
        db_path=db_path,
        speaker_id_enabled=True,
    )
    client = await aiohttp_client(app)

    unguarded = []
    for route in app.router.routes():
        # aiohttp mirrors every GET onto the same handler as HEAD.
        if route.method == "HEAD":
            continue
        canonical = route.resource.canonical
        name = f"{route.method} {canonical}"
        resp = await client.request(route.method, re.sub(r"\{[^}]+\}", "x", canonical))
        if resp.status == 401 or name in _UNAUTHENTICATED_ROUTES:
            continue
        unguarded.append(f"{name} -> {resp.status}")

    assert not unguarded, (
        "these routes answered an anonymous request while PUSH_TOKEN was set: "
        f"{unguarded}. Call `auth_ok(request, push_token)` in the handler, or — "
        "if the route is genuinely public — add it to _UNAUTHENTICATED_ROUTES "
        "with the reason it is safe."
    )


async def test_blank_token_leaves_every_route_open_by_design(aiohttp_client, db_path):
    """#1300 decided a blank PUSH_TOKEN stays open — see gatekeeper/auth.py.

    Pinned here so flipping it to fail-closed is a deliberate act that breaks a
    test, not a silent change of the pod's HTTP surface.
    """
    app = build_combined_app(
        piper_uri="tcp://127.0.0.1:10200",
        devices={},
        push_token="",
        db_path=db_path,
    )
    client = await aiohttp_client(app)
    assert (await client.get("/rooms")).status == 200
    assert (await client.delete("/rooms/nobody")).status == 200
