"""Tests for the push HTTP endpoint.

We don't run a real Piper or a real Voice PE Wyoming server — we patch
`_push_to_device` in the push module to return a fake chunk count. The
endpoint's auth/validation/routing logic is what we exercise.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from gatekeeper.push import build_app


@pytest.fixture
def devices() -> dict[str, str]:
    return {"office": "tcp://192.168.1.10:10700", "bedroom": "tcp://192.168.1.11:10700"}


@pytest.fixture
async def client(aiohttp_client, devices):
    app = build_app(piper_uri="tcp://piper:10200", devices=devices, push_token="")
    return await aiohttp_client(app)


async def test_push_happy_path(client):
    with patch("gatekeeper.push._push_to_device", AsyncMock(return_value=7)):
        resp = await client.post(
            "/push", json={"endpoint": "voice-pe:office", "text": "Pizza fertig"}
        )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True, "device": "office", "chunks": 7}


async def test_push_unknown_device_returns_404(client):
    resp = await client.post("/push", json={"endpoint": "voice-pe:garage", "text": "x"})
    assert resp.status == 404
    body = await resp.json()
    assert body["reason"] == "unknown_device"
    assert body["device"] == "garage"


async def test_push_rejects_non_voice_pe_endpoint(client):
    resp = await client.post(
        "/push", json={"endpoint": "signal:+49151...", "text": "x"}
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "unsupported_endpoint"


async def test_push_rejects_missing_fields(client):
    resp = await client.post("/push", json={"endpoint": "voice-pe:office"})
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "missing_endpoint_or_text"


async def test_push_rejects_invalid_json(client):
    resp = await client.post("/push", data="not json")
    assert resp.status == 400


async def test_push_502_when_device_unreachable(client):
    with patch(
        "gatekeeper.push._push_to_device",
        AsyncMock(side_effect=ConnectionRefusedError()),
    ):
        resp = await client.post(
            "/push", json={"endpoint": "voice-pe:office", "text": "x"}
        )
    assert resp.status == 502
    body = await resp.json()
    assert body["reason"] == "push_failed"


async def test_health_lists_configured_devices(client):
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True, "devices": ["bedroom", "office"]}


async def test_push_token_required_when_set(aiohttp_client, devices):
    app = build_app(piper_uri="tcp://piper:10200", devices=devices, push_token="secret")
    client = await aiohttp_client(app)
    with patch("gatekeeper.push._push_to_device", AsyncMock(return_value=1)):
        bad = await client.post(
            "/push",
            json={"endpoint": "voice-pe:office", "text": "x"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert bad.status == 401
        good = await client.post(
            "/push",
            json={"endpoint": "voice-pe:office", "text": "x"},
            headers={"Authorization": "Bearer secret"},
        )
        assert good.status == 200
