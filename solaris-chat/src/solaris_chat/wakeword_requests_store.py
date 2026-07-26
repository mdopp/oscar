"""Storage for wakeword improvement sessions (#1056).

Tracks active user wakeword recording requests (target count, collected count, status)
in solaris.db for interactive voice enrollment dialogs.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


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
        conn.execute("""
            INSERT INTO wakeword_requests (uid, target_count, collected_count, status, created_at, updated_at)
            VALUES (?, ?, 0, 'active', datetime('now'), datetime('now'))
            ON CONFLICT(uid) DO UPDATE SET
                target_count = excluded.target_count,
                collected_count = 0,
                status = 'active',
                updated_at = datetime('now')
        """, (uid, target_count))
        conn.commit()

    return get_request(db_path, uid) or {}


def get_request(db_path: str, uid: str) -> dict | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT uid, target_count, collected_count, status, created_at, updated_at FROM wakeword_requests WHERE uid = ?",
            (uid,)
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
        conn.execute("""
            UPDATE wakeword_requests
            SET collected_count = ?, status = ?, updated_at = datetime('now')
            WHERE uid = ?
        """, (new_count, new_status, uid))
        conn.commit()

    return get_request(db_path, uid) or {}


def finish_request(db_path: str, uid: str) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE wakeword_requests SET status = 'finished', updated_at = datetime('now') WHERE uid = ?",
            (uid,)
        )
        conn.commit()


def has_active_request(db_path: str, uid: str) -> bool:
    req = get_request(db_path, uid)
    if not req:
        return False
    return req.get("status") == "active"


def has_any_active_request(db_path: str) -> bool:
    """True if ANY uid has an active wakeword recording session."""
    if not Path(db_path).exists():
        return False
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM wakeword_requests WHERE status = 'active' LIMIT 1"
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
        conn.execute("""
            UPDATE wakeword_requests
            SET collected_count = ?, status = 'active', updated_at = datetime('now')
            WHERE uid = ?
        """, (new_count, uid))
        conn.commit()

    return get_request(db_path, uid) or {}
