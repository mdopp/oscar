"""YouTube-Music import job → `wishlist`/`play_count` album facts (#868, P3).

Covers the P3 slice wiring `music_shopping` into a durable import-job kind:
  - the `music` kind is registered in the importer REGISTRY + the jobs runner;
  - a job run over a `watch-history.json` writes source=import `wishlist` +
    `play_count` facts onto album entities (resolved/created by P1a's
    "Artist – Album" canonical_name/slug), surfaced by `music_query op="wishlist"`
    minus what is owned/digital;
  - the Hörspiel/Podcast classification is mechanical (shipped seed lists);
  - the run is idempotent (re-run updates facts, no duplicate album nodes) and
    per-resident (owner-scoped).

ytmusicapi is mocked via the on-disk album cache (no network). Schema mirrors the
affinity/ingest tests.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from solaris_chat.engine.importers import jobs as jobs_mod
from solaris_chat.engine.importers.google_takeout import REGISTRY, catalog
from solaris_chat.engine.importers.jobs import JobRunner, registered_kind
from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.tools.music_query import build_music_query_tools

_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL,
  PRIMARY KEY (entity_id, alias),
  FOREIGN KEY (entity_id) REFERENCES entities (id));
CREATE INDEX entity_aliases_alias_idx ON entity_aliases (alias);
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (subject_entity_id) REFERENCES entities (id));
CREATE INDEX facts_subject_predicate_idx ON facts (subject_entity_id, predicate);
CREATE TABLE events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, resident_uid TEXT NOT NULL,
  kind TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE event_entities (
  event_id TEXT NOT NULL, entity_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (event_id, entity_id, role));
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, ref_id TEXT NOT NULL,
  ref_kind TEXT NOT NULL CHECK (ref_kind IN ('entity', 'event')),
  okf_path TEXT NOT NULL, embedding_id TEXT, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE ingest_log (
  source TEXT NOT NULL, external_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (source, external_id));
CREATE TABLE engine_import_jobs (
  id TEXT PRIMARY KEY, owner_uid TEXT NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', payload TEXT NOT NULL DEFAULT '{}',
  progress TEXT NOT NULL DEFAULT '{}', error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')));
"""


def _entry(title, artist, vid, topic=True):
    name = f"{artist} - Topic" if topic else artist
    return {
        "header": "YouTube Music",
        "title": title + " angesehen",
        "titleUrl": f"https://music.youtube.com/watch?v={vid}",
        "subtitles": [{"name": name}],
        "time": "2026-07-15T10:00:00.000Z",
    }


_HIST = json.dumps(
    [
        _entry("Anti-Hero", "Taylor Swift", "vidA"),
        _entry("Anti-Hero", "Taylor Swift", "vidA"),  # 2 plays
        _entry("Karma", "Taylor Swift", "vidB"),
    ]
).encode()


@pytest.fixture
def env(tmp_path):
    """A db (with entities/facts + engine_import_jobs) + notes + on-disk trees."""
    db_path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    music_dir = tmp_path / "music"
    data_dir = tmp_path / "data"
    music_dir.mkdir()
    data_dir.mkdir()
    # Seed the ytmusicapi album cache so resolution never hits the network.
    (data_dir / "ytmusic_album_cache.json").write_text(
        json.dumps(
            {
                "vidA": {"album": "Midnights", "artist": "Taylor Swift"},
                "vidB": {"album": "Midnights", "artist": "Taylor Swift"},
            }
        )
    )
    return db_path, str(tmp_path / "notes"), str(music_dir), str(data_dir)


def _payload(db_path, notes_dir, music_dir, data_dir, owner="mdopp"):
    return {
        "history": _HIST.decode("utf-8"),
        "owner_uid": owner,
        "db_path": db_path,
        "notes_dir": notes_dir,
        "music_dir": music_dir,
        "data_dir": data_dir,
        "model": "gemma4:e4b",
        "cap": 0,  # resolution comes from the seeded cache, no network
    }


def _run_job(db_path, payload, owner="mdopp"):
    import time

    r = JobRunner(db_path)
    jid = r.start(owner, "music", payload)
    for _ in range(400):
        snap = r.get(jid, owner)
        if snap and snap["status"] in {"done", "failed"}:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"job never finished: {r.get(jid, owner)}")


