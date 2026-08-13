"""Tests for the gatekeeper's STT-provider mode (#350, approach b).

When HA's Assist pipeline uses the gatekeeper as its Wyoming STT engine, the
client opens the turn with a `Transcribe` event and expects a `Transcript`
back. In that mode the gatekeeper must:

  * transcribe + resolve the speaking resident,
  * return the `Transcript` to HA (so its pipeline continues to
    conversation.solaris),
  * stash `{transcript -> uid}` for the engine facade to read,
  * and NOT do the full satellite turn (no facade POST, no piper TTS).

The wyoming-satellite path (no `Transcribe`) must keep doing the full turn.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from unittest.mock import AsyncMock

from gatekeeper import handler as handler_mod
from gatekeeper import uid_stash
from gatekeeper.handler import GatekeeperHandler
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop


_STASH_SCHEMA = """
CREATE TABLE voice_uid_stash (
    transcript TEXT PRIMARY KEY,
    uid        TEXT NOT NULL,
    matched    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _stash_db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_STASH_SCHEMA)
    conn.commit()
    conn.close()
    return path


class _StubInfo:
    def event(self):
        return "info-event"


def _audio_events():
    return [
        AudioStart(rate=16000, width=2, channels=1).event(),
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00").event(),
        AudioStop().event(),
    ]


async def _drive(handler: GatekeeperHandler, events) -> bool:
    handled = True
    for ev in events:
        handled = await handler.handle_event(ev)
    return handled


async def test_stt_mode_returns_transcript_and_stashes_uid(tmp_path, monkeypatch):
    db = _stash_db(tmp_path)
    monkeypatch.setattr(
        handler_mod,
        "settings",
        dataclasses.replace(handler_mod.settings, solaris_db_path=db),
    )

    handler = GatekeeperHandler(None, None, _StubInfo())
    handler.write_event = AsyncMock()
    handler._transcribe = AsyncMock(return_value="mach das licht an")
    handler._resolve_speaker = AsyncMock(
        return_value=handler_mod.SpeakerResolution("anna", attributed=True)
    )
    # Guard rails: the STT path must touch neither the facade nor TTS.
    handler._solaris.converse = AsyncMock()
    handler._synthesize_and_stream = AsyncMock()

    last = await _drive(handler, [Transcribe().event(), *_audio_events()])

    # The connection closes after the turn (AudioStop returned False).
    assert last is False
    # A Transcript carrying the text went back to HA.
    written = [
        Transcript.from_event(c.args[0]) for c in handler.write_event.await_args_list
    ]
    assert [t.text for t in written] == ["mach das licht an"]
    # The resolved resident was stashed under the transcript.
    assert uid_stash  # module imported for the side-channel
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT uid FROM voice_uid_stash WHERE transcript = ?", ("mach das licht an",)
    ).fetchone()
    conn.close()
    assert row[0] == "anna"
    # Never ran the full satellite turn.
    handler._solaris.converse.assert_not_awaited()
    handler._synthesize_and_stream.assert_not_awaited()


async def test_stt_mode_empty_transcript_returns_empty_no_stash(tmp_path, monkeypatch):
    db = _stash_db(tmp_path)
    monkeypatch.setattr(
        handler_mod,
        "settings",
        dataclasses.replace(handler_mod.settings, solaris_db_path=db),
    )

    handler = GatekeeperHandler(None, None, _StubInfo())
    handler.write_event = AsyncMock()
    handler._transcribe = AsyncMock(return_value="")
    handler._resolve_speaker = AsyncMock(
        return_value=handler_mod.SpeakerResolution("anna", attributed=True)
    )

    await _drive(handler, [Transcribe().event(), *_audio_events()])

    # HA still gets a (blank) Transcript so its pipeline doesn't hang.
    written = [
        Transcript.from_event(c.args[0]) for c in handler.write_event.await_args_list
    ]
    assert [t.text for t in written] == [""]
    # Nothing stashed (no resident resolution for an empty utterance).
    handler._resolve_speaker.assert_not_awaited()
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM voice_uid_stash").fetchone()[0]
    conn.close()
    assert n == 0


async def test_satellite_path_unchanged_no_transcribe(tmp_path, monkeypatch):
    # Without a Transcribe event the gatekeeper runs the full satellite turn:
    # transcribe -> facade POST -> TTS, and returns NO Transcript event.
    db = _stash_db(tmp_path)
    monkeypatch.setattr(
        handler_mod,
        "settings",
        dataclasses.replace(handler_mod.settings, solaris_db_path=db),
    )

    handler = GatekeeperHandler(None, None, _StubInfo())
    handler.write_event = AsyncMock()
    handler._transcribe = AsyncMock(return_value="hallo")
    handler._resolve_speaker = AsyncMock(
        return_value=handler_mod.SpeakerResolution("michael", attributed=False)
    )
    handler._resolve_location = AsyncMock(return_value=None)
    handler._solaris.converse = AsyncMock(return_value="Hallo zurück.")
    handler._synthesize_and_stream = AsyncMock()

    await _drive(handler, _audio_events())

    # Full turn ran.
    handler._solaris.converse.assert_awaited_once()
    handler._synthesize_and_stream.assert_awaited_once_with("Hallo zurück.")
    # No Transcript was returned (satellite contract), and an unattributed turn
    # stashes nothing — see test_speaker.py for the attributed counterpart.
    for call in handler.write_event.await_args_list:
        assert not Transcript.is_type(call.args[0].type)
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM voice_uid_stash").fetchone()[0]
    conn.close()
    assert n == 0
