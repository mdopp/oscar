"""The household profile's room MCP client (#1295).

Drives `RoomMcpToolbox` over real streamable HTTP against a real MCP server
that mirrors the gatekeeper's two room tools behind the same bearer gate, so
the wiring (token in the header, tools reachable, write withheld from an
unidentified voice turn) is proven end to end rather than mocked.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse

from solaris_chat.engine.tools import (
    CHANNEL_VOICE,
    current_channel,
    current_speaker_id_enabled,
    current_speaker_matched,
)
from solaris_chat.engine.tools.rooms_mcp import RoomMcpToolbox

TOKEN = "gk-room-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _BearerAuth:
    """The gatekeeper's own pure-ASGI bearer gate (mcp_server.py), copied so
    this test proves the client actually presents the token."""

    def __init__(self, app, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self._token:
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {self._token}":
                await JSONResponse(
                    {"ok": False, "reason": "unauthorized"}, status_code=401
                )(scope, receive, send)
                return
        await self._app(scope, receive, send)


@pytest.fixture
async def rooms_url():
    """A stand-in for the gatekeeper's room MCP: same two tools, same gate."""
    rooms: dict[str, str] = {}
    server = MCPServer("solaris-gatekeeper-rooms")

    @server.tool()
    async def set_room(room: str, satellite_id: str = "", endpoint: str = "") -> dict:
        """Map a voice satellite to a room."""
        rooms[satellite_id] = room
        return {"ok": True, "satellite_id": satellite_id, "room": room}

    @server.tool()
    async def list_rooms() -> dict:
        """Return the known satellite->room mappings."""
        return {"rooms": dict(rooms)}

    port = _free_port()
    uv = uvicorn.Server(
        uvicorn.Config(
            _BearerAuth(server.streamable_http_app(), TOKEN),
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
        )
    )
    task = asyncio.create_task(uv.serve())
    while not uv.started:
        await asyncio.sleep(0.01)
    yield f"http://127.0.0.1:{port}/mcp"
    uv.should_exit = True
    await task


@pytest.fixture(autouse=True)
def _reset_turn_context():
    """Each test sets the turn's channel/speaker context itself."""
    current_channel.set("")
    current_speaker_matched.set(False)
    current_speaker_id_enabled.set(True)


async def test_toolbox_reaches_the_room_tools_with_its_own_token(rooms_url):
    box = RoomMcpToolbox(rooms_url, TOKEN)
    await box.prepare()

    assert set(box.names()) == {"set_room", "list_rooms"}
    out = await box.dispatch("set_room", {"satellite_id": "sat-1", "room": "Küche"})
    assert json.loads(out)["ok"] is True
    assert json.loads(await box.dispatch("list_rooms", {}))["rooms"] == {
        "sat-1": "Küche"
    }


async def test_a_wrong_token_leaves_the_toolbox_empty_not_broken(rooms_url):
    box = RoomMcpToolbox(rooms_url, "not-the-token")
    await box.prepare()

    assert box.names() == []
    assert "unknown tool" in await box.dispatch("set_room", {"room": "Küche"})


async def test_the_write_is_withheld_from_an_unidentified_voice_turn(rooms_url):
    """Speaker-ID ran and matched nobody: `set_room` is neither offered nor
    dispatched, and the gatekeeper is never called."""
    box = RoomMcpToolbox(rooms_url, TOKEN)
    await box.prepare()
    current_channel.set(CHANNEL_VOICE)
    current_speaker_id_enabled.set(True)
    current_speaker_matched.set(False)

    offered = {d["function"]["name"] for d in box.definitions()}
    assert offered == {"list_rooms"}
    refused = json.loads(await box.dispatch("set_room", {"room": "Schlafzimmer"}))
    assert refused["ok"] is False
    assert refused["reason"] == "unidentified_speaker"
    assert refused["say"]
    # The read stays open — satellite ids and room names are household data.
    assert json.loads(await box.dispatch("list_rooms", {}))["rooms"] == {}


async def test_a_matched_speaker_may_set_the_room(rooms_url):
    box = RoomMcpToolbox(rooms_url, TOKEN)
    await box.prepare()
    current_channel.set(CHANNEL_VOICE)
    current_speaker_matched.set(True)

    assert "set_room" in {d["function"]["name"] for d in box.definitions()}
    out = await box.dispatch("set_room", {"satellite_id": "sat-2", "room": "Bad"})
    assert json.loads(out)["ok"] is True


async def test_with_speaker_id_off_the_gate_collapses(rooms_url):
    """#1130's collapse: where nothing can ever match, gating would mean nobody
    could ever answer "which room is this?" out loud."""
    box = RoomMcpToolbox(rooms_url, TOKEN)
    await box.prepare()
    current_channel.set(CHANNEL_VOICE)
    current_speaker_id_enabled.set(False)
    current_speaker_matched.set(False)

    assert "set_room" in {d["function"]["name"] for d in box.definitions()}
    out = await box.dispatch("set_room", {"satellite_id": "sat-3", "room": "Flur"})
    assert json.loads(out)["ok"] is True


# -- profile wiring ----------------------------------------------------------


async def _clients(rooms_url: str, tmp_path):
    from solaris_chat.engine.profiles import build_engine_clients

    return build_engine_clients(
        db_path=str(tmp_path / "solaris.db"),
        ollama_url="http://x",
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
        soul_path="/nonexistent/SOUL.md",
        gatekeeper_mcp_url=rooms_url,
        gatekeeper_mcp_token=TOKEN,
    )


async def test_the_household_profile_can_call_the_room_tools(rooms_url, tmp_path):
    household, *_rest = await _clients(rooms_url, tmp_path)

    out = await household.dispatch_tool(
        "set_room", {"satellite_id": "sat-4", "room": "Wohnzimmer"}
    )
    assert json.loads(out)["ok"] is True


async def test_the_guest_profile_has_no_room_tools_at_all(rooms_url, tmp_path):
    """#353's denial-by-absence: an unknown speaker's toolbox doesn't hold
    them, so there is nothing to gate."""
    _, _, guest, librarian, enrollment, _, _ = await _clients(rooms_url, tmp_path)

    for client in (guest, librarian, enrollment):
        toolsets = await client.list_toolsets()
        assert not ({"set_room", "list_rooms"} & set(toolsets[0]["tools"]))
        assert "unknown tool" in await client.dispatch_tool(
            "set_room", {"satellite_id": "sat-5", "room": "Schlafzimmer"}
        )


async def test_no_room_toolbox_without_a_url(tmp_path):
    from solaris_chat.engine.profiles import build_engine_clients

    household, *_rest = build_engine_clients(
        db_path=str(tmp_path / "solaris.db"),
        ollama_url="http://x",
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
        soul_path="/nonexistent/SOUL.md",
    )
    assert "unknown tool" in await household.dispatch_tool("list_rooms", {})
