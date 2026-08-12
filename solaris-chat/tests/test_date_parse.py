"""Deterministic German date/time resolution (#1126, guideline G-1).

Every case is pinned against a fixed `now` — Monday 2026-08-03, 10:00 Europe/Berlin
— so the resolution is the code's, never the model's. The ambiguity cases prove
Solaris asks back instead of inventing a date.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from solaris_chat.engine import date_parse

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=date_parse.LOCAL_TZ)  # a Monday


def r(expression: str):
    return date_parse.resolve(expression, now=NOW)


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("heute", date(2026, 8, 3)),
        ("morgen", date(2026, 8, 4)),
        ("übermorgen", date(2026, 8, 5)),
        ("Dienstag", date(2026, 8, 4)),
        ("am Sonntag", date(2026, 8, 9)),
        ("nächsten Dienstag", date(2026, 8, 11)),
        ("kommenden Freitag", date(2026, 8, 14)),
        ("in zwei Wochen", date(2026, 8, 17)),
        ("in 3 Tagen", date(2026, 8, 6)),
        ("in einem Monat", date(2026, 9, 3)),
        ("am 5.9.", date(2026, 9, 5)),
        ("am 5. September", date(2026, 9, 5)),
        ("am 1.3.", date(2027, 3, 1)),  # already past this year → next year
        ("2026-12-24", date(2026, 12, 24)),
    ],
)
def test_relative_days_resolve(expression, expected):
    res = r(expression)
    assert res.question == ""
    assert res.day == expected


@pytest.mark.parametrize(
    "expression,day,at",
    [
        ("Donnerstag 15 Uhr", date(2026, 8, 6), time(15, 0)),
        ("Donnerstag um 15:30", date(2026, 8, 6), time(15, 30)),
        ("morgen 9 Uhr", date(2026, 8, 4), time(9, 0)),
        ("heute Abend", date(2026, 8, 3), time(19, 0)),
        ("morgen früh", date(2026, 8, 4), time(8, 0)),
        ("übermorgen mittags", date(2026, 8, 5), time(12, 0)),
        ("morgen um 3 Uhr nachmittags", date(2026, 8, 4), time(15, 0)),
        ("morgen 19.30 Uhr", date(2026, 8, 4), time(19, 30)),
        ("um 18 Uhr", date(2026, 8, 3), time(18, 0)),  # no day → still today
    ],
)
def test_times_resolve(expression, day, at):
    res = r(expression)
    assert res.question == ""
    assert (res.day, res.at) == (day, at)


def test_day_without_time_is_all_day():
    res = r("nächsten Dienstag")
    assert res.at is None
    assert res.start() is None


def test_start_is_household_timezone_aware():
    start = r("Donnerstag 15 Uhr").start()
    assert start == datetime(2026, 8, 6, 15, 0, tzinfo=date_parse.LOCAL_TZ)


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "irgendwann",
        "nächste Woche",  # which day?
        "Montag",  # today IS Monday
        "um 4 Uhr",  # 04:00 or 16:00?
        "morgen um 30 Uhr",
        "um 8 Uhr",  # 08:00 today is already past
    ],
)
def test_ambiguous_expressions_ask_back(expression):
    res = r(expression)
    assert res.question
    assert res.day is None


def test_qualified_weekday_beats_bare_weekday_on_the_same_day():
    """On a Monday, "Montag" is ambiguous but "nächsten Montag" is not."""
    assert r("Montag").question
    assert r("nächsten Montag").day == date(2026, 8, 10)


def test_month_end_clamps():
    res = date_parse.resolve(
        "in einem Monat",
        now=datetime(2026, 1, 31, 10, 0, tzinfo=date_parse.LOCAL_TZ),
    )
    assert res.day == date(2026, 2, 28)


def test_format_de():
    assert date_parse.format_de(date(2026, 8, 6), time(15, 0)) == (
        "Donnerstag, 06.08. um 15:00"
    )
    assert date_parse.format_de(date(2026, 8, 6), None) == "Donnerstag, 06.08."
