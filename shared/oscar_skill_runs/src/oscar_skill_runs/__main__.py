"""CLI: `python -m oscar_skill_runs append|detect`.

POSTGRES_DSN env is required; otherwise the same shape as oscar_audit's CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from oscar_logging import log

from .core import append_run, detect_correction


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN", "")
    if not dsn:
        log.error("oscar_skill_runs.no_dsn")
        sys.stderr.write("POSTGRES_DSN not set\n")
        sys.exit(2)
    return dsn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oscar_skill_runs")
    sub = parser.add_subparsers(dest="action", required=True)

    p_app = sub.add_parser("append", help="Log a finished skill run.")
    p_app.add_argument("--trace-id", required=True)
    p_app.add_argument("--uid", required=True)
    p_app.add_argument("--endpoint", required=True)
    p_app.add_argument("--skill-name", required=True)
    p_app.add_argument("--utterance", required=True)
    p_app.add_argument("--response", default=None)
    p_app.add_argument("--outcome", default="ok", choices=["ok", "error", "no_skill"])

    p_det = sub.add_parser(
        "detect", help="Check if utterance is a correction of a recent run."
    )
    p_det.add_argument("--uid", required=True)
    p_det.add_argument("--endpoint", required=True)
    p_det.add_argument("--utterance", required=True)
    p_det.add_argument("--window-s", type=int, default=30)

    args = parser.parse_args(argv)
    dsn = _dsn()

    if args.action == "append":
        run_id = asyncio.run(
            append_run(
                dsn,
                trace_id=args.trace_id,
                uid=args.uid,
                endpoint=args.endpoint,
                skill_name=args.skill_name,
                utterance=args.utterance,
                response=args.response,
                outcome=args.outcome,
            )
        )
        sys.stdout.write(run_id + "\n")
        return 0

    if args.action == "detect":
        corr_id = asyncio.run(
            detect_correction(
                dsn,
                uid=args.uid,
                endpoint=args.endpoint,
                utterance=args.utterance,
                window_s=args.window_s,
            )
        )
        if corr_id is None:
            return 1
        sys.stdout.write(corr_id + "\n")
        return 0

    parser.error(f"unknown action {args.action!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
