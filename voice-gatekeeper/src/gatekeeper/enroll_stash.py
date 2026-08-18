"""Reverse enroll-stash — the gatekeeper side of live-voice enrolment (#376).

The mirror of `uid_stash.py`. There, the gatekeeper writes `{transcript -> uid}`
for the engine to read; here, the engine writes an `enroll_requests` row for a
candidate uid and the gatekeeper reads it, captures the speaker's PCM across the
onboarding turns, and writes the enrolment result back.

When the gatekeeper is HA's Wyoming STT provider it already holds each turn's PCM
(16 kHz mono int16) — the same format `/enrol` wants — and the enrol store is
in-process (`embeddings_store.insert_embedding`), so no HTTP round-trip is
needed. Per onboarding turn the handler claims the pending row, embeds this
turn's audio as one sample, and once the target count is reached enrols the
averaged embedding and flips the row to `done` (or `failed` with a reason).

Consume-once + a short TTL bound the misattribution risk (same as the uid
stash): the request only captures the speaker for a brief window after the engine
opens it, so a later unrelated turn can't enrol into someone else's profile, and
a request no gatekeeper picks up (speaker-ID off) ages out so the engine side can
time out honestly rather than hang.

Sync sqlite3 over the same `solaris.db`; the table is provisioned by alembic
migration `0014_enroll_requests`. A missing table/DB makes every op a no-op so
the STT path keeps working when the init container hasn't migrated yet.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

# A capture window: an onboarding request that no gatekeeper has finished within
# this many seconds is stale (e.g. speaker-ID was off, or the speaker walked
# away mid-flow). The handler ignores stale rows so it never enrols a later,
# unrelated speaker into the candidate's profile.
ENROLL_TTL_SECONDS = 120

# Upper bound on captured turns for one request. The post-enrol self-test decides
# when a profile carries, so the wizard asks for as many sentences as it takes
# rather than a fixed three — but not forever: past this, enrolment fails
# honestly. Matches the engine wizard's own extra-sentence budget
# (target 3 + _MAX_EXTRA_SAMPLE_TURNS).
MAX_ENROLL_SAMPLES = 6

STATUS_PENDING = "pending"
STATUS_CAPTURING = "capturing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class EnrollRequest:
    uid: str
    status: str
    target_samples: int
    collected: int


# Per-uid in-process accumulation of the captured per-turn embeddings. The raw
# biometric PCM is embedded the moment it's captured and only the 192-d vectors
# are held here (never the audio, never the DB) until N are collected and
# averaged into the durable `voice_embeddings` row. Keyed by candidate uid; one
# onboarding runs at a time in a household, but a dict keeps concurrent requests
# isolated rather than cross-contaminating one buffer.
_pending_embeddings: dict[str, list[bytes]] = {}

# Per-uid lock serialising one candidate's capture critical section
# (add → increment → target check → take → enrol). Without it two concurrent
# same-uid turns can both pass the target check, both call take_embeddings, and
# the loser averages an empty list (ValueError). Different uids stay independent.
_capture_locks: dict[str, asyncio.Lock] = {}

# When each uid's buffer was last written, so an abandoned sitting's vectors age
# out on the same window as its DB row. Monotonic: a wall-clock step must not
# keep biometric data alive (or drop a live sitting's).
_last_capture: dict[str, float] = {}


def capture_lock(uid: str) -> asyncio.Lock:
    """Return the (lazily created) per-uid lock for the capture critical section."""
    return _capture_locks.setdefault(uid, asyncio.Lock())


def expire_stale_embeddings() -> None:
    """Drop the buffered vectors of any sitting that went quiet longer ago than
    the capture window. The gatekeeper is long-lived, so without this an
    abandoned onboarding's biometric vectors stay resident until restart — and a
    later sitting for the same uid would average them in. A resident who
    abandons an enrolment starts over instead of resuming it."""
    cutoff = time.monotonic() - ENROLL_TTL_SECONDS
    for uid, last in list(_last_capture.items()):
        if last < cutoff:
            _pending_embeddings.pop(uid, None)
            _last_capture.pop(uid, None)


def add_embedding(uid: str, embedding: bytes) -> int:
    """Append one captured-turn embedding for this uid; return the count held."""
    bucket = _pending_embeddings.setdefault(uid, [])
    bucket.append(embedding)
    _last_capture[uid] = time.monotonic()
    return len(bucket)


def restore_embeddings(uid: str, embeddings: list[bytes]) -> int:
    """Put samples that survived the self-test back in the buffer so the next
    onboarding turn adds to them instead of starting the sitting over. Prepends,
    because a concurrent turn for the same uid may have captured meanwhile."""
    bucket = _pending_embeddings.setdefault(uid, [])
    bucket[:0] = embeddings
    _last_capture[uid] = time.monotonic()
    return len(bucket)


def take_embeddings(uid: str) -> list[bytes]:
    """Pop and return the accumulated embeddings for this uid, clearing the
    in-process buffer so the biometric vectors don't linger after enrolment."""
    _last_capture.pop(uid, None)
    return _pending_embeddings.pop(uid, [])


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def claim_active_request(db_path: str) -> EnrollRequest | None:
    """Return the one fresh request still collecting samples, marking it
    `capturing` so the handler can attribute this turn's PCM to it. None when
    there is no such row, the row is stale (past TTL), or the table/DB is
    missing. Best-effort — a gap must not break the STT response."""
    if not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT uid, status, target_samples, collected
                  FROM enroll_requests
                 WHERE status IN (?, ?)
                   AND created_at >= datetime('now', ?)
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (STATUS_PENDING, STATUS_CAPTURING, f"-{ENROLL_TTL_SECONDS} seconds"),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE enroll_requests SET status = ? WHERE uid = ?",
                (STATUS_CAPTURING, row["uid"]),
            )
            conn.commit()
    except sqlite3.OperationalError:
        return None
    return EnrollRequest(
        uid=str(row["uid"]),
        status=str(row["status"]),
        target_samples=int(row["target_samples"]),
        collected=int(row["collected"]),
    )


def increment_collected(db_path: str, uid: str) -> int:
    """Record that one more usable sample was captured for this request; return
    the new collected count (0 on any failure)."""
    if not Path(db_path).exists():
        return 0
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                UPDATE enroll_requests
                   SET collected = collected + 1
                 WHERE uid = ?
                RETURNING collected
                """,
                (uid,),
            ).fetchone()
            conn.commit()
    except sqlite3.OperationalError:
        return 0
    return int(row["collected"]) if row else 0


def finish_request(db_path: str, uid: str, *, ok: bool, result: str) -> None:
    """Write the terminal status for a request: `done` on a successful enrol,
    `failed` (with a short reason in `result`) otherwise. Best-effort."""
    if not Path(db_path).exists():
        return
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE enroll_requests SET status = ?, result = ? WHERE uid = ?",
                (STATUS_DONE if ok else STATUS_FAILED, result, uid),
            )
            conn.commit()
    except sqlite3.OperationalError:
        return
