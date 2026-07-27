"""Migration 0022 (wartung_seen_updates) applies onto the 0021 head (#788).

An alembic-in-the-loop test lives HERE, in database/ — the solaris-chat tests
must never import alembic (CI runs that package in a clean env without it). This
runs `alembic upgrade head` against a throwaway sqlite file and asserts the
wartung_seen_updates table landed with its update_id PRIMARY KEY (the dedupe key
behind the Wartung update-cards), and that the chain still has a single head.
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
    # Pinning the newest revision id here rots with every later migration; the
    # property this file cares about is that the chain never forks.
    heads = ScriptDirectory.from_config(_cfg(str(tmp_path / "x.db"))).get_heads()
    assert len(heads) == 1


def test_upgrade_creates_wartung_seen_updates(tmp_path):
    db = str(tmp_path / "solaris.db")
    command.upgrade(_cfg(db), "head")
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(wartung_seen_updates)")}
        assert cols == {"update_id", "seen_at"}

        # update_id PRIMARY KEY: a duplicate id must fail (the dedupe guarantee).
        conn.execute(
            "INSERT INTO wartung_seen_updates (update_id) VALUES ('image:immich:x')"
        )
        try:
            conn.execute(
                "INSERT INTO wartung_seen_updates (update_id) VALUES ('image:immich:x')"
            )
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised
    finally:
        conn.close()
