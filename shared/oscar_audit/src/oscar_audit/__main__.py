"""CLI for oscar-audit. Single subcommand `query`."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import asyncpg

from . import core
from .timeparse import parse_since


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        sys.stderr.write("POSTGRES_DSN not set\n")
        sys.exit(2)
    return dsn


def _print(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


async def _query(args: argparse.Namespace) -> int:
    since = parse_since(args.since) if args.since else None
    until = parse_since(args.until) if args.until else None

    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        rows = await core.query(
            pool,
            stream=args.stream,
            since=since,
            until=until,
            uid=args.uid,
            vendor=args.vendor,
            trace_id=args.trace_id,
            gateway=args.gateway,
            kind=args.kind,
            state=args.state,
            min_cost_micro_usd=args.min_cost_micro_usd,
            limit=args.limit,
        )
    finally:
        await pool.close()

    _print(
        {
            "ok": True,
            "stream": args.stream,
            "count": len(rows),
            "rows": rows,
        }
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="oscar-audit")
    sub = parser.add_subparsers(dest="action", required=True)

    q = sub.add_parser("query", help="Query an audit stream.")
    q.add_argument("--stream", required=True, choices=core.supported_streams())
    q.add_argument(
        "--since",
        help="ISO 8601 datetime or shorthand (1h, 24h, 7d, today, yesterday).",
    )
    q.add_argument("--until", help="ISO 8601 datetime or shorthand.")
    q.add_argument("--uid", help="LLDAP uid to filter on.")
    q.add_argument(
        "--vendor", help="Vendor prefix (cloud_audit only): anthropic | google"
    )
    q.add_argument(
        "--trace-id", help="Find every row matching this trace_id (cloud_audit only)."
    )
    q.add_argument(
        "--gateway",
        help="Gateway name (gateway_identities only): signal | telegram | …",
    )
    q.add_argument("--kind", help="Job kind (time_jobs only): timer | alarm")
    q.add_argument(
        "--state",
        help="Job state (time_jobs only): armed | firing | done | cancelled | snoozed",
    )
    q.add_argument(
        "--min-cost-micro-usd",
        type=int,
        help="cloud_audit only — minimum cost in micro-USD.",
    )
    q.add_argument(
        "--limit", type=int, default=50, help="Max rows returned (1–500, default 50)."
    )

    args = parser.parse_args()
    runner = {"query": _query}[args.action]
    sys.exit(asyncio.run(runner(args)))


if __name__ == "__main__":
    main()
