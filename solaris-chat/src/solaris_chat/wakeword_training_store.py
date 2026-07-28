"""Engine side of the wakeword-training queue (#1066).

The mirror of `enroll_requests_store`: there the engine opens a request and the
gatekeeper writes the result back; here the engine enqueues a training run and
the `solaris-wakeword-trainer` GPU companion claims it, runs microWakeWord and
writes `done` + `model_path` (or `failed` + a reason) back.

The engine cannot start the training itself — the chat image is a slim Python
base with no TensorFlow, no GPU and no training script, and a pod container has
no route to the host's podman — so this table IS the capability.

Sync sqlite3, like `enroll_requests_store`. The table is provisioned by
migration `0029_wakeword_training_runs`; a missing table/DB reads as "no queue",
which the tool reports honestly instead of promising a model nobody builds.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
PENDING_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def enqueue_run(db_path: str, uid: str) -> int | None:
    """Queue one training run for this resident and return its id, or None when
    the queue is unavailable (table/DB missing — schema-init hasn't migrated
    yet). The caller must not claim a training was scheduled on None."""
    if not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "INSERT INTO wakeword_training_runs (uid, status)"
                " VALUES (?, ?) RETURNING id",
                (uid, STATUS_QUEUED),
            ).fetchone()
            conn.commit()
    except sqlite3.OperationalError:
        return None
    return int(row["id"]) if row else None


def latest_run(db_path: str) -> dict | None:
    """The newest run on the box, or None when nothing was ever queued (or the
    queue is unavailable).

    Household-wide on purpose: one model wakes the box for everyone and a run
    folds in every resident's recordings (#1074), so "is a training in flight"
    is a household question, not a per-resident one. The trainer claims the
    oldest queued row first, so the newest row is always the last state a
    resident's screen should show.
    """
    if not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, uid, status, requested_at, started_at, finished_at,"
                " result, model_path FROM wakeword_training_runs"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None
