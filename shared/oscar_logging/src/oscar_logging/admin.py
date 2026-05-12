"""`python -m oscar_logging.admin debug-set …` — write system_settings.debug_mode.

Used by the debug-set HERMES skill so the agent can flip debug logging
on or off cluster-wide without restarting any container. Containers
that opted into `oscar_logging.runtime.watch_debug_mode` pick up the
change within ~5 seconds; the rest stay on their env-var setting.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        sys.stderr.write("POSTGRES_DSN not set\n")
        sys.exit(2)
    return dsn


async def _set_debug_mode(
    *, active: bool, ttl_hours: float | None, latency_annotations: bool
) -> dict:
    verbose_until = None
    if ttl_hours and active:
        verbose_until = (
            datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        ).isoformat()
    payload = {
        "active": active,
        "verbose_until": verbose_until,
        "latency_annotations": latency_annotations,
    }
    conn = await asyncpg.connect(dsn=_dsn(), timeout=5)
    try:
        await conn.execute(
            """
            INSERT INTO system_settings (key, value, updated_at)
            VALUES ('debug_mode', $1::jsonb, now())
            ON CONFLICT (key) DO UPDATE
            SET value = excluded.value, updated_at = now()
            """,
            json.dumps(payload),
        )
    finally:
        await conn.close()
    return payload


async def _show_debug_mode() -> dict | None:
    conn = await asyncpg.connect(dsn=_dsn(), timeout=5)
    try:
        row = await conn.fetchrow(
            "SELECT value, updated_at FROM system_settings WHERE key = 'debug_mode'"
        )
    finally:
        await conn.close()
    if row is None:
        return None
    return {"value": row["value"], "updated_at": row["updated_at"]}


def _print(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(prog="oscar-logging-admin")
    sub = parser.add_subparsers(dest="action", required=True)

    s = sub.add_parser("debug-set", help="Set system_settings.debug_mode.")
    s.add_argument(
        "--active",
        required=True,
        choices=("true", "false"),
        help="Whether debug logging is on cluster-wide.",
    )
    s.add_argument(
        "--ttl-hours",
        type=float,
        help="If --active=true, auto-turn-off after this many hours. Omit for unbounded.",
    )
    s.add_argument(
        "--latency-annotations",
        action="store_true",
        help="Enable path/latency annotations on Voice-PE responses (admin-only).",
    )

    sub.add_parser("debug-show", help="Print current system_settings.debug_mode.")

    args = parser.parse_args()

    if args.action == "debug-set":
        payload = asyncio.run(
            _set_debug_mode(
                active=(args.active == "true"),
                ttl_hours=args.ttl_hours,
                latency_annotations=args.latency_annotations,
            )
        )
        _print({"ok": True, **payload})
        return

    if args.action == "debug-show":
        payload = asyncio.run(_show_debug_mode())
        if payload is None:
            _print({"ok": True, "set": False})
        else:
            _print({"ok": True, "set": True, **payload})
        return


if __name__ == "__main__":
    main()
