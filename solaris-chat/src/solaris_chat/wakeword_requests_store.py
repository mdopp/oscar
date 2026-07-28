"""Storage for wakeword improvement sessions (#1056).

Tracks active user wakeword recording requests (target count, collected count, status)
in solaris.db for interactive voice enrollment dialogs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


# How long a dialog stays "active" without a new sample. An active row reroutes
# EVERY voice and browser turn of EVERY resident into the enrolment path and
# strips the toolbox down to the enrolment tools, so a row left behind by a
# killed process (or a resident who walked away mid-dialog) would silently take
# the whole box hostage. `updated_at` is bumped per sample, so a live dialog
# never expires under the speaker.
WAKEWORD_TTL_SECONDS = 600

# How many recordings of "Solaris" the browser recorder asks for.
WAKEWORD_TARGET_SAMPLES = 10

# The status of a set collected in the browser/app (#1081). Deliberately NOT
# `active`: `active` is the gatekeeper's capture flag and it also reroutes EVERY
# resident's turn into the enrolment path. The browser is the sole writer of its
# own samples and needs neither.
STATUS_BROWSER = "browser"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wakeword_requests (
                uid TEXT PRIMARY KEY,
                target_count INTEGER NOT NULL DEFAULT 10,
                collected_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def start_request(db_path: str, uid: str, target_count: int = 10) -> dict:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO wakeword_requests (uid, target_count, collected_count, status, created_at, updated_at)
            VALUES (?, ?, 0, 'active', datetime('now'), datetime('now'))
            ON CONFLICT(uid) DO UPDATE SET
                target_count = excluded.target_count,
                collected_count = 0,
                status = 'active',
                updated_at = datetime('now')
        """,
            (uid, target_count),
        )
        conn.commit()

    return get_request(db_path, uid) or {}


def get_request(db_path: str, uid: str) -> dict | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT uid, target_count, collected_count, status, created_at, updated_at FROM wakeword_requests WHERE uid = ?",
            (uid,),
        ).fetchone()
        if row:
            return dict(row)
        return None


def record_sample(db_path: str, uid: str) -> dict:
    init_db(db_path)
    req = get_request(db_path, uid)
    if not req or req["status"] != "active":
        req = start_request(db_path, uid, target_count=10)

    new_count = req["collected_count"] + 1
    new_status = "completed" if new_count >= req["target_count"] else "active"

    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE wakeword_requests
            SET collected_count = ?, status = ?, updated_at = datetime('now')
            WHERE uid = ?
        """,
            (new_count, new_status, uid),
        )
        conn.commit()

    return get_request(db_path, uid) or {}


def set_browser_progress(
    db_path: str,
    uid: str,
    collected: int,
    target_count: int = WAKEWORD_TARGET_SAMPLES,
) -> dict:
    """Record how many wake-word recordings the browser recorder holds (#1081).

    `collected` is ABSOLUTE — recomputed from the files on disk, never an
    increment. The resident can re-record sample 3, and a retry can replay the
    same upload; both must leave the counter where it is (#1080).
    """
    init_db(db_path)
    status = "completed" if collected >= target_count else STATUS_BROWSER
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO wakeword_requests (uid, target_count, collected_count, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(uid) DO UPDATE SET
                target_count = excluded.target_count,
                collected_count = excluded.collected_count,
                status = excluded.status,
                updated_at = datetime('now')
        """,
            (uid, target_count, collected, status),
        )
        conn.commit()

    return get_request(db_path, uid) or {}


def finish_request(db_path: str, uid: str) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE wakeword_requests SET status = 'finished', updated_at = datetime('now') WHERE uid = ?",
            (uid,),
        )
        conn.commit()


def has_active_request(db_path: str, uid: str) -> bool:
    req = get_request(db_path, uid)
    if not req:
        return False
    return req.get("status") == "active"


def has_any_active_request(db_path: str) -> bool:
    """True if ANY uid has an active wakeword recording session.

    TTL-bounded: this answer reroutes every resident's turns, so a row nobody
    finished must stop counting rather than hold the box in enrolment mode
    forever.
    """
    if not Path(db_path).exists():
        return False
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM wakeword_requests"
                " WHERE status = 'active' AND updated_at > datetime('now', ?)"
                " LIMIT 1",
                (f"-{WAKEWORD_TTL_SECONDS} seconds",),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def decrement_sample(db_path: str, uid: str) -> dict:
    init_db(db_path)
    req = get_request(db_path, uid)
    if not req:
        return {}

    new_count = max(0, req["collected_count"] - 1)
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE wakeword_requests
            SET collected_count = ?, status = 'active', updated_at = datetime('now')
            WHERE uid = ?
        """,
            (new_count, uid),
        )
        conn.commit()

    return get_request(db_path, uid) or {}
