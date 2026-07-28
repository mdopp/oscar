"""SQLite-backed storage for Solaris voice embeddings (#937 Phase 2).

The `voice_embeddings` table is provisioned by the baseline Alembic
migration and reshaped by `20260728_0030_voice_embeddings_multi.py`:
**several** rows per resident `uid` (surrogate `id` PK, index on
`uid`), BLOB embedding (192 × float32 = 768 B), `sample_count`
averaged over that enrolment, and `enrolled_via`. One row per
resident forced every sitting to be averaged into one vector, which
lands between the conditions a voice actually spreads over — close
to the device vs. across the room, quiet vs. TV (#1084).

This module owns the read/write contract; the resolver in
`speaker.py` calls into it for the k-NN sweep, and the enrolment
endpoint calls into it to add a freshly-averaged embedding.

Design notes:

  * Sync sqlite3. The store is called from async handlers, but each
    op is millisecond-cheap (read 3–10 rows, write one row).
    Wrapping it in `asyncio.to_thread` was considered and dropped;
    the simplicity of sync I/O wins until enrolment rows reach the
    hundreds, which they never will in a household setting.
  * Embeddings on disk are little-endian float32. `numpy.tobytes()`
    produces that on every architecture we target.
  * If solaris.db does not yet exist (gatekeeper booted before the
    init container has run), `list_embeddings` returns `[]` and
    `insert_embedding` raises. Callers downgrade to default_uid in
    that case.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

EMBEDDING_DIM = 192
EMBEDDING_BYTES = EMBEDDING_DIM * 4  # float32


@dataclass(frozen=True)
class VoiceEmbedding:
    uid: str
    embedding_bytes: bytes  # raw float32 little-endian
    sample_count: int

    def as_array(self):
        # Imported lazily so the module is usable without numpy
        # (e.g. when speaker-id is disabled and the store is only
        # exercised by tests of unrelated handlers).
        import numpy as np

        return np.frombuffer(self.embedding_bytes, dtype="<f4")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def list_embeddings(db_path: str) -> list[VoiceEmbedding]:
    """Return every enrolled embedding. Empty list when the DB is
    missing or the table is empty — callers treat both the same way
    (fall back to default_uid)."""
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT uid, embedding, sample_count FROM voice_embeddings"
            ).fetchall()
    except sqlite3.OperationalError:
        # Table missing (init container hasn't migrated yet).
        return []
    out: list[VoiceEmbedding] = []
    for row in rows:
        blob = bytes(row["embedding"])
        if len(blob) != EMBEDDING_BYTES:
            # Skip malformed rows rather than throwing; an admin can
            # re-enrol the affected resident.
            continue
        out.append(
            VoiceEmbedding(
                uid=row["uid"],
                embedding_bytes=blob,
                sample_count=int(row["sample_count"]),
            )
        )
    return out


def insert_embedding(
    db_path: str,
    uid: str,
    embedding_bytes: bytes,
    *,
    sample_count: int,
    enrolled_via: str,
) -> None:
    """Add one more voice fingerprint for a resident.

    Adds, never replaces: a second enrolment is a second condition
    (another room, another time of day), and keeping both is the whole
    point. Re-enrolling to *discard* the old profile means
    `delete_embedding` first.
    """
    if len(embedding_bytes) != EMBEDDING_BYTES:
        raise ValueError(
            f"embedding must be {EMBEDDING_BYTES} bytes ({EMBEDDING_DIM} float32), got {len(embedding_bytes)}"
        )
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO voice_embeddings (uid, embedding, sample_count, enrolled_via)
            VALUES (?, ?, ?, ?)
            """,
            (uid, embedding_bytes, sample_count, enrolled_via),
        )
        conn.commit()


def touch_last_seen(db_path: str, uid: str) -> None:
    """Record that this uid was matched on a recent turn — every row of
    theirs, since the stamp says "this resident was heard", not "this
    fingerprint won". Best-effort — a failure here doesn't break the
    conversation pipeline."""
    if not Path(db_path).exists():
        return
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE voice_embeddings SET last_seen_at = datetime('now') WHERE uid = ?",
                (uid,),
            )
            conn.commit()
    except sqlite3.OperationalError:
        return


def delete_embedding(db_path: str, uid: str) -> bool:
    """Remove a resident's enrolment — *all* of their fingerprints. Used
    by admin un-enrol flows, where leaving even one row behind would keep
    recognising someone who asked to be forgotten."""
    if not Path(db_path).exists():
        return False
    try:
        with _connect(db_path) as conn:
            cur = conn.execute("DELETE FROM voice_embeddings WHERE uid = ?", (uid,))
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.OperationalError:
        return False


def list_uids(db_path: str) -> list[str]:
    """The enrolled residents, one entry each however many fingerprints
    they have. Convenience for admin listings; cheaper than loading full
    BLOBs."""
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows: Iterable[sqlite3.Row] = conn.execute(
                "SELECT DISTINCT uid FROM voice_embeddings ORDER BY uid"
            ).fetchall()
            return [str(row["uid"]) for row in rows]
    except sqlite3.OperationalError:
        return []
