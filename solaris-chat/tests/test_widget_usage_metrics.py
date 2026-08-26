"""Widget-usage forwarding to the household usage-metrics service (#1026).

Two properties are the whole ticket, and both are asserted here rather than
claimed in prose:

1. **What leaves the house is exactly `{app, event, day}`** — the key set is
   pinned, so a future edit can't slip a uid, a room, an entity id or any
   request content into the outbound body without turning this red.
2. **Fail-open, always** — an unset token, an unreachable service, a slow
   service or a non-2xx answer must never raise, never delay and never change
   the resident-facing response.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp import web

from solaris_chat import server


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


@pytest.fixture
async def sink(aiohttp_server, monkeypatch):
    """A stand-in usage-metrics service; records what Solaris posts."""
    seen: list[dict] = []
    arrived = asyncio.Event()

    async def ingest(request: web.Request) -> web.Response:
        seen.append(
            {
                "body": await request.json(),
                "auth": request.headers.get("Authorization"),
            }
        )
        arrived.set()
        return web.json_response({"ok": True})

    metrics = web.Application()
    metrics.router.add_post("/ingest", ingest)
    srv = await aiohttp_server(metrics)
    monkeypatch.setattr(server, "USAGE_METRICS_URL", str(srv.make_url("/ingest")))
    return seen, arrived


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


# ── 2. exactly {app, event, day} leaves the house ─────────────────────────


def test_the_payload_carries_the_three_allowed_keys_and_no_others():
    payload = server.usage_metrics_payload("widget.tasks.compose")
    assert set(payload) == {"app", "event", "day"}
    assert payload["app"] == "solaris"
    assert payload["event"] == "widget.tasks.compose"
    assert len(payload["day"]) == len("2026-08-26")


async def test_a_napi_hit_posts_exactly_one_increment(aiohttp_client, app, sink, token):
    seen, arrived = sink
    client = await aiohttp_client(app)

    resp = await client.get(
        "/napi/portal/start", params={"src": "widget.tasks.compose"}
    )
    assert resp.status == 200

    await asyncio.wait_for(arrived.wait(), 5)
    await _drain()
    assert len(seen) == 1
    # The privacy line, enforced: no uid, no room, no entity id, no content.
    assert set(seen[0]["body"]) == {"app", "event", "day"}
    assert seen[0]["body"]["app"] == "solaris"
    assert seen[0]["body"]["event"] == "widget.tasks.compose"
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
    aiohttp_client, app, aiohttp_server, token, monkeypatch
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
