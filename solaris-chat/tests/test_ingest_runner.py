"""On-boot OKF ingest trigger (#517).

The runner is the missing trigger that actually runs the Phase-1 adapters on
the box. These tests prove: (1) the Obsidian adapter runs against the local
vault and produces real OKF concept files + projection rows (the core
acceptance — okf/*.md exists, entities/ingest_log > 0); (2) Immich runs only
when configured and is mocked (no live network in CI); (3) CalDAV/CardDAV and
an unconfigured Immich degrade gracefully (log + skip, no crash); (4) one
adapter raising never crashes the trigger.

Schema is built from inlined DDL mirroring the #446 migration (importing
alembic from a solaris-chat test fails CI's clean env).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import aiohttp
import pytest

from solaris_chat.engine import ingest
from solaris_chat.engine.ingest import runner


# Mirrors database/migrations/versions/20260615_0016_okf_knowledge_index.py.
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
CREATE INDEX events_ts_idx ON events (ts);
CREATE INDEX events_resident_ts_idx ON events (resident_uid, ts);
CREATE TABLE event_entities (
  event_id TEXT NOT NULL, entity_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (event_id, entity_id, role),
  FOREIGN KEY (event_id) REFERENCES events (id),
  FOREIGN KEY (entity_id) REFERENCES entities (id));
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, ref_id TEXT NOT NULL,
  ref_kind TEXT NOT NULL CHECK (ref_kind IN ('entity', 'event')),
  okf_path TEXT NOT NULL, embedding_id TEXT, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE ingest_log (
  source TEXT NOT NULL, external_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (source, external_id));
CREATE INDEX ingest_log_source_external_idx ON ingest_log (source, external_id);
"""


@dataclass
class FakeSettings:
    solaris_db_path: str
    notes_dir: str
    default_uid: str = "household"
    immich_base_url: str = ""
    immich_api_key: str = ""
    caldav_url: str = ""
    caldav_username: str = ""
    caldav_password: str = ""
    carddav_url: str = ""
    carddav_username: str = ""
    carddav_password: str = ""
    jellyfin_url: str = ""
    jellyfin_username: str = ""
    jellyfin_password: str = ""
    jellyfin_library_owners: dict = field(default_factory=dict)
    imap_accounts: tuple = ()
    paperless_url: str = ""
    paperless_token: str = ""


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "solaris.db")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path, notes_dir


def _seed_vault(notes_dir):
    """A small hand-written vault: a person note + a diary that links it."""
    (notes_dir / "people").mkdir()
    (notes_dir / "people" / "anna.md").write_text(
        "---\ntype: person\ntitle: Anna\n---\n\n# Anna\n\nA friend.\n",
        encoding="utf-8",
    )
    (notes_dir / "diary.md").write_text(
        "---\ntitle: Diary\n---\n\nMet [[Anna]] today.\n", encoding="utf-8"
    )


def _counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("entities", "ingest_log", "concepts", "facts", "events")
        }
    finally:
        conn.close()


# --- the core acceptance: Obsidian produces real OKF concepts ----------------


async def test_run_ingest_obsidian_produces_okf_and_projection(env):
    db_path, notes_dir = env
    _seed_vault(notes_dir)
    await runner.run_ingest(
        FakeSettings(solaris_db_path=db_path, notes_dir=str(notes_dir))
    )
    # okf/*.md exists with real concepts.
    assert (notes_dir / "okf" / "people" / "anna.md").is_file()
    assert (notes_dir / "okf" / "notes" / "diary.md").is_file()
    # entities / ingest_log / concepts have > 0 rows.
    counts = _counts(db_path)
    assert counts["entities"] >= 2
    assert counts["ingest_log"] >= 2
    assert counts["concepts"] >= 2


async def test_run_ingest_enqueues_concept_embeddings(env, tmp_path):
    db_path, notes_dir = env
    _seed_vault(notes_dir)
    await runner.run_ingest(
        FakeSettings(solaris_db_path=db_path, notes_dir=str(notes_dir))
    )
    # The runner uses the durable PendingEmbeddingQueue -> an append-only JSONL
    # sidecar with > 0 lines.
    queue = tmp_path / "okf_embedding_queue.jsonl"
    assert queue.is_file()
    lines = [ln for ln in queue.read_text().splitlines() if ln]
    assert len(lines) >= 2


