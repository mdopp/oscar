"""Migration 0023 (wartung_seen_approvals) applies onto the 0022 head (#790).

An alembic-in-the-loop test lives HERE, in database/ — the solaris-chat tests
must never import alembic (CI runs that package in a clean env without it). This
runs `alembic upgrade head` against a throwaway sqlite file and asserts the
wartung_seen_approvals dedupe table landed with its approval_id PRIMARY KEY, and
that the migration chain still has a single linear head.

NOTE (stacked on #788/#799): this revision chains on 0022_wartung_seen_updates,
which is introduced by #788's PR #799. On a branch off `main` where #799 has not
yet merged, the 0022 revision file is absent and `alembic upgrade head` cannot
resolve the chain — this test (and the migration) go green once #799 is in the
base. The approval cards reuse the Wartung injection path the update cards added,
so stacking the migration after 0022 keeps the head linear rather than forking a
second 0022 sibling.
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


def test_upgrade_creates_wartung_seen_approvals(tmp_path):
    db = str(tmp_path / "solaris.db")
    command.upgrade(_cfg(db), "head")
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(wartung_seen_approvals)")}
        assert cols == {"approval_id", "seen_at"}

        # approval_id PRIMARY KEY: a duplicate insert must fail.
        conn.execute(
            "INSERT INTO wartung_seen_approvals (approval_id) VALUES ('req-1')"
        )
        try:
            conn.execute(
                "INSERT INTO wartung_seen_approvals (approval_id) VALUES ('req-1')"
            )
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised
    finally:
        conn.close()
