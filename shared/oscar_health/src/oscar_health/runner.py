"""wait_for_ready (loop) + check_all (one-shot)."""

from __future__ import annotations

import asyncio
import time
from typing import Iterable

from oscar_logging import log

from .checks import Check, CheckResult


class ReadinessTimeout(Exception):
    """Raised when wait_for_ready can't get all deps green before timeout_s."""


async def check_all(checks: Iterable[Check]) -> list[CheckResult]:
    """Run every check once in parallel, return the unsorted list of results."""
    return list(await asyncio.gather(*(c.probe() for c in checks)))


async def wait_for_ready(
    *,
    checks: Iterable[Check],
    timeout_s: float = 120.0,
    initial_interval_s: float = 1.0,
    max_interval_s: float = 8.0,
) -> list[CheckResult]:
    """Block until every check returns ok=True or `timeout_s` elapses.

    Exponential backoff between rounds (interval doubles, capped at
    `max_interval_s`). On every round, logs the still-failing names so
    boot logs say what we're waiting for.
    """
    checks = list(checks)
    if not checks:
        return []

    started = time.monotonic()
    interval = initial_interval_s
    round_num = 0

    while True:
        round_num += 1
        results = await check_all(checks)
        failing = [r for r in results if not r.ok]
        if not failing:
            log.info(
                "oscar_health.ready",
                round=round_num,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                checks=[r.name for r in results],
            )
            return results

        log.info(
            "oscar_health.waiting",
            round=round_num,
            elapsed_s=round(time.monotonic() - started, 1),
            failing=[{"name": r.name, "detail": r.detail} for r in failing],
        )

        if (time.monotonic() - started) >= timeout_s:
            log.error(
                "oscar_health.timeout",
                elapsed_s=round(time.monotonic() - started, 1),
                failing=[r.name for r in failing],
            )
            raise ReadinessTimeout(
                f"timed out after {timeout_s}s; still failing: {[r.name for r in failing]}"
            )

        await asyncio.sleep(interval)
        interval = min(interval * 2, max_interval_s)
