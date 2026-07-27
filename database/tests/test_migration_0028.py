"""Migration 0028 (engine_messages conversation scoping) applies onto 0027 (#1067).

An alembic-in-the-loop test lives HERE, in database/ — the solaris-chat tests
must never import alembic (CI runs that package in a clean env without it). This
runs `alembic upgrade head` against a throwaway sqlite file and asserts both new
columns landed with the defaults the prompt filter relies on, and that the
migration chain still has a single linear head.

The load-bearing property is the default: existing rows must keep
`conversation_id = NULL` (so they can never match a stamped id and therefore
drop out of every future prompt) while staying `in_prompt = 1` (so nothing else
changes about them). That is what lets the 141 rows already on the box stop
poisoning prompts without a single UPDATE against real user data.
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
    assert tuple(heads) == ("0028_engine_messages_conversation",)


def test_upgrade_adds_conversation_columns(tmp_path):
    db = str(tmp_path / "solaris.db")
    command.upgrade(_cfg(db), "head")
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(engine_messages)")}
        assert {"conversation_id", "in_prompt"} <= cols

        # A pre-migration-shaped insert keeps the legacy defaults: no
        # conversation (invisible to every scoped prompt), still in_prompt.
        conn.execute(
            "INSERT INTO engine_sessions (id, owner_uid) VALUES ('s1', 'anna')"
        )
        conn.execute(
            "INSERT INTO engine_messages (session_id, seq, role, content)"
            " VALUES ('s1', 1, 'assistant', 'alte Zeile')"
        )
        row = conn.execute(
            "SELECT conversation_id, in_prompt FROM engine_messages"
        ).fetchone()
        assert row == (None, 1)

        idx = {r[1] for r in conn.execute("PRAGMA index_list(engine_messages)")}
        assert "engine_messages_conv_idx" in idx
    finally:
        conn.close()
