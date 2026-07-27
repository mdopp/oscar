"""Cross-source person dedup + human-confirmed merge (#994, ADR 0010).

Merging two `person` entities is DESTRUCTIVE + irreversible, so these tests pin
the safety invariants: detection is conservative (a shared contact key AND
compatible names — never name-only), merge is confirmation-gated, re-validates
that same predicate before it mutates, and is scoped to persons the caller OWNS
(never the shared-household pot, which both residents write through). Every
merge is auditable and *really* undoable via the `person_merges` trail.

The privacy tests assert what ANOTHER resident sees through
`documents_portal_db.person_directory` after a merge — that surface reads an
entity's facts and aliases unscoped, so it is where a leak would show up.

The schema mirrors migrations 0016 + 0018 + 0030 verbatim, FOREIGN KEYs and
`concepts`/`okf_vectors` included, with `PRAGMA foreign_keys = ON` as in
`projection.open_conn` — otherwise the orphans a partial teardown leaves behind
are structurally invisible here.
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat.documents_portal_db import person_directory
from solaris_chat.engine.knowledge import person_dedup


# Mirrors migrations 0016_okf_knowledge_index + 0018_okf_vectors + 0030_person_merges.
_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL,
  PRIMARY KEY (entity_id, alias),
  FOREIGN KEY (entity_id) REFERENCES entities (id));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (subject_entity_id) REFERENCES entities (id));
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
CREATE TABLE okf_vectors (
  embedding_id TEXT PRIMARY KEY, concept_id TEXT NOT NULL, model TEXT NOT NULL,
  dim INTEGER NOT NULL, vector BLOB NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE person_merges (
  id TEXT PRIMARY KEY, primary_entity_id TEXT NOT NULL,
  secondary_entity_id TEXT NOT NULL, secondary_name TEXT NOT NULL,
  secondary_resident_uid TEXT NOT NULL, secondary_source TEXT NOT NULL,
  snapshot TEXT NOT NULL, merged_by TEXT NOT NULL,
  merged_at TEXT NOT NULL DEFAULT (datetime('now')), undone_at TEXT);
"""


@pytest.fixture
def db_path(tmp_path):
    """A real file (not `:memory:`) so `person_directory`, which opens its own
    connection, sees the same database."""
    p = str(tmp_path / "solaris.db")
    c = sqlite3.connect(p)
    c.executescript(_SCHEMA)
    c.commit()
    c.close()
    return p


@pytest.fixture
def conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.commit()
    c.close()


def _person(conn, eid, name, resident, source="contact", facts=(), aliases=()):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'person', ?, ?, ?, 'h')",
        (eid, name, resident, source),
    )
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
        (eid, name),
    )
    for a in aliases:
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (eid, a),
        )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, 1.0, ?)",
            (f"{eid}-{i}", eid, resident, pred, val, source),
        )


def _project(conn, eid, okf_path, embedding_id="emb-1"):
    """Give a person the projection rows a real ingest writes: the `concepts`
    link to its OKF file plus the embedding row RAG retrieves through."""
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, embedding_id,"
        " content_hash) VALUES (?, ?, 'entity', ?, ?, 'h')",
        (f"c-{eid}", eid, okf_path, embedding_id),
    )
    conn.execute(
        "INSERT INTO okf_vectors (embedding_id, concept_id, model, dim, vector)"
        " VALUES (?, ?, 'nomic-embed-text', 3, X'00')",
        (embedding_id, eid),
    )


def _directory(db_path, uid):
    return {r["name"]: r for r in person_directory(db_path, uid) or []}


# --- normalization -----------------------------------------------------------


def test_phone_normalization_folds_german_prefix():
    assert person_dedup._normalize_phone(
        "0177 5524222"
    ) == person_dedup._normalize_phone("+49 177 5524222")
    assert person_dedup._normalize_phone(
        "0049177/5524222"
    ) == person_dedup._normalize_phone("+49 177 5524222")


def test_short_phone_is_not_a_key():
    assert person_dedup._normalize_phone("123") == ""


def test_email_normalization_lowercases():
    assert person_dedup._normalize_email("  Mdopp@Web.DE ") == "mdopp@web.de"
    assert person_dedup._normalize_email("not-an-email") == ""


def test_name_normalization_folds_every_diacritic():
    # A whitelist class ([a-z0-9äöüß]) SPLIT any other diacritic, and the
    # fragment then satisfied the subset rule. NFKD folds them all.
    assert person_dedup._normalize_name("Anaïs Müller") == "anais muller"
    assert person_dedup._normalize_name("Zoë Šimek") == "zoe simek"
    # casefold() already maps ß -> ss.
    assert person_dedup._normalize_name("Straßer") == "strasser"


