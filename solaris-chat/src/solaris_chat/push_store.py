"""SQLite access to the resident Web Push subscriptions.

Reads/writes the `push_subscriptions` table (migration 0020) in solaris.db —
the operational state behind Web Push (#713). Same shape as `favorites_store`:
sync sqlite3 (each op is millisecond-cheap), WAL + busy_timeout, and reads
degrade to empty when the DB or table is missing (the schema-init sidecar
hasn't migrated yet), so the engine is safe to deploy before the migration
lands.

Scoping is per-resident: every row carries the owning `owner_uid`; a read
returns that resident's own endpoints only. The browser endpoint URL is the
natural unique key, so `upsert` re-registers an existing endpoint (its owner may
change residents on a shared device) instead of duplicating it.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def upsert(
    db_path: str,
    owner_uid: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str = "",
) -> str:
    """Register (or re-register) an endpoint for a resident; returns its id."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions
              (id, owner_uid, endpoint, p256dh, auth, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
              owner_uid = excluded.owner_uid,
              p256dh    = excluded.p256dh,
              auth      = excluded.auth,
              user_agent = excluded.user_agent
            """,
            (uuid.uuid4().hex, owner_uid, endpoint, p256dh, auth, user_agent),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        ).fetchone()
    return row["id"]


def list_for_uid(db_path: str, uid: str) -> list[dict[str, Any]]:
    """The resident's registered subscriptions. Empty when the DB/table is missing."""
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, owner_uid, endpoint, p256dh, auth, user_agent, created,"
                " last_ok FROM push_subscriptions WHERE owner_uid = ?",
                (uid,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def list_owner_uids(db_path: str) -> set[str]:
    """Every resident with a subscription. Empty when the DB/table is missing.

    The household-wide read the per-resident `list_for_uid` cannot serve: HA
    notice targeting (#1276) needs the *set* of addressable residents before it
    knows which one it is addressing.
    """
    if not Path(db_path).exists():
        return set()
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT owner_uid FROM push_subscriptions"
            ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(r["owner_uid"]) for r in rows if r["owner_uid"]}


def remove_by_endpoint(db_path: str, endpoint: str) -> int:
    """Delete a subscription by its endpoint URL; returns rows deleted."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        )
        conn.commit()
        return cur.rowcount


def mark_ok(db_path: str, endpoint: str) -> None:
    """Stamp a successful delivery so a stale endpoint is visible."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE push_subscriptions SET last_ok = datetime('now') WHERE endpoint = ?",
            (endpoint,),
        )
        conn.commit()
