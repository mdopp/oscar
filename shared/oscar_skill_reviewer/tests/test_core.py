"""Aggregation + rate-limit checks (Postgres mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from oscar_skill_reviewer.core import (
    K_THRESHOLD,
    REVIEWER_RATE_LIMIT_S,
    _normalize_prefix,
    aggregate_corrections,
    can_apply_now,
)


def test_normalize_prefix_lowercases_and_keeps_first_5_tokens():
    assert (
        _normalize_prefix("Mach das LICHT, bitte schnell!")
        == "mach das licht bitte schnell"
    )
    assert (
        _normalize_prefix("Nein, ich meinte dimmen, viel weniger")
        == "nein ich meinte dimmen viel"
    )
    assert _normalize_prefix("") == ""


def test_normalize_prefix_punctuation_independent():
    a = _normalize_prefix("nein, ich meinte das")
    b = _normalize_prefix("Nein! Ich meinte das.")
    assert a == b


async def _stub_fetch(rows):
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    return conn


async def test_aggregate_returns_only_groups_at_or_above_k():
    # All three light rows normalise to the same (utterance_prefix,
    # correction_prefix) — 4 tokens each, after lowercase + punctuation strip.
    rows = [
        _row("oscar-light", "mach das licht hell", "nein, ich meinte dimmen"),
        _row("oscar-light", "Mach das Licht hell!", "Nein! Ich meinte dimmen."),
        _row("oscar-light", "MACH das licht hell.", "nein ich meinte dimmen"),
        # Different skill, only 2 entries — below k=3
        _row("oscar-timer", "stell den timer", "nein, ich meinte alarm"),
        _row("oscar-timer", "stell den timer", "nein, ich meinte alarm"),
    ]
    conn = await _stub_fetch(rows)
    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        groups = await aggregate_corrections(dsn="dsn", k=3)
    assert len(groups) == 1
    g = groups[0]
    assert g.skill_name == "oscar-light"
    assert g.count == 3
    assert len(g.correction_ids) == 3


async def test_aggregate_empty_when_no_pending_rows():
    conn = await _stub_fetch([])
    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        assert await aggregate_corrections(dsn="dsn") == []


async def test_can_apply_blocks_when_reviewer_edit_in_last_24h():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"reviewer_recent": True, "user_recent": False}
    )
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        assert await can_apply_now(dsn="dsn", skill_name="oscar-light") is False


async def test_can_apply_blocks_when_user_edit_in_last_24h():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"reviewer_recent": False, "user_recent": True}
    )
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        assert await can_apply_now(dsn="dsn", skill_name="oscar-light") is False


async def test_can_apply_ok_when_neither_recent():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"reviewer_recent": False, "user_recent": False}
    )
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        assert await can_apply_now(dsn="dsn", skill_name="oscar-light") is True


def test_constants_match_design():
    assert K_THRESHOLD == 3
    assert REVIEWER_RATE_LIMIT_S == 24 * 60 * 60


def _row(skill_name: str, utterance: str, correction: str):
    """Minimal duck-typed row that responds to ['key']."""
    import uuid as _u

    return {
        "correction_id": _u.uuid4(),
        "run_id": _u.uuid4(),
        "correction_utterance": correction,
        "skill_name": skill_name,
        "utterance": utterance,
    }