# --- Immich: configured -> runs (mocked); unconfigured -> skipped ------------


@dataclass(frozen=True)
class _FakeAsset:
    id: str
    file_name: str
    when: str
    checksum: str
    latitude: float | None = None
    longitude: float | None = None
    city: str = ""
    state: str = ""
    country: str = ""
    people: list = field(default_factory=list)
    shared_with: list = field(default_factory=list)


class _FakeImmich:
    """Stands in for RestImmichClient — no network."""

    def __init__(self, *args, **kwargs):
        pass

    async def iter_assets(self, *, updated_after: str = ""):
        yield _FakeAsset(
            id="a1", file_name="beach.jpg", when="2026-05-01", checksum="x"
        )

    def asset_uri(self, asset_id: str) -> str:
        return f"immich://{asset_id}"


async def _healthy(*a, **k):
    return True


async def test_run_ingest_immich_runs_when_configured(env, monkeypatch):
    db_path, notes_dir = env
    monkeypatch.setattr(runner, "RestImmichClient", _FakeImmich)
    monkeypatch.setattr(runner, "_wait_for_health", _healthy)
    await runner.run_ingest(
        FakeSettings(
            solaris_db_path=db_path,
            notes_dir=str(notes_dir),
            immich_base_url="http://immich",
            immich_api_key="k",
        )
    )
    # The mocked asset is a projection-only photo event (ADR 0002 — no per-photo
    # markdown/concept): it lands as an events-table row, not an OKF file.
    assert _counts(db_path)["events"] >= 1
    assert not list((notes_dir / "okf").rglob("*.md"))


class _CursorImmich:
    """Records the updated_after it was asked for; yields one dated asset."""

    seen: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def iter_assets(self, *, updated_after: str = ""):
        _CursorImmich.seen.append(updated_after)
        yield _FakeAsset(
            id="a1", file_name="beach.jpg", when="2026-05-30T10:00:00", checksum="x"
        )

    def asset_uri(self, asset_id: str) -> str:
        return f"immich://{asset_id}"


async def test_run_ingest_immich_persists_and_reuses_cursor(env, monkeypatch):
    db_path, notes_dir = env
    _CursorImmich.seen = []
    monkeypatch.setattr(runner, "RestImmichClient", _CursorImmich)
    monkeypatch.setattr(runner, "_wait_for_health", _healthy)
    settings = FakeSettings(
        solaris_db_path=db_path,
        notes_dir=str(notes_dir),
        immich_base_url="http://immich",
        immich_api_key="k",
    )
    # First boot: no cursor yet -> full scan; the high-water `when` is persisted.
    await runner.run_ingest(settings)
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "SELECT content_hash FROM ingest_log"
        " WHERE source = 'immich' AND external_id = '__cursor__'"
    ).fetchone()[0]
    conn.close()
    assert cursor == "2026-05-30T10:00:00"
    # Second boot: the persisted cursor is passed to the client so only changes
    # since then are fetched — not a full re-scan.
    await runner.run_ingest(settings)
    assert _CursorImmich.seen == ["", "2026-05-30T10:00:00"]


async def test_run_ingest_immich_skipped_when_unconfigured(env, monkeypatch):
    db_path, notes_dir = env

    def _boom(*a, **k):  # the client must never be built when unconfigured.
        raise AssertionError("Immich client built without config")

    monkeypatch.setattr(runner, "RestImmichClient", _boom)
    # No creds -> Immich is skipped; no crash.
    await runner.run_ingest(
        FakeSettings(solaris_db_path=db_path, notes_dir=str(notes_dir))
    )


# --- Jellyfin: configured -> runs (mocked); unconfigured -> skipped ----------


