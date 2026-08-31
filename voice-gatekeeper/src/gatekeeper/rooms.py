"""HTTP endpoint to set/remap a voice satellite's room.

The satellite->room mapping is self-enrolled by conversation: when a
resident gives a room-dependent command and the gatekeeper has no room for
the originating satellite, the Solaris Engine asks ("which room am I in?") and POSTs
the answer here; "this is the bath" remaps the same satellite (see #94).

Accepts either `satellite_id` (the gatekeeper client id = peer host) or an
`endpoint` of the form `voice-pe:<satellite_id>`. Shares `PUSH_TOKEN` with
the push/enrolment endpoints — same pod-internal trust boundary.
"""

from __future__ import annotations

import asyncio

from aiohttp import web
from gatekeeper.logging import log

from .rooms_store import delete_room, list_rooms, set_room

VOICE_PE_PREFIX = "voice-pe:"
_MAX_ROOM_LEN = 64
_MAX_SAT_LEN = 128


def _auth_ok(request: web.Request, token: str) -> bool:
    if not token:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {token}"


def add_routes(app: web.Application, *, db_path: str, push_token: str) -> None:
    """Attach room endpoints to an existing aiohttp app (shares the push port)."""

    async def set_room_route(request: web.Request) -> web.Response:
        if not _auth_ok(request, push_token):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )

        satellite_id = str(body.get("satellite_id") or "").strip()
        if not satellite_id:
            endpoint = str(body.get("endpoint") or "")
            if endpoint.startswith(VOICE_PE_PREFIX):
                satellite_id = endpoint[len(VOICE_PE_PREFIX) :].strip()
        room = str(body.get("room") or "").strip()

        if not satellite_id or len(satellite_id) > _MAX_SAT_LEN:
            return web.json_response(
                {"ok": False, "reason": "invalid_satellite_id"}, status=400
            )
        if not room or len(room) > _MAX_ROOM_LEN:
            return web.json_response(
                {"ok": False, "reason": "invalid_room"}, status=400
            )

        try:
            await asyncio.to_thread(set_room, db_path, satellite_id, room)
        except Exception as exc:  # noqa: BLE001 — DB/table not ready
            log.error(
                "gatekeeper.room.set_error", satellite_id=satellite_id, error=str(exc)
            )
            return web.json_response(
                {"ok": False, "reason": "db_not_ready"}, status=503
            )
        log.info("gatekeeper.room.set", satellite_id=satellite_id, room=room)
        return web.json_response(
            {"ok": True, "satellite_id": satellite_id, "room": room}
        )

    async def list_rooms_route(request: web.Request) -> web.Response:
        if not _auth_ok(request, push_token):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        rooms = await asyncio.to_thread(list_rooms, db_path)
        return web.json_response({"rooms": rooms})

    async def delete_room_route(request: web.Request) -> web.Response:
        if not _auth_ok(request, push_token):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        satellite_id = request.match_info.get("satellite_id", "")
        removed = await asyncio.to_thread(delete_room, db_path, satellite_id)
        return web.json_response({"ok": True, "removed": removed})

    app.router.add_post("/room", set_room_route)
    app.router.add_get("/rooms", list_rooms_route)
    app.router.add_delete("/rooms/{satellite_id}", delete_room_route)
