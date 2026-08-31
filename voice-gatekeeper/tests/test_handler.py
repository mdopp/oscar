"""Tests for client-id derivation from the socket peer.

Regression guard for the previously-undefined `self.client_id` referenced
in the conversation endpoint: Wyoming exposes no client identity, so it is
derived from the connection peer here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart
from wyoming.info import Describe

from gatekeeper import handler as handler_mod
from gatekeeper.handler import (
    GUEST_UID,
    MAX_AUDIO_BYTES,
    GatekeeperHandler,
    SpeakerResolution,
    client_id_from_peername,
)


class _StubInfo:
    """Minimal stand-in for wyoming.info.Info — only `.event()` is used."""

    def event(self):
        return "info-event"


def test_client_id_from_tcp_peername():
    assert client_id_from_peername(("192.168.178.42", 53124)) == "192.168.178.42"


def test_client_id_from_unix_socket_path():
    assert client_id_from_peername("/run/wyoming/sat.sock") == "/run/wyoming/sat.sock"


def test_client_id_none_when_peer_missing():
    assert client_id_from_peername(None) is None
    assert client_id_from_peername(()) is None


def test_client_id_none_when_host_empty():
    assert client_id_from_peername(("", 5000)) is None


def test_handler_constructs_with_info_arg():
    # Regression: the server builds GatekeeperHandler(r, w, _info());
    # forwarding the 3rd arg to Wyoming's base used to raise TypeError.
    handler = GatekeeperHandler(None, None, _StubInfo())
    assert handler.client_id is None  # no writer -> peer unavailable
    assert isinstance(handler._info, _StubInfo)


async def test_handler_answers_describe():
    handler = GatekeeperHandler(None, None, _StubInfo())
    handler.write_event = AsyncMock()
    handled = await handler.handle_event(Describe().event())
    assert handled is True
    handler.write_event.assert_awaited_once_with("info-event")


async def test_audio_buffer_capped_and_connection_dropped():
    # A client that streams AudioChunks and never sends AudioStop used to grow
    # _audio_buffer for the life of the connection — an unauthenticated OOM on
    # the hostNetwork Wyoming port (#1174).
    handler = GatekeeperHandler(None, None, _StubInfo())
    await handler.handle_event(
        AudioStart(rate=16000, width=2, channels=1).event(),
    )
    frame = AudioChunk(
        rate=16000, width=2, channels=1, audio=b"\x00" * (1024 * 1024)
    ).event()

    for _ in range(MAX_AUDIO_BYTES // (1024 * 1024)):
        assert await handler.handle_event(frame) is True

    assert await handler.handle_event(frame) is False
    assert handler._audio_buffer == []


async def test_audio_start_resets_the_byte_count():
    handler = GatekeeperHandler(None, None, _StubInfo())
    await handler.handle_event(AudioStart(rate=16000, width=2, channels=1).event())
    await handler.handle_event(
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00" * 4096).event()
    )
    await handler.handle_event(AudioStart(rate=16000, width=2, channels=1).event())
    assert handler._audio_bytes == 0


class _RecordingWhisper:
    """Stands in for the Whisper Wyoming client, capturing what we send it."""

    def __init__(self, sent):
        self._sent = sent

    @classmethod
    def bind(cls, sent):
        class _Factory:
            @staticmethod
            def from_uri(_uri):
                return cls(sent)

        return _Factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def write_event(self, event):
        self._sent.append(event)

    async def read_event(self):
        return Transcript(text="turn on the light").event()


async def test_transcribe_uses_the_configured_system_language(monkeypatch):
    """SOLARIS_LANGUAGE must reach Whisper — "de" was hardcoded here (#1288)."""
    monkeypatch.setenv("SOLARIS_LANGUAGE", "en")
    sent = []
    monkeypatch.setattr(handler_mod, "AsyncClient", _RecordingWhisper.bind(sent))

    handler = GatekeeperHandler(None, None, _StubInfo())
    handler._audio_start = AudioStart(rate=16000, width=2, channels=1)
    handler._audio_buffer = [
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00")
    ]

    assert await handler._transcribe() == "turn on the light"
    opening = [e for e in sent if Transcribe.is_type(e.type)]
    assert Transcribe.from_event(opening[0]).language == "en"


async def _converse_kwargs_for(speaker: SpeakerResolution) -> dict:
    """Run one satellite turn and return the kwargs the handler sent to the
    engine client. The stub replies with "" so the turn ends before TTS."""
    solaris = AsyncMock()
    solaris.converse = AsyncMock(return_value="")
    handler = GatekeeperHandler(None, None, _StubInfo(), solaris)
    handler._audio_start = AudioStart(rate=16000, width=2, channels=1)
    handler._audio_buffer = [
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00")
    ]
    handler._transcribe = AsyncMock(return_value="wie spät ist es")
    handler._resolve_speaker = AsyncMock(return_value=speaker)
    handler._resolve_location = AsyncMock(return_value=None)
    handler._stash_speaker = AsyncMock()
    await handler._process_pipeline()
    return solaris.converse.await_args.kwargs


async def test_unmatched_speaker_turn_is_not_remembered():
    # The guest sentinel is shared by every unmatched voice, so its turns must
    # not be written to any conversation history (#1289).
    kwargs = await _converse_kwargs_for(SpeakerResolution(GUEST_UID, attributed=True))
    assert kwargs["uid"] == GUEST_UID
    assert kwargs["remember"] is False


async def test_resident_turn_is_remembered():
    kwargs = await _converse_kwargs_for(
        SpeakerResolution("michael", attributed=True, matched=True)
    )
    assert kwargs["remember"] is True


async def test_unattributed_fallback_turn_is_still_remembered():
    # Speaker-ID off / no extractor falls back to the configured resident uid —
    # a real person, so their rolling history is unaffected by the guest fix.
    kwargs = await _converse_kwargs_for(SpeakerResolution("michael"))
    assert kwargs["remember"] is True