class _FakeJellyfin:
    """Stands in for RestJellyfinMusicClient — no network."""

    def __init__(self, *args, **kwargs):
        pass

    async def authenticate(self):
        pass

    async def libraries(self):
        return [("lib-music", "Music")]

    async def iter_library(self, library_id: str):
        from solaris_chat.engine.ingest.jellyfin import JellyfinItem

        yield JellyfinItem(
            id="t1",
            kind="Audio",
            name="Bohemian Rhapsody",
            artist="Queen",
            genre="Rock",
            year="1975",
            changed="2026-05-01",
        )

    def audio_uri(self, item_id: str) -> str:
        return f"jellyfin://audio/{item_id}"


async def test_run_ingest_jellyfin_runs_when_configured(env, monkeypatch):
    db_path, notes_dir = env
    monkeypatch.setattr(runner, "RestJellyfinMusicClient", _FakeJellyfin)
    monkeypatch.setattr(runner, "_wait_for_health", _healthy)
    await runner.run_ingest(
        FakeSettings(
            solaris_db_path=db_path,
            notes_dir=str(notes_dir),
            jellyfin_url="http://jellyfin",
            jellyfin_username="u",
            jellyfin_password="p",
        )
    )
    # The mocked track produced projection-only song/album/band entities (ADR
    # 0002 — no per-item markdown), with their facts in the DB.
    assert _counts(db_path)["entities"] >= 1
    assert not (notes_dir / "okf" / "songs").exists()
    assert not (notes_dir / "okf" / "albums").exists()
    assert not (notes_dir / "okf" / "bands").exists()


async def test_run_ingest_jellyfin_skipped_when_unconfigured(env, monkeypatch):
    db_path, notes_dir = env

    def _boom(*a, **k):  # the client must never be built when unconfigured.
        raise AssertionError("Jellyfin client built without config")

    monkeypatch.setattr(runner, "RestJellyfinMusicClient", _boom)
    # No JELLYFIN_URL -> skipped; no crash, no client built.
    await runner.run_ingest(
        FakeSettings(solaris_db_path=db_path, notes_dir=str(notes_dir))
    )


# --- graceful degradation -----------------------------------------------------


async def test_run_ingest_caldav_skipped_when_unconfigured(env, monkeypatch):
    db_path, notes_dir = env

    def _boom(*a, **k):  # the client must never be built when unconfigured.
        raise AssertionError("DAV client built without config")

    monkeypatch.setattr(runner, "HttpDavClient", _boom)
    # No CalDAV/CardDAV URL -> skipped; no crash, no client built.
    await runner.run_ingest(
        FakeSettings(solaris_db_path=db_path, notes_dir=str(notes_dir))
    )


class _FakeContact:
    """Stands in for a Contact dataclass from a mocked DAV client."""

    uid = "c-1"
    name = "Anna Müller"
    aliases: list = []
    phones = ["+49 89 123"]
    emails: list = []
    resource = "carddav://book/c-1.vcf"
    etag = "e1"


class _FakeDav:
    """Stands in for HttpDavClient — no network."""

    def __init__(self, *args, **kwargs):
        pass

    async def iter_contacts(self, *, sync_token: str = ""):
        yield _FakeContact()

    async def iter_events(self, *, sync_token: str = ""):
        return
        yield  # pragma: no cover — makes this an async generator.


async def test_run_ingest_caldav_runs_when_configured(env, monkeypatch):
    db_path, notes_dir = env
    monkeypatch.setattr(runner, "HttpDavClient", _FakeDav)
    monkeypatch.setattr(runner, "_wait_for_health", _healthy)
    await runner.run_ingest(
        FakeSettings(
            solaris_db_path=db_path,
            notes_dir=str(notes_dir),
            caldav_url="https://radicale/cal",
            carddav_url="https://radicale/contacts",
        )
    )
    # The mocked contact produced a person concept + a phone fact.
    counts = _counts(db_path)
    assert counts["entities"] >= 1
    assert counts["facts"] >= 1


