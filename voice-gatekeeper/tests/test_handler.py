"""Tests for client-id derivation from the socket peer.

Regression guard for the previously-undefined `self.client_id` referenced
in the conversation endpoint: Wyoming exposes no client identity, so it is
derived from the connection peer here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from wyoming.audio import AudioChunk, AudioStart
from wyoming.info import Describe

from gatekeeper.handler import (
    MAX_AUDIO_BYTES,
    GatekeeperHandler,
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