def test_names_compatible_needs_two_shared_tokens():
    assert person_dedup._names_compatible("anna meyer", "anna meyer")
    assert person_dedup._names_compatible("anna meyer", "anna meyer koch")
    # a single-token name has to match exactly: a shared surname on a family
    # landline is not an identity.
    assert not person_dedup._names_compatible("meyer", "anna meyer")
    assert not person_dedup._names_compatible("anna", "anna meyer")
    assert not person_dedup._names_compatible("anna meyer", "anna schmidt")
    assert not person_dedup._names_compatible("", "anna")


def test_folded_diacritics_do_not_make_distinct_names_compatible():
    a = person_dedup._normalize_name("Ana Müller")
    b = person_dedup._normalize_name("Anaïs Müller")
    assert not person_dedup._names_compatible(a, b)


# --- detection: precision ----------------------------------------------------


def test_shared_phone_and_compatible_name_is_a_candidate(conn):
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    _person(
        conn,
        "b",
        "Anna Meyer",
        "mdopp",
        source="caldav",
        facts=[("phone", "+49177 5524222")],
    )
    cands = person_dedup.find_merge_candidates(conn, "mdopp")
    assert len(cands) == 1
    assert {cands[0]["primary"], cands[0]["secondary"]} == {"a", "b"}
    assert cands[0]["reason"] == ["phone:491775524222"]


def test_shared_email_across_sources_is_a_candidate(conn):
    _person(conn, "a", "Michael Dopp", "mdopp", facts=[("email", "mdopp@web.de")])
    _person(
        conn,
        "b",
        "Michael Dopp",
        "mdopp",
        source="caldav",
        facts=[("email", "MDOPP@web.de")],
    )
    cands = person_dedup.find_merge_candidates(conn, "mdopp")
    assert len(cands) == 1


def test_no_false_merge_on_name_only(conn):
    # Two distinct "Anna Meyer"s with NO shared contact key must never be offered.
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 1111111")])
    _person(conn, "b", "Anna Meyer", "mdopp", facts=[("phone", "0177 2222222")])
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []


def test_no_false_merge_on_shared_key_but_disjoint_names(conn):
    # A shared phone but clearly different people (roommates share a landline) is
    # NOT a candidate — the name guard blocks it.
    _person(conn, "a", "Anna Schmidt", "mdopp", facts=[("phone", "030 1234567")])
    _person(conn, "b", "Bernd Müller", "mdopp", facts=[("phone", "030 1234567")])
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []


def test_no_false_merge_on_shared_landline_and_shared_surname(conn):
    # The family landline case: same surname, one side single-token.
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "030 1234567")])
    _person(
        conn, "b", "Meyer", "mdopp", source="caldav", facts=[("phone", "030 1234567")]
    )
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []


def test_unnamed_contact_key_share_is_not_a_candidate(conn):
    # An email with no name signal on one side is not enough (needs both).
    _person(conn, "a", "", "mdopp", facts=[("email", "x@y.de")])
    _person(conn, "b", "", "mdopp", facts=[("email", "x@y.de")])
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []


# --- detection: same-owner isolation -----------------------------------------


def test_no_cross_resident_candidate(conn):
    # Same name + same phone but owned by different residents: never offered.
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    _person(conn, "b", "Anna Meyer", "lena", facts=[("phone", "0177 5524222")])
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []
    assert person_dedup.find_merge_candidates(conn, "lena") == []


def test_shared_household_person_is_out_of_scope(conn):
    # A private↔household pair is NOT offered: the merge would move private facts
    # onto a shared entity (or delete the shared one) — see the module docstring.
    _person(conn, "a", "Anna Meyer", "household", facts=[("phone", "0177 5524222")])
    _person(
        conn,
        "b",
        "Anna Meyer",
        "mdopp",
        source="caldav",
        facts=[("phone", "0177 5524222")],
    )
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []
    assert person_dedup.find_merge_candidates(conn, "household") == []


# --- preview -----------------------------------------------------------------


def test_preview_is_read_only_and_unions(conn):
    _person(
        conn,
        "a",
        "Anna Meyer",
        "mdopp",
        facts=[("phone", "0177 5524222")],
        aliases=["Anni"],
    )
    _person(
        conn,
        "b",
        "Anna Meyer",
        "mdopp",
        source="caldav",
        facts=[("email", "anna@x.de")],
    )
    before = conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
    prev = person_dedup.preview_merge(conn, "a", "b", "mdopp")
    assert prev is not None
    assert "Anni" in prev["aliases"] and "Anna Meyer" in prev["aliases"]
    assert {"phone:491775524222", "email:anna@x.de"} <= set(prev["keys"])
    assert conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"] == before


