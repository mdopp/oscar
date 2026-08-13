"""Tests for the gatekeeper-side speaker stash writer (#350, #1152)."""

from __future__ import annotations

import sqlite3

import pytest
from gatekeeper.uid_stash import stash_uid


_SCHEMA = """
CREATE TABLE voice_uid_stash (
    transcript TEXT PRIMARY KEY,
    uid        TEXT NOT NULL,
    matched    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# The table as migration 0012 left it, before 0031 added `matched`. A
# gatekeeper image can reach a box whose schema-init sidecar hasn't run yet.
_SCHEMA_LEGACY = """
CREATE TABLE voice_uid_stash (
    transcript TEXT PRIMARY KEY,
    uid        TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _db(tmp_path, schema: str = _SCHEMA) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    conn.commit()
    conn.close()
    return path


def _read(db: str, transcript: str):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT uid, matched FROM voice_uid_stash WHERE transcript = ?", (transcript,)
    ).fetchone()
    conn.close()
    return row


def test_stash_writes_row_with_the_match_claim(tmp_path):
    db = _db(tmp_path)
    stash_uid(db, "licht an", "anna", matched=True)
    assert _read(db, "licht an") == ("anna", 1)


def test_stash_writes_an_unmatched_row_for_an_unknown_speaker(tmp_path):
    # The guest row exists so the turn routes to the guest profile — but it
    # carries no recognition claim, whatever the sentinel uid happens to be.
    db = _db(tmp_path)
    stash_uid(db, "wer bin ich", "guest", matched=False)
    assert _read(db, "wer bin ich") == ("guest", 0)


def test_stash_requires_the_match_claim_to_be_stated(tmp_path):
    # `matched` is keyword-only and has no default: a caller cannot stash a
    # speaker without saying whether speaker-ID recognised them.
    db = _db(tmp_path)
    with pytest.raises(TypeError):
        stash_uid(db, "licht an", "anna")  # type: ignore[call-arg]


def test_stash_upserts_latest_uid_and_claim(tmp_path):
    db = _db(tmp_path)
    stash_uid(db, "licht an", "anna", matched=True)
    stash_uid(db, "licht an", "guest", matched=False)
    # The later verdict wins in both columns — a stale match can't survive
    # under a fresh unknown-speaker row.
    assert _read(db, "licht an") == ("guest", 0)


def test_stash_empty_transcript_is_noop(tmp_path):
    db = _db(tmp_path)
    stash_uid(db, "", "anna", matched=True)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM voice_uid_stash").fetchone()[0] == 0
    conn.close()


def test_stash_missing_db_is_noop(tmp_path):
    # No DB file yet (init container hasn't migrated) — must not raise.
    stash_uid(str(tmp_path / "absent.db"), "licht an", "anna", matched=True)


def test_stash_missing_table_is_noop(tmp_path):
    path = str(tmp_path / "solaris.db")
    sqlite3.connect(path).close()  # DB exists, table doesn't
    stash_uid(path, "licht an", "anna", matched=True)  # OperationalError swallowed


def test_stash_on_a_premigration_table_keeps_routing_but_claims_nothing(tmp_path):
    # Independent deploys: this gatekeeper can start against a DB the 0031
    # migration hasn't reached. The row is still written (guest routing and
    # uid attribution keep working) in the legacy shape — which no reader can
    # read as a match, so the window fails closed rather than open.
    db = _db(tmp_path, _SCHEMA_LEGACY)
    stash_uid(db, "licht an", "anna", matched=True)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT uid FROM voice_uid_stash WHERE transcript = ?", ("licht an",)
    ).fetchone()
    cols = {c[1] for c in conn.execute("PRAGMA table_info(voice_uid_stash)")}
    conn.close()
    assert row == ("anna",)
    assert "matched" not in cols
