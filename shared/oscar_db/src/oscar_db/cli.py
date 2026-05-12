"""CLI wrapper around alembic: wait-for-postgres, then `alembic upgrade head`.

Designed to run inside a container in the oscar-brain pod. Reads
POSTGRES_DSN from env. Logs progress via oscar_logging so the migrate
step shares the same JSON-on-stdout format as the rest of OSCAR.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

from oscar_logging import log


_MAX_WAIT_S = 300
_POLL_S = 2.0


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        log.error("oscar_db.no_dsn")
        sys.stderr.write("POSTGRES_DSN not set\n")
        sys.exit(2)
    return dsn


def _wait_for_postgres(dsn: str) -> None:
    """Synchronous wait so we can call alembic right after. Uses psycopg sync.

    Doesn't pull in oscar_health because that's async and the alembic CLI
    we shell out to isn't event-loop friendly. The duplication is small
    and intentional — the migrate sidecar is the one place that runs sync.
    """
    import psycopg  # local import so the rest of oscar_db doesn't pull it in

    started = time.monotonic()
    last_log = 0.0
    while True:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            elapsed = int(time.monotonic() - started)
            log.info("oscar_db.postgres_ready", elapsed_s=elapsed)
            return
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - started
            if elapsed > _MAX_WAIT_S:
                log.error(
                    "oscar_db.postgres_timeout",
                    elapsed_s=int(elapsed),
                    error=str(exc)[:200],
                )
                raise SystemExit(3)
            if elapsed - last_log > 15:
                log.info(
                    "oscar_db.waiting_postgres",
                    elapsed_s=int(elapsed),
                    reason=str(exc)[:120],
                )
                last_log = elapsed
            time.sleep(_POLL_S)


def _alembic_dir() -> pathlib.Path:
    """alembic.ini and migrations/ are sibling to this package."""
    here = pathlib.Path(__file__).resolve()
    # src/oscar_db/cli.py → project root is src/../
    return here.parent.parent.parent  # → shared/oscar_db/


def _run_alembic(*args: str) -> int:
    from alembic.config import Config
    from alembic import command

    project_dir = _alembic_dir()
    cfg = Config(str(project_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_dir / "migrations"))
    cfg.set_main_option("sqlalchemy.url", _dsn())

    action = args[0] if args else "upgrade"
    if action == "upgrade":
        target = args[1] if len(args) > 1 else "head"
        log.info("oscar_db.alembic.upgrade.start", target=target)
        command.upgrade(cfg, target)
        log.info("oscar_db.alembic.upgrade.done", target=target)
        return 0
    if action == "current":
        command.current(cfg, verbose=True)
        return 0
    if action == "history":
        command.history(cfg, verbose=True)
        return 0
    if action == "downgrade":
        target = args[1] if len(args) > 1 else "-1"
        log.warn("oscar_db.alembic.downgrade.start", target=target)
        command.downgrade(cfg, target)
        log.warn("oscar_db.alembic.downgrade.done", target=target)
        return 0
    sys.stderr.write(
        f"unknown action {action!r}; supported: upgrade | downgrade | current | history\n"
    )
    return 2


def main() -> None:
    args = sys.argv[1:] or ["upgrade"]
    dsn = _dsn()
    if args[0] in ("upgrade", "downgrade"):
        _wait_for_postgres(dsn)
    rc = _run_alembic(*args)
    if rc == 0 and args[0] == "upgrade":
        log.info("oscar_db.run_complete")
        # Keep the container alive so the pod stays healthy; the next
        # pod restart triggers a fresh migration check.
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