def test_preview_refuses_cross_resident(conn):
    _person(conn, "a", "Anna", "mdopp", facts=[("phone", "0177 5524222")])
    _person(conn, "b", "Anna", "lena", facts=[("phone", "0177 5524222")])
    assert person_dedup.preview_merge(conn, "a", "b", "mdopp") is None


def test_preview_refuses_a_household_person(conn):
    _person(conn, "a", "Anna Meyer", "household", facts=[("phone", "0177 5524222")])
    _person(conn, "b", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    assert person_dedup.preview_merge(conn, "a", "b", "mdopp") is None
    assert person_dedup.preview_merge(conn, "b", "a", "mdopp") is None


# --- merge: confirmation-gated, owner-scoped ---------------------------------


def test_merge_moves_aliases_facts_events(conn):
    _person(
        conn,
        "a",
        "Anna Meyer",
        "mdopp",
        facts=[("phone", "0177 5524222")],
        aliases=["Anni"],
    )
    _person(
        conn,
        "b",
        "Anna Meyer",
        "mdopp",
        source="caldav",
        facts=[("phone", "0177 5524222"), ("email", "anna@x.de")],
        aliases=["Anna M."],
    )
    conn.execute(
        "INSERT INTO events (id, ts, resident_uid, kind, source)"
        " VALUES ('e1', '2026-01-01', 'mdopp', 'meeting', 'caldav')"
    )
    conn.execute(
        "INSERT INTO event_entities (event_id, entity_id, role)"
        " VALUES ('e1', 'b', 'attendee')"
    )
    mid = person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="mdopp"
    )
    assert mid is not None
    # secondary gone, primary carries both sources' facts + the alias.
    assert conn.execute("SELECT 1 FROM entities WHERE id = 'b'").fetchone() is None
    preds = {
        r["predicate"]
        for r in conn.execute(
            "SELECT predicate FROM facts WHERE subject_entity_id = 'a'"
        ).fetchall()
    }
    assert preds == {"phone", "email"}
    aliases = {
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = 'a'"
        ).fetchall()
    }
    assert {"Anni", "Anna Meyer", "Anna M."} <= aliases
    # the event edge now points at the primary.
    assert (
        conn.execute(
            "SELECT entity_id FROM event_entities WHERE event_id = 'e1'"
        ).fetchone()["entity_id"]
        == "a"
    )


def test_merge_refuses_a_pair_detection_would_not_offer(conn):
    # The mutating call re-checks the SAME predicate as find_merge_candidates —
    # a hand-built pair can't reach past detection.
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 1111111")])
    _person(conn, "b", "Bernd Mueller", "mdopp", facts=[("phone", "0177 2222222")])
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []
    assert person_dedup.merge_refusal(conn, "a", "b", "mdopp") == "not_a_duplicate"
    assert (
        person_dedup.merge_persons(conn, primary_id="a", secondary_id="b", uid="mdopp")
        is None
    )
    assert conn.execute("SELECT 1 FROM entities WHERE id = 'b'").fetchone() is not None


def test_merge_refuses_a_shared_key_with_disjoint_names(conn):
    # Shared landline, different people: offered by neither path.
    _person(conn, "a", "Anna Schmidt", "mdopp", facts=[("phone", "030 1234567")])
    _person(conn, "b", "Bernd Mueller", "mdopp", facts=[("phone", "030 1234567")])
    assert (
        person_dedup.merge_persons(conn, primary_id="a", secondary_id="b", uid="mdopp")
        is None
    )


def test_merge_refuses_cross_resident(conn):
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    _person(conn, "b", "Anna Meyer", "lena", facts=[("phone", "0177 5524222")])
    assert person_dedup.merge_refusal(conn, "a", "b", "mdopp") == "not_in_scope"
    assert (
        person_dedup.merge_persons(conn, primary_id="a", secondary_id="b", uid="mdopp")
        is None
    )
    # lena's person is untouched.
    assert conn.execute("SELECT 1 FROM entities WHERE id = 'b'").fetchone() is not None


def test_merge_refuses_self(conn):
    _person(conn, "a", "Anna", "mdopp", facts=[("phone", "0177 5524222")])
    assert person_dedup.merge_refusal(conn, "a", "a", "mdopp") == "same_person"
    assert (
        person_dedup.merge_persons(conn, primary_id="a", secondary_id="a", uid="mdopp")
        is None
    )


# --- privacy: what the OTHER resident sees after a merge ---------------------


