"""Tests for the outbound POST /send endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from signal_gateway.outbound import build_app
from signal_gateway.signal_client import SignalRestError


@pytest.fixture
def signal_mock():
    m = MagicMock()
    m.send = AsyncMock()
    m.health = AsyncMock(return_value={"ok": True, "details": {"version": "0.13"}})
    return m


@pytest.fixture
async def client(aiohttp_client, signal_mock):
    app = build_app(signal=signal_mock, signal_token="")
    return await aiohttp_client(app)


async def test_send_happy_path(client, signal_mock):
    resp = await client.post("/send", json={"to": "+4915112345678", "text": "hallo"})
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True}
    signal_mock.send.assert_awaited_once_with("+4915112345678", "hallo")


async def test_send_rejects_missing_fields(client):
    resp = await client.post("/send", json={"to": "+49…"})
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "missing_to_or_text"


async def test_send_rejects_non_e164(client):
    resp = await client.post("/send", json={"to": "0151123", "text": "x"})
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "to_must_be_e164"


async def test_send_rejects_invalid_json(client):
    resp = await client.post("/send", data="not-json")
    assert resp.status == 400


async def test_send_502_on_signal_error(client, signal_mock):
    signal_mock.send.side_effect = SignalRestError("400 boom")
    resp = await client.post("/send", json={"to": "+49…", "text": "x"})
    assert resp.status == 502
    body = await resp.json()
    assert body["reason"] == "signal_error"


async def test_send_requires_token_when_set(aiohttp_client, signal_mock):
    app = build_app(signal=signal_mock, signal_token="secret")
    client = await aiohttp_client(app)
    bad = await client.post(
        "/send",
        json={"to": "+49…", "text": "x"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert bad.status == 401
    good = await client.post(
        "/send",
        json={"to": "+49…", "text": "x"},
        headers={"Authorization": "Bearer secret"},
    )
    assert good.status == 200


async def test_health_reports_signal_status(client, signal_mock):
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["signal"]["ok"] is True
