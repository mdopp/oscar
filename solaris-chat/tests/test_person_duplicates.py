"""ADR 0010 §1 at the `.contacts` create site + the read-only duplicate report (#994).

Two `.contacts` creates of the same human used to mint two person entities (a
fresh random `identity_key` per create, which also short-circuited the writer's
name/alias match). These cover the create path end to end through the tool-action
callback, and the salvaged detection predicate that reports the duplicates that
already exist — which the fix does NOT remove.

The OKF schema is owned by the alembic migration in `database/`; importing
alembic here fails CI's clean env, so the fixture mirrors the DDL (like
`test_okf_writer.py`).
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat import notes_index
from solaris_chat.engine.knowledge import projection
from solaris_chat.scripts import report_person_duplicates as rpd
from solaris_chat.server import build_app


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
"""


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    notes_index.ensure_schema(conn)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(aiohttp_client, tmp_path, db):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path / "notes"),
    )
    return aiohttp_client(app)


async def _add(cl, uid: str, **params):
    r = await cl.post(
        "/api/action-callback",
        json={"action_id": "contact.add", "params": params},
        headers={"Remote-User": uid},
    )
    assert r.status == 200
    return await r.json()


def _persons(db_path: str) -> list[sqlite3.Row]:
    conn = projection.open_conn(db_path)
    try:
        return conn.execute(
            "SELECT id, canonical_name, resident_uid FROM entities"
            " WHERE type = 'person' ORDER BY canonical_name"
        ).fetchall()
    finally:
        conn.close()


def _facts(db_path: str, entity_id: str) -> dict[str, str]:
    conn = projection.open_conn(db_path)
    try:
        return {
            r["predicate"]: r["value"]
            for r in conn.execute(
                "SELECT predicate, value FROM facts WHERE subject_entity_id = ?",
                (entity_id,),
            ).fetchall()
        }
    finally:
        conn.close()


# --- the create site: one human, one entity ----------------------------------


async def test_creating_the_same_contact_twice_yields_one_person(client, db):
    cl = await client
    first = await _add(cl, "mdopp", name="Anna Meyer", email="anna@example.com")
    second = await _add(cl, "mdopp", name="Anna Meyer", phone="0177 5524222")
    assert first["ok"] and second["ok"]
    assert second["id"] == first["id"]
    assert len(_persons(db)) == 1


async def test_the_second_create_keeps_what_the_first_recorded(client, db):
    # The write replaces a source's facts wholesale, so converging must carry the
    # first create's email forward instead of dropping it for the new phone.
    cl = await client
    res = await _add(cl, "mdopp", name="Anna Meyer", email="anna@example.com")
    await _add(cl, "mdopp", name="Anna Meyer", phone="0177 5524222")
    assert _facts(db, res["id"]) == {
        "email": "anna@example.com",
        "phone": "0177 5524222",
    }


async def test_a_different_name_stays_a_different_person(client, db):
    cl = await client
    await _add(cl, "mdopp", name="Anna Meyer", phone="0177 5524222")
    # Same number, near-miss name: two humans can share a landline. Converging
    # here would conflate them, which is worse than the duplicate.
    await _add(cl, "mdopp", name="Anna Meier", phone="0177 5524222")
    assert {p["canonical_name"] for p in _persons(db)} == {"Anna Meyer", "Anna Meier"}


async def test_two_residents_keep_their_own_person(client, db):
    cl = await client
    await _add(cl, "mdopp", name="Anna Meyer", email="anna@example.com")
    await _add(cl, "lena", name="Anna Meyer", email="anna@example.com")
    rows = _persons(db)
    assert len(rows) == 2
    assert {r["resident_uid"] for r in rows} == {"mdopp", "lena"}


# --- the salvaged predicate (from the closed sec/issue-994-person-dedup-merge) -


@pytest.mark.parametrize(
    "a,b",
    [
        ("Anna Meyer", "Anna Schmidt"),  # disjoint surname
        ("Meyer", "Anna Meyer"),  # single-token name must match exactly
        ("Thomas Meyer", "Thomas Meyer jr"),  # generational suffix = NOT the same
        ("Ana Müller", "Anaïs Müller"),  # NFKD folds ï, no token to subset
        ("", "Anna Meyer"),  # an unnamed contact is no name signal
        # Known recall gap, kept on purpose: NFKD folds `ü`→`u`, it does not
        # transliterate to `ue`. So the two spellings of one name are reported as
        # two people. Missing a duplicate is the safe direction to be wrong in.
        ("Anna Müller", "Anna Mueller"),
    ],
)
def test_names_compatible_refuses_a_near_miss(a, b):
    assert not rpd.names_compatible(rpd.normalize_name(a), rpd.normalize_name(b))