async def test_run_ingest_survives_an_adapter_failure(env, monkeypatch):
    db_path, notes_dir = env
    _seed_vault(notes_dir)

    class _BadImmich:
        def __init__(self, *a, **k):
            raise RuntimeError("immich down")

    monkeypatch.setattr(runner, "RestImmichClient", _BadImmich)
    monkeypatch.setattr(runner, "_wait_for_health", _healthy)
    # Immich blows up, but Obsidian still ran and the trigger did not raise.
    await runner.run_ingest(
        FakeSettings(
            solaris_db_path=db_path,
            notes_dir=str(notes_dir),
            immich_base_url="http://immich",
            immich_api_key="k",
        )
    )
    assert _counts(db_path)["entities"] >= 2  # Obsidian wrote despite Immich failing


# --- the trigger is exported for the boot wiring -----------------------------


def test_run_ingest_is_exported_from_ingest_package():
    assert ingest.run_ingest is runner.run_ingest


# --- boot-vs-source race: wait-for-health + bounded retry (#531) --------------


class _RanImmich:
    """Records whether the adapter was actually constructed/run."""

    built = False

    def __init__(self, *a, **k):
        _RanImmich.built = True

    async def iter_assets(self, *, updated_after: str = ""):
        yield _FakeAsset(id="a1", file_name="b.jpg", when="2026-05-01", checksum="x")

    def asset_uri(self, asset_id: str) -> str:
        return f"immich://{asset_id}"


async def test_immich_runs_after_health_retries(env, monkeypatch):
    db_path, notes_dir = env
    _RanImmich.built = False
    # The probe fails twice (source not up yet) then answers; the adapter runs.
    session = _FakeSession(fail_count=2)
    monkeypatch.setattr(runner.asyncio, "sleep", _healthy)  # skip the backoff wait.
    monkeypatch.setattr(runner.aiohttp, "ClientSession", lambda *a, **k: session)
    monkeypatch.setattr(runner, "RestImmichClient", _RanImmich)
    await runner.run_ingest(
        FakeSettings(
            solaris_db_path=db_path,
            notes_dir=str(notes_dir),
            immich_base_url="http://immich",
            immich_api_key="k",
        )
    )
    assert session.gets == 3  # two refusals, then a 200.
    assert _RanImmich.built
    assert _counts(db_path)["events"] >= 1  # projection-only photo event


async def test_immich_skipped_cleanly_when_health_never_answers(env, monkeypatch):
    db_path, notes_dir = env
    _RanImmich.built = False

    async def _never(*a, **k):
        return False  # capped out — source never came up.

    monkeypatch.setattr(runner, "_wait_for_health", _never)
    monkeypatch.setattr(runner, "RestImmichClient", _RanImmich)
    # Must NOT raise and must NOT build/run the adapter after the cap.
    await runner.run_ingest(
        FakeSettings(
            solaris_db_path=db_path,
            notes_dir=str(notes_dir),
            immich_base_url="http://immich",
            immich_api_key="k",
        )
    )
    assert not _RanImmich.built
    assert _counts(db_path)["concepts"] == 0


class _FakeResp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Mocks aiohttp.ClientSession: raises ClientError until `fail_count`
    GETs have happened, then returns a 200. No real network."""

    def __init__(self, fail_count):
        self._fail_count = fail_count
        self.gets = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url):
        self.gets += 1
        if self.gets <= self._fail_count:
            raise aiohttp.ClientConnectionError("refused")
        return _FakeResp(200)


async def test_wait_for_health_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(runner.asyncio, "sleep", _healthy)  # no real backoff wait.
    session = _FakeSession(fail_count=2)
    monkeypatch.setattr(runner.aiohttp, "ClientSession", lambda *a, **k: session)
    assert await runner._wait_for_health("immich", "http://immich/api/server/ping")
    assert session.gets == 3  # two refusals, then a 200.


async def test_wait_for_health_gives_up_after_cap(monkeypatch):
    monkeypatch.setattr(runner.asyncio, "sleep", _healthy)  # no real backoff wait.
    session = _FakeSession(fail_count=999)  # never recovers.
    monkeypatch.setattr(runner.aiohttp, "ClientSession", lambda *a, **k: session)
    # Does not raise; returns False after the bounded attempts.
    assert not await runner._wait_for_health("immich", "http://immich/api/server/ping")
    assert session.gets == len(runner._HEALTH_BACKOFF) + 1
