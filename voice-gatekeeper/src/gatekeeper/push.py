"""HTTP push endpoint — make the gatekeeper speak on a named Voice PE device.

Skills can't talk Wyoming directly to a satellite. After a timer or alarm
fires, the Solaris Engine POSTs here with `{endpoint, text}`; we look the endpoint up
in the configured device map, synthesize via Piper, and forward the
resulting AudioStart/Chunk*/AudioStop events to the device's Wyoming URI.

Auth: bearer token (`PUSH_TOKEN` env). Empty token disables auth — fine
for the default loopback bind (`PUSH_HOST=127.0.0.1`), where only the engine
on the same host reaches it. If an operator rebinds it off loopback, set a
token (under hostNetwork, 0.0.0.0 means the LAN — see #116).
"""

from __future__ import annotations

import uuid
from typing import Any

from aiohttp import web
from gatekeeper.logging import log
from wyoming.client import AsyncClient

from .auth import auth_ok
from .tts import synthesize_to_writer


VOICE_PE_PREFIX = "voice-pe:"


def build_app(
    *,
    piper_uri: str,
    devices: dict[str, str],
    push_token: str = "",
) -> web.Application:
    """Construct the aiohttp app. Pure factory so tests can build it standalone."""

    async def push(request: web.Request) -> web.Response:
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())

        if not auth_ok(request, push_token):
            log.warn("gatekeeper.push.unauthorized", trace_id=trace_id)
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )

        endpoint = str(body.get("endpoint") or "")
        text = str(body.get("text") or "")
        if not endpoint or not text:
            return web.json_response(
                {"ok": False, "reason": "missing_endpoint_or_text"}, status=400
            )
        if not endpoint.startswith(VOICE_PE_PREFIX):
            return web.json_response(
                {"ok": False, "reason": "unsupported_endpoint"}, status=400
            )

        device_name = endpoint[len(VOICE_PE_PREFIX) :]
        device_uri = devices.get(device_name)
        if not device_uri:
            log.warn(
                "gatekeeper.push.unknown_device", trace_id=trace_id, device=device_name
            )
            return web.json_response(
                {"ok": False, "reason": "unknown_device", "device": device_name},
                status=404,
            )

        log.info(
            "gatekeeper.push.start",
            trace_id=trace_id,
            device=device_name,
            chars=len(text),
        )
        try:
            chunks = await _push_to_device(piper_uri, device_uri, text)
        except Exception as exc:  # noqa: BLE001 — surface as 502
            log.error(
                "gatekeeper.push.error",
                trace_id=trace_id,
                device=device_name,
                error=str(exc),
            )
            return web.json_response(
                {"ok": False, "reason": "push_failed", "device": device_name},
                status=502,
            )

        log.info(
            "gatekeeper.push.done",
            trace_id=trace_id,
            device=device_name,
            chunks=chunks,
        )
        return web.json_response({"ok": True, "device": device_name, "chunks": chunks})

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "devices": sorted(devices.keys())})

    app = web.Application()
    app.router.add_post("/push", push)
    app.router.add_get("/health", health)
    return app


def build_combined_app(
    *,
    piper_uri: str,
    devices: dict[str, str],
    push_token: str = "",
    db_path: str | None = None,
    speaker_id_enabled: bool = False,
) -> web.Application:
    """Build the push app and add the DB-backed routes.

    Defined here (not in __main__) so tests can construct the full
    aiohttp surface without spinning up the Wyoming server. Room routes
    need solaris.db regardless of speaker-ID; enrolment is gated on it."""
    app = build_app(piper_uri=piper_uri, devices=devices, push_token=push_token)
    if db_path:
        # Imported here to keep these modules out of the push-only test
        # path that doesn't care about the DB / ML stack.
        from .rooms import add_routes as add_room_routes

        add_room_routes(app, db_path=db_path, push_token=push_token)
        if speaker_id_enabled:
            from .enrollment import add_routes as add_enrolment_routes

            add_enrolment_routes(app, db_path=db_path, push_token=push_token)
    return app


async def _push_to_device(piper_uri: str, device_uri: str, text: str) -> int:
    """Open a Wyoming client to the device, write the synthesized audio."""
    async with AsyncClient.from_uri(device_uri) as device:

        async def forward(event: Any) -> None:
            await device.write_event(event)

        return await synthesize_to_writer(piper_uri, text, forward)


async def serve(
    host: str,
    port: int,
    *,
    piper_uri: str,
    devices: dict[str, str],
    push_token: str = "",
    db_path: str | None = None,
    speaker_id_enabled: bool = False,
) -> None:
    """Run the aiohttp app forever. Caller composes this with the Wyoming server."""
    app = build_combined_app(
        piper_uri=piper_uri,
        devices=devices,
        push_token=push_token,
        db_path=db_path,
        speaker_id_enabled=speaker_id_enabled,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(
        "gatekeeper.push.listening",
        host=host,
        port=port,
        devices=sorted(devices.keys()),
        auth=bool(push_token),
    )
    try:
        # Block forever — aiohttp's TCPSite returns immediately after binding.
        import asyncio

        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
