"""Migration 0034 (ha_notice_backlog) applies onto 0033 (#1284).

The `ha` event kind had no backlog at all, so a notice published while the app
was not listening was lost. This table is what a catch-up reads; what the
migration owes it is the shape the retention logic depends on — a per-stream
lookup that stays cheap, and a millisecond stamp, because a cursor at second
resolution silently drops a second notice published in the same second.
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
    script = ScriptDirectory.from_config(_cfg(str(tmp_path / "x.db")))
    assert len(script.get_heads()) == 1  # name-free: every later migration re-points it


def test_upgrade_creates_the_backlog_table(tmp_path):
    db = str(tmp_path / "solaris.db")
    cfg = _cfg(db)
    command.upgrade(cfg, "0033_voice_uid_stash_room")
    conn = sqlite3.connect(db)
    assert (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'ha_notice_backlog'"
        ).fetchone()
        is None
    )
    conn.close()

    command.upgrade(cfg, "0034_ha_notice_backlog")
    conn = sqlite3.connect(db)
    cols = {c[1] for c in conn.execute("PRAGMA table_info(ha_notice_backlog)")}
    assert cols == {"id", "target_uid", "created_at", "payload"}
    indexes = {i[1] for i in conn.execute("PRAGMA index_list(ha_notice_backlog)")}
    conn.close()
    # The only query shape there is: one stream, ordered by time.
    assert "ha_notice_backlog_target_idx" in indexes


def test_the_stamp_is_sub_second_and_the_order_is_the_id(tmp_path):
    db = str(tmp_path / "solaris.db")
    command.upgrade(_cfg(db), "head")
    conn = sqlite3.connect(db)
    for _ in range(2):
        conn.execute(
            "INSERT INTO ha_notice_backlog (target_uid, payload) VALUES ('anna', '{}')"
        )
    rows = conn.execute("SELECT id, created_at FROM ha_notice_backlog").fetchall()
    conn.close()
    assert all("." in created for _id, created in rows)
    # Two rows can share a millisecond; the ids never tie.
    assert [r[0] for r in rows] == [1, 2]
