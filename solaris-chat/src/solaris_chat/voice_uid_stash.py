"""Facade-side reader for the transcript-keyed speaker side-channel (#350).

The gatekeeper, acting as HA's Wyoming STT provider, resolves the speaking
resident and writes `{transcript, room -> uid, matched}` into
`solaris.db.voice_uid_stash` (see `gatekeeper/uid_stash.py`). HA then calls the
engine facade (`conversation.solaris`) with the same transcript as the latest
user message but no uid. This module looks the row up by that transcript so the
spoken turn is attributed to the right resident.

The room is the second half of the correlation key (#1218). On the satellite
path the gatekeeper resolves it and sends it as the `[room: X]` prefix the
facade parses off the utterance, so both ends compute the same string and two
rooms saying the same sentence are two independent rows. Where only one end
knows the room — the HA-STT path, where the gatekeeper's peer is HA and not the
satellite — the transcript alone still resolves a *single* candidate row. What
it must never do is pick one of several: two live rows for one transcript that
the room cannot separate are an unresolvable identity collision, and this
module answers it with `guest`/not-matched and reaps them all, so neither
speaker can be handed the other's identity.

The row carries two independent facts and this module keeps them apart (#1152):
`uid` is who the turn belongs to (routing — `guest` for an unknown speaker),
`matched` is the gatekeeper's explicit claim that speaker-ID recognised an
enrolled resident. Only `matched` may unlock the PERSONAL tool class; the mere
existence of a row, and the uid's value whatever it becomes, never can. Every
degenerate case answers "not matched": no row, no `matched` column (a DB the
0031 migration hasn't reached, or an older gatekeeper that wrote the legacy
shape), a NULL or non-1 value, a missing table, an ambiguous hit.

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

GUEST_UID = "guest"


@dataclass(frozen=True)
class StashedSpeaker:
    """One consumed stash row: who the turn is attributed to, and whether the
    gatekeeper claimed to have recognised that voice. `matched` defaults to
    False so any path that builds one without asserting a match is closed."""

    uid: str
    matched: bool = False


_SELECT = """
    SELECT room, uid, matched FROM voice_uid_stash
    WHERE transcript = ? AND created_at >= datetime('now', ?)
"""

# Pre-0033 shape: no `room` column, so the transcript is the whole primary key
# and there can only ever be the one candidate row.
_SELECT_NO_ROOM = """
    SELECT uid, matched FROM voice_uid_stash
    WHERE transcript = ? AND created_at >= datetime('now', ?)
"""

# Pre-0031 shape: no `matched` column, so nothing in such a row can assert a
# recognition and the turn reads as unmatched.
_SELECT_LEGACY = """
    SELECT uid FROM voice_uid_stash
    WHERE transcript = ? AND created_at >= datetime('now', ?)
"""

_REAP_ALL = "DELETE FROM voice_uid_stash WHERE transcript = ?"
_REAP_ROOM = "DELETE FROM voice_uid_stash WHERE transcript = ? AND room = ?"
_REAP_EXPIRED = """
    DELETE FROM voice_uid_stash
    WHERE transcript = ? AND created_at < datetime('now', ?)
"""


def normalize_room(room: str | None) -> str:
    """The room as it enters the correlation key. Both ends normalise the same
    way so a case or padding difference can't split one turn into two rows."""
    return (room or "").strip().casefold()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout so concurrent writers wait instead of raising
    # "database is locked" (#600). Switching the journal mode needs the whole
    # database to itself, which is exactly what a concurrent turn denies — and
    # it is a persistent, install-wide setting, so losing that one race must
    # not cost this turn its speaker attribution.
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def _live_rows(
    conn: sqlite3.Connection, transcript: str, ttl: str
) -> list[sqlite3.Row]:
    for statement in (_SELECT, _SELECT_NO_ROOM, _SELECT_LEGACY):
        try:
            return conn.execute(statement, (transcript, ttl)).fetchall()
        except sqlite3.OperationalError:
            continue
    raise sqlite3.OperationalError("voice_uid_stash is not readable")


def consume_speaker(
    db_path: str, transcript: str, room: str | None = None
) -> StashedSpeaker | None:
    """Return the speaker the gatekeeper stashed for this transcript, or None
    on a miss/expiry. Consume-once: a fresh hit is deleted so it can't be
    re-read by a later identical utterance. An identity collision the room
    can't resolve fails closed to `guest`/not-matched. Best-effort — a missing
    table/DB returns None and the caller falls back to the default uid."""
    if not transcript or not Path(db_path).exists():
        return None
    ttl = f"-{STASH_TTL_SECONDS} seconds"
    room = normalize_room(room)
    try:
        with _connect(db_path) as conn:
            # BEGIN IMMEDIATE takes the write lock before the read so two
            # concurrent turns with the same transcript can't both consume the
            # same row; the read and the reap land in one transaction.
            conn.execute("BEGIN IMMEDIATE")
            rows = _live_rows(conn, transcript, ttl)
            # A row from a room this turn demonstrably didn't come from belongs
            # to someone else's turn: not a candidate, and not ours to reap.
            candidates = [r for r in rows if _rooms_agree(room, _room_of(r))]
            picked = candidates[0] if len(candidates) == 1 else None
            ambiguous = len(candidates) > 1
            # Consume-once burns every row this call could have been, so the
            # loser of a collision can't be handed to the next caller either.
            # TTL-expired rows go too, so the table can't grow unbounded.
            conn.execute(_REAP_EXPIRED, (transcript, ttl))
            for row in candidates:
                if "room" in row.keys():
                    conn.execute(_REAP_ROOM, (transcript, row["room"]))
                else:
                    conn.execute(_REAP_ALL, (transcript,))
            conn.commit()
    except sqlite3.OperationalError:
        return None
    if ambiguous:
        return StashedSpeaker(GUEST_UID, matched=False)
    if picked is None:
        return None
    matched = "matched" in picked.keys() and picked["matched"] == 1
    return StashedSpeaker(str(picked["uid"]), matched=matched)


def _room_of(row: sqlite3.Row) -> str:
    return normalize_room(row["room"]) if "room" in row.keys() else ""


def _rooms_agree(want: str, got: str) -> bool:
    """True unless both ends named a room and named different ones. `''` is
    "this end doesn't know" — the HA-STT path, where the gatekeeper's peer is
    HA rather than the satellite — and must not disqualify the row."""
    return not want or not got or want == got