def _album_facts(db_path, name):
    conn = projection.open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE type = 'album' AND canonical_name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return {
            (f["predicate"], f["value"], f["source"])
            for f in conn.execute(
                "SELECT predicate, value, source FROM facts"
                " WHERE subject_entity_id = ?",
                (row["id"],),
            ).fetchall()
        }
    finally:
        conn.close()


# --- registration ------------------------------------------------------------


def test_music_kind_registered():
    assert "music" in REGISTRY
    assert registered_kind("music")
    assert "music" in jobs_mod._RUNNERS


# --- job run → facts → wishlist query ----------------------------------------


def test_job_writes_wishlist_and_play_count_facts(env):
    db_path, notes_dir, music_dir, data_dir = env
    snap = _run_job(db_path, _payload(db_path, notes_dir, music_dir, data_dir))
    assert snap["status"] == "done"
    assert snap["result"]["albums_written"] == 1
    # The album entity was created with source=import wishlist + play_count facts
    # (Anti-Hero 2× + Karma 1× on Midnights → 3 plays).
    assert _album_facts(db_path, "Taylor Swift – Midnights") == {
        ("wishlist", "", "import"),
        ("play_count", "3", "import"),
    }


async def test_wishlist_query_surfaces_imported_album(env):
    db_path, notes_dir, music_dir, data_dir = env
    _run_job(db_path, _payload(db_path, notes_dir, music_dir, data_dir))
    (tool,) = build_music_query_tools(db_path, lambda: "mdopp")
    out = json.loads(await tool.handler({"op": "wishlist"}))
    assert out["albums"] == ["Taylor Swift – Midnights"]


async def test_wishlist_suppresses_digitally_present_album(env):
    db_path, notes_dir, music_dir, data_dir = env
    _run_job(db_path, _payload(db_path, notes_dir, music_dir, data_dir))
    # A Jellyfin-projected song links the album via on_album → the album is
    # digitally present, so the wishlist (op=wishlist filters !has_digital) drops it.
    conn = projection.open_conn(db_path)
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES ('s1', 'song', 'Anti-Hero', 'mdopp', 'jellyfin', 'h')"
    )
    # album okf_path must match the album's slug so _album_is_digital resolves it.
    row = conn.execute(
        "SELECT id FROM entities WHERE type='album'"
        " AND canonical_name='Taylor Swift – Midnights'"
    ).fetchone()
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash)"
        " VALUES ('c1', ?, 'entity', 'okf/albums/taylor-swift-midnights.md', 'h')",
        (row["id"],),
    )
    conn.execute(
        "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate, value,"
        " source) VALUES ('f1', 's1', 'mdopp', 'on_album',"
        " 'albums/taylor-swift-midnights', 'jellyfin')"
    )
    conn.commit()
    conn.close()
    (tool,) = build_music_query_tools(db_path, lambda: "mdopp")
    out = json.loads(await tool.handler({"op": "wishlist"}))
    assert out["albums"] == []


# --- idempotency + owner scope -----------------------------------------------


def test_rerun_is_idempotent_no_duplicate_album(env):
    db_path, notes_dir, music_dir, data_dir = env
    _run_job(db_path, _payload(db_path, notes_dir, music_dir, data_dir))
    snap2 = _run_job(db_path, _payload(db_path, notes_dir, music_dir, data_dir))
    # Second run: unchanged facts short-circuit (ingest_log), nothing re-written.
    assert snap2["result"]["albums_written"] == 0
    conn = projection.open_conn(db_path)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM entities WHERE type='album'"
    ).fetchone()["n"]
    conn.close()
    assert n == 1


def test_facts_are_owner_scoped(env):
    db_path, notes_dir, music_dir, data_dir = env
    _run_job(
        db_path, _payload(db_path, notes_dir, music_dir, data_dir, owner="lena"), "lena"
    )
    # lena's album exists; mdopp sees nothing of it.
    conn = projection.open_conn(db_path)
    row = conn.execute(
        "SELECT resident_uid FROM entities WHERE type='album'"
    ).fetchone()
    conn.close()
    assert row["resident_uid"] == "lena"


# --- classification: mechanical seed list ------------------------------------


def test_classify_uses_mechanical_seed_list():
    # Deterministic seed-list matching — no LLM in the path.
    assert catalog.classify("Fest & Flauschig", "Folge 300") == "Podcast"
    assert catalog.classify("Benjamin Blümchen", "Der Zoo") == "Hörspiel"
    assert catalog.classify("Taylor Swift", "Anti-Hero") is None
