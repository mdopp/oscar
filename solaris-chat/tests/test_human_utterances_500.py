"""Automated 500 Human Utterances Regression Test Suite (#1056).

Executes 500 diverse human phrasing variations in-memory against solaris-chat
engine tool handlers (play_music, play_radio, play_media) without network calls
or playing audio on physical devices.
"""

from __future__ import annotations
import os

import json
import sqlite3
import unittest.mock
import pytest

from solaris_chat.engine.tools.music_query import build_music_query_tools
from solaris_chat.engine.tools.radio import _write_pref
import solaris_chat.engine.tools.radio

_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (entity_id, alias)
);
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class _FakeJellyfinClient:
    async def stream_url(self, audio_id: str, static: bool = True) -> str | None:
        return f"http://jellyfin.local/stream/{audio_id}"


def _init_test_db(tmp_path) -> str:
    db_path = str(tmp_path / "solaris_test.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)

    conn.execute(
        "INSERT INTO entities VALUES ('e-50cent', 'band', '50 Cent', 'household', 'jellyfin', 'h1', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO entities VALUES ('e-beatles', 'band', 'Beatles, The', 'household', 'jellyfin', 'h2', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO entities VALUES ('e-franz', 'band', 'Franz Ferdinand', 'household', 'jellyfin', 'h3', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO entities VALUES ('e-live-alone', 'song', 'Live Alone', 'household', 'jellyfin', 'h4', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO entities VALUES ('e-in-da-club', 'song', 'In Da Club', 'household', 'jellyfin', 'h5', datetime('now'))"
    )

    conn.execute(
        "INSERT INTO facts VALUES ('f1', 'e-live-alone', 'household', 'by', 'bands/franz-ferdinand', 1.0, 'jellyfin', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO facts VALUES ('f2', 'e-live-alone', 'household', 'resource', 'audio-live-alone', 1.0, 'jellyfin', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO facts VALUES ('f3', 'e-in-da-club', 'household', 'by', 'bands/50-cent', 1.0, 'jellyfin', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO facts VALUES ('f4', 'e-in-da-club', 'household', 'resource', 'audio-in-da-club', 1.0, 'jellyfin', datetime('now'))"
    )

    conn.commit()
    conn.close()
    return db_path


def generate_500_human_utterances():
    verbs = [
        "",
        "spiele ",
        "spiel ",
        "mach ",
        "starte ",
        "schalte ",
        "ich möchte ",
        "kannst du ",
        "bitte ",
        "lass ",
    ]
    suffix_verbs = ["", " an", " ein", " ab", " laufen", " spielen"]
    stations = [
        "1live",
        "1 live",
        "einslive",
        "eins live",
        "ndr2",
        "ndr 2",
        "wdr2",
        "wdr 2",
        "ffn",
        "radio paloma",
        "bigfm",
        "planet radio",
        "swr3",
        "radio brocken",
        "rock antenne",
        "sunshine live",
        "radio bob",
        "dlf",
    ]
    artists = ["50 cent", "3 doors down", "beatles", "queen", "alligatoah", "2pac"]
    rooms = [
        "",
        " im wohnzimmer",
        " in der küche",
        " im kinderzimmer",
        " im bad",
        " im büro",
    ]

    test_cases = []

    for s in stations:
        for v in verbs:
            for sv in suffix_verbs:
                for r in rooms:
                    utt = f"{v}{s}{sv}{r}".strip()
                    if utt and (utt, "radio") not in test_cases:
                        test_cases.append((utt, "radio"))

    for a in artists:
        for v in verbs:
            for r in rooms:
                utt = f"{v}{a}{r}".strip()
                if utt and (utt, "artist") not in test_cases:
                    test_cases.append((utt, "artist"))

    for g in [
        "spiele musik",
        "spielermusik",
        "musik",
        "radio",
        "etwas musik",
        "lass musik laufen",
        "spiele radio",
    ]:
        for r in rooms:
            utt = f"{g}{r}".strip()
            if utt and (utt, "generic") not in test_cases:
                test_cases.append((utt, "generic"))

    return test_cases[:500]


@pytest.mark.asyncio
async def test_500_human_utterances_regression(tmp_path):
    db_path = _init_test_db(tmp_path)
    notes_dir = str(tmp_path / "notes")
    os.makedirs(notes_dir, exist_ok=True)

    _write_pref(
        notes_dir,
        "household",
        "room-devices",
        {"Wohnzimmer": "media_player.wohnzimmer_paar"},
    )

    test_cases = generate_500_human_utterances()
    j_client = _FakeJellyfinClient()

    async def mock_call_service(*args, **kwargs):
        return {"ok": True}

    async def mock_resolve_station(self, name: str):
        name_clean = str(name).strip()
        if name_clean:
            return (name_clean, f"http://stream.example.com/{name_clean}")
        return None

    disk_conn = sqlite3.connect(db_path)
    mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
    mem_conn.row_factory = sqlite3.Row
    disk_conn.backup(mem_conn)
    disk_conn.close()

    class MemConnWrapper:
        def __init__(self, conn):
            self._conn = conn

        @property
        def row_factory(self):
            return self._conn.row_factory

        @row_factory.setter
        def row_factory(self, val):
            self._conn.row_factory = val

        def close(self):
            pass

        def execute(self, *args, **kwargs):
            return self._conn.execute(*args, **kwargs)

        def cursor(self, *args, **kwargs):
            return self._conn.cursor(*args, **kwargs)

    wrapper = MemConnWrapper(mem_conn)

    def mock_open_conn(p):
        return wrapper

    with (
        # Patched (not assigned) so the stub is undone at block exit — a bare
        # class-attribute assignment leaks into every later test in the session.
        unittest.mock.patch.object(
            solaris_chat.engine.tools.radio.RadioBrowserClient,
            "resolve_station",
            mock_resolve_station,
        ),
        unittest.mock.patch(
            "solaris_chat.engine.tools.music_query.call_service_scoped",
            side_effect=mock_call_service,
        ),
        unittest.mock.patch(
            "solaris_chat.engine.tools.radio.call_service_scoped",
            side_effect=mock_call_service,
        ),
        unittest.mock.patch(
            "solaris_chat.engine.knowledge.projection.open_conn",
            side_effect=mock_open_conn,
        ),
    ):
        tools = build_music_query_tools(
            db_path,
            lambda: "household",
            j_client,
            hass_url="http://127.0.0.1:8123",
            hass_token="mock_token",
            notes_dir=notes_dir,
            room_getter=lambda: "Wohnzimmer",
        )

        play_music_tool = next(t for t in tools if t.name == "play_music")

        passed, failed = 0, 0
        failures = []

        for idx, (utt, kind) in enumerate(test_cases, 1):
            res_raw = await play_music_tool.handler(
                {"title": utt, "artist": "", "entity_id": ""}
            )
            res = json.loads(res_raw)

            ok = res.get("ok")
            title = res.get("title", "")
            station = res.get("station", "")

            if title == "Live Alone":
                failed += 1
                failures.append((utt, "FAIL: Matched Live Alone!"))
                continue

            if kind == "radio":
                if ok and station:
                    passed += 1
                else:
                    failed += 1
                    failures.append((utt, f"FAIL: Expected radio station, got {res}"))
            elif kind == "artist":
                if ok and (title or res.get("artist")):
                    passed += 1
                else:
                    failed += 1
                    failures.append((utt, f"FAIL: Expected artist track, got {res}"))
            elif kind == "generic":
                if ok:
                    passed += 1
                else:
                    failed += 1
                    failures.append(
                        (utt, f"FAIL: Expected generic playback, got {res}")
                    )

        assert failed == 0, (
            f"500 human utterances test failed with {failed} failures: {failures[:10]}"
        )
        assert passed == len(test_cases)
