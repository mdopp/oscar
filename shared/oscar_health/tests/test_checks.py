"""Unit tests for individual probes."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("OSCAR_COMPONENT", "test")

from oscar_health import Check, check_all, wait_for_ready
from oscar_health.runner import ReadinessTimeout


@pytest.mark.asyncio
async def test_postgres_ok():
    fake_conn = AsyncMock()
    fake_conn.execute = AsyncMock(return_value=None)
    fake_conn.close = AsyncMock()
    with patch(
        "oscar_health.checks.asyncpg.connect", new=AsyncMock(return_value=fake_conn)
    ):
        result = await Check.postgres(dsn="postgresql://stub").probe()
    assert result.ok is True
    assert result.name == "postgres"


@pytest.mark.asyncio
async def test_postgres_failure_reports_detail():
    with patch(
        "oscar_health.checks.asyncpg.connect",
        new=AsyncMock(side_effect=OSError("nope")),
    ):
        result = await Check.postgres(dsn="postgresql://stub").probe()
    assert result.ok is False
    assert "nope" in (result.detail or "")


@pytest.mark.asyncio
async def test_http_ok(httpx_mock):
    httpx_mock.add_response(url="http://stub.local/health", status_code=200, text="ok")
    result = await Check.http("http://stub.local/health").probe()
    assert result.ok is True


@pytest.mark.asyncio
async def test_http_5xx_is_failure(httpx_mock):
    httpx_mock.add_response(url="http://stub.local/health", status_code=503)
    result = await Check.http("http://stub.local/health").probe()
    assert result.ok is False
    assert "503" in (result.detail or "")


@pytest.mark.asyncio
async def test_tcp_open(unused_tcp_port):
    async def _serve():
        server = await asyncio.start_server(
            lambda r, w: w.close(), "127.0.0.1", unused_tcp_port
        )
        async with server:
            await asyncio.sleep(0.5)

    server_task = asyncio.create_task(_serve())
    await asyncio.sleep(0.05)
    try:
        result = await Check.tcp("127.0.0.1", unused_tcp_port).probe()
        assert result.ok is True
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_tcp_closed():
    result = await Check.tcp("127.0.0.1", 1, timeout_s=0.3).probe()
    assert result.ok is False


@pytest.mark.asyncio
async def test_check_all_runs_in_parallel(httpx_mock):
    httpx_mock.add_response(url="http://a.local/", status_code=200)
    httpx_mock.add_response(url="http://b.local/", status_code=200)
    results = await check_all(
        [
            Check.http("http://a.local/", name="a"),
            Check.http("http://b.local/", name="b"),
        ]
    )
    names = {r.name for r in results}
    assert names == {"a", "b"}
    assert all(r.ok for r in results)


@pytest.mark.asyncio
async def test_wait_for_ready_succeeds_after_flaky_dep():
    state = {"calls": 0}

    async def flaky() -> "Check.CheckResult":  # type: ignore[name-defined]
        from oscar_health.checks import CheckResult

        state["calls"] += 1
        return CheckResult(
            name="x",
            ok=state["calls"] >= 3,
            latency_ms=1,
            detail=None if state["calls"] >= 3 else "warming up",
        )

    fake_check = Check(name="x", probe=flaky)
    results = await wait_for_ready(
        checks=[fake_check], timeout_s=20, initial_interval_s=0.01, max_interval_s=0.01
    )
    assert all(r.ok for r in results)
    assert state["calls"] >= 3


@pytest.mark.asyncio
async def test_wait_for_ready_timeout():
    from oscar_health.checks import CheckResult

    async def never() -> CheckResult:
        return CheckResult(name="never", ok=False, latency_ms=1, detail="nope")

    with pytest.raises(ReadinessTimeout):
        await wait_for_ready(
            checks=[Check(name="never", probe=never)],
            timeout_s=0.2,
            initial_interval_s=0.05,
            max_interval_s=0.05,
        )
