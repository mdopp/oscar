"""Facade-side reader for the transcript-keyed speaker side-channel (#350).

The gatekeeper, acting as HA's Wyoming STT provider, resolves the speaking
resident and writes `{transcript -> uid, matched}` into
`solaris.db.voice_uid_stash` (see `gatekeeper/uid_stash.py`). HA then calls the
engine facade (`conversation.solaris`) with the same transcript as the latest
user message but no uid. This module looks the row up by that transcript so the
spoken turn is attributed to the right resident.

The row carries two independent facts and this module keeps them apart (#1152):
`uid` is who the turn belongs to (routing — `guest` for an unknown speaker),
`matched` is the gatekeeper's explicit claim that speaker-ID recognised an
enrolled resident. Only `matched` may unlock the PERSONAL tool class; the mere
existence of a row, and the uid's value whatever it becomes, never can. Every
degenerate case answers "not matched": no row, no `matched` column (a DB the
0031 migration hasn't reached, or an older gatekeeper that wrote the legacy
shape), a NULL or non-1 value, a missing table.

Consume-once + short TTL: a lookup deletes the row (so a later turn with the
same utterance never re-reads a stale identity) and ignores rows older than
the TTL (so a transcript that never reached the facade — e.g. HA dropped the
turn — can't attribute a much-later identical utterance). On any miss the
caller falls back to `household`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

# A spoken turn flows STT -> conversation within a couple of seconds; 30s is
# generously above that and well below the gap to an unrelated later turn.
STASH_TTL_SECONDS = 30


@dataclass(frozen=True)
class StashedSpeaker:
    """One consumed stash row: who the turn is attributed to, and whether the
    gatekeeper claimed to have recognised that voice. `matched` defaults to
    False so any path that builds one without asserting a match is closed."""

    uid: str
    matched: bool = False


_CONSUME = """
    DELETE FROM voice_uid_stash
    WHERE transcript = ?
      AND created_at >= datetime('now', ?)
    RETURNING uid, matched
"""

# Pre-0031 shape: no `matched` column, so nothing in such a row can assert a
# recognition and the turn reads as unmatched.
_CONSUME_LEGACY = """
    DELETE FROM voice_uid_stash
    WHERE transcript = ?
      AND created_at >= datetime('now', ?)
    RETURNING uid
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout so concurrent writers wait instead of raising
    # "database is locked" (#600).
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def consume_speaker(db_path: str, transcript: str) -> StashedSpeaker | None:
    """Return the speaker the gatekeeper stashed for this transcript, or None
    on a miss/expiry. Consume-once: a fresh hit is deleted so it can't be
    re-read by a later identical utterance. Best-effort — a missing table/DB
    returns None and the caller falls back to the default uid."""
    if not transcript or not Path(db_path).exists():
        return None
    ttl = f"-{STASH_TTL_SECONDS} seconds"
    try:
        with _connect(db_path) as conn:
            # BEGIN IMMEDIATE takes the write lock before the read so two
            # concurrent turns with the same transcript can't both consume the
            # row; the DELETE ... RETURNING reads and reaps in one statement.
            # The TTL guard keeps a stale row (past TTL) from being consumed
            # while still reaping it so the table can't grow unbounded.
            conn.execute("BEGIN IMMEDIATE")
            try:
                fresh = conn.execute(_CONSUME, (transcript, ttl)).fetchone()
            except sqlite3.OperationalError:
                fresh = conn.execute(_CONSUME_LEGACY, (transcript, ttl)).fetchone()
            conn.execute(
                "DELETE FROM voice_uid_stash WHERE transcript = ?", (transcript,)
            )
            conn.commit()
    except sqlite3.OperationalError:
        return None
    if not fresh:
        return None
    matched = "matched" in fresh.keys() and fresh["matched"] == 1
    return StashedSpeaker(str(fresh["uid"]), matched=matched)
