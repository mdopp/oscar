"""Notify-admin sends a POST to signal-gateway with bearer auth."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest

from oscar_skill_reviewer.notify import _truncate, notify_admin_via_signal


def test_truncate_under_limit():
    text = "x" * 100
    assert _truncate(text, 200) == text


def test_truncate_at_limit():
    text = "x" * 600
    out = _truncate(text, 500)
    assert len(out) <= 500
    assert out.endswith("…[truncated]")


def _fake_client(captured: dict, status: int = 200, body: dict | None = None):
    """Return a MagicMock that mimics httpx.AsyncClient as a context manager."""
    client = MagicMock()

    async def post(url, json=None, headers=None):  # noqa: A002 — match httpx signature
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers or {}
        resp = MagicMock()
        resp.status_code = status
        resp.text = ""
        if status >= 400:
            resp.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError("boom", request=None, response=resp)
            )
        else:
            resp.raise_for_status = MagicMock()
        return resp

    client.post = post

    @asynccontextmanager
    async def _ctx(*args, **kwargs):
        yield client

    return _ctx


async def test_notify_posts_to_signal_send_endpoint():
    captured = {}
    with patch("oscar_skill_reviewer.notify.httpx.AsyncClient", _fake_client(captured)):
        await notify_admin_via_signal(
            signal_url="http://gateway:8090",
            signal_token="secret",
            admin_number="+4915112345678",
            skill_name="oscar-light",
            diff="--- a/...\n+++ b/...\n",
            reason="3 corrections in 4 days",
        )
    assert captured["url"].endswith("/send")
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["to"] == "+4915112345678"
    assert "oscar-light" in captured["json"]["text"]
    assert "/revert oscar-light" in captured["json"]["text"]


async def test_notify_bubbles_http_errors():
    captured = {}
    with patch(
        "oscar_skill_reviewer.notify.httpx.AsyncClient",
        _fake_client(captured, status=503),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await notify_admin_via_signal(
                signal_url="http://gateway",
                signal_token="",
                admin_number="+49…",
                skill_name="x",
                diff="d",
                reason="r",
            )


async def test_notify_skips_auth_header_when_token_empty():
    captured = {}
    with patch("oscar_skill_reviewer.notify.httpx.AsyncClient", _fake_client(captured)):
        await notify_admin_via_signal(
            signal_url="http://gateway",
            signal_token="",
            admin_number="+49…",
            skill_name="x",
            diff="d",
            reason="r",
        )
    assert "Authorization" not in captured["headers"]