@pytest.mark.parametrize(
    "a,b",
    [
        ("Anna Meyer", "anna  meyer"),  # case + whitespace only
        ("Anna Meyer", "Anna Maria Meyer"),  # middle name, two shared tokens
    ],
)
def test_names_compatible_accepts_the_same_human(a, b):
    assert rpd.names_compatible(rpd.normalize_name(a), rpd.normalize_name(b))


def test_phone_normalization_folds_the_german_prefix():
    assert rpd.normalize_phone("0177 5524222") == rpd.normalize_phone("+49 177 5524222")
    assert rpd.normalize_phone("12345") == ""  # a fragment is not a person key


# --- the report: read-only, same-owner ---------------------------------------


def _seed_person(db_path, entity_id, name, uid, facts=()):
    conn = projection.open_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
            " content_hash) VALUES (?, 'person', ?, ?, 'contact', 'h')",
            (entity_id, name, uid),
        )
        for i, (predicate, value) in enumerate(facts):
            conn.execute(
                "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
                " value, confidence, source) VALUES (?, ?, ?, ?, ?, 1.0, 'contact')",
                (f"{entity_id}-f{i}", entity_id, uid, predicate, value),
            )
        conn.commit()
    finally:
        conn.close()


def test_report_finds_a_shared_contact_key_pair(db):
    _seed_person(db, "p1", "Anna Meyer", "mdopp", [("phone", "0177 5524222")])
    _seed_person(db, "p2", "Anna Maria Meyer", "mdopp", [("phone", "+49177 5524222")])
    conn = projection.open_conn(db)
    pairs = rpd.find_duplicate_persons(conn, "mdopp")
    conn.close()
    assert [p["evidence"] for p in pairs] == ["shared_contact_key"]
    assert pairs[0]["detail"] == ["phone:491775524222"]


def test_report_finds_the_contactless_same_name_pair(db):
    # The `.contacts` duplicates this fix prevents carry no phone/email at all —
    # the shared-key predicate cannot see them, which is why the report also
    # reports exact-name pairs, separately labelled.
    _seed_person(db, "p1", "Anna Meyer", "mdopp")
    _seed_person(db, "p2", "anna meyer", "mdopp")
    conn = projection.open_conn(db)
    pairs = rpd.find_duplicate_persons(conn, "mdopp")
    conn.close()
    assert [p["evidence"] for p in pairs] == ["same_name"]


def test_report_never_pairs_across_residents(db):
    _seed_person(db, "p1", "Anna Meyer", "mdopp", [("phone", "0177 5524222")])
    _seed_person(db, "p2", "Anna Meyer", "lena", [("phone", "0177 5524222")])
    _seed_person(db, "p3", "Anna Meyer", "household", [("phone", "0177 5524222")])
    conn = projection.open_conn(db)
    try:
        # Neither cross-resident nor private↔household is a pair: the people
        # surfaces read an entity's facts unscoped, so treating them as one human
        # is how a private number reaches the whole household.
        for uid in ("mdopp", "lena", "household"):
            assert rpd.find_duplicate_persons(conn, uid) == []
    finally:
        conn.close()


def test_report_refuses_a_near_miss_pair(db):
    _seed_person(db, "p1", "Thomas Meyer", "mdopp", [("phone", "05404 5209")])
    _seed_person(db, "p2", "Thomas Meyer jr", "mdopp", [("phone", "05404 5209")])
    conn = projection.open_conn(db)
    pairs = rpd.find_duplicate_persons(conn, "mdopp")
    conn.close()
    # Father and son on the family landline: the key matches, the name vetoes it.
    assert pairs == []


def test_report_mutates_nothing(db):
    _seed_person(db, "p1", "Anna Meyer", "mdopp", [("phone", "0177 5524222")])
    _seed_person(db, "p2", "Anna Meyer", "mdopp", [("phone", "0177 5524222")])
    before = [tuple(r) for r in _persons(db)]
    assert rpd.main(["--db", db]) == 0
    assert [tuple(r) for r in _persons(db)] == before


async def test_umlaut_spelling_variant_converges_on_the_same_person(client, db):
    # `safe_slug` folds `ü`→`ue`, so both spellings derive the SAME identity key —
    # and the same `okf/people/anna-mueller.md`. They were already one file; now
    # they are also one entity, which the create renames to what was just typed.
    cl = await client
    first = await _add(cl, "mdopp", name="Anna Müller", phone="0177 5524222")
    second = await _add(cl, "mdopp", name="Anna Mueller")
    assert second["id"] == first["id"]
    rows = _persons(db)
    assert len(rows) == 1 and rows[0]["canonical_name"] == "Anna Mueller"
