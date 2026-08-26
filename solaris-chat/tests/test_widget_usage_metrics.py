"""Widget-usage forwarding to the household usage-metrics service (#1026).

Three properties are the whole ticket, and all three are asserted here rather
than claimed in prose:

1. **What leaves the house is exactly `{app, event, day, count}`** — the key
   set is pinned to what the real service accepts, so a future edit can't slip
   a uid, a room, an entity id or any request content into the outbound body
   without turning this red.
2. **The stand-in `/ingest` enforces the REAL contract** — the first version of
   this file pinned our *assumption* (`{app, event, day}`) against a stub that
   accepted anything, so a payload the real service answers with 400 shipped
   green (#1252). The stub below mirrors `ingest.go` in mdopp/usage-metrics:
   exactly those four fields, unknown fields rejected, `count` a positive int.
3. **Fail-open, never fail-silent** — an unset token, an unreachable service, a
   slow service or a non-2xx answer must never raise, never delay and never
   change the resident-facing response, but a failure IS logged once (#1252).
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest
from aiohttp import web

from solaris_chat import server

# The authoritative field list, read off the service's own `/ingest` handler
# (mdopp/usage-metrics `ingest.go`, `type increment`): all four are required,
# anything else is a 400.
INGEST_FIELDS = {"app", "event", "day", "count"}


@pytest.fixture
def app():
    """A minimal app carrying only the widget-usage middleware."""

    async def ok(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    application = web.Application(middlewares=[server.widget_usage])
    application.router.add_get("/", ok)
    application.router.add_get("/napi/portal/start", ok)
    return application


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("USAGE_METRICS_TOKEN", "sekrit")
    return "sekrit"


def ingest_error(body: object) -> str | None:
    """The 400 the real `/ingest` would answer with, or None (#1252).

    Mirrors `decodeIncrement` in mdopp/usage-metrics `ingest.go`."""
    if not isinstance(body, dict):
        return "body must be a JSON object"
    if set(body) != INGEST_FIELDS:
        return f"body must be exactly {sorted(INGEST_FIELDS)}, got {sorted(body)}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", str(body["app"])):
        return '"app" must match [a-z0-9][a-z0-9._-]{0,63}'
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", str(body["event"])):
        return '"event" must match [a-z0-9][a-z0-9._-]{0,127}'
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(body["day"])):
        return '"day" must be a calendar date formatted YYYY-MM-DD'
    if not isinstance(body["count"], int) or isinstance(body["count"], bool):
        return '"count" must be a positive integer'
    if body["count"] < 1:
        return '"count" must be a positive integer'
    return None


@pytest.fixture
async def sink(aiohttp_server, monkeypatch):
    """A stand-in usage-metrics service enforcing the real `/ingest` contract."""
    seen: list[dict] = []
    arrived = asyncio.Event()

    async def ingest(request: web.Request) -> web.Response:
        body = await request.json()
        error = ingest_error(body)
        if error is not None:
            arrived.set()
            return web.json_response({"error": error}, status=400)
        seen.append(
            {
                "body": body,
                "auth": request.headers.get("Authorization"),
            }
        )
        arrived.set()
        return web.json_response({"total": len(seen)})

    metrics = web.Application()
    metrics.router.add_post("/ingest", ingest)
    srv = await aiohttp_server(metrics)
    monkeypatch.setattr(server, "USAGE_METRICS_URL", str(srv.make_url("/ingest")))
    return seen, arrived


@pytest.fixture(autouse=True)
def quiet_log(monkeypatch):
    """Capture the once-per-process failure log and reset its latch."""
    warnings: list[tuple[str, dict]] = []

    class _Log:
        def warn(self, message: str, **args: object) -> None:
            warnings.append((message, dict(args)))

        def __getattr__(self, name: str):
            return lambda *a, **k: None

    monkeypatch.setattr(server, "log", _Log())
    monkeypatch.setattr(server, "_usage_metrics_failure_logged", False)
    return warnings


async def _drain() -> None:
    """Let the fire-and-forget posts finish, and surface any exception."""
    for task in list(server._usage_metrics_tasks):
        await task


# ── 1. tag recognition ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "src",
    [
        "widget.tasks.compose",
        "widget.notes.row",
        "widget.favorites.run",
        "widget.energy.header",
        "widget.a-b.c_d",
    ],
)
def test_a_widget_tag_is_recognized(src):
    assert server.widget_event(src) == src


@pytest.mark.parametrize(
    "src",
    [
        None,
        "",
        "widget",
        "widget.tasks",
        "widget.tasks.compose.extra",
        "widgets.tasks.compose",
        "app.tasks.compose",
        "widget.tasks.compose?x=1",
        "widget.tasks.../../etc/passwd",
        "widget.Tasks.Compose",
        "widget." + "a" * 40 + ".compose",
    ],
)
def test_everything_else_is_not_a_widget_tag(src):
    assert server.widget_event(src) is None


# ── 2. exactly {app, event, day, count} leaves the house ──────────────────


def test_the_payload_is_exactly_the_fields_ingest_requires():
    payload = server.usage_metrics_payload("widget.tasks.compose")
    assert set(payload) == INGEST_FIELDS
    assert payload["app"] == "solaris"
    assert payload["event"] == "widget.tasks.compose"
    assert len(payload["day"]) == len("2026-08-26")
    assert payload["count"] == 1
    # What the service itself would say about this body: nothing.
    assert ingest_error(payload) is None


def test_the_stub_rejects_the_payload_that_shipped_in_v0_44_0():
    """Guards the guard: the fixture must 400 the body that caused #1252."""
    assert (
        ingest_error(
            {"app": "solaris", "event": "widget.tasks.compose", "day": "2026-08-26"}
        )
        is not None
    )
    assert (
        ingest_error(
            {
                "app": "solaris",
                "event": "widget.tasks.compose",
                "day": "2026-08-26",
                "count": 0,
            }
        )
        is not None
    )
    # A uid can't sneak in either — the service rejects unknown fields.
    assert (
        ingest_error(
            {
                "app": "solaris",
                "event": "widget.tasks.compose",
                "day": "2026-08-26",
                "count": 1,
                "uid": "anna",
            }
        )
        is not None
    )


async def test_a_napi_hit_posts_exactly_one_increment(aiohttp_client, app, sink, token):
    seen, arrived = sink
    client = await aiohttp_client(app)

    resp = await client.get(
        "/napi/portal/start", params={"src": "widget.tasks.compose"}
    )
    assert resp.status == 200

    await asyncio.wait_for(arrived.wait(), 5)
    await _drain()
    # `seen` only holds bodies the contract-enforcing stub ACCEPTED, so this
    # is the 200-path assertion the old stub could never make (#1252).
    assert len(seen) == 1
    # The privacy line, enforced: no uid, no room, no entity id, no content.
    assert set(seen[0]["body"]) == INGEST_FIELDS
    assert seen[0]["body"]["app"] == "solaris"
    assert seen[0]["body"]["event"] == "widget.tasks.compose"
    assert seen[0]["body"]["count"] == 1
    assert seen[0]["auth"] == "Bearer sekrit"


async def test_an_untagged_request_posts_nothing(aiohttp_client, app, sink, token):
    seen, _ = sink
    client = await aiohttp_client(app)

    assert (await client.get("/")).status == 200
    assert (await client.get("/", params={"src": "app.home"})).status == 200

    await _drain()
    assert seen == []


# ── 3. fail-open ──────────────────────────────────────────────────────────


async def test_no_token_means_no_call_and_no_error(
    aiohttp_client, app, sink, monkeypatch
):
    seen, _ = sink
    monkeypatch.delenv("USAGE_METRICS_TOKEN", raising=False)
    client = await aiohttp_client(app)

    resp = await client.get("/", params={"src": "widget.tasks.compose"})
    assert resp.status == 200
    assert await resp.json() == {"ok": True}

    await _drain()
    assert seen == []


async def test_an_unreachable_service_neither_errors_nor_delays(
    aiohttp_client, app, token, monkeypatch, unused_tcp_port
):
    monkeypatch.setattr(
        server, "USAGE_METRICS_URL", f"http://127.0.0.1:{unused_tcp_port}/ingest"
    )
    client = await aiohttp_client(app)

    started = time.monotonic()
    resp = await client.get("/", params={"src": "widget.tasks.compose"})
    assert resp.status == 200
    assert time.monotonic() - started < 1.0

    await _drain()  # the swallowed connection error must not surface here


async def test_a_slow_service_neither_errors_nor_delays(
    aiohttp_client, app, aiohttp_server, token, monkeypatch
):
    async def crawl(request: web.Request) -> web.Response:
        await asyncio.sleep(5)
        return web.json_response({"ok": True})

    metrics = web.Application()
    metrics.router.add_post("/ingest", crawl)
    srv = await aiohttp_server(metrics)
    monkeypatch.setattr(server, "USAGE_METRICS_URL", str(srv.make_url("/ingest")))
    monkeypatch.setattr(server, "USAGE_METRICS_TIMEOUT_S", 0.1)
    client = await aiohttp_client(app)

    started = time.monotonic()
    resp = await client.get("/", params={"src": "widget.tasks.compose"})
    assert resp.status == 200
    assert await resp.json() == {"ok": True}
    # The resident got their answer while the metrics service is still asleep.
    assert time.monotonic() - started < 1.0

    await _drain()  # the timeout must be swallowed, not raised


async def test_a_rejecting_service_neither_errors_nor_delays(
    aiohttp_client, app, aiohttp_server, token, monkeypatch, quiet_log
):
    async def refuse(request: web.Request) -> web.Response:
        return web.json_response({"error": "unknown field"}, status=400)

    metrics = web.Application()
    metrics.router.add_post("/ingest", refuse)
    srv = await aiohttp_server(metrics)
    monkeypatch.setattr(server, "USAGE_METRICS_URL", str(srv.make_url("/ingest")))
    client = await aiohttp_client(app)

    resp = await client.get("/", params={"src": "widget.tasks.compose"})
    assert resp.status == 200
    assert await resp.json() == {"ok": True}

    await _drain()
    # Fail-open, but NOT fail-silent: the 400 that hid #1252 for a whole
    # release is now in the log, with the service's own reason.
    assert len(quiet_log) == 1
    message, args = quiet_log[0]
    assert message == "chat.usage_metrics.post_failed"
    assert args["status"] == 400
    assert "unknown field" in args["detail"]
    assert args["event"] == "widget.tasks.compose"


async def test_a_broken_service_is_logged_once_not_on_every_hit(
    aiohttp_client, app, aiohttp_server, token, monkeypatch, quiet_log
):
    async def refuse(request: web.Request) -> web.Response:
        return web.json_response({"error": "nope"}, status=500)

    metrics = web.Application()
    metrics.router.add_post("/ingest", refuse)
    srv = await aiohttp_server(metrics)
    monkeypatch.setattr(server, "USAGE_METRICS_URL", str(srv.make_url("/ingest")))
    client = await aiohttp_client(app)

    for _ in range(5):
        resp = await client.get("/", params={"src": "widget.tasks.compose"})
        assert resp.status == 200
        await _drain()

    assert len(quiet_log) == 1


async def test_an_unreachable_service_is_visible_in_the_log(
    aiohttp_client, app, token, monkeypatch, unused_tcp_port, quiet_log
):
    monkeypatch.setattr(
        server, "USAGE_METRICS_URL", f"http://127.0.0.1:{unused_tcp_port}/ingest"
    )
    client = await aiohttp_client(app)

    assert (await client.get("/", params={"src": "widget.tasks.compose"})).status == 200

    await _drain()
    assert len(quiet_log) == 1
    assert quiet_log[0][0] == "chat.usage_metrics.post_failed"
    assert quiet_log[0][1]["error"]
