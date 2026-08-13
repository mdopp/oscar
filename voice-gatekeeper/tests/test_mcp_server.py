"""Tests for the room MCP tool server (#104)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from gatekeeper.mcp_server import _BearerAuth, build_mcp

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


@asynccontextmanager
async def connect(server):
    """An initialized client session wired to `server` in-process."""
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _payload(result):
    """Tools return a dict, which MCPServer serializes into text content."""
    return json.loads(result.content[0].text)


async def test_lists_room_tools(db_path):
    async with connect(build_mcp(db_path=db_path)) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == {"set_room", "list_rooms"}


def test_only_tools_capability_advertised(db_path):
    """#312 — we register zero prompts/resources, so the server must advertise
    only the tools capability (else the engine surfaces list_prompts/get_prompt/
    list_resources/read_resource as four useless tools in every prompt)."""
    from mcp.server.lowlevel.server import NotificationOptions

    caps = build_mcp(db_path=db_path)._lowlevel_server.get_capabilities(
        NotificationOptions(), {}
    )
    assert caps.tools is not None
    assert caps.prompts is None
    assert caps.resources is None


async def test_set_and_list_room(db_path):
    async with connect(build_mcp(db_path=db_path)) as client:
        set_res = await client.call_tool(
            "set_room", {"satellite_id": "192.168.178.42", "room": "kitchen"}
        )
        assert _payload(set_res) == {
            "ok": True,
            "satellite_id": "192.168.178.42",
            "room": "kitchen",
        }
        list_res = await client.call_tool("list_rooms", {})
        assert _payload(list_res) == {"rooms": {"192.168.178.42": "kitchen"}}


async def test_set_room_accepts_endpoint_form(db_path):
    async with connect(build_mcp(db_path=db_path)) as client:
        res = await client.call_tool(
            "set_room", {"endpoint": "voice-pe:10.0.0.5", "room": "office"}
        )
        assert _payload(res)["satellite_id"] == "10.0.0.5"


async def test_set_room_rejects_blank_room(db_path):
    async with connect(build_mcp(db_path=db_path)) as client:
        res = await client.call_tool("set_room", {"satellite_id": "x", "room": "   "})
        assert _payload(res) == {"ok": False, "reason": "invalid_room"}


async def test_set_room_rejects_missing_satellite(db_path):
    async with connect(build_mcp(db_path=db_path)) as client:
        res = await client.call_tool("set_room", {"room": "kitchen"})
        assert _payload(res) == {"ok": False, "reason": "invalid_satellite_id"}


# ── bearer auth middleware ───────────────────────────────────────────────────


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_bearer_auth_open_when_token_empty():
    transport = httpx.ASGITransport(app=_BearerAuth(_ok_app, ""))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/mcp")).status_code == 200


async def test_bearer_auth_enforced_when_token_set():
    transport = httpx.ASGITransport(app=_BearerAuth(_ok_app, "secret"))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/mcp")).status_code == 401
        assert (
            await c.get("/mcp", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (
            await c.get("/mcp", headers={"Authorization": "Bearer secret"})
        ).status_code == 200
