"""Wake-word sample capture — the gatekeeper side of onboarding stage 2 (#1060).

The sibling of `enroll_stash.py`. There the engine opens an `enroll_requests`
row and the gatekeeper embeds each onboarding turn's PCM; here the wizard opens
a `wakeword_requests` row for the resident and the gatekeeper writes each turn's
PCM as one `.wav` under the path the engine's `wakeword_samples_store` points
its rows at. Without this half the dialog only counted turns and filed rows for
audio nobody ever recorded.

As HA's Wyoming STT provider the gatekeeper already holds the turn's PCM
(16 kHz mono int16), so a sample is a `wave.open` away — no extractor, no HTTP.

Consume-once + a TTL bound the misattribution risk exactly as in the enrol
stash: only an `active` request the engine touched within the capture window is
claimed, so a later unrelated speaker can't be recorded into someone's wake-word
set. `updated_at` is bumped per captured sample, so a live dialog never expires
under the speaker.

Sync sqlite3 over the same `solaris.db`; the table is provisioned by the
engine's `wakeword_requests_store`. A missing table/DB makes every op a no-op so
the STT path keeps working.
"""

from __future__ import annotations

import os
import sqlite3
import wave
from dataclasses import dataclass
from pathlib import Path

# The capture window. Must stay <= the engine's
# `wakeword_requests_store.WAKEWORD_TTL_SECONDS` so the gatekeeper never records
# into a request the wizard already considers dead.
WAKEWORD_TTL_SECONDS = 120


@dataclass(frozen=True)
class WakewordRequest:
    uid: str
    target_count: int
    collected_count: int


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def claim_active_request(db_path: str) -> WakewordRequest | None:
    """Return the one fresh wake-word request still collecting samples. None
    when there is none, the row is stale (past TTL), or the table/DB is missing.
    Best-effort — a gap must not break the STT response."""
    if not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT uid, target_count, collected_count
                  FROM wakeword_requests
                 WHERE status = 'active'
                   AND collected_count < target_count
                   AND updated_at >= datetime('now', ?)
                 ORDER BY updated_at DESC
                 LIMIT 1
                """,
                (f"-{WAKEWORD_TTL_SECONDS} seconds",),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return WakewordRequest(
        uid=str(row["uid"]),
        target_count=int(row["target_count"]),
        collected_count=int(row["collected_count"]),
    )


def sample_path(db_path: str, uid: str, index: int) -> str:
    """Where the engine's `wakeword_samples_store` says the n-th sample lives:
    next to the database, under `wakeword/user_samples/<uid>/`."""
    return os.path.join(
        os.path.dirname(os.path.abspath(db_path)),
        "wakeword",
        "user_samples",
        uid,
        f"sample_{uid}_{index}.wav",
    )


def write_sample(
    path: str, pcm: bytes, *, rate: int, width: int, channels: int
) -> None:
    """Write one turn's PCM as the sample `.wav`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(pcm)


def record_sample(db_path: str, uid: str) -> int:
    """Count one captured sample for this request — called only once the `.wav`
    exists, so the count the wizard reads back never promises audio nobody
    recorded. Returns the new collected count (0 on any failure)."""
    if not Path(db_path).exists():
        return 0
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                UPDATE wakeword_requests
                   SET collected_count = collected_count + 1,
                       status = CASE WHEN collected_count + 1 >= target_count
                                     THEN 'completed' ELSE 'active' END,
                       updated_at = datetime('now')
                 WHERE uid = ?
                RETURNING collected_count
                """,
                (uid,),
            ).fetchone()
            conn.commit()
    except sqlite3.OperationalError:
        return 0
    return int(row["collected_count"]) if row else 0
