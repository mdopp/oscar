"""Tests for the trainer side of the wakeword-training handshake (#1066).

The queue is the whole capability: the engine can only enqueue, so if the claim
is not exactly-once or the write-back never lands, the resident is promised a
model nobody builds. The training itself is a multi-hour GPU job and is never
exercised here — these cover claim, write-back and the no-op robustness the
poller needs while schema-init hasn't migrated yet.

Loaded via importlib (like templates/tests) — the trainer is a single stdlib
script in an image, not an installed package.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import signal
import sqlite3
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# `wakeword_training_runs` as migration 0029 creates it. The trainer never
# creates the table itself — a missing one means schema-init hasn't run.
QUEUE_DDL = """
CREATE TABLE wakeword_training_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    uid          TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at   TEXT,
    finished_at  TEXT,
    result       TEXT,
    model_path   TEXT
)
"""


@pytest.fixture(scope="module")
def trainer():
    spec = importlib.util.spec_from_file_location("trainer", ROOT / "trainer.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["trainer"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "solaris.db")
    with sqlite3.connect(path) as conn:
        conn.execute(QUEUE_DDL)
        conn.commit()
    return path


def _enqueue(db_path: str, uid: str) -> int:
    """What the engine's wakeword_training_store.enqueue_run writes."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "INSERT INTO wakeword_training_runs (uid, status)"
            " VALUES (?, 'queued') RETURNING id",
            (uid,),
        ).fetchone()
        conn.commit()
    return int(row[0])


def _row(db_path: str, run_id: int) -> tuple:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT status, started_at IS NOT NULL, finished_at IS NOT NULL,"
            " result, model_path FROM wakeword_training_runs WHERE id = ?",
            (run_id,),
        ).fetchone()


def test_claim_takes_the_oldest_queued_run_once(trainer, db):
    first = _enqueue(db, "alex")
    second = _enqueue(db, "marco")

    assert trainer.claim_queued_run(db) == (first, "alex")
    assert _row(db, first)[:2] == ("running", 1)

    # One run at a time: the second is only handed out after the first is done.
    assert trainer.claim_queued_run(db) == (second, "marco")
    assert trainer.claim_queued_run(db) is None


def test_finish_run_writes_done_with_the_model_path(trainer, db):
    run_id = _enqueue(db, "alex")
    trainer.claim_queued_run(db)
    trainer.finish_run(
        db, run_id, ok=True, result="Modell gebaut", model_path="/work/out/x.tflite"
    )
    assert _row(db, run_id) == (
        "done",
        1,
        1,
        "Modell gebaut",
        "/work/out/x.tflite",
    )


def test_finish_run_records_the_failure_reason(trainer, db):
    run_id = _enqueue(db, "alex")
    trainer.claim_queued_run(db)
    trainer.finish_run(db, run_id, ok=False, result="CalledProcessError: exit 1")
    assert _row(db, run_id) == ("failed", 1, 1, "CalledProcessError: exit 1", None)


def test_restart_fails_runs_it_can_no_longer_be_running(trainer, db):
    # The process holding a `running` row died with the previous container;
    # leaving it running would make the resident wait forever, re-queueing it
    # would crash-loop an hours-long GPU job that already failed once.
    run_id = _enqueue(db, "alex")
    trainer.claim_queued_run(db)

    assert trainer.fail_orphaned_runs(db) == 1
    status, _, finished, result, _ = _row(db, run_id)
    assert (status, finished) == ("failed", 1)
    assert "neu gestartet" in result
    # Nothing left to reap on the next start.
    assert trainer.fail_orphaned_runs(db) == 0


def test_every_op_is_a_no_op_without_the_table(trainer, tmp_path):
    # schema-init hasn't migrated yet: the poller must idle, not crash.
    unmigrated = str(tmp_path / "empty.db")
    sqlite3.connect(unmigrated).close()
    assert trainer.claim_queued_run(unmigrated) is None
    assert trainer.fail_orphaned_runs(unmigrated) == 0
    trainer.finish_run(unmigrated, 1, ok=True, result="x")


def test_every_op_is_a_no_op_without_the_db(trainer, tmp_path):
    missing = str(tmp_path / "nope.db")
    assert trainer.claim_queued_run(missing) is None
    assert trainer.fail_orphaned_runs(missing) == 0
    trainer.finish_run(missing, 1, ok=True, result="x")
    assert not pathlib.Path(missing).exists()


@pytest.fixture
def stoppable(trainer):
    """Let a test drive trainer.main() through a real SIGTERM and put the
    process's own handlers back afterwards."""
    saved = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    sent = threading.Event()

    def send_when(ready):
        def run():
            deadline = time.monotonic() + 10
            while not ready() and time.monotonic() < deadline:
                time.sleep(0.01)
            sent.set()
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=run, daemon=True).start()

    try:
        yield send_when
    finally:
        signal.signal(signal.SIGTERM, saved[0])
        signal.signal(signal.SIGINT, saved[1])
        trainer._stopping = False
        assert sent.is_set(), "the stop signal was never sent"


def test_sigterm_while_idle_exits_promptly(trainer, db, monkeypatch, stoppable):
    # The GPU lease stops this unit by design; podman SIGKILLs after 10s, and
    # systemd calls that exit 137 a failed unit (#1407).
    monkeypatch.setattr(trainer, "DB_PATH", db)
    monkeypatch.setattr(trainer, "POLL_SECONDS", 30)
    stoppable(lambda: signal.getsignal(signal.SIGTERM) is trainer._on_stop)

    started = time.monotonic()
    trainer.main()
    assert time.monotonic() - started < 10


def test_sigterm_during_a_run_fails_the_row_and_exits(
    trainer, db, monkeypatch, stoppable
):
    run_id = _enqueue(db, "alex")
    monkeypatch.setattr(trainer, "DB_PATH", db)
    monkeypatch.setattr(trainer, "POLL_SECONDS", 1)
    monkeypatch.setattr(trainer, "provision_work", lambda work: None)
    monkeypatch.setattr(
        trainer,
        "train",
        lambda work: trainer._run(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        ),
    )
    stoppable(lambda: trainer._child is not None)

    started = time.monotonic()
    trainer.main()
    assert time.monotonic() - started < 10

    status, _, finished, result, _ = _row(db, run_id)
    assert (status, finished) == ("failed", 1)
    assert result == "Trainer wurde angehalten"


def test_voice_urls_match_the_training_script_defaults(trainer):
    # The script raises SystemExit when no voice .onnx is present, so a drifted
    # voice list means every run fails in phase 1.
    script = (ROOT / "train-micro-wake-word.py").read_text(encoding="utf-8")
    for voice in trainer.VOICES:
        assert f'"{voice}"' in script
