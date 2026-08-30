"""SQLite access to the resident device-auth tokens.

Reads/writes the `device_tokens` table (migration 0021) in solaris.db — the
operational state behind native-client bearer auth (#717). Same shape as
`push_store`/`favorites_store`: sync sqlite3 (each op is millisecond-cheap),
WAL + busy_timeout, and reads degrade to empty when the DB or table is missing
(the schema-init sidecar hasn't migrated yet), so the engine is safe to deploy
before the migration lands.

A native Android widget/tile can't ride the browser's Authelia cookies, so it
authenticates with a long-lived bearer minted from an authenticated interactive
session. Security invariants held here:

- The plaintext token is NEVER stored. Only its `sha256` hex digest lives in the
  DB; the plaintext (`sol_device_<token_urlsafe(32)>`) is returned to the caller
  exactly once, at creation.
- Lookup is by the stored hash and uses a constant-time compare, so a timing
  side channel can't probe the hash space.
- Scoping is per-resident: every row carries `owner_uid`; a resolved token
  authenticates as its owner and a revoke is owner-checked.
- Fail closed: a revoked or unknown token resolves to None, never a fallback uid.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Any

TOKEN_PREFIX = "sol_device_"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create(db_path: str, owner_uid: str, label: str = "") -> tuple[str, str]:
    """Mint a device token for a resident; returns (id, plaintext_token).

    The plaintext is generated here, its hash stored, and the plaintext returned
    to the caller ONCE — it is never persisted and can't be recovered later."""
    token = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    token_id = uuid.uuid4().hex
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO device_tokens (id, owner_uid, token_hash, label)
            VALUES (?, ?, ?, ?)
            """,
            (token_id, owner_uid, _hash(token), label or None),
        )
        conn.commit()
    return token_id, token


def resolve(db_path: str, token: str) -> str | None:
    """Map a plaintext token to its owner_uid, or None (fail-closed).

    Hash-lookup with a constant-time compare; ignores revoked rows and stamps
    `last_used`. Returns None for an unknown/revoked/malformed token — never a
    fallback uid — so an invalid token can't be treated as authenticated."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    if not Path(db_path).exists():
        return None
    token_hash = _hash(token)
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, owner_uid, token_hash FROM device_tokens "
                "WHERE token_hash = ? AND revoked = 0",
                (token_hash,),
            ).fetchone()
            if row is None or not hmac.compare_digest(row["token_hash"], token_hash):
                return None
            conn.execute(
                "UPDATE device_tokens SET last_used = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
            return row["owner_uid"]
    except sqlite3.OperationalError:
        return None


def resolve_device(db_path: str, token: str) -> tuple[str, str] | None:
    """Map a plaintext token to its (device_id, owner_uid), or None (fail-closed).

    Like `resolve` but also returns the token row's id so a per-device store (the
    native watch-set, #810) can key on the individual device rather than only its
    owner — one resident may pair several devices, each with its own widgets."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    if not Path(db_path).exists():
        return None
    token_hash = _hash(token)
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, owner_uid, token_hash FROM device_tokens "
                "WHERE token_hash = ? AND revoked = 0",
                (token_hash,),
            ).fetchone()
            if row is None or not hmac.compare_digest(row["token_hash"], token_hash):
                return None
            conn.execute(
                "UPDATE device_tokens SET last_used = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
            return row["id"], row["owner_uid"]
    except sqlite3.OperationalError:
        return None


def list_for_uid(db_path: str, owner_uid: str) -> list[dict[str, Any]]:
    """The resident's tokens — metadata only, NEVER the hash or plaintext.

    Empty when the DB/table is missing."""
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, owner_uid, label, created, last_used, revoked "
                "FROM device_tokens WHERE owner_uid = ? AND revoked = 0 "
                "ORDER BY created DESC",
                (owner_uid,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def list_owner_uids(db_path: str) -> set[str]:
    """Every resident with a live paired device — uids only, no token metadata.

    The household-wide read the owner-scoped `list_for_uid` cannot serve: HA
    notice targeting (#1276) needs the *set* of addressable residents before it
    knows which one it is addressing. Revoked rows are excluded, so unpairing a
    device removes its owner once nothing else is paired.
    """
    if not Path(db_path).exists():
        return set()
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT owner_uid FROM device_tokens WHERE revoked = 0"
            ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(r["owner_uid"]) for r in rows if r["owner_uid"]}


def revoke(db_path: str, owner_uid: str, token_id: str) -> bool:
    """Revoke one token (owner-checked); True when a row was revoked.

    Only the caller's own token is revoked, so a resident can't revoke another's
    device by guessing its id."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE device_tokens SET revoked = 1 "
            "WHERE owner_uid = ? AND id = ? AND revoked = 0",
            (owner_uid, token_id),
        )
        conn.commit()
        return cur.rowcount > 0
