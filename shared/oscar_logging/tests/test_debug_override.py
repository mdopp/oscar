"""Verifies the env-var → runtime-override fallback semantics."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

import oscar_logging
from oscar_logging import set_debug_override


@pytest.fixture(autouse=True)
def _reset_override():
    set_debug_override(None)
    yield
    set_debug_override(None)


def test_env_var_default_off(monkeypatch):
    monkeypatch.delenv("OSCAR_DEBUG_MODE", raising=False)
    assert oscar_logging._debug_active() is False


def test_env_var_on(monkeypatch):
    monkeypatch.setenv("OSCAR_DEBUG_MODE", "true")
    assert oscar_logging._debug_active() is True


def test_override_beats_env_var(monkeypatch):
    monkeypatch.setenv("OSCAR_DEBUG_MODE", "true")
    set_debug_override(False)
    assert oscar_logging._debug_active() is False


def test_override_clear_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("OSCAR_DEBUG_MODE", "true")
    set_debug_override(True)
    set_debug_override(None)
    assert oscar_logging._debug_active() is True


def test_log_debug_respects_override(monkeypatch, capsys):
    monkeypatch.delenv("OSCAR_DEBUG_MODE", raising=False)
    oscar_logging.log.debug("nope", body="suppressed")
    assert capsys.readouterr().out == ""

    set_debug_override(True)
    oscar_logging.log.debug("yes", body="shown")
    out = capsys.readouterr().out
    payload = json.loads(out.strip())
    assert payload["level"] == "debug"


@pytest.mark.asyncio
async def test_runtime_reads_row_and_sets_active():
    from oscar_logging.runtime import _read_debug_mode

    fake_conn = AsyncMock()
    fake_conn.fetchrow.return_value = {"value": {"active": True, "verbose_until": None}}
    fake_conn.close = AsyncMock()

    with patch(
        "oscar_logging.runtime.asyncpg.connect", new=AsyncMock(return_value=fake_conn)
    ):
        result = await _read_debug_mode("postgresql://stub")
    assert result is True


@pytest.mark.asyncio
async def test_runtime_honours_verbose_until_expiry():
    from oscar_logging.runtime import _read_debug_mode

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    fake_conn = AsyncMock()
    fake_conn.fetchrow.return_value = {"value": {"active": True, "verbose_until": past}}
    fake_conn.close = AsyncMock()

    with patch(
        "oscar_logging.runtime.asyncpg.connect", new=AsyncMock(return_value=fake_conn)
    ):
        result = await _read_debug_mode("postgresql://stub")
    assert result is False  # expired — override should be False


@pytest.mark.asyncio
async def test_runtime_future_verbose_until_keeps_active():
    from oscar_logging.runtime import _read_debug_mode

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    fake_conn = AsyncMock()
    fake_conn.fetchrow.return_value = {
        "value": {"active": True, "verbose_until": future}
    }
    fake_conn.close = AsyncMock()

    with patch(
        "oscar_logging.runtime.asyncpg.connect", new=AsyncMock(return_value=fake_conn)
    ):
        result = await _read_debug_mode("postgresql://stub")
    assert result is True


@pytest.mark.asyncio
async def test_runtime_missing_row_returns_none():
    from oscar_logging.runtime import _read_debug_mode

    fake_conn = AsyncMock()
    fake_conn.fetchrow.return_value = None
    fake_conn.close = AsyncMock()

    with patch(
        "oscar_logging.runtime.asyncpg.connect", new=AsyncMock(return_value=fake_conn)
    ):
        result = await _read_debug_mode("postgresql://stub")
    assert result is None
