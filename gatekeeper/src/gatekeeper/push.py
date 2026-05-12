"""HTTP push endpoint — make the gatekeeper speak on a named Voice PE device.

Skills can't talk Wyoming directly to a satellite. After a timer or alarm
fires, HERMES POSTs here with `{endpoint, text}`; we look the endpoint up
in the configured device map, synthesize via Piper, and forward the
resulting AudioStart/Chunk*/AudioStop events to the device's Wyoming URI.

Auth: bearer token (`PUSH_TOKEN` env). Empty token disables auth — fine
for a pod-internal listener since the port isn't routed outside the pod.
"""

from __future__ import annotations

import uuid
from typing import Any

from aiohttp import web
from oscar_logging import log
from wyoming.client import AsyncClient

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

        if push_token:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {push_token}":
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
) -> None:
    """Run the aiohttp app forever. Caller composes this with the Wyoming server."""
    app = build_app(piper_uri=piper_uri, devices=devices, push_token=push_token)
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
