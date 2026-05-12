"""Tests for oscar_audit.core against a FakePool."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("OSCAR_COMPONENT", "test")

from oscar_audit import query


class _FakeConn:
    def __init__(self, rows_by_table: dict):
        self.rows_by_table = rows_by_table
        self.last_query: str | None = None
        self.last_args: tuple = ()

    async def fetch(self, sql: str, *args):
        self.last_query = sql
        self.last_args = args
        if "FROM cloud_audit" in sql:
            return list(self.rows_by_table.get("cloud_audit", []))
        if "FROM gateway_identities" in sql:
            return list(self.rows_by_table.get("gateway_identities", []))
        if "FROM time_jobs" in sql:
            return list(self.rows_by_table.get("time_jobs", []))
        raise AssertionError(f"FakeConn: no fixture for query: {sql[:60]}")


class FakePool:
    def __init__(self, rows_by_table: dict):
        self.conn = _FakeConn(rows_by_table)

    async def acquire(self):
        return self.conn

    async def release(self, _conn):
        return None


@pytest.mark.asyncio
async def test_cloud_audit_metadata_only_by_default(monkeypatch):
    monkeypatch.setenv("OSCAR_DEBUG_MODE", "false")
    pool = FakePool(
        {
            "cloud_audit": [
                {
                    "id": "11",
                    "ts": datetime(2026, 5, 12, 8, tzinfo=timezone.utc),
                    "trace_id": "tr-1",
                    "uid": "michael",
                    "vendor": "anthropic:claude-sonnet-4",
                    "prompt_hash": "hash",
                    "prompt_length": 50,
                    "response_length": 100,
                    "latency_ms": 800,
                    "cost_usd_micro": 1500,
                    "router_score": 0.7,
                    "escalation_reason": "multi-step",
                    "prompt_fulltext": "the secret prompt",
                    "response_fulltext": "the secret response",
                }
            ]
        }
    )

    rows = await query(pool, stream="cloud_audit")
    assert len(rows) == 1
    assert rows[0]["prompt_fulltext"] is None
    assert rows[0]["response_fulltext"] is None
    assert rows[0]["_pii_redacted"] is True
    assert rows[0]["uid"] == "michael"  # metadata stays


@pytest.mark.asyncio
async def test_cloud_audit_fulltext_in_debug_mode(monkeypatch):
    monkeypatch.setenv("OSCAR_DEBUG_MODE", "true")
    pool = FakePool(
        {
            "cloud_audit": [
                {
                    "id": "11",
                    "ts": datetime(2026, 5, 12, 8, tzinfo=timezone.utc),
                    "trace_id": "tr-1",
                    "uid": "michael",
                    "vendor": "anthropic:claude-sonnet-4",
                    "prompt_hash": "h",
                    "prompt_length": 50,
                    "response_length": 100,
                    "latency_ms": 800,
                    "cost_usd_micro": 1500,
                    "router_score": 0.7,
                    "escalation_reason": "multi-step",
                    "prompt_fulltext": "the secret prompt",
                    "response_fulltext": "the secret response",
                }
            ]
        }
    )

    rows = await query(pool, stream="cloud_audit")
    assert rows[0]["prompt_fulltext"] == "the secret prompt"
    assert rows[0]["response_fulltext"] == "the secret response"
    assert "_pii_redacted" not in rows[0]


@pytest.mark.asyncio
async def test_filter_args_become_sql_params(monkeypatch):
    monkeypatch.setenv("OSCAR_DEBUG_MODE", "false")
    pool = FakePool({"cloud_audit": []})
    await query(
        pool,
        stream="cloud_audit",
        since=datetime(2026, 5, 12, 0, tzinfo=timezone.utc),
        uid="michael",
        vendor="anthropic",
        limit=10,
    )
    sql = pool.conn.last_query
    assert "ts >=" in sql
    assert "uid =" in sql
    assert "vendor LIKE" in sql
    assert "LIMIT 10" in sql
    assert len(pool.conn.last_args) == 3


@pytest.mark.asyncio
async def test_unknown_stream_rejected():
    pool = FakePool({})
    with pytest.raises(ValueError):
        await query(pool, stream="not_a_stream")


@pytest.mark.asyncio
async def test_limit_bounds():
    pool = FakePool({"cloud_audit": []})
    with pytest.raises(ValueError):
        await query(pool, stream="cloud_audit", limit=0)
    with pytest.raises(ValueError):
        await query(pool, stream="cloud_audit", limit=1000)


@pytest.mark.asyncio
async def test_time_jobs_stream():
    pool = FakePool(
        {
            "time_jobs": [
                {
                    "id": "j1",
                    "kind": "timer",
                    "owner_uid": "michael",
                    "label": "Pizza",
                    "fires_at": datetime(2026, 5, 12, 8, tzinfo=timezone.utc),
                    "rrule": None,
                    "target_endpoint": "voice-pe:office",
                    "state": "armed",
                    "created_at": datetime(2026, 5, 12, 7, 55, tzinfo=timezone.utc),
                }
            ]
        }
    )
    rows = await query(pool, stream="time_jobs", uid="michael")
    assert len(rows) == 1
    assert rows[0]["state"] == "armed"
