"""CLI for oscar-health.

Three subcommands:
  wait    — block until deps ready, exit 0 on success, exit 2 on timeout
  check   — single round-trip, JSON output, always exit 0
  doctor  — auto-discover checks from env vars (used by the status skill)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from .checks import Check, CheckResult
from .runner import ReadinessTimeout, check_all, wait_for_ready


def _build_explicit_checks(args: argparse.Namespace) -> list[Check]:
    checks: list[Check] = []
    if args.postgres:
        checks.append(Check.postgres(dsn=args.postgres))
    for url in args.http or []:
        checks.append(Check.http(url))
    for hp in args.tcp or []:
        host, port = hp.rsplit(":", 1)
        checks.append(Check.tcp(host, int(port)))
    return checks


def _build_doctor_checks() -> list[Check]:
    checks: list[Check] = []
    if dsn := os.environ.get("OSCAR_POSTGRES_DSN"):
        checks.append(Check.postgres(dsn=dsn, name="postgres"))
    if url := os.environ.get("OSCAR_HERMES_URL"):
        checks.append(Check.http(url.rstrip("/") + "/health", name="hermes"))
    if url := os.environ.get("OSCAR_OLLAMA_URL"):
        checks.append(Check.http(url.rstrip("/") + "/api/tags", name="ollama"))
    for label, env in (
        ("whisper", "OSCAR_WHISPER_HOST"),
        ("piper", "OSCAR_PIPER_HOST"),
        ("openwakeword", "OSCAR_OPENWAKEWORD_HOST"),
    ):
        if value := os.environ.get(env):
            host, port = value.rsplit(":", 1)
            checks.append(Check.tcp(host, int(port), name=label))
    if urls := os.environ.get("OSCAR_CONNECTORS_URLS"):
        for url in [u.strip() for u in urls.split(",") if u.strip()]:
            checks.append(Check.http(url, name=f"connector:{url}"))
    if url := os.environ.get("OSCAR_HA_MCP_URL"):
        checks.append(Check.http(url, name="ha-mcp"))
    if url := os.environ.get("OSCAR_SERVICEBAY_MCP_URL"):
        checks.append(Check.http(url, name="servicebay-mcp"))
    return checks


def _print(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


def _result_to_dict(r: CheckResult) -> dict[str, Any]:
    out: dict[str, Any] = {"name": r.name, "ok": r.ok, "latency_ms": r.latency_ms}
    if r.detail:
        out["detail"] = r.detail
    return out


async def _wait(args: argparse.Namespace) -> int:
    checks = _build_explicit_checks(args)
    if not checks:
        sys.stderr.write(
            "nothing to wait for; pass at least one --postgres/--http/--tcp\n"
        )
        return 2
    try:
        results = await wait_for_ready(checks=checks, timeout_s=args.timeout)
    except ReadinessTimeout as exc:
        _print({"ok": False, "reason": "timeout", "error": str(exc)})
        return 2
    _print({"ok": True, "results": [_result_to_dict(r) for r in results]})
    return 0


async def _check(args: argparse.Namespace) -> int:
    checks = _build_explicit_checks(args)
    if not checks:
        sys.stderr.write(
            "nothing to check; pass at least one --postgres/--http/--tcp\n"
        )
        return 2
    results = await check_all(checks)
    _print(
        {
            "ok": all(r.ok for r in results),
            "results": [_result_to_dict(r) for r in results],
        }
    )
    return 0


async def _doctor(_args: argparse.Namespace) -> int:
    checks = _build_doctor_checks()
    if not checks:
        _print({"ok": True, "results": [], "note": "no probes configured via env vars"})
        return 0
    results = await check_all(checks)
    _print(
        {
            "ok": all(r.ok for r in results),
            "results": [_result_to_dict(r) for r in results],
        }
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="oscar-health")
    sub = parser.add_subparsers(dest="action", required=True)

    for name, helptext in [("wait", "block until ready"), ("check", "one-shot probe")]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--postgres", help="Postgres DSN — probed via SELECT 1.")
        p.add_argument(
            "--http",
            action="append",
            help="HTTP URL — probed via GET, success if status < 500. Repeatable.",
        )
        p.add_argument(
            "--tcp",
            action="append",
            help="host:port — probed via TCP open. Repeatable.",
        )
        if name == "wait":
            p.add_argument(
                "--timeout",
                type=float,
                default=120.0,
                help="Overall timeout in seconds.",
            )

    sub.add_parser(
        "doctor",
        help="auto-discover checks from OSCAR_* env vars (used by skills/status)",
    )

    args = parser.parse_args()
    runner = {"wait": _wait, "check": _check, "doctor": _doctor}[args.action]
    sys.exit(asyncio.run(runner(args)))


if __name__ == "__main__":
    main()
