"""Provider orgs + personal persons → the Solaris address books (#doc-graph/#996).

`document_contacts_sync.sync_contacts` reads `organization` entities and the
resident's `.contacts` `person` entities with their contact facts, and PUTs one
vCard each into the dedicated `solaris` account's CardDAV collections
(authenticated HTTP, not a filesystem mount). These tests mock the DAV
`ensure_addressbook`/`put_item` and prove the vCard content, the stable overwrite
UID, the target collection/suffix, the PER-RESIDENT routing of persons (a
resident's contacts never land in another resident's principal), that
CardDAV-ingested persons are not written back, and the disabled no-op.
"""

from __future__ import annotations

import sqlite3

from solaris_chat.engine import document_contacts_sync
from solaris_chat.engine.document_contacts_sync import sync_contacts

_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
"""

_URL = "https://caldav.example/solaris/anbieter/"
_BASE = "https://caldav.example/"


def _person(conn, eid, name, uid, source, facts=()):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'person', ?, ?, ?, 'h')",
        (eid, name, uid, source),
    )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, 1.0, 'contact')",
            (f"{eid}-{i}", eid, uid, pred, val),
        )


def _org(conn, eid, name, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'organization', ?, 'mdopp', 'documents', 'h')",
        (eid, name),
    )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, 'mdopp', ?, ?, 0.6, 'd')",
            (f"{eid}-{i}", eid, pred, val),
        )


def _db(tmp_path):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _org(
        conn,
        "o-ergo",
        "ERGO Versicherung AG",
        [
            ("phone", "05404 5209"),
            ("email", "dirk.mutert@ergo.de"),
            ("contact_person", "Dirk Mutert"),
        ],
    )
    conn.commit()
    conn.close()
    return db


def _capture(monkeypatch, ensured=None):
    calls = []

    async def fake_put(self, collection_url, uid, body, *, suffix, content_type):
        calls.append(
            {
                "url": collection_url,
                "uid": uid,
                "body": body,
                "suffix": suffix,
                "content_type": content_type,
            }
        )
        return collection_url + uid

    async def fake_ensure(self, collection_url, displayname):
        if ensured is not None:
            ensured.append(collection_url)

    monkeypatch.setattr(document_contacts_sync.HttpDavClient, "put_item", fake_put)
    monkeypatch.setattr(
        document_contacts_sync.HttpDavClient, "ensure_addressbook", fake_ensure
    )
    return calls


async def test_sync_puts_vcard_to_collection(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    out = await sync_contacts(_db(tmp_path), _URL, "solaris", "pw")
    assert out == {"written": 1, "failed": 0}
    assert len(calls) == 1
    c = calls[0]
    assert c["url"] == _URL and c["suffix"] == ".vcf"
    assert c["uid"] == "solaris-provider-o-ergo"  # stable overwrite UID
    assert "FN:ERGO Versicherung AG" in c["body"]
    assert "05404 5209" in c["body"] and "dirk.mutert@ergo.de" in c["body"]
    assert "Dirk Mutert" in c["body"]  # in the NOTE


async def test_one_bad_card_does_not_abort(tmp_path, monkeypatch):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    _org(conn, "o-lbs", "LBS", [("phone", "1")])
    conn.commit()
    conn.close()

    async def flaky_put(self, collection_url, uid, body, *, suffix, content_type):
        if "o-ergo" in uid:
            raise RuntimeError("boom")

    _capture(monkeypatch)
    monkeypatch.setattr(document_contacts_sync.HttpDavClient, "put_item", flaky_put)
    out = await sync_contacts(db, _URL, "solaris", "pw")
    assert out == {"written": 1, "failed": 1}


async def test_persons_land_in_their_own_residents_book(tmp_path, monkeypatch):
    ensured: list[str] = []
    calls = _capture(monkeypatch, ensured)
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    _person(conn, "p-anna", "Anna Meyer", "mdopp", "contact", [("phone", "555")])
    _person(conn, "p-bo", "Bo Berg", "lea", "contact")
    conn.commit()
    conn.close()

    out = await sync_contacts(db, _URL, "solaris", "pw", _BASE, "mdopp")
    assert out == {"written": 3, "failed": 0}
    by_uid = {c["uid"]: c for c in calls}
    anna = by_uid["solaris-person-p-anna"]
    assert anna["url"] == "https://caldav.example/mdopp/solaris-contacts/"
    assert anna["suffix"] == ".vcf"
    assert "FN:Anna Meyer" in anna["body"] and "555" in anna["body"]
    # Lea's contact goes under LEA's principal — never into mdopp's book.
    assert (
        by_uid["solaris-person-p-bo"]["url"]
        == "https://caldav.example/lea/solaris-contacts/"
    )
    assert "https://caldav.example/mdopp/solaris-contacts/" in ensured
    assert "https://caldav.example/lea/solaris-contacts/" in ensured


async def test_carddav_ingested_persons_are_not_written_back(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    _person(conn, "p-ext", "Ute Extern", "mdopp", "carddav", [("phone", "1")])
    conn.commit()
    conn.close()

    await sync_contacts(db, "", "solaris", "pw", _BASE, "mdopp")
    assert calls == []  # already in the resident's own book — no duplicate card


async def test_household_persons_route_to_the_primary_resident(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    _person(conn, "p-shared", "Hausarzt", "household", "contact")
    conn.commit()
    conn.close()

    await sync_contacts(db, "", "solaris", "pw", _BASE, "mdopp")
    assert [c["url"] for c in calls] == [
        "https://caldav.example/mdopp/solaris-contacts/"
    ]


async def test_disabled_when_unconfigured(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    db = _db(tmp_path)
    assert await sync_contacts(db, "", "solaris", "pw") == {"written": 0, "failed": 0}
    assert await sync_contacts(db, _URL, "", "pw") == {"written": 0, "failed": 0}
    assert await sync_contacts(db, _URL, "solaris", "") == {"written": 0, "failed": 0}
    assert calls == []
