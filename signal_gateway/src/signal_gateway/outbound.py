"""HTTP outbound endpoint: POST /send + GET /health.

Symmetric to the gatekeeper push endpoint (#34) — same auth shape, same
factory pattern so tests can build the app standalone.
"""

from __future__ import annotations

from aiohttp import web
from oscar_logging import log

from .signal_client import SignalRestClient, SignalRestError


def build_app(*, signal: SignalRestClient, signal_token: str = "") -> web.Application:
    async def send(request: web.Request) -> web.Response:
        if signal_token:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {signal_token}":
                log.warn("signal_gateway.send.unauthorized")
                return web.json_response(
                    {"ok": False, "reason": "unauthorized"}, status=401
                )

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )

        to = str(body.get("to") or "")
        text = str(body.get("text") or "")
        if not to or not text:
            return web.json_response(
                {"ok": False, "reason": "missing_to_or_text"}, status=400
            )
        if not to.startswith("+"):
            return web.json_response(
                {"ok": False, "reason": "to_must_be_e164"}, status=400
            )

        try:
            await signal.send(to, text)
        except SignalRestError as exc:
            log.error("signal_gateway.send.error", to=to, error=str(exc))
            return web.json_response(
                {"ok": False, "reason": "signal_error", "detail": str(exc)[:200]},
                status=502,
            )
        log.info("signal_gateway.send.ok", to=to, chars=len(text))
        return web.json_response({"ok": True})

    async def health(_request: web.Request) -> web.Response:
        signal_health = await signal.health()
        return web.json_response(
            {"ok": bool(signal_health.get("ok")), "signal": signal_health}
        )

    app = web.Application()
    app.router.add_post("/send", send)
    app.router.add_get("/health", health)
    return app


async def serve(
    host: str, port: int, *, signal: SignalRestClient, signal_token: str = ""
) -> None:
    app = build_app(signal=signal, signal_token=signal_token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(
        "signal_gateway.outbound.listening",
        host=host,
        port=port,
        auth=bool(signal_token),
    )
    try:
        import asyncio

        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
