#!/usr/bin/env python3
"""
post-deploy hook for the `paperless` template.

One responsibility: **converge the postgres `paperless` role onto the deployed
password** (#1297).

`PAPERLESS_DB_PASSWORD` is a ServiceBay-generated `secret`, but the official
postgres entrypoint applies `POSTGRES_PASSWORD` only during `initdb` — that is,
only while `PGDATA` is empty. An existing install therefore keeps whatever the
role was stamped with at first install (for the boxes that predate this change:
the literal default that used to ship in `variables.json`), while the webserver
renders with the generated secret and hits `FATAL: password authentication
failed`. Nothing else in this repo rotates that role.

So: probe the role over TCP with the deployed secret; only if that fails, take
the container-local socket — the entrypoint's generated `pg_hba.conf` leaves
`local` connections on `trust`/`peer` — and `ALTER ROLE ... PASSWORD`, then
restart the webserver so it reconnects. A converged install re-probes green and
does nothing, so re-deploys are no-ops.

The secret never reaches argv: the probe passes it as a container env var and
the ALTER goes to psql on stdin, so it is out of the host process list; it is
never logged either.

See lib/registry.ts:getTemplatePostDeployScript for the script protocol.
ServiceBay Mustache-renders this file before executing it and does NOT export
the template variables to it, so all config is read from the running containers
(see templates/tests/test_post_deploy_mustache.py).
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time

POSTGRES_CONTAINER = os.environ.get(
    "PAPERLESS_POSTGRES_CONTAINER", "paperless-postgres"
)
WEBSERVER_CONTAINER = os.environ.get(
    "PAPERLESS_WEBSERVER_CONTAINER", "paperless-webserver"
)
DB_NAME = "paperless"
DB_USER = "paperless"
READY_TIMEOUT_SEC = 180


def env(key: str, default: str = "") -> str:
    val = os.environ.get(key, default)
    return val if val else default


def jlog(level: str, tag: str, message: str, **args: object) -> None:
    """Emit a TEMPLATE_LOGGING.md-shaped line on stdout."""
    sys.stdout.write(
        json.dumps(
            {
                "ts": datetime.datetime.now().astimezone().isoformat(),
                "level": level,
                "tag": tag,
                "message": message,
                "args": args,
            }
        )
        + "\n"
    )
    sys.stdout.flush()


# ── pure decision logic (unit-tested in templates/tests) ─────────────────────


def needs_rotation(desired: str, auth_ok: bool) -> bool:
    """True iff the role must be re-stamped with `desired`.

    An empty `desired` means the deployed password could not be read — rotating
    to it would lock the webserver out of its own database, so it is never a
    reason to write. `auth_ok` is the result of connecting as the role with
    `desired`: green means the install is already converged.
    """
    if not desired:
        return False
    return not auth_ok


def build_alter_sql(password: str) -> str:
    """`ALTER ROLE paperless PASSWORD '<password>'` with the password as a
    standard SQL string literal.

    Postgres runs with `standard_conforming_strings = on`, so a backslash is a
    plain character inside a literal and the single quote is the only character
    that needs escaping — by doubling it.
    """
    if not password:
        raise ValueError("refusing to build an ALTER ROLE with an empty password")
    literal = password.replace("'", "''")
    return f"ALTER ROLE {DB_USER} PASSWORD '{literal}';"


# ── the box side ─────────────────────────────────────────────────────────────


def container_env(container: str, name: str) -> str:
    """Read a rendered env var out of a running pod container, '' if unreadable."""
    try:
        proc = subprocess.run(
            ["podman", "exec", container, "printenv", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def wait_for_postgres(deadline_sec: int) -> bool:
    """Poll `pg_isready` inside the postgres container until it accepts."""
    started = time.time()
    last_beat = 0.0
    while time.time() - started < deadline_sec:
        try:
            proc = subprocess.run(
                [
                    "podman",
                    "exec",
                    POSTGRES_CONTAINER,
                    "pg_isready",
                    "-U",
                    DB_USER,
                    "-d",
                    DB_NAME,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        elapsed = time.time() - started
        if elapsed - last_beat >= 15:
            jlog(
                "info", "paperless:db", "waiting for postgres", elapsed_sec=int(elapsed)
            )
            last_beat = elapsed
        time.sleep(3)
    return False


def role_accepts(password: str, port: str) -> bool:
    """True iff the `paperless` role authenticates over TCP with `password`.

    `-h 127.0.0.1` is what makes this a real test: a socket connection would be
    trusted by pg_hba and pass whatever the role holds. The password is handed
    over as a container env var, so it stays out of the host process list.
    """
    try:
        proc = subprocess.run(
            [
                "podman",
                "exec",
                "-e",
                f"PGPASSWORD={password}",
                POSTGRES_CONTAINER,
                "psql",
                "-h",
                "127.0.0.1",
                "-p",
                port,
                "-U",
                DB_USER,
                "-d",
                DB_NAME,
                "-tAc",
                "SELECT 1",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as e:
        jlog("warn", "paperless:db", "role probe did not run", error=str(e))
        return False
    return proc.returncode == 0


def rotate_role(password: str) -> bool:
    """`ALTER ROLE paperless PASSWORD` over the container-local socket."""
    try:
        proc = subprocess.run(
            [
                "podman",
                "exec",
                "-i",
                POSTGRES_CONTAINER,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                DB_USER,
                "-d",
                DB_NAME,
            ],
            input=build_alter_sql(password),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        jlog("warn", "paperless:db", "ALTER ROLE did not run", error=str(e))
        return False
    if proc.returncode != 0:
        jlog(
            "warn",
            "paperless:db",
            "ALTER ROLE failed",
            stderr=proc.stderr.strip()[:400],
        )
        return False
    return True


def restart_webserver() -> bool:
    try:
        proc = subprocess.run(
            ["podman", "restart", WEBSERVER_CONTAINER],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        jlog("warn", "paperless:db", "webserver restart did not run", error=str(e))
        return False
    if proc.returncode != 0:
        jlog(
            "warn",
            "paperless:db",
            "webserver restart failed",
            stderr=proc.stderr.strip()[:400],
        )
        return False
    return True


def main() -> int:
    if not wait_for_postgres(READY_TIMEOUT_SEC):
        jlog(
            "warn",
            "paperless:db",
            "postgres did not become ready; skipping the role check. Re-run the deploy once the pod is up.",
            container=POSTGRES_CONTAINER,
        )
        return 0

    port = container_env(POSTGRES_CONTAINER, "PGPORT") or env(
        "PAPERLESS_DB_PORT", "5442"
    )
    desired = container_env(POSTGRES_CONTAINER, "POSTGRES_PASSWORD")
    if not desired:
        jlog(
            "warn",
            "paperless:db",
            "could not read the deployed database password from the postgres container; leaving the role untouched.",
            container=POSTGRES_CONTAINER,
        )
        return 0

    if not needs_rotation(desired, role_accepts(desired, port)):
        jlog(
            "info",
            "paperless:db",
            "database role already accepts the deployed password — no-op",
        )
        print("✅ Paperless: the postgres role matches the deployed password.")
        return 0

    jlog(
        "info",
        "paperless:db",
        "database role rejects the deployed password (postgres applies POSTGRES_PASSWORD only at initdb) — rotating it",
    )
    if not rotate_role(desired):
        print(
            "⚠️  Paperless: could not rotate the postgres role to the deployed password — "
            "the webserver will not reach its database. Check the install log."
        )
        return 0

    if not role_accepts(desired, port):
        jlog(
            "warn",
            "paperless:db",
            "role still rejects the deployed password after ALTER ROLE",
        )
        print(
            "⚠️  Paperless: the database role did not take the new password. Check the install log."
        )
        return 0

    restart_webserver()
    jlog("info", "paperless:db", "database role rotated; webserver restarted")
    print(
        "✅ Paperless: rotated the postgres role onto the deployed password and restarted the webserver."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
