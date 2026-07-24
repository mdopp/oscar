"""DB-backed document category views + the correction endpoint (#doc).

`documents_portal_db` tables the `document` entities + facts per category, owner-
scoped, with the highest-confidence value winning per predicate (a human-
confirmed 1.0 over the agent-extracted 0.6). The `/api/portal/documents/correct`
endpoint writes that confirmation.
"""

from __future__ import annotations

import sqlite3

from solaris_chat import documents_portal_db

_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (entity_id, alias));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
"""


def _doc(conn, eid, title, resident, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'document', ?, ?, 'documents', 'h')",
        (eid, title, resident),
    )
    for i, (pred, val, conf, src) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"{eid}-{i}", eid, resident, pred, val, conf, src),
        )


def _seed(tmp_path):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _doc(
        conn,
        "d-ins",
        "ERGO Rechtsschutz",
        "household",
        [
            ("category", "insurance", 0.6, "documents"),
            ("provider", "ERGO", 0.6, "documents"),
            ("provider_key", "ergo", 0.6, "documents"),
            ("cancellation_deadline", "2026-12-15", 0.6, "documents"),
            # a human-confirmed correction of the deadline
            ("cancellation_deadline", "2026-11-30", 1.0, "documents:confirmed"),
        ],
    )
    _doc(
        conn,
        "d-emp",
        "Arbeitsvertrag ACME",
        "mdopp",
        [("category", "employment", 0.6, "documents")],
    )
    _doc(
        conn,
        "d-lena",
        "Lena Hausrat",
        "lena",
        [("category", "insurance", 0.6, "documents")],
    )
    conn.commit()
    conn.close()
    return db


def test_categories_owner_scoped(tmp_path):
    db = _seed(tmp_path)
    # mdopp sees the shared insurance + own employment, NOT lena's private insurance.
    assert documents_portal_db.categories(db, "mdopp") == {
        "insurance": 1,
        "employment": 1,
    }
    # lena sees the shared insurance + her own (2 insurances), no employment.
    assert documents_portal_db.categories(db, "lena") == {"insurance": 2}


def test_category_view_confirmed_value_wins(tmp_path):
    db = _seed(tmp_path)
    rows = documents_portal_db.category_view(db, "mdopp", "insurance")
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "ERGO Rechtsschutz"
    assert row["facts"]["provider"]["value"] == "ERGO"
    # The confirmed 1.0 deadline outranks the extracted 0.6 one.
    dl = row["facts"]["cancellation_deadline"]
    assert dl == {"value": "2026-11-30", "confidence": 1.0}


def test_category_view_excludes_other_residents(tmp_path):
    db = _seed(tmp_path)
    # mdopp's insurance view has only the shared ERGO doc, not lena's.
    titles = {
        r["title"] for r in documents_portal_db.category_view(db, "mdopp", "insurance")
    }
    assert titles == {"ERGO Rechtsschutz"}


def test_category_view_carries_normalized_vorgang_key(tmp_path):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    # Two documents of one policy (spaced/cased variants of the number) + one
    # unrelated document that carries no policy number.
    _doc(
        conn,
        "d-pol1",
        "Police",
        "household",
        [
            ("category", "insurance", 0.6, "documents"),
            ("policy_number", "KR 74536202", 0.6, "documents"),
        ],
    )
    _doc(
        conn,
        "d-pol2",
        "Rechnung",
        "household",
        [
            ("category", "insurance", 0.6, "documents"),
            ("policy_number", "kr74536202", 0.6, "documents"),
        ],
    )
    _doc(
        conn,
        "d-solo",
        "Hausrat",
        "household",
        [("category", "insurance", 0.6, "documents")],
    )
    conn.commit()
    conn.close()
    rows = {
        r["title"]: r["vorgang"]
        for r in documents_portal_db.category_view(db, "mdopp", "insurance")
    }
    # The two policy documents normalize to the same Vorgang key; the solo one
    # gets an empty key so it stays standalone.
    assert rows["Police"] == rows["Rechnung"] == "kr74536202"
    assert rows["Hausrat"] == ""


def _org(conn, eid, name, resident, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'organization', ?, ?, 'documents', 'h')",
        (eid, name, resident),
    )
    for i, (pred, val, conf, src) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"{eid}-c{i}", eid, resident, pred, val, conf, src),
        )


def test_contacts_groups_documents_and_contact_facts(tmp_path):
    db = _seed(tmp_path)
    conn = sqlite3.connect(db)
    _org(
        conn,
        "o-ergo",
        "ERGO",
        "household",
        [
            ("provider_key", "ergo", 0.6, "documents:a"),
            ("phone", "05404 5209", 0.6, "documents:a"),
            ("email", "service@ergo.de", 0.6, "documents:b"),
        ],
    )
    conn.commit()
    conn.close()
    rows = documents_portal_db.contacts(db, "mdopp")
    ergo = next(r for r in rows if r["name"] == "ERGO")
    # Contact facts from both documents surface on the shared org.
    assert ergo["contact"]["phone"]["value"] == "05404 5209"
    assert ergo["contact"]["email"]["value"] == "service@ergo.de"
    # The ERGO document (shared provider_key) groups under it.
    assert [d["title"] for d in ergo["documents"]] == ["ERGO Rechtsschutz"]


def test_contacts_excludes_other_residents(tmp_path):
    # A private org (another resident's) must not appear in the caller's book.
    db = _seed(tmp_path)
    conn = sqlite3.connect(db)
    _org(conn, "o-priv", "Dr. Privat", "lena", [("phone", "0", 0.6, "documents:x")])
    conn.commit()
    conn.close()
    names = {r["name"] for r in documents_portal_db.contacts(db, "mdopp")}
    assert "Dr. Privat" not in names


def test_missing_projection_returns_none(tmp_path):
    assert documents_portal_db.categories(str(tmp_path / "nope.db"), "mdopp") is None
    assert documents_portal_db.contacts(str(tmp_path / "nope.db"), "mdopp") is None


# --- .doc search (title/category LIKE, owner-scoped) --------------------------


def test_search_matches_title_and_category(tmp_path):
    db = _seed(tmp_path)
    # title match (mdopp's own employment doc)
    rows = documents_portal_db.search(db, "mdopp", "acme")
    assert [r["title"] for r in rows] == ["Arbeitsvertrag ACME"]
    assert rows[0] == {
        "entity_id": "d-emp",
        "title": "Arbeitsvertrag ACME",
        "category": "employment",
    }
    # category match (shared insurance)
    ins = documents_portal_db.search(db, "mdopp", "insurance")
    assert [r["title"] for r in ins] == ["ERGO Rechtsschutz"]


def test_search_empty_q_returns_full_owner_scoped_list(tmp_path):
    db = _seed(tmp_path)
    # mdopp: shared ERGO + own ACME, NOT lena's private doc.
    assert {r["title"] for r in documents_portal_db.search(db, "mdopp", "")} == {
        "ERGO Rechtsschutz",
        "Arbeitsvertrag ACME",
    }


def test_search_excludes_other_residents(tmp_path):
    db = _seed(tmp_path)
    # lena's private insurance doc must not surface for mdopp even on a match.
    titles = {r["title"] for r in documents_portal_db.search(db, "mdopp", "hausrat")}
    assert "Lena Hausrat" not in titles


def test_search_missing_projection_returns_none(tmp_path):
    assert documents_portal_db.search(str(tmp_path / "nope.db"), "mdopp", "x") is None


# --- person_directory (ADR 0010: all person entities, own ∪ shared) ----------


def _person(conn, eid, name, resident, aliases=(), facts=()):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'person', ?, ?, 'contact', 'h')",
        (eid, name, resident),
    )
    # the writer records the canonical name as a self-alias; mirror that.
    for alias in (name, *aliases):
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (eid, alias),
        )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, 1.0, 'contact')",
            (f"{eid}-p{i}", eid, resident, pred, val),
        )


def test_person_directory_includes_aliases_and_contact_facts(tmp_path):
    db = _seed(tmp_path)
    conn = sqlite3.connect(db)
    _person(
        conn,
        "p-mike",
        "Michael",
        "household",
        aliases=("mike",),
        facts=[("email", "m@ex.de")],
    )
    conn.commit()
    conn.close()
    people = documents_portal_db.person_directory(db, "mdopp")
    mike = next(p for p in people if p["name"] == "Michael")
    # The canonical name is not duplicated into aliases; the real alias is present.
    assert mike["aliases"] == ["mike"]
    assert mike["email"] == "m@ex.de"


def test_person_directory_includes_contactless_person(tmp_path):
    # A person with no email/phone still appears (unlike person_contacts).
    db = _seed(tmp_path)
    conn = sqlite3.connect(db)
    _person(conn, "p-oma", "Erika", "household")
    conn.commit()
    conn.close()
    names = {p["name"] for p in documents_portal_db.person_directory(db, "mdopp")}
    assert "Erika" in names


def test_person_directory_owner_scoped(tmp_path):
    # A resident's private person is not visible to another resident.
    db = _seed(tmp_path)
    conn = sqlite3.connect(db)
    _person(conn, "p-priv", "Lenas Freundin", "lena")
    _person(conn, "p-shared", "Nachbar", "household")
    conn.commit()
    conn.close()
    names = {p["name"] for p in documents_portal_db.person_directory(db, "mdopp")}
    assert "Nachbar" in names
    assert "Lenas Freundin" not in names


def test_person_directory_missing_projection_returns_none(tmp_path):
    assert documents_portal_db.person_directory(str(tmp_path / "nope.db"), "x") is None


# --- person_contacts (a view over person_directory: same aliases, .contacts) --


def test_person_contacts_carries_aliases_and_filters_contactless(tmp_path):
    # `.contacts` shares the person_directory alias model (unified @-mention path):
    # a contact-bearing person carries its aliases; a contactless one is dropped.
    db = _seed(tmp_path)
    conn = sqlite3.connect(db)
    _person(
        conn,
        "p-mike",
        "Michael",
        "household",
        aliases=("mike",),
        facts=[("phone", "0123")],
    )
    _person(conn, "p-oma", "Erika", "household")
    conn.commit()
    conn.close()
    contacts = documents_portal_db.person_contacts(db, "mdopp")
    names = {c["name"] for c in contacts}
    assert names == {"Michael"}
    mike = next(c for c in contacts if c["name"] == "Michael")
    assert mike["aliases"] == ["mike"]
    assert mike["phone"] == "0123"


def test_person_contacts_missing_projection_returns_none(tmp_path):
    assert documents_portal_db.person_contacts(str(tmp_path / "nope.db"), "x") is None


# --- correction endpoint -----------------------------------------------------


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


def _app(db):
    from solaris_chat.server import build_app

    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(db) + "-notes",
    )


async def test_correct_endpoint_writes_confirmed_fact(aiohttp_client, tmp_path):
    from solaris_chat.engine.knowledge import projection

    db = _seed(tmp_path)
    client = await aiohttp_client(_app(db))
    r = await client.post(
        "/api/portal/documents/correct",
        json={"entity_id": "d-ins", "predicate": "provider", "value": "ERGO Group"},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200 and (await r.json())["ok"] is True
    conn = projection.open_conn(db)
    try:
        row = conn.execute(
            "SELECT value, confidence FROM facts WHERE subject_entity_id = 'd-ins'"
            " AND predicate = 'provider' AND source = 'documents:confirmed'"
        ).fetchone()
    finally:
        conn.close()
    assert (row["value"], row["confidence"]) == ("ERGO Group", 1.0)


async def test_correct_endpoint_rejects_other_residents_document(
    aiohttp_client, tmp_path
):
    db = _seed(tmp_path)
    client = await aiohttp_client(_app(db))
    # mdopp may not correct lena's private document.
    r = await client.post(
        "/api/portal/documents/correct",
        json={"entity_id": "d-lena", "predicate": "provider", "value": "X"},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 404


# --- .doc search endpoint (interactive; Remote-User → owner-scoped rows) ------


async def test_search_endpoint_returns_owner_scoped_rows(aiohttp_client, tmp_path):
    db = _seed(tmp_path)
    client = await aiohttp_client(_app(db))
    r = await client.get(
        "/api/portal/documents/search?q=insurance", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert [d["title"] for d in body["documents"]] == ["ERGO Rechtsschutz"]


async def test_search_endpoint_excludes_other_residents(aiohttp_client, tmp_path):
    db = _seed(tmp_path)
    client = await aiohttp_client(_app(db))
    # mdopp searching for lena's private doc gets nothing back.
    r = await client.get(
        "/api/portal/documents/search?q=hausrat", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    assert (await r.json())["documents"] == []


async def test_search_endpoint_empty_q_defaults_to_full_list(aiohttp_client, tmp_path):
    db = _seed(tmp_path)
    client = await aiohttp_client(_app(db))
    # Absent q → the caller's full owner-scoped list (own ∪ shared).
    r = await client.get(
        "/api/portal/documents/search", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    titles = {d["title"] for d in (await r.json())["documents"]}
    assert titles == {"ERGO Rechtsschutz", "Arbeitsvertrag ACME"}
