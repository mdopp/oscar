"""CLI for the reviewer cron skill.

Designed to be called from `skills/skill-reviewer/SKILL.md` prose so HERMES
does the LLM-drafting bit and shells out for aggregation, apply, notify.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

from oscar_logging import log

from .core import (
    K_THRESHOLD,
    aggregate_corrections,
    can_apply_now,
    mark_corrections_dismissed,
    mark_group_edited,
)
from .notify import notify_admin_via_signal


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN", "")
    if not dsn:
        sys.stderr.write("POSTGRES_DSN not set\n")
        sys.exit(2)
    return dsn


def _read_file_or_arg(value: str) -> str:
    if value.startswith("@"):
        return pathlib.Path(value[1:]).read_text(encoding="utf-8")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oscar_skill_reviewer")
    sub = parser.add_subparsers(dest="action", required=True)

    p_agg = sub.add_parser("aggregate", help="List groups with count >= k.")
    p_agg.add_argument("--window-days", type=int, default=14)
    p_agg.add_argument("--k", type=int, default=K_THRESHOLD)

    p_can = sub.add_parser(
        "can-apply", help="Check rate-limit + user-interference for a skill."
    )
    p_can.add_argument("--skill-name", required=True)

    p_mark = sub.add_parser("mark-edited", help="Mark correction_ids as edited.")
    p_mark.add_argument("--correction-ids", required=True, help="comma-separated UUIDs")

    p_dis = sub.add_parser(
        "mark-dismissed", help="Mark correction_ids dismissed (post-rejection)."
    )
    p_dis.add_argument("--correction-ids", required=True)

    p_not = sub.add_parser("notify-admin", help="Send Signal DM about an applied edit.")
    p_not.add_argument("--admin-number", required=True)
    p_not.add_argument("--skill-name", required=True)
    p_not.add_argument("--diff", required=True, help="diff text or @file")
    p_not.add_argument("--reason", required=True)
    p_not.add_argument(
        "--signal-url", default=os.environ.get("SIGNAL_URL", "http://127.0.0.1:8090")
    )
    p_not.add_argument("--signal-token", default=os.environ.get("SIGNAL_TOKEN", ""))

    args = parser.parse_args(argv)
    dsn = _dsn()

    if args.action == "aggregate":
        groups = asyncio.run(
            aggregate_corrections(dsn=dsn, window_days=args.window_days, k=args.k)
        )
        json.dump(
            [
                {
                    "skill_name": g.skill_name,
                    "count": g.count,
                    "sample_utterance": g.sample_utterance,
                    "sample_correction": g.sample_correction,
                    "correction_ids": list(g.correction_ids),
                }
                for g in groups
            ],
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    if args.action == "can-apply":
        ok = asyncio.run(can_apply_now(dsn=dsn, skill_name=args.skill_name))
        sys.stdout.write("yes\n" if ok else "no\n")
        return 0 if ok else 1

    if args.action == "mark-edited":
        ids = [s for s in args.correction_ids.split(",") if s]

        class _G:
            skill_name = ""
            correction_ids = tuple(ids)

        asyncio.run(mark_group_edited(dsn=dsn, group=_G()))  # type: ignore[arg-type]
        return 0

    if args.action == "mark-dismissed":
        ids = [s for s in args.correction_ids.split(",") if s]
        asyncio.run(mark_corrections_dismissed(dsn=dsn, correction_ids=ids))
        return 0

    if args.action == "notify-admin":
        diff = _read_file_or_arg(args.diff)
        try:
            asyncio.run(
                notify_admin_via_signal(
                    signal_url=args.signal_url,
                    signal_token=args.signal_token,
                    admin_number=args.admin_number,
                    skill_name=args.skill_name,
                    diff=diff,
                    reason=args.reason,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.error("skill_reviewer.notify.crash", error=str(exc))
            sys.stderr.write(f"{exc}\n")
            return 3
        return 0

    parser.error(f"unknown action {args.action!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
