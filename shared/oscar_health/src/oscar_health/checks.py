"""Probe definitions — Postgres, HTTP, TCP."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import asyncpg
import httpx


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    latency_ms: int
    detail: str | None = None  # short human-readable note on failure


@dataclass(frozen=True)
class Check:
    name: str
    probe: Callable[[], Awaitable[CheckResult]]

    @classmethod
    def postgres(
        cls, *, dsn: str, name: str = "postgres", timeout_s: float = 3.0
    ) -> "Check":
        async def _probe() -> CheckResult:
            started = time.monotonic()
            try:
                conn = await asyncpg.connect(dsn=dsn, timeout=timeout_s)
                try:
                    await conn.execute("SELECT 1")
                finally:
                    await conn.close()
                return CheckResult(name=name, ok=True, latency_ms=_ms(started))
            except Exception as exc:  # noqa: BLE001
                return CheckResult(
                    name=name, ok=False, latency_ms=_ms(started), detail=str(exc)[:200]
                )

        return cls(name=name, probe=_probe)

    @classmethod
    def http(
        cls,
        url: str,
        *,
        name: str | None = None,
        expected_status_below: int = 500,
        timeout_s: float = 3.0,
    ) -> "Check":
        check_name = name or f"http:{url}"

        async def _probe() -> CheckResult:
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    response = await client.get(url)
                if response.status_code < expected_status_below:
                    return CheckResult(
                        name=check_name, ok=True, latency_ms=_ms(started)
                    )
                return CheckResult(
                    name=check_name,
                    ok=False,
                    latency_ms=_ms(started),
                    detail=f"HTTP {response.status_code}",
                )
            except Exception as exc:  # noqa: BLE001
                return CheckResult(
                    name=check_name,
                    ok=False,
                    latency_ms=_ms(started),
                    detail=str(exc)[:200],
                )

        return cls(name=check_name, probe=_probe)

    @classmethod
    def tcp(
        cls, host: str, port: int, *, name: str | None = None, timeout_s: float = 3.0
    ) -> "Check":
        check_name = name or f"tcp:{host}:{port}"

        async def _probe() -> CheckResult:
            started = time.monotonic()
            try:
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=timeout_s,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                return CheckResult(name=check_name, ok=True, latency_ms=_ms(started))
            except Exception as exc:  # noqa: BLE001
                return CheckResult(
                    name=check_name,
                    ok=False,
                    latency_ms=_ms(started),
                    detail=str(exc)[:200],
                )

        return cls(name=check_name, probe=_probe)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["Check", "CheckResult"]
