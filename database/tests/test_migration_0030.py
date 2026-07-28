"""Migration 0030 (voice_embeddings multi-row) applies onto 0029 (#1084).

The load-bearing property is *preservation*: `voice_embeddings` holds biometric
voice profiles of real residents, and this migration rebuilds the table to drop
the `uid PRIMARY KEY`. A resident enrolled before the upgrade must come out the
other side with the exact same embedding bytes — losing it would silently
un-enrol them, and the only repair is asking a human to speak into the box
again.

The second property is the point of the change: after the upgrade the same uid
can hold several rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parent.parent

_PROFILE = bytes(range(256)) * 3  # 768 B — one 192-d float32 embedding


def _cfg(db_path: str) -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_single_linear_head(tmp_path):
    heads = ScriptDirectory.from_config(_cfg(str(tmp_path / "x.db"))).get_heads()
    assert len(heads) == 1


def test_upgrade_preserves_an_already_enrolled_resident(tmp_path):
    db = str(tmp_path / "solaris.db")
    cfg = _cfg(db)
    command.upgrade(cfg, "0029_wakeword_training_runs")

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO voice_embeddings (uid, embedding, enrolled_at, enrolled_via,"
            " sample_count, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "anna",
                _PROFILE,
                "2026-07-01 08:00:00",
                "voice",
                3,
                "2026-07-27 19:00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT uid, embedding, enrolled_at, enrolled_via, sample_count,"
            " last_seen_at FROM voice_embeddings"
        ).fetchall()
        assert row == [
            (
                "anna",
                _PROFILE,
                "2026-07-01 08:00:00",
                "voice",
                3,
                "2026-07-27 19:00:00",
            )
        ]
    finally:
        conn.close()


def test_upgrade_allows_several_rows_per_resident(tmp_path):
    db = str(tmp_path / "solaris.db")
    command.upgrade(_cfg(db), "head")

    conn = sqlite3.connect(db)
    try:
        for via in ("voice", "voice", "http"):
            conn.execute(
                "INSERT INTO voice_embeddings (uid, embedding, enrolled_via,"
                " sample_count) VALUES (?, ?, ?, ?)",
                ("anna", _PROFILE, via, 3),
            )
        conn.commit()

        assert (
            conn.execute(
                "SELECT count(*) FROM voice_embeddings WHERE uid = 'anna'"
            ).fetchone()[0]
            == 3
        )
        # Distinct surrogate keys — the rows are individually addressable, and
        # `uid` alone can no longer be the primary key.
        ids = [r[0] for r in conn.execute("SELECT id FROM voice_embeddings")]
        assert len(set(ids)) == 3

        idx = {r[1] for r in conn.execute("PRAGMA index_list(voice_embeddings)")}
        assert "voice_embeddings_uid_idx" in idx
    finally:
        conn.close()
