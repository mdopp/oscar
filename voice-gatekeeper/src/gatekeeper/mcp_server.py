"""MCP tool server exposing the satellite->room store to the Solaris Engine.

#94 route (b): the engine calls these MCP tools to read/write the
satellite->room mapping instead of POSTing the gatekeeper's HTTP `/room`
endpoint. `/room` shares `PUSH_TOKEN` with `/push`, so reaching it from
the engine would mean shipping that token into the engine's pod — a wider trust
surface (a prompt-injected session could then drive `/push`). A dedicated
MCP server keeps room enrolment reachable while the push credential stays
out of the agent.

Runs as its own streamable-HTTP ASGI app on `MCP_PORT` (a third listener
beside the Wyoming server and the push/HTTP app). The engine reaches it from
the household profile's `RoomMcpToolbox` via `GATEKEEPER_MCP_URL` /
`GATEKEEPER_MCP_TOKEN` (#1295) — there is no `mcp_servers:` registry block.
"""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from gatekeeper.logging import log

from .rooms_store import list_rooms as _list_rooms
from .rooms_store import set_room as _set_room

VOICE_PE_PREFIX = "voice-pe:"
_MAX_ROOM_LEN = 64
_MAX_SAT_LEN = 128


def build_mcp(*, db_path: str) -> MCPServer:
    """Construct the MCP server with the room tools. Pure factory so
    tests can drive the tools without binding a port."""
    mcp = MCPServer("solaris-gatekeeper-rooms")

    @mcp.tool()
    async def set_room(room: str, satellite_id: str = "", endpoint: str = "") -> dict:
        """Map a voice satellite to a room (insert or remap).

        Pass either `satellite_id` (the gatekeeper client id / peer host)
        or `endpoint` of the form `voice-pe:<satellite_id>`. Use this when a
        resident answers "which room am I in?" during a room-dependent
        command (#94).
        """
        sat = satellite_id.strip()
        if not sat and endpoint.startswith(VOICE_PE_PREFIX):
            sat = endpoint[len(VOICE_PE_PREFIX) :].strip()
        room_value = room.strip()
        if not sat or len(sat) > _MAX_SAT_LEN:
            return {"ok": False, "reason": "invalid_satellite_id"}
        if not room_value or len(room_value) > _MAX_ROOM_LEN:
            return {"ok": False, "reason": "invalid_room"}
        try:
            await asyncio.to_thread(_set_room, db_path, sat, room_value)
        except Exception as exc:  # noqa: BLE001 — DB/table not ready
            log.error("gatekeeper.mcp.set_room_error", satellite_id=sat, error=str(exc))
            return {"ok": False, "reason": "db_not_ready"}
        log.info("gatekeeper.mcp.set_room", satellite_id=sat, room=room_value)
        return {"ok": True, "satellite_id": sat, "room": room_value}

    @mcp.tool()
    async def list_rooms() -> dict:
        """Return the known satellite->room mappings as `{satellite_id: room}`."""
        rooms = await asyncio.to_thread(_list_rooms, db_path)
        return {"rooms": rooms}

    # We register zero prompts and zero resources, but MCPServer unconditionally
    # installs their protocol handlers, so `get_capabilities` advertises the
    # prompts+resources capabilities and the engine's MCP client surfaces
    # list_prompts/get_prompt/list_resources/read_resource as four useless
    # model-callable tools in every prompt (#312). Drop those handlers so the
    # initialize response advertises only `tools`.
    for _method in (
        "prompts/list",
        "prompts/get",
        "resources/list",
        "resources/read",
        "resources/templates/list",
    ):
        mcp._lowlevel_server._request_handlers.pop(_method, None)

    return mcp


class _BearerAuth:
    """Pure-ASGI bearer gate. Empty token = open, mirroring the push
    endpoint's `PUSH_TOKEN` semantics (the listener is pod-internal). Pure
    ASGI rather than BaseHTTPMiddleware so it doesn't buffer the
    streamable-HTTP/SSE responses."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._token:
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {self._token}":
                await JSONResponse(
                    {"ok": False, "reason": "unauthorized"}, status_code=401
                )(scope, receive, send)
                return
        await self._app(scope, receive, send)


async def serve(*, db_path: str, host: str, port: int, token: str = "") -> None:
    """Run the room MCP server forever. Caller composes this with the
    Wyoming + push servers under one asyncio loop."""
    import uvicorn

    mcp = build_mcp(db_path=db_path)
    app = _BearerAuth(mcp.streamable_http_app(), token)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", lifespan="on"
    )
    log.info("gatekeeper.mcp.listening", host=host, port=port, auth=bool(token))
    await uvicorn.Server(config).serve()
