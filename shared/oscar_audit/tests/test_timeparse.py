"""Tests for the relative-time parser."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from oscar_audit.timeparse import parse_since


_NOW = datetime(2026, 5, 12, 14, 30, 0, tzinfo=timezone.utc)


def test_relative_hours():
    assert parse_since("1h", now=_NOW) == _NOW - timedelta(hours=1)
    assert parse_since("24h", now=_NOW) == _NOW - timedelta(hours=24)


def test_relative_days_and_weeks():
    assert parse_since("7d", now=_NOW) == _NOW - timedelta(days=7)
    assert parse_since("2w", now=_NOW) == _NOW - timedelta(weeks=2)


def test_today_returns_local_midnight_utc():
    assert parse_since("today", now=_NOW) == datetime(
        2026, 5, 12, 0, 0, tzinfo=timezone.utc
    )


def test_yesterday_returns_prior_day_midnight():
    assert parse_since("yesterday", now=_NOW) == datetime(
        2026, 5, 11, 0, 0, tzinfo=timezone.utc
    )


def test_iso8601_passthrough():
    assert parse_since("2026-05-01T08:00:00") == datetime(
        2026, 5, 1, 8, 0, tzinfo=timezone.utc
    )


def test_unparseable_raises():
    with pytest.raises(ValueError):
        parse_since("eine Stunde")
