"""Runtime debug-mode watcher.

Containers opt in by spawning `watch_debug_mode` at startup; it polls
the `system_settings.debug_mode` row in Postgres every `interval_s`
seconds and updates the in-process override that `_debug_active()`
consults. If Postgres is unreachable the override is cleared, so
`OSCAR_DEBUG_MODE` env-var fallback takes over.

The `verbose_until` field is honoured: once it lies in the past, the
watcher treats the row as `active=false` regardless of what's stored,
matching the architecture's auto-off-by-TTL contract.

Typical usage in a long-lived async service:

    import asyncio, os
    from oscar_logging import log
    from oscar_logging.runtime import watch_debug_mode

    async def main():
        asyncio.create_task(watch_debug_mode(os.environ["POSTGRES_DSN"]))
        # … rest of the service …
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

from . import set_debug_override


_DEFAULT_INTERVAL_S = 5.0


async def watch_debug_mode(
    dsn: str, *, interval_s: float = _DEFAULT_INTERVAL_S
) -> None:
    """Poll Postgres for system_settings.debug_mode and update the override.

    Errors are swallowed silently — the worst case is that the override
    is cleared and env-var fallback kicks in. We *never* want a flaky
    Postgres to prevent the rest of the service from logging.
    """
    while True:
        try:
            active = await _read_debug_mode(dsn)
            set_debug_override(active)
        except Exception:
            set_debug_override(None)
        await asyncio.sleep(interval_s)


async def _read_debug_mode(dsn: str) -> bool | None:
    """Single read. Returns True / False, or None if the row is missing."""
    conn = await asyncpg.connect(dsn=dsn, timeout=2)
    try:
        row = await conn.fetchrow(
            "SELECT value FROM system_settings WHERE key = 'debug_mode'"
        )
    finally:
        await conn.close()

    if row is None:
        return None
    cfg = _coerce_jsonb(row["value"])
    active = bool(cfg.get("active", False))

    vu = cfg.get("verbose_until")
    if vu and active:
        try:
            expiry = datetime.fromisoformat(vu.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            expiry = None
        if expiry is not None and expiry <= datetime.now(timezone.utc):
            active = False

    return active


def _coerce_jsonb(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}
