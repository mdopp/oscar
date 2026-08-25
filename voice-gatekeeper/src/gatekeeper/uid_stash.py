"""Transcript-keyed uid side-channel for the live HA Assist path (#350).

When the gatekeeper serves as HA's Wyoming STT provider it transcribes the
turn AND resolves the speaking resident (ECAPA + k-NN), but HA — not the
gatekeeper — runs the conversation step. HA forwards only the transcript
text to the engine facade (`conversation.solaris`), with no uid. So the
gatekeeper stashes `{transcript, room -> uid}` here; the facade reads it back by
the incoming utterance text to attribute the spoken turn to the resident.

The transcript is the shared correlation key: the gatekeeper produced it and
the facade receives the identical string a moment later. `room` widens that key
where a second shared value exists — on the satellite path the gatekeeper
resolves the originating room and sends it as the `[room: X]` prefix the facade
parses back out, so two rooms saying the same sentence are two independent rows
(#1218). On the HA-STT path the peer is HA itself, so the room is `''` and the
key is the transcript alone.

Consume-once + a short TTL bound the stale collision; the *concurrent* one is
bounded here, by failing closed: when a live row for the same key already
carries a different uid, the row is degraded to `guest` / not-matched rather
than overwritten. Both speakers then lose the personal scope for that turn,
which is the only safe answer when the two turns cannot be told apart.

A row says two separate things, and the second one is the security-relevant
one: `uid` is *who the turn is attributed to* (routing), `matched` is *whether
speaker-ID actually recognised an enrolled resident* (the claim the engine's
PERSONAL gate turns on). The unknown-speaker row carries `uid='guest'` with
`matched=0` — it routes, it does not recognise. The caller must state `matched`
explicitly; nothing here derives it from the uid's value (#1152).

Sync sqlite3 over the same `solaris.db` the rest of the gatekeeper opens
(`rooms_store`, `embeddings_store`). The table is provisioned by alembic
migration `0012_voice_uid_stash`, the `matched` column by
`0031_voice_uid_stash_matched` and the `room` key column by
`0033_voice_uid_stash_room`; if any is missing (init container hasn't migrated
yet) the writer degrades — no table means no row at all, an un-migrated table
means a narrower key or a legacy-shaped row, which every reader treats as
*not matched*.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

GUEST_UID = "guest"

# Must not be shorter than the facade reader's STASH_TTL_SECONDS: a row the
# reader would still consume has to count as a live collision here.
STASH_TTL_SECONDS = 30

# The conflicting row is overwritten only when it is the same speaker, or when
# it is already too old for any reader to consume. A live row belonging to
# someone else is degraded to the unknown-speaker row instead — never handed
# the newcomer's identity, never left holding the incumbent's (#1218).
_ON_CONFLICT_FAIL_CLOSED = """
    DO UPDATE SET
        uid        = CASE WHEN voice_uid_stash.uid = excluded.uid
                            OR voice_uid_stash.created_at < datetime('now', :ttl)
                          THEN excluded.uid ELSE :guest END,
        matched    = CASE WHEN voice_uid_stash.uid = excluded.uid
                            OR voice_uid_stash.created_at < datetime('now', :ttl)
                          THEN excluded.matched ELSE 0 END,
        created_at = excluded.created_at
"""

_INSERT = f"""
    INSERT INTO voice_uid_stash (transcript, room, uid, matched, created_at)
    VALUES (:transcript, :room, :uid, :matched, datetime('now'))
    ON CONFLICT(transcript, room) {_ON_CONFLICT_FAIL_CLOSED}
"""

# Pre-0033 shape: no `room` column, so the key is the transcript alone and two
# rooms collide like two speakers do — the fail-closed conflict rule still
# applies, which is what keeps that window safe.
_INSERT_NO_ROOM = f"""
    INSERT INTO voice_uid_stash (transcript, uid, matched, created_at)
    VALUES (:transcript, :uid, :matched, datetime('now'))
    ON CONFLICT(transcript) {_ON_CONFLICT_FAIL_CLOSED}
"""

# Pre-0031 shape, used only while the schema-init sidecar hasn't added the
# column yet. It cannot express a match, which is the safe direction: guest
# routing keeps working through the window and a recognition reads as
# unmatched until the migration lands.
_INSERT_LEGACY = """
    INSERT INTO voice_uid_stash (transcript, uid, created_at)
    VALUES (:transcript, :uid, datetime('now'))
    ON CONFLICT(transcript) DO UPDATE SET
        uid        = CASE WHEN voice_uid_stash.uid = excluded.uid
                            OR voice_uid_stash.created_at < datetime('now', :ttl)
                          THEN excluded.uid ELSE :guest END,
        created_at = excluded.created_at
"""


def normalize_room(room: str | None) -> str:
    """The room as it enters the correlation key. Both ends normalise the same
    way so a case or padding difference can't split one turn into two rows."""
    return (room or "").strip().casefold()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Two satellites can stash at the same instant. Without the wait one write
    # is dropped on "database is locked", and a dropped write is worse than a
    # collision: the surviving row is then the *only* candidate for both
    # turns, which is exactly the identity swap this module fails closed on.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def stash_uid(
    db_path: str, transcript: str, uid: str, *, matched: bool, room: str | None = None
) -> None:
    """Record `{transcript, room -> uid, matched}` for the facade to consume on
    the next turn. `matched` must be stated by the caller: it is True only for a
    voice speaker-ID recognised as an enrolled resident, and it is the only
    thing the engine's PERSONAL gate accepts as proof of that. `room` is the
    originating room when the gatekeeper knows it (satellite path) and `''`
    otherwise; it widens the key, it never asserts anything.

    Best-effort: a missing table/DB (init container not yet migrated) must not
    break the STT response, so failures are swallowed."""
    if not transcript or not Path(db_path).exists():
        return
    params = {
        "transcript": transcript,
        "room": normalize_room(room),
        "uid": uid,
        "matched": 1 if matched else 0,
        "ttl": f"-{STASH_TTL_SECONDS} seconds",
        "guest": GUEST_UID,
    }
    try:
        with _connect(db_path) as conn:
            for statement in (_INSERT, _INSERT_NO_ROOM, _INSERT_LEGACY):
                try:
                    conn.execute(statement, params)
                    break
                except sqlite3.OperationalError:
                    continue
            conn.commit()
    except sqlite3.OperationalError:
        return
