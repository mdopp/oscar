"""Tests for the inbound envelope handler (no real signal-cli, no real DB)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_gateway.inbound import UNKNOWN_NUMBER_REPLY, _extract_text, handle_envelope


@pytest.fixture
def signal_mock():
    m = MagicMock()
    m.send = AsyncMock()
    return m


@pytest.fixture
def hermes_mock():
    m = MagicMock()
    m.converse = AsyncMock(return_value="OSCAR sagt hi")
    return m


def test_extract_text_pulls_data_message():
    env = {
        "envelope": {
            "source": "+4915112345678",
            "dataMessage": {"message": "Hallo OSCAR"},
        }
    }
    assert _extract_text(env) == ("+4915112345678", "Hallo OSCAR")


def test_extract_text_ignores_typing_envelope():
    env = {"envelope": {"source": "+49…", "typingMessage": {"action": "STARTED"}}}
    assert _extract_text(env) is None


def test_extract_text_ignores_empty_message():
    env = {"envelope": {"source": "+49…", "dataMessage": {"message": ""}}}
    assert _extract_text(env) is None


async def test_known_number_forwards_to_hermes(signal_mock, hermes_mock):
    env = {
        "envelope": {
            "source": "+4915112345678",
            "dataMessage": {"message": "wie spät ist es?"},
        }
    }
    with patch(
        "signal_gateway.inbound.lookup_uid",
        AsyncMock(return_value=("michael", "Michael")),
    ):
        await handle_envelope(
            env, signal=signal_mock, hermes=hermes_mock, postgres_dsn="dsn"
        )
    hermes_mock.converse.assert_awaited_once()
    args = hermes_mock.converse.await_args.kwargs
    assert args["uid"] == "michael"
    assert args["endpoint"] == "signal:+4915112345678"
    assert args["text"] == "wie spät ist es?"
    signal_mock.send.assert_awaited_once_with("+4915112345678", "OSCAR sagt hi")


async def test_unknown_number_replies_with_link_hint(signal_mock, hermes_mock):
    env = {"envelope": {"source": "+490000", "dataMessage": {"message": "hi"}}}
    with patch("signal_gateway.inbound.lookup_uid", AsyncMock(return_value=None)):
        await handle_envelope(
            env, signal=signal_mock, hermes=hermes_mock, postgres_dsn="dsn"
        )
    hermes_mock.converse.assert_not_awaited()
    signal_mock.send.assert_awaited_once_with("+490000", UNKNOWN_NUMBER_REPLY)


async def test_empty_hermes_reply_is_swallowed(signal_mock, hermes_mock):
    """HERMES returning '' shouldn't push a blank message — let user retry."""
    hermes_mock.converse.return_value = ""
    env = {"envelope": {"source": "+49…", "dataMessage": {"message": "x"}}}
    with patch(
        "signal_gateway.inbound.lookup_uid", AsyncMock(return_value=("michael", None))
    ):
        await handle_envelope(
            env, signal=signal_mock, hermes=hermes_mock, postgres_dsn="dsn"
        )
    signal_mock.send.assert_not_awaited()
