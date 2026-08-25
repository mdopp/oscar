"""Migration 0033 (voice_uid_stash.room) applies onto 0032 (#1218).

The load-bearing property is the *key*. Keyed on the transcript alone, two
residents saying the same sentence inside the stash window shared one row and
the second write handed the first's turn the wrong `{uid, matched}` — across
the PERSONAL visibility gate. `room` joins the primary key so the satellite
path, where both ends know the room, keeps them apart.
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
    assert script.get_heads() == ["0033_voice_uid_stash_room"]


def test_upgrade_widens_the_key_and_keeps_existing_rows(tmp_path):
    db = str(tmp_path / "solaris.db")
    cfg = _cfg(db)
    command.upgrade(cfg, "0032_engine_timers_room")

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO voice_uid_stash (transcript, uid, matched) VALUES (?, ?, 1)",
        ("licht an", "anna"),
    )
    conn.commit()
    conn.close()

    command.upgrade(cfg, "head")

    conn = sqlite3.connect(db)
    # The in-flight row survives, in the "room unknown" slot.
    assert conn.execute(
        "SELECT room, uid, matched FROM voice_uid_stash"
    ).fetchall() == [("", "anna", 1)]
    # Two rooms, one transcript: two rows, no overwrite.
    conn.execute(
        "INSERT INTO voice_uid_stash (transcript, room, uid, matched)"
        " VALUES (?, 'küche', ?, 1)",
        ("guten morgen", "anna"),
    )
    conn.execute(
        "INSERT INTO voice_uid_stash (transcript, room, uid, matched)"
        " VALUES (?, 'bad', ?, 1)",
        ("guten morgen", "bob"),
    )
    conn.commit()
    rows = conn.execute(
        "SELECT room, uid FROM voice_uid_stash WHERE transcript = ? ORDER BY room",
        ("guten morgen",),
    ).fetchall()
    conn.close()
    assert rows == [("bad", "bob"), ("küche", "anna")]


def test_room_is_part_of_the_primary_key(tmp_path):
    db = str(tmp_path / "solaris.db")
    command.upgrade(_cfg(db), "head")
    conn = sqlite3.connect(db)
    key = [
        (c[1], c[5]) for c in conn.execute("PRAGMA table_info(voice_uid_stash)") if c[5]
    ]
    conn.close()
    assert sorted(key) == [("room", 2), ("transcript", 1)]
