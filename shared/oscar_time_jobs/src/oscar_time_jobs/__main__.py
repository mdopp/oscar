"""CLI entry — `python -m oscar_time_jobs <action> [args]`.

Skills invoke this from HERMES. POSTGRES_DSN env var supplies the
connection; same DSN as HERMES uses for its own Postgres reads.

Exit code 0 = success; non-zero = failure with a one-line stderr.
Stdout is always a JSON object so the calling agent can parse it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any

import asyncpg

from . import core


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        sys.stderr.write("POSTGRES_DSN not set\n")
        sys.exit(2)
    return dsn


def _print(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


async def _add(args: argparse.Namespace) -> int:
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        at = datetime.fromisoformat(args.at) if args.at else None
        result = await core.add(
            pool,
            kind=args.kind,
            owner_uid=args.uid,
            target_endpoint=args.endpoint,
            duration=args.duration,
            at=at,
            rrule=args.rrule,
            label=args.label,
        )
    finally:
        await pool.close()
    _print(
        {
            "ok": True,
            "job_id": result.job_id,
            "fires_at": result.fires_at.isoformat(),
            "kind": result.kind,
            "label": result.label,
            "target_endpoint": result.target_endpoint,
        }
    )
    return 0


async def _cancel(args: argparse.Namespace) -> int:
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        count = await core.cancel(
            pool, owner_uid=args.uid, job_id=args.job_id, label=args.label
        )
    finally:
        await pool.close()
    _print({"ok": True, "cancelled": count})
    return 0 if count else 4


async def _list(args: argparse.Namespace) -> int:
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        rows = await core.list_for(pool, owner_uid=args.uid, kind=args.kind)
    finally:
        await pool.close()
    _print({"ok": True, "jobs": rows})
    return 0


async def _fire(args: argparse.Namespace) -> int:
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        payload = await core.fire(pool, job_id=args.job_id)
    finally:
        await pool.close()
    _print(payload)
    return 0 if payload.get("ok") else 5


def main() -> None:
    parser = argparse.ArgumentParser(prog="oscar-time-jobs")
    sub = parser.add_subparsers(dest="action", required=True)

    p_add = sub.add_parser("add", help="Create an armed timer or alarm.")
    p_add.add_argument("--kind", choices=("timer", "alarm"), required=True)
    p_add.add_argument("--uid", required=True, help="LLDAP uid of the owner.")
    p_add.add_argument(
        "--endpoint", required=True, help="Routing endpoint, e.g. voice-pe:office."
    )
    p_add.add_argument(
        "--duration", help="ISO-8601 duration (PT5M, P1D, …) for timers."
    )
    p_add.add_argument("--at", help="ISO-8601 datetime for one-shot alarms.")
    p_add.add_argument("--rrule", help="RFC-5545 RRULE for recurring alarms.")
    p_add.add_argument("--label", help="Optional short label, e.g. 'Pizza'.")

    p_cancel = sub.add_parser("cancel", help="Cancel a job by id or label.")
    p_cancel.add_argument("--uid", required=True)
    p_cancel.add_argument("--job-id")
    p_cancel.add_argument("--label")

    p_list = sub.add_parser("list", help="List active jobs for a user.")
    p_list.add_argument("--uid", required=True)
    p_list.add_argument("--kind", choices=("timer", "alarm"))

    p_fire = sub.add_parser("fire", help="Fire a job (called by HERMES cron).")
    p_fire.add_argument("--job-id", required=True)

    args = parser.parse_args()
    runner = {"add": _add, "cancel": _cancel, "list": _list, "fire": _fire}[args.action]
    sys.exit(asyncio.run(runner(args)))


if __name__ == "__main__":
    main()
