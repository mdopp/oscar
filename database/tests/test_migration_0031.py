"""Migration 0031 (voice_uid_stash.matched) applies onto 0030 (#1152).

The load-bearing property is the *default*. The column carries the gatekeeper's
explicit claim that speaker-ID recognised an enrolled resident, and the engine
unlocks the PERSONAL tool class on nothing else. A row written by a gatekeeper
image that predates the column — the two deploy independently — must therefore
come out as "not matched", never as a match.
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
    assert len(heads) == 1


def test_upgrade_adds_matched_defaulting_to_not_matched(tmp_path):
    db = str(tmp_path / "solaris.db")
    cfg = _cfg(db)
    command.upgrade(cfg, "0030_voice_embeddings_multi")

    # A row stashed before the upgrade, by a writer that knew only the uid.
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO voice_uid_stash (transcript, uid) VALUES (?, ?)",
            ("licht an", "anna"),
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT uid, matched FROM voice_uid_stash WHERE transcript = 'licht an'"
        ).fetchone() == ("anna", 0)
        # And a legacy-shaped INSERT keeps working after the upgrade, still
        # without being able to claim a match.
        conn.execute(
            "INSERT INTO voice_uid_stash (transcript, uid) VALUES (?, ?)",
            ("wer bin ich", "guest"),
        )
        conn.commit()
        assert conn.execute(
            "SELECT matched FROM voice_uid_stash WHERE transcript = 'wer bin ich'"
        ).fetchone() == (0,)
    finally:
        conn.close()
