"""Migration 0029 (wakeword_training_runs) applies onto 0028 (#1066).

An alembic-in-the-loop test lives HERE, in database/ — neither solaris-chat nor
the trainer image may import alembic (CI runs both in clean envs without it).
This runs `alembic upgrade head` against a throwaway sqlite file and asserts the
queue table landed with the columns and defaults both sides of the handshake
rely on, and that the migration chain still has a single linear head.

The load-bearing property is the claim: `started_at` is NULL and `status` is
`queued` on insert, so the trainer's conditional `queued -> running` UPDATE is
what hands exactly one trainer exactly one run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parent.parent


def _cfg(db_path: str) -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_single_linear_head(tmp_path):
    heads = ScriptDirectory.from_config(_cfg(str(tmp_path / "x.db"))).get_heads()
    assert len(heads) == 1  # name-free: every later migration re-points it


def test_upgrade_creates_queue_table_with_queued_default(tmp_path):
    db = str(tmp_path / "solaris.db")
    command.upgrade(_cfg(db), "head")
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(wakeword_training_runs)")}
        assert cols == {
            "id",
            "uid",
            "status",
            "requested_at",
            "started_at",
            "finished_at",
            "result",
            "model_path",
        }

        conn.execute("INSERT INTO wakeword_training_runs (uid) VALUES ('anna')")
        row = conn.execute(
            "SELECT status, started_at, finished_at, result, model_path,"
            " requested_at IS NOT NULL FROM wakeword_training_runs"
        ).fetchone()
        assert row == ("queued", None, None, None, None, 1)

        idx = {r[1] for r in conn.execute("PRAGMA index_list(wakeword_training_runs)")}
        assert "wakeword_training_runs_status_idx" in idx
    finally:
        conn.close()
