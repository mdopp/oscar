"""One predicate for "solaris.db cannot be served right now" (#1272, #1273).

Two surfaces answer for the database's state — `/health`, which ServiceBay's
tile hangs on, and the `/napi/` device-token gate the native clients hit. During
the 2026-08-30 outage (#1271) they said different things about the same state:
the tile stayed green for over a day while every `/napi/*` request answered 500.
A service that reports two different things about one state is worse than one
that is consistently wrong, so both surfaces classify a failure through here.

What counts as **infrastructure** — the file cannot be opened or read at all:
an `OSError` from the path itself (the `PermissionError` an SELinux relabel
caused in #1271 is one), or SQLite reporting that it cannot open / cannot read
the file. What deliberately does NOT count: a missing table (`no such table`) —
the schema-init sidecar may not have migrated yet and the stores degrade to
empty there by design — and every other exception. The match is on the sqlite
message rather than the class because `sqlite3.OperationalError` covers both a
missing table and an unopenable file; catching the class wholesale would hide
real programming errors behind a 503.
"""

from __future__ import annotations

import sqlite3

_SQLITE_UNAVAILABLE = (
    "unable to open database file",
    "file is not a database",
    "database disk image is malformed",
    "disk i/o error",
)


class Unavailable(RuntimeError):
    """solaris.db is unreadable — an infrastructure fault, not a client error."""


def unavailable_reason(exc: BaseException) -> str | None:
    """A one-line reason when `exc` means the database is unreadable, else None."""
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {exc}"
    if isinstance(exc, sqlite3.Error):
        text = str(exc).lower()
        if any(marker in text for marker in _SQLITE_UNAVAILABLE):
            return f"{type(exc).__name__}: {exc}"
    return None


def probe(db_path: str) -> str | None:
    """None when solaris.db can be read, else a one-line reason (#1273).

    Opens the file read-only and runs `SELECT 1`: cheap enough for the 30s
    healthcheck interval, and it covers permissions, SELinux labels, a missing
    file and corruption in one shot. `mode=ro` matters — a plain
    `sqlite3.connect` would CREATE a missing database and report health.

    Deliberately checks only this service's own database. Ollama, Home Assistant
    and Radicale stay out of it: a probe that folds in neighbours goes red
    whenever one of them coughs, which makes the tile worthless again, only in
    the other direction.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return unavailable_reason(exc) or f"{type(exc).__name__}: {exc}"
    return None
