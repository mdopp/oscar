"""SQLite access to pending resident-registration requests.

Reads/writes the `pending_residents` table (migration 0013) in solaris.db. The
onboarding registration flow (#376) writes a row here after the gatekeeper has
enrolled a guest's voice; the admin-approval step (#355, separate) reads the
pending rows and flips their status. A pending row is **not** an account — it is
only the local record of a request awaiting approval.

Sync sqlite3, like `topics_store` / `mentions_store`: each op is millisecond-
cheap. If solaris.db or the table is missing (the schema-init sidecar hasn't
migrated yet), a read degrades to empty and a write raises so the registration
tool can surface the failure rather than silently dropping the request.

Not per-resident scoped: a candidate has no resident uid of their own yet (only
the one they're asking for), so a pending request belongs to the household. The
biometric audio never reaches this table — only the candidate uid/name and
whether enrolment succeeded.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_residents (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            enrolled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    return conn


def add_pending_resident(
    db_path: str, uid: str, display_name: str, enrolled: bool
) -> int:
    """Record a registration request awaiting admin approval; return its row id."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO pending_residents (uid, display_name, status, enrolled)
            VALUES (?, ?, ?, ?)
            """,
            (uid, display_name, STATUS_PENDING, 1 if enrolled else 0),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_pending_by_uid(db_path: str, uid: str) -> dict[str, Any] | None:
    """The newest still-pending request for a uid, or None. The #355 approval
    flow keys off the uid (the candidate's chosen login), not the row id."""
    if not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT id, uid, display_name, status, enrolled, request_id,
                       email, requested_at
                  FROM pending_residents
                 WHERE uid = ? AND status = ?
                 ORDER BY requested_at DESC, id DESC
                 LIMIT 1
                """,
                (uid, STATUS_PENDING),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def set_request_id(db_path: str, row_id: int, request_id: str) -> None:
    """Record the ServiceBay access-request id returned by file_access_request,
    so a later approval poll can find it."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE pending_residents SET request_id = ? WHERE id = ?",
            (request_id, row_id),
        )
        conn.commit()


def mark_approved(db_path: str, row_id: int) -> None:
    """Flip a request to approved once the admin has resolved it in SB's list.
    Solaris never sets this on its own — only after an SB-side approval."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE pending_residents SET status = ? WHERE id = ?",
            (STATUS_APPROVED, row_id),
        )
        conn.commit()


def mark_denied(db_path: str, row_id: int) -> None:
    """Flip a request to denied once the admin has dismissed it in SB's list,
    or when SB no longer knows the request (resolved-gone). The candidate's
    captured biometrics are dropped separately; this only closes the local row
    so it stops surfacing as pending. Idempotent on a missing/absent row."""
    if not Path(db_path).exists():
        return
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE pending_residents SET status = ? WHERE id = ?",
                (STATUS_DENIED, row_id),
            )
            conn.commit()
    except sqlite3.OperationalError:
        return


def list_pending_residents(db_path: str) -> list[dict[str, Any]]:
    """The open registration requests, newest first (the #355 approval surface).

    Empty when the DB/table is missing.
    """
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, uid, display_name, status, enrolled, requested_at
                  FROM pending_residents
                 WHERE status = ?
                 ORDER BY requested_at DESC, id DESC
                """,
                (STATUS_PENDING,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]
