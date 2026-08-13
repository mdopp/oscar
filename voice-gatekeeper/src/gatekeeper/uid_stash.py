"""Transcript-keyed uid side-channel for the live HA Assist path (#350).

When the gatekeeper serves as HA's Wyoming STT provider it transcribes the
turn AND resolves the speaking resident (ECAPA + k-NN), but HA — not the
gatekeeper — runs the conversation step. HA forwards only the transcript
text to the engine facade (`conversation.solaris`), with no uid. So the
gatekeeper stashes `{transcript -> uid}` here; the facade reads it back by
the incoming utterance text to attribute the spoken turn to the resident.

The transcript is the shared correlation key: the gatekeeper produced it and
the facade receives the identical string a moment later. Consume-once + a
short TTL bound the only failure mode — a stale or collided uid never leaks
into a later turn.

A row says two separate things, and the second one is the security-relevant
one: `uid` is *who the turn is attributed to* (routing), `matched` is *whether
speaker-ID actually recognised an enrolled resident* (the claim the engine's
PERSONAL gate turns on). The unknown-speaker row carries `uid='guest'` with
`matched=0` — it routes, it does not recognise. The caller must state `matched`
explicitly; nothing here derives it from the uid's value (#1152).

Sync sqlite3 over the same `solaris.db` the rest of the gatekeeper opens
(`rooms_store`, `embeddings_store`). The table is provisioned by alembic
migration `0012_voice_uid_stash` and the `matched` column by
`0031_voice_uid_stash_matched`; if either is missing (init container hasn't
migrated yet) the writer degrades — no table means no row at all, an
un-migrated table means a legacy-shaped row, which every reader treats as
*not matched*.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


_INSERT = """
    INSERT INTO voice_uid_stash (transcript, uid, matched, created_at)
    VALUES (?, ?, ?, datetime('now'))
    ON CONFLICT(transcript) DO UPDATE SET
        uid        = excluded.uid,
        matched    = excluded.matched,
        created_at = excluded.created_at
"""

# Pre-0031 shape, used only while the schema-init sidecar hasn't added the
# column yet. It cannot express a match, which is the safe direction: guest
# routing keeps working through the window and a recognition reads as
# unmatched until the migration lands.
_INSERT_LEGACY = """
    INSERT INTO voice_uid_stash (transcript, uid, created_at)
    VALUES (?, ?, datetime('now'))
    ON CONFLICT(transcript) DO UPDATE SET
        uid        = excluded.uid,
        created_at = excluded.created_at
"""


def stash_uid(db_path: str, transcript: str, uid: str, *, matched: bool) -> None:
    """Record `{transcript -> uid, matched}` for the facade to consume on the
    next turn. `matched` must be stated by the caller: it is True only for a
    voice speaker-ID recognised as an enrolled resident, and it is the only
    thing the engine's PERSONAL gate accepts as proof of that.

    Best-effort: a missing table/DB (init container not yet migrated) must not
    break the STT response, so failures are swallowed."""
    if not transcript or not Path(db_path).exists():
        return
    try:
        with _connect(db_path) as conn:
            try:
                conn.execute(_INSERT, (transcript, uid, 1 if matched else 0))
            except sqlite3.OperationalError:
                conn.execute(_INSERT_LEGACY, (transcript, uid))
            conn.commit()
    except sqlite3.OperationalError:
        return