def test_merge_cannot_publish_a_private_person_into_the_household(conn, db_path):
    """Private secondary, household primary: mdopp's private therapist alias and
    phone must not appear on lena's shared contact."""
    # A genuine-looking duplicate — same name, same email — so ONLY the
    # same-owner rule stands between mdopp's private facts and lena's view.
    _person(conn, "shared", "Anna Weber", "household", facts=[("email", "aw@x.de")])
    _person(
        conn,
        "priv",
        "Anna Weber",
        "mdopp",
        source="caldav",
        facts=[("email", "aw@x.de"), ("phone", "0177 5524222")],
        aliases=["Dr. Weber Therapie"],
    )
    conn.commit()
    before = _directory(db_path, "lena")

    assert (
        person_dedup.merge_persons(
            conn, primary_id="shared", secondary_id="priv", uid="mdopp"
        )
        is None
    )
    conn.commit()

    after = _directory(db_path, "lena")
    assert after == before
    assert after["Anna Weber"]["phone"] == ""
    assert after["Anna Weber"]["aliases"] == []


def test_merge_cannot_delete_the_household_person_for_everyone(conn, db_path):
    """Mirror direction — household secondary, private primary: the shared
    contact must survive, mdopp was never entitled to remove it for lena."""
    _person(
        conn,
        "shared",
        "Anna Weber",
        "household",
        facts=[("phone", "0177 5524222")],
    )
    _person(
        conn,
        "priv",
        "Anna Weber",
        "mdopp",
        source="caldav",
        facts=[("phone", "0177 5524222")],
    )
    conn.commit()
    before = _directory(db_path, "lena")

    assert (
        person_dedup.merge_persons(
            conn, primary_id="priv", secondary_id="shared", uid="mdopp"
        )
        is None
    )
    conn.commit()

    assert _directory(db_path, "lena") == before
    assert conn.execute("SELECT 1 FROM entities WHERE id = 'shared'").fetchone()


def test_a_legitimate_merge_leaves_the_other_resident_untouched(conn, db_path):
    """The allowed case still must not move anything into another resident's
    view: two of mdopp's OWN persons merge, lena's directory is unchanged."""
    _person(conn, "l", "Anna Weber", "lena")
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    _person(
        conn,
        "b",
        "Anna Meyer",
        "mdopp",
        source="caldav",
        facts=[("phone", "0177 5524222")],
        aliases=["Anni"],
    )
    conn.commit()
    before = _directory(db_path, "lena")

    assert person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="mdopp"
    )
    conn.commit()
    assert _directory(db_path, "lena") == before
    assert "Anni" in _directory(db_path, "mdopp")["Anna Meyer"]["aliases"]


# --- teardown: no orphaned projection ----------------------------------------


def test_merge_tears_down_the_secondarys_concept_and_embedding(conn):
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    _person(
        conn,
        "b",
        "Anna Meyer",
        "mdopp",
        source="caldav",
        facts=[("phone", "0177 5524222")],
    )
    _project(conn, "a", "okf/persons/anna-meyer.md", "emb-a")
    _project(conn, "b", "okf/persons/anna-meyer-caldav.md", "emb-b")

    assert person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="mdopp"
    )
    # the merged-away duplicate leaves Notizen (concepts) and RAG (okf_vectors).
    assert conn.execute("SELECT 1 FROM concepts WHERE ref_id = 'b'").fetchone() is None
    assert (
        conn.execute(
            "SELECT 1 FROM okf_vectors WHERE embedding_id = 'emb-b'"
        ).fetchone()
        is None
    )
    # the primary's own projection is untouched.
    assert conn.execute("SELECT 1 FROM concepts WHERE ref_id = 'a'").fetchone()
    assert conn.execute(
        "SELECT 1 FROM okf_vectors WHERE embedding_id = 'emb-a'"
    ).fetchone()


# --- provenance / undo -------------------------------------------------------


def test_merge_records_audit_trail(conn):
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    _person(
        conn,
        "b",
        "Anna Meyer",
        "mdopp",
        source="caldav",
        facts=[("phone", "0177 5524222")],
    )
    mid = person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="mdopp"
    )
    row = conn.execute("SELECT * FROM person_merges WHERE id = ?", (mid,)).fetchone()
    assert row["secondary_entity_id"] == "b"
    assert row["secondary_name"] == "Anna Meyer"
    assert row["secondary_source"] == "caldav"
    assert row["merged_by"] == "mdopp"
    assert row["undone_at"] is None


