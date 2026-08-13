"""`calendar_create` writes a VEVENT straight to Radicale (#1125, ADR-09).

The tool must use the existing `dav_client` — not `ha_call_service` — and land in
the caller's own `{base}/{uid}/solaris/` collection, the same layout the deadline
sync defines. These tests capture the PUTs instead of issuing them.
"""

from __future__ import annotations

import dataclasses
import functools
import json
from datetime import datetime

import pytest

from solaris_chat.engine import date_parse
from solaris_chat.engine.tools import calendar_tools

_BASE = "https://caldav.example/dav"
# A fixed Monday. `date_parse` deliberately asks back for a bare weekday that IS
# today ("Donnerstag" on a Thursday means either), so without a frozen clock
# these cases would fail one day in seven.
_NOW = datetime(2026, 8, 10, 9, 0, tzinfo=date_parse.LOCAL_TZ)


class _FakeClient:
    """Stands in for HttpDavClient — records what would have been PUT."""

    puts: list[tuple[str, str, str]] = []
    ensured: list[str] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def ensure_calendar(self, url):
        _FakeClient.ensured.append(url)

    async def put_item(self, url, uid, body, **kwargs):
        _FakeClient.puts.append((url, uid, body))
        return f"{url}{uid}.ics"


def _configure(monkeypatch, **overrides):
    """Settings is a frozen dataclass — swap the module-level instance."""
    from solaris_chat import config

    monkeypatch.setattr(
        config, "settings", dataclasses.replace(config.settings, **overrides)
    )


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(
        date_parse, "resolve", functools.partial(date_parse.resolve, now=_NOW)
    )


@pytest.fixture
def dav(monkeypatch):
    _FakeClient.puts = []
    _FakeClient.ensured = []
    monkeypatch.setattr(calendar_tools, "HttpDavClient", _FakeClient)
    _configure(
        monkeypatch,
        deadlines_sync_url_base=_BASE,
        sync_dav_username="solaris",
        sync_dav_password="pw",
        household_calendar_uid="mdopp",
    )
    return _FakeClient


def _tool(uid="mdopp"):
    return calendar_tools.build_calendar_tools(lambda: uid)[0]


@pytest.mark.asyncio
async def test_writes_vevent_into_the_callers_own_collection(dav):
    out = json.loads(
        await _tool().handler({"title": "Zahnarzt", "when": "Donnerstag 15 Uhr"})
    )
    assert out["ok"] is True
    url, uid, body = dav.puts[0]
    assert url == f"{_BASE}/mdopp/solaris/"
    assert dav.ensured == [url]
    assert uid.startswith("solaris-event-")
    assert "BEGIN:VEVENT" in body
    assert "SUMMARY:Zahnarzt" in body
    assert "DTSTART;TZID=Europe/Berlin:" in body
    assert out["say"].startswith("Eingetragen: Zahnarzt am Donnerstag")


@pytest.mark.asyncio
async def test_household_uid_routes_to_the_primary_resident(dav):
    await _tool(uid="household").handler({"title": "Müll", "when": "morgen"})
    assert dav.puts[0][0] == f"{_BASE}/mdopp/solaris/"


@pytest.mark.asyncio
async def test_day_without_a_time_becomes_all_day(dav):
    await _tool().handler({"title": "Urlaub", "when": "nächsten Dienstag"})
    body = dav.puts[0][2]
    assert "DTSTART;VALUE=DATE:" in body


@pytest.mark.asyncio
async def test_ambiguous_when_asks_back_and_writes_nothing(dav):
    out = json.loads(await _tool().handler({"title": "Zahnarzt", "when": "um 4 Uhr"}))
    assert out["ok"] is False
    assert out["reason"] == "unklar"
    assert "nachmittags" in out["say"]
    assert dav.puts == []


@pytest.mark.asyncio
async def test_unconfigured_calendar_says_so_instead_of_failing(monkeypatch):
    monkeypatch.setattr(calendar_tools, "HttpDavClient", _FakeClient)
    _configure(monkeypatch, deadlines_sync_url_base="")
    out = json.loads(await _tool().handler({"title": "Zahnarzt", "when": "morgen"}))
    assert out["reason"] == "kalender_nicht_konfiguriert"


@pytest.mark.asyncio
async def test_dav_failure_answers_plainly(dav, monkeypatch):
    async def boom(self, url, uid, body, **kwargs):
        raise RuntimeError("503")

    monkeypatch.setattr(_FakeClient, "put_item", boom)
    out = json.loads(await _tool().handler({"title": "Zahnarzt", "when": "morgen"}))
    assert out["reason"] == "kalender_nicht_erreichbar"
    assert out["say"]
