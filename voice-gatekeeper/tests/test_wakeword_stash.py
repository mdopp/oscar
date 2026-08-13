"""Tests for wake-word sample capture (#1060): the gatekeeper-side store and
the handler capture path.

Stage 2 of the onboarding wizard used to file `wakeword_samples` rows pointing
at `.wav` files nobody ever wrote — the sample directory on the box was empty.
The gatekeeper now writes each turn's PCM there and only then counts it, so the
wizard's countdown can never run ahead of the recorded audio.
"""

from __future__ import annotations

import dataclasses
import os
import sqlite3
import wave
from unittest.mock import AsyncMock

from gatekeeper import handler as handler_mod
from gatekeeper import wakeword_stash
from gatekeeper.handler import GatekeeperHandler
from wyoming.asr import Transcribe
from wyoming.audio import AudioChunk, AudioStart, AudioStop

_SCHEMA = """
CREATE TABLE voice_uid_stash (
    transcript TEXT PRIMARY KEY,
    uid        TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE wakeword_requests (
    uid             TEXT PRIMARY KEY,
    target_count    INTEGER NOT NULL DEFAULT 10,
    collected_count INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# One 10 ms frame of 16 kHz mono int16 — what HA's STT client sends.
_FRAME = b"\x01\x00" * 160


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _request(db: str, *, uid: str = "alex", target: int = 10, collected: int = 0):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO wakeword_requests (uid, target_count, collected_count, status)"
        " VALUES (?, ?, ?, 'active')",
        (uid, target, collected),
    )
    conn.commit()
    conn.close()


def _row(db: str, uid: str = "alex"):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT collected_count, status FROM wakeword_requests WHERE uid = ?", (uid,)
    ).fetchone()
    conn.close()
    return row


class _StubInfo:
    def event(self):
        return "info-event"


async def _turn(handler: GatekeeperHandler):
    events = [
        Transcribe().event(),
        AudioStart(rate=16000, width=2, channels=1).event(),
        AudioChunk(rate=16000, width=2, channels=1, audio=_FRAME).event(),
        AudioStop().event(),
    ]
    for ev in events:
        await handler.handle_event(ev)


def _new_handler(db: str, monkeypatch, *, transcript: str = "Solaris"):
    monkeypatch.setattr(
        handler_mod,
        "settings",
        dataclasses.replace(handler_mod.settings, solaris_db_path=db),
    )
    monkeypatch.setattr(handler_mod, "get_extractor", lambda: None)
    h = GatekeeperHandler(None, None, _StubInfo())
    h.write_event = AsyncMock()
    h._transcribe = AsyncMock(return_value=transcript)
    h._resolve_speaker = AsyncMock(
        return_value=handler_mod.SpeakerResolution("alex", attributed=True)
    )
    return h


# --- store ------------------------------------------------------------------


def test_claim_returns_the_active_request(tmp_path):
    db = _db(tmp_path)
    _request(db, collected=2)
    req = wakeword_stash.claim_active_request(db)
    assert req is not None
    assert (req.uid, req.target_count, req.collected_count) == ("alex", 10, 2)


def test_claim_ignores_stale_request(tmp_path):
    db = _db(tmp_path)
    _request(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE wakeword_requests SET updated_at = datetime('now', ?)",
        (f"-{wakeword_stash.WAKEWORD_TTL_SECONDS + 30} seconds",),
    )
    conn.commit()
    conn.close()
    assert wakeword_stash.claim_active_request(db) is None


def test_claim_ignores_finished_and_complete_requests(tmp_path):
    db = _db(tmp_path)
    _request(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE wakeword_requests SET status = 'finished'")
    conn.commit()
    conn.close()
    assert wakeword_stash.claim_active_request(db) is None

    conn = sqlite3.connect(db)
    conn.execute("UPDATE wakeword_requests SET status = 'active', collected_count = 10")
    conn.commit()
    conn.close()
    assert wakeword_stash.claim_active_request(db) is None


def test_claim_missing_db_is_none(tmp_path):
    assert wakeword_stash.claim_active_request(str(tmp_path / "absent.db")) is None


def test_missing_table_is_a_noop(tmp_path):
    path = str(tmp_path / "bare.db")
    sqlite3.connect(path).close()
    assert wakeword_stash.claim_active_request(path) is None
    assert wakeword_stash.record_sample(path, "alex") == 0
    assert wakeword_stash.record_sample(str(tmp_path / "absent.db"), "alex") == 0


def test_record_sample_completes_at_the_target(tmp_path):
    db = _db(tmp_path)
    _request(db, target=2)
    assert wakeword_stash.record_sample(db, "alex") == 1
    assert _row(db) == (1, "active")
    assert wakeword_stash.record_sample(db, "alex") == 2
    assert _row(db) == (2, "completed")


def test_sample_path_mirrors_the_engine_convention(tmp_path):
    db = str(tmp_path / "solaris.db")
    assert wakeword_stash.sample_path(db, "alex", 3) == str(
        tmp_path / "wakeword" / "user_samples" / "alex" / "sample_alex_3.wav"
    )


def test_write_sample_produces_a_readable_wav(tmp_path):
    path = str(tmp_path / "nested" / "sample.wav")
    wakeword_stash.write_sample(path, _FRAME, rate=16000, width=2, channels=1)
    with wave.open(path, "rb") as wav:
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2
        assert wav.getnchannels() == 1
        assert wav.readframes(160) == _FRAME


# --- handler capture path ---------------------------------------------------


async def test_turn_writes_the_wav_and_counts_it(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _request(db, target=2)

    await _turn(_new_handler(db, monkeypatch))

    path = wakeword_stash.sample_path(db, "alex", 1)
    assert os.path.exists(path)
    with wave.open(path, "rb") as wav:
        assert wav.getnframes() == 160
    assert _row(db) == (1, "active")

    await _turn(_new_handler(db, monkeypatch))
    assert os.path.exists(wakeword_stash.sample_path(db, "alex", 2))
    assert _row(db) == (2, "completed")


async def test_no_request_records_nothing(tmp_path, monkeypatch):
    db = _db(tmp_path)

    await _turn(_new_handler(db, monkeypatch))

    assert not os.path.exists(os.path.join(os.path.dirname(db), "wakeword"))


async def test_silent_turn_is_not_a_sample(tmp_path, monkeypatch):
    """An empty transcript is silence or a dropped turn — counting it would
    hand the trainer a `.wav` with no wake word in it."""
    db = _db(tmp_path)
    _request(db)

    await _turn(_new_handler(db, monkeypatch, transcript=""))

    assert _row(db) == (0, "active")


async def test_failed_write_does_not_count_a_sample(tmp_path, monkeypatch):
    """The count is what the wizard reads back, so it may only move once the
    audio is on disk — a full or read-only volume must not fake a sample."""
    db = _db(tmp_path)
    _request(db)

    def _boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(handler_mod, "write_wakeword_sample", _boom)
    await _turn(_new_handler(db, monkeypatch))

    assert _row(db) == (0, "active")