def test_undo_restores_both_sides(conn):
    _person(
        conn,
        "a",
        "Anna Meyer",
        "mdopp",
        facts=[("phone", "0177 5524222")],
        aliases=["Anni"],
    )
    _person(
        conn,
        "b",
        "Anna Meyer",
        "mdopp",
        source="caldav",
        facts=[("email", "anna@x.de"), ("phone", "0177 5524222")],
        aliases=["Änni"],
    )
    conn.execute(
        "INSERT INTO events (id, ts, resident_uid, kind, source)"
        " VALUES ('e1', '2026-01-01', 'mdopp', 'meeting', 'caldav')"
    )
    conn.execute(
        "INSERT INTO event_entities (event_id, entity_id, role)"
        " VALUES ('e1', 'b', 'attendee')"
    )
    mid = person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="mdopp"
    )
    assert conn.execute("SELECT 1 FROM entities WHERE id = 'b'").fetchone() is None

    assert person_dedup.undo_merge(conn, mid, "mdopp") is True
    b = conn.execute("SELECT * FROM entities WHERE id = 'b'").fetchone()
    assert b is not None
    assert b["canonical_name"] == "Anna Meyer"
    assert b["resident_uid"] == "mdopp"
    assert b["source"] == "caldav"
    # its own facts + aliases come back.
    assert (
        conn.execute(
            "SELECT value FROM facts WHERE subject_entity_id = 'b' AND predicate = 'email'"
        ).fetchone()["value"]
        == "anna@x.de"
    )
    b_aliases = {
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = 'b'"
        ).fetchall()
    }
    assert {"Anna Meyer", "Änni"} <= b_aliases
    # ...and the PRIMARY is restored too: what the merge moved onto it is gone
    # again, what it already carried stays.
    a_aliases = {
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = 'a'"
        ).fetchall()
    }
    assert a_aliases == {"Anna Meyer", "Anni"}
    a_preds = {
        r["predicate"]
        for r in conn.execute(
            "SELECT predicate FROM facts WHERE subject_entity_id = 'a'"
        ).fetchall()
    }
    assert a_preds == {"phone"}
    assert (
        conn.execute(
            "SELECT 1 FROM event_entities WHERE event_id = 'e1' AND entity_id = 'a'"
        ).fetchone()
        is None
    )
    assert conn.execute(
        "SELECT 1 FROM event_entities WHERE event_id = 'e1' AND entity_id = 'b'"
    ).fetchone()
    # the trail is marked undone → a second undo is a no-op.
    assert person_dedup.undo_merge(conn, mid, "mdopp") is False


def test_only_the_merging_resident_may_undo(conn):
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    _person(
        conn,
        "b",
        "Anna Meyer",
        "mdopp",
        source="caldav",
        facts=[("phone", "0177 5524222")],
    )
    mid = person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="mdopp"
    )
    # a third resident can't reverse someone else's merge...
    assert person_dedup.undo_merge(conn, mid, "lena") is False
    assert person_dedup.undo_merge(conn, mid, "household") is False
    assert conn.execute("SELECT 1 FROM entities WHERE id = 'b'").fetchone() is None
    # ...the resident who made it can.
    assert person_dedup.undo_merge(conn, mid, "mdopp") is True


def test_a_household_merge_is_not_undoable_by_any_resident(conn):
    # The one case where the secondary really IS shared: gating undo on the
    # secondary's owner would hand every resident the button.
    _person(conn, "a", "Anna Meyer", "household", facts=[("phone", "0177 5524222")])
    _person(
        conn,
        "b",
        "Anna Meyer",
        "household",
        source="caldav",
        facts=[("phone", "0177 5524222")],
    )
    mid = person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="household"
    )
    assert mid is not None
    assert person_dedup.undo_merge(conn, mid, "lena") is False
    assert person_dedup.undo_merge(conn, mid, "mdopp") is False
    assert conn.execute("SELECT 1 FROM entities WHERE id = 'b'").fetchone() is None
    assert person_dedup.undo_merge(conn, mid, "household") is True


def test_generational_suffix_marks_two_different_people():
    """A suffix is the one token that asserts these are NOT the same person.
    Father and son share a full name and, on a family landline, the contact key
    too — so the subset rule alone would have offered them as duplicates."""
    from solaris_chat.engine.knowledge.person_dedup import _names_compatible

    assert _names_compatible("thomas meyer", "thomas meyer jr") is False
    assert _names_compatible("hans mueller", "hans mueller sr") is False
    assert _names_compatible("karl otto", "karl otto iii") is False
    # A middle name is not a suffix — still the same person.
    assert _names_compatible("anna meyer", "anna maria meyer") is True
    assert _names_compatible("thomas meyer", "thomas meyer") is True
