"""Tests for the gatekeeper-side speaker stash writer (#350, #1152, #1218)."""

from __future__ import annotations

import sqlite3
import threading

import pytest
from gatekeeper.uid_stash import STASH_TTL_SECONDS, stash_uid


_SCHEMA = """
CREATE TABLE voice_uid_stash (
    transcript TEXT NOT NULL,
    room       TEXT NOT NULL DEFAULT '',
    uid        TEXT NOT NULL,
    matched    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (transcript, room)
);
"""

# The table as migration 0031 left it, before 0033 widened the key with the
# room. The gatekeeper and the schema-init sidecar deploy independently.
_SCHEMA_NO_ROOM = """
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


def _rows(db: str, transcript: str):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT room, uid, matched FROM voice_uid_stash WHERE transcript = ?"
        " ORDER BY room",
        (transcript,),
    ).fetchall()
    conn.close()
    return rows


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
    # A stale match can't survive under a fresh unknown-speaker row.
    assert _read(db, "licht an") == ("guest", 0)


def test_stash_refreshes_the_same_speakers_own_row(tmp_path):
    # The same resident re-stashing (their earlier turn never reached the
    # engine, so the row is still there) is not a collision — the row stays
    # theirs, claim intact.
    db = _db(tmp_path)
    stash_uid(db, "licht an", "anna", matched=True)
    stash_uid(db, "licht an", "anna", matched=True)
    assert _read(db, "licht an") == ("anna", 1)


# -- #1218: two residents, one transcript -----------------------------------


def test_stash_keeps_two_rooms_apart(tmp_path):
    # The satellite path knows the originating room and it rides the key, so
    # the same sentence from two rooms is two rows — each consumer still gets
    # its own resident.
    db = _db(tmp_path)
    stash_uid(db, "licht an", "anna", matched=True, room="Küche")
    stash_uid(db, "licht an", "bob", matched=True, room="Bad")
    assert _rows(db, "licht an") == [("bad", "bob", 1), ("küche", "anna", 1)]


def test_stash_fails_closed_on_a_live_collision_with_another_speaker(tmp_path):
    # Same transcript, same (unknown) room, two different residents inside the
    # window: neither may be handed the other's identity, so the row degrades
    # to the unknown speaker and both turns lose the personal scope.
    db = _db(tmp_path)
    stash_uid(db, "licht an", "anna", matched=True)
    stash_uid(db, "licht an", "bob", matched=True)
    assert _read(db, "licht an") == ("guest", 0)


def test_stash_fail_closed_row_survives_a_third_speaker(tmp_path):
    db = _db(tmp_path)
    stash_uid(db, "licht an", "anna", matched=True)
    stash_uid(db, "licht an", "bob", matched=True)
    stash_uid(db, "licht an", "carl", matched=True)
    assert _read(db, "licht an") == ("guest", 0)


def test_stash_overwrites_a_row_no_reader_could_still_consume(tmp_path):
    # Past the TTL the incumbent row is dead to every reader, so the next
    # speaker takes the key outright — an unconsumed leftover must not
    # permanently downgrade a later identical utterance.
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO voice_uid_stash (transcript, room, uid, matched, created_at) "
        "VALUES (?, '', ?, 1, datetime('now', ?))",
        ("licht an", "anna", f"-{STASH_TTL_SECONDS + 60} seconds"),
    )
    conn.commit()
    conn.close()
    stash_uid(db, "licht an", "bob", matched=True)
    assert _read(db, "licht an") == ("bob", 1)


def test_stash_normalizes_the_room_into_the_key(tmp_path):
    # Both ends normalise identically, so padding/case drift between the
    # gatekeeper's room name and HA's `[room: X]` prefix can't split one turn
    # into two rows.
    db = _db(tmp_path)
    stash_uid(db, "licht an", "anna", matched=True, room=" Küche ")
    stash_uid(db, "licht an", "anna", matched=True, room="küche")
    assert _rows(db, "licht an") == [("küche", "anna", 1)]


def test_stash_race_between_two_residents_never_hands_over_an_identity(tmp_path):
    # The actual concurrency: two satellites transcribe the same sentence and
    # both writers land inside the window. Whoever wins the lock, the row that
    # survives must not carry a resident's uid with a recognition claim on it.
    db = _db(tmp_path)
    barrier = threading.Barrier(2)

    def write(uid: str) -> None:
        barrier.wait()
        stash_uid(db, "licht an", uid, matched=True)

    threads = [threading.Thread(target=write, args=(u,)) for u in ("anna", "bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert _read(db, "licht an") == ("guest", 0)


def test_stash_on_a_prekey_table_still_fails_closed(tmp_path):
    # A DB the 0033 migration hasn't reached has no room column, so the key is
    # the transcript alone — the collision rule is what keeps that window safe.
    db = _db(tmp_path, _SCHEMA_NO_ROOM)
    stash_uid(db, "licht an", "anna", matched=True, room="Küche")
    assert _read(db, "licht an") == ("anna", 1)
    stash_uid(db, "licht an", "bob", matched=True, room="Bad")
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

    # The collision rule reaches the legacy shape too: a second speaker on the
    # same transcript degrades the row instead of taking it over.
    stash_uid(db, "licht an", "bob", matched=True)
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT uid FROM voice_uid_stash WHERE transcript = ?", ("licht an",)
    ).fetchone()
    conn.close()
    assert row == ("guest",)
