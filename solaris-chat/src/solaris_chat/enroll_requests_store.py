"""Engine side of the reverse enroll-stash for live-voice onboarding (#376).

The mirror of `voice_uid_stash.py`: there the gatekeeper writes and the engine
reads; here the engine *opens* an `enroll_requests` row for a candidate uid and
the gatekeeper — when it is HA's Wyoming STT provider — captures the speaker's
PCM across the onboarding turns and writes the enrolment result back. This module
opens the request and reads the result; the gatekeeper-side writer is
`gatekeeper/enroll_stash.py`.

The biometric audio never reaches this table — only the candidate uid, a sample
target/count, and the gatekeeper's terminal status. `created_at` bounds a TTL so
a request no gatekeeper picks up (speaker-ID off) times out honestly instead of
leaving the dialog hanging.

Sync sqlite3, like `pending_residents_store`. The table is provisioned by
migration `0014_enroll_requests`; a missing table/DB raises on open (so the tool
surfaces the failure) and reads as a non-terminal status (so the dialog can fall
through to its timeout).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# Must stay >= the gatekeeper's capture window (enroll_stash.ENROLL_TTL_SECONDS)
# so the engine doesn't declare a timeout while a capture is still in flight.
ENROLL_TTL_SECONDS = 120


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout so concurrent writers wait instead of raising
    # "database is locked" (#600).
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def open_request(db_path: str, uid: str, target_samples: int = 3) -> None:
    """Open (or reset) the enrol request for a candidate uid: status `pending`,
    zero samples collected, a fresh `created_at` so the TTL starts now. Raises if
    the table/DB is missing so the tool can report the failure rather than
    silently dropping the request."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO enroll_requests (uid, status, target_samples, collected,
                                         result, created_at)
            VALUES (?, ?, ?, 0, NULL, datetime('now'))
            ON CONFLICT(uid) DO UPDATE SET
                status         = excluded.status,
                target_samples = excluded.target_samples,
                collected      = 0,
                result         = NULL,
                created_at     = excluded.created_at
            """,
            (uid, STATUS_PENDING, target_samples),
        )
        conn.commit()


def read_request(db_path: str, uid: str) -> dict[str, Any] | None:
    """Return the request row (status/collected/target/result/timed_out) for a
    uid, or None when the row/DB is missing. `timed_out` is True when the row is
    still non-terminal but older than the TTL — the gatekeeper never picked it up
    (speaker-ID off, or the speaker walked away)."""
    if not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT status, target_samples, collected, result,
                       created_at < datetime('now', ?) AS expired
                  FROM enroll_requests
                 WHERE uid = ?
                """,
                (f"-{ENROLL_TTL_SECONDS} seconds", uid),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    status = str(row["status"])
    return {
        "status": status,
        "target_samples": int(row["target_samples"]),
        "collected": int(row["collected"]),
        "result": row["result"],
        "timed_out": status not in (STATUS_DONE, STATUS_FAILED)
        and bool(row["expired"]),
    }


def clear_request(db_path: str, uid: str) -> None:
    """Drop the request row once the engine has acted on its result — the
    enrolment artifact is the embedding in `voice_embeddings`, not this row."""
    if not Path(db_path).exists():
        return
    try:
        with _connect(db_path) as conn:
            conn.execute("DELETE FROM enroll_requests WHERE uid = ?", (uid,))
            conn.commit()
    except sqlite3.OperationalError:
        return


def has_active_request(db_path: str, uid: str) -> bool:
    req = read_request(db_path, uid)
    if req is None:
        return False
    return not req.get("timed_out") and req.get("status") in (STATUS_PENDING, STATUS_CAPTURING)
