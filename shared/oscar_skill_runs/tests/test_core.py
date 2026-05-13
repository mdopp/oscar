"""Tests for the negation-detector heuristic + DB layer (mocked asyncpg)."""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oscar_skill_runs.core import (
    NEGATION_PREFIXES,
    append_run,
    detect_correction,
    looks_like_correction,
)


@pytest.mark.parametrize(
    "text",
    [
        "Nein, ich meinte dimmen",
        "nein.",
        "Stopp! falsche Lampe",
        "doch nicht das Wohnzimmer",
        "Lass das",
        "FALSCH",
        "wait, never mind",
        "Moment, falsches Zimmer",
        "Nicht das, das andere",
    ],
)
def test_looks_like_correction_matches_expected(text):
    assert looks_like_correction(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "stell den Wecker auf 7",
        "stopper kaufen",  # 'stopp' as a substring, not a prefix
        "neinverkrampft",  # 'nein' as a prefix without word boundary
        "",
        "   ",
        "ich möchte das anders",  # no negation prefix
    ],
)
def test_looks_like_correction_rejects_non_correction(text):
    assert looks_like_correction(text) is False


def test_negation_prefix_list_is_lowercase_and_unique():
    assert len(set(NEGATION_PREFIXES)) == len(NEGATION_PREFIXES)
    assert all(p == p.lower() for p in NEGATION_PREFIXES)


async def test_append_run_inserts_and_returns_uuid():
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        run_id = await append_run(
            "dsn",
            trace_id="11111111-1111-1111-1111-111111111111",
            uid="michael",
            endpoint="voice-pe:office",
            skill_name="oscar-light",
            utterance="mach das licht an",
            response="office light on",
            outcome="ok",
        )
    assert isinstance(run_id, str) and len(run_id) == 36
    conn.execute.assert_awaited_once()
    sql = conn.execute.await_args.args[0]
    assert "INSERT INTO skill_runs" in sql


async def test_append_run_rejects_bad_outcome():
    with pytest.raises(ValueError):
        await append_run(
            "dsn",
            trace_id="0",
            uid="x",
            endpoint="x",
            skill_name="x",
            utterance="x",
            response=None,
            outcome="weird",
        )


async def test_detect_correction_skips_non_negation():
    """No prefix match → never even opens a DB connection."""
    with patch("asyncpg.connect", AsyncMock()) as m:
        result = await detect_correction(
            "dsn", uid="michael", endpoint="voice-pe:office", utterance="hallo"
        )
    assert result is None
    m.assert_not_called()


async def test_detect_correction_writes_row_when_recent_run_exists():
    """A recent run + negation prefix → one skill_corrections row."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": "22222222-2222-2222-2222-222222222222",
            "skill_name": "oscar-light",
            "ts": dt.datetime(2026, 5, 13, tzinfo=dt.timezone.utc),
        }
    )
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        corr_id = await detect_correction(
            "dsn",
            uid="michael",
            endpoint="voice-pe:office",
            utterance="Nein, ich meinte dimmen",
        )
    assert corr_id is not None
    sql = conn.execute.await_args.args[0]
    assert "INSERT INTO skill_corrections" in sql


async def test_detect_correction_returns_none_when_no_recent_run():
    """Negation but no run in the last 30 s → no correction logged."""
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        result = await detect_correction(
            "dsn",
            uid="michael",
            endpoint="voice-pe:office",
            utterance="Nein, das war falsch",
        )
    assert result is None
    conn.execute.assert_not_awaited()
