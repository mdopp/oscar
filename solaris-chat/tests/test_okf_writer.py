"""OKF write-path core (#447, docs/okf-write-contract.md §3–§6).

The OKF knowledge-index schema is owned by the alembic migration in `database/`
(#446); importing alembic from a solaris-chat test fails CI's clean env, so the
fixture creates the same tables directly from DDL that mirrors the migration.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from solaris_chat.engine.knowledge import (
    ConceptRecord,
    PendingEmbeddingQueue,
    Relationship,
    safe_slug,
)
from solaris_chat.engine.knowledge import okf, projection
from solaris_chat.engine.knowledge.writer import OkfWriter


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


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "solaris.db")
    notes_dir = str(tmp_path / "notes")
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    queue = PendingEmbeddingQueue(db_path)
    writer = OkfWriter(db_path=db_path, notes_dir=notes_dir, embedding_queue=queue)
    return writer, db_path, tmp_path


def _person(title="Anna", external_id="immich:person/1", **kw) -> ConceptRecord:
    return ConceptRecord(
        type="person",
        title=title,
        source="immich",
        external_id=external_id,
        description="A friend.",
        body="Met at the climbing gym.",
        aliases=kw.pop("aliases", []),
        relationships=kw.pop("relationships", []),
        **kw,
    )


# --- slug safety -------------------------------------------------------------


def test_safe_slug_lowercases_and_dashes():
    assert safe_slug("Anna Müller") == "anna-mueller"


def test_safe_slug_strips_path_traversal():
    # No `/`, `..`, leading dot or whitespace can survive (§2).
    assert safe_slug("../../etc/passwd") == "etc-passwd"
    assert safe_slug("  .hidden file  ") == "hidden-file"
    assert "/" not in safe_slug("a/b/c") and ".." not in safe_slug("a..b")


def test_safe_slug_empty_raises():
    with pytest.raises(ValueError):
        safe_slug("...///   ")


# --- entity resolution / dedup -----------------------------------------------


def test_new_entity_is_created(env):
    writer, db_path, _ = env
    res = writer.write_concept(_person(), ingesting_uid="mdopp")
    assert res.ref_kind == "entity" and not res.skipped
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 1
    row = conn.execute("SELECT * FROM entities").fetchone()
    assert row["canonical_name"] == "Anna" and row["resident_uid"] == "mdopp"
    conn.close()


def test_alias_dedup_reuses_entity(env):
    writer, db_path, _ = env
    writer.write_concept(_person(aliases=["Anni"]), ingesting_uid="mdopp")
    # A second source names her by the alias — must resolve to the same entity.
    second = ConceptRecord(
        type="person",
        title="Anni",
        source="caldav",
        external_id="caldav:contact/9",
        body="Birthday in May.",
    )
    res = writer.write_concept(second, ingesting_uid="mdopp")
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 1
    assert res.skipped is False
    conn.close()


def test_dedup_is_per_resident(env):
    writer, db_path, _ = env
    writer.write_concept(_person(external_id="a"), ingesting_uid="mdopp")
    writer.write_concept(_person(external_id="b"), ingesting_uid="lena")
    conn = projection.open_conn(db_path)
    # Same name, different residents -> two distinct entities (§6).
    assert projection.row_count(conn, "entities") == 2
    conn.close()


# --- ADR 0010 §1: a person converges even with an identity_key (#994) --------


def _contact(title: str, uid: str = "mdopp") -> ConceptRecord:
    """A `.contacts`-shaped person record: its identity key is derived from the
    resident + name, so it carries one (which used to skip resolve_entity)."""
    key = f"contact:{uid}:{safe_slug(title)}"
    return ConceptRecord(
        type="person",
        title=title,
        source="contact",
        external_id=key,
        identity_key=key,
        resident=uid,
    )


def test_person_with_identity_key_converges_on_the_existing_person(env):
    writer, db_path, _ = env
    first = writer.write_concept(
        _person(title="Anna Meyer", external_id="a"), ingesting_uid="mdopp"
    )
    # A second create with its OWN identity key: pre-#994 the key short-circuited
    # resolve_entity and minted a parallel "Anna Meyer".
    second = writer.write_concept(_contact("Anna Meyer"), ingesting_uid="mdopp")
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 1
    conn.close()
    assert second.ref_id == first.ref_id


def test_person_with_identity_key_refuses_a_near_miss_name(env):
    writer, db_path, _ = env
    writer.write_concept(
        _person(title="Anna Meyer", external_id="a"), ingesting_uid="mdopp"
    )
    # "Anna Meier" is NOT "Anna Meyer": convergence is exact name/alias only, so a
    # near miss stays its own person. Conflating two humans is worse than a dupe.
    writer.write_concept(_contact("Anna Meier"), ingesting_uid="mdopp")
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 2
    conn.close()


def test_person_convergence_never_crosses_residents(env):
    writer, db_path, _ = env
    writer.write_concept(
        _person(title="Anna Meyer", external_id="a"), ingesting_uid="mdopp"
    )
    # Same name, another resident's contact — and the household one. Neither may
    # converge onto mdopp's private person: that would publish her facts/aliases
    # to a scope she was never in.
    for scope in ("lena", "household"):
        writer.write_concept(_contact("Anna Meyer", uid=scope), ingesting_uid=scope)
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 3
    scopes = {
        r["resident_uid"] for r in conn.execute("SELECT resident_uid FROM entities")
    }
    assert scopes == {"mdopp", "lena", "household"}
    conn.close()


def test_non_person_identity_key_still_pins_identity(env):
    writer, db_path, _ = env
    # Two document uploads that happen to share a title must stay two entities —
    # collapsing them is data loss (#doc). The person exception is person-only.
    for upload in ("uploads/a.pdf", "uploads/b.pdf"):
        writer.write_concept(
            ConceptRecord(
                type="document",
                title="Rechnung",
                source="upload",
                external_id=f"upload:{upload}",
                identity_key=upload,
                resident="mdopp",
            ),
            ingesting_uid="mdopp",
        )
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 2
    conn.close()


def test_new_person_with_identity_key_keeps_the_deterministic_id(env):
    writer, _, _ = env
    # Nothing to converge onto -> the key still pins the id (the fast path is
    # respected, not deleted), so a re-create lands on the same entity.
    res = writer.write_concept(_contact("Bernd"), ingesting_uid="mdopp")
    assert res.ref_id == okf.deterministic_id("contact:mdopp:bernd")


# --- OKF file shape ----------------------------------------------------------


def test_okf_file_written_with_frontmatter_and_relationships(env):
    writer, _, tmp_path = env
    rec = _person(
        relationships=[Relationship("friend-of", "people/lena")],
    )
    res = writer.write_concept(rec, ingesting_uid="mdopp")
    # A real-resident concept is private-by-default under the owner's path (#576).
    assert res.okf_path == "users/mdopp/okf/people/anna.md"
    path = tmp_path / "notes" / res.okf_path
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "type: person" in text
    assert f"id: {res.ref_id}" in text  # id == .db entity id (§3)
    assert "resident: mdopp" in text
    assert "source: immich" in text
    assert "## Relationships" in text
    assert "- friend-of → [[people/lena]]" in text


def test_resident_concept_routes_under_user_path(env):
    # An explicit real-resident concept lands under users/<resident>/okf/... .
    writer, _, tmp_path = env
    rec = _person(external_id="immich:person/cd", resident="cdopp")
    res = writer.write_concept(rec, ingesting_uid="household")
    assert res.okf_path == "users/cdopp/okf/people/anna.md"
    assert (tmp_path / "notes" / res.okf_path).is_file()


def test_household_concept_stays_shared_root(env):
    writer, _, tmp_path = env
    rec = _person(external_id="immich:person/h", resident="household")
    res = writer.write_concept(rec, ingesting_uid="cdopp")
    # An explicit household resident is shared at the vault-root okf/.
    assert res.okf_path == "okf/people/anna.md"
    assert (tmp_path / "notes" / res.okf_path).is_file()


def test_event_concept_is_date_prefixed_and_event_kind(env):
    writer, db_path, tmp_path = env
    rec = ConceptRecord(
        type="event",
        title="Climbing trip",
        source="immich",
        external_id="immich:album/7",
        event_ts="2026-05-30T10:00:00",
        event_kind="trip",
        # Participants are OKF link paths (§3), resolved back to the entity.
        relationships=[Relationship("with", "people/anna")],
    )
    # Anna must exist for the event->entity edge to resolve.
    writer.write_concept(_person(), ingesting_uid="mdopp")
    res = writer.write_concept(rec, ingesting_uid="mdopp")
    assert res.ref_kind == "event"
    assert res.okf_path == "users/mdopp/okf/events/2026/2026-05-30-climbing-trip.md"
    assert (tmp_path / "notes" / res.okf_path).is_file()
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 1
    assert projection.row_count(conn, "event_entities") == 1
    edge = conn.execute("SELECT * FROM event_entities").fetchone()
    assert edge["role"] == "with"
    conn.close()


# --- .db projection ----------------------------------------------------------


def test_projection_rows_and_concept_link(env):
    writer, db_path, _ = env
    rec = _person(relationships=[Relationship("likes", "songs/x")])
    res = writer.write_concept(rec, ingesting_uid="mdopp")
    conn = projection.open_conn(db_path)
    concept = conn.execute("SELECT * FROM concepts").fetchone()
    assert concept["ref_id"] == res.ref_id
    assert concept["ref_kind"] == "entity"
    assert concept["okf_path"] == res.okf_path
    assert concept["embedding_id"]  # keyed for the embedding store (§5)
    assert concept["content_hash"] == res.content_hash
    fact = conn.execute("SELECT * FROM facts").fetchone()
    assert fact["predicate"] == "likes" and fact["value"] == "songs/x"
    conn.close()


# --- projection-only policy (ADR 0002/0005) ----------------------------------


def test_projection_only_writes_no_markdown_no_embedding(env):
    # An externally re-ingestable per-item concept (projection_only=True) lives
    # ONLY as an entity + facts + ingest_log: no OKF markdown file, no embedding
    # enqueue, and no cross-layer `concepts` link row.
    writer, db_path, tmp_path = env
    rec = ConceptRecord(
        type="song",
        title="Bohemian Rhapsody",
        source="jellyfin",
        external_id="audio/t1",
        resource="jellyfin://audio/t1",
        relationships=[Relationship("on_album", "albums/queen-a-night-at-the-opera")],
        projection_only=True,
    )
    res = writer.write_concept(rec, ingesting_uid="household")
    assert res.skipped is False
    assert res.embedded is False
    assert res.okf_path == ""
    conn = projection.open_conn(db_path)
    # The entity + its on_album fact + the idempotency marker are projected.
    assert projection.row_count(conn, "entities") == 1
    fact = conn.execute("SELECT * FROM facts WHERE predicate = 'on_album'").fetchone()
    assert fact is not None and fact["value"] == "albums/queen-a-night-at-the-opera"
    assert projection.row_count(conn, "ingest_log") == 1
    # No concepts link row (it would point at a file/embedding that don't exist).
    assert projection.row_count(conn, "concepts") == 0
    conn.close()
    # No markdown materialized anywhere under the vault.
    assert not list((tmp_path / "notes").rglob("*.md"))
    # No embedding was enqueued.
    queue_file = tmp_path / "okf_embedding_queue.jsonl"
    assert not queue_file.exists() or not queue_file.read_text().strip()


def test_projection_only_reingest_unchanged_is_skipped(env):
    # Idempotency still holds via the ingest_log marker even with no concepts row.
    writer, db_path, _ = env
    rec = ConceptRecord(
        type="song",
        title="Song",
        source="jellyfin",
        external_id="audio/t1",
        projection_only=True,
    )
    first = writer.write_concept(rec, ingesting_uid="household")
    second = writer.write_concept(
        ConceptRecord(
            type="song",
            title="Song",
            source="jellyfin",
            external_id="audio/t1",
            projection_only=True,
        ),
        ingesting_uid="household",
    )
    assert first.skipped is False and second.skipped is True
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 1
    assert projection.row_count(conn, "ingest_log") == 1
    conn.close()


def test_projection_only_event_keeps_projection_no_markdown(env):
    # A projection-only EVENT (an Immich photo) skips its OKF markdown, embedding,
    # and concepts row like a projection-only entity — but step-3 event projection
    # still runs, so the events-table row + event_entities edge survive (the photo
    # stays queryable by date/who/where without a per-photo stub).
    writer, db_path, tmp_path = env
    writer.write_concept(_person(), ingesting_uid="mdopp")  # edge target
    rec = ConceptRecord(
        type="event",
        title="Seaside",
        source="immich",
        external_id="asset/a1",
        resource="immich://asset/a1",
        event_ts="2026-06-01T09:00:00",
        event_kind="photo",
        relationships=[Relationship("depicted", "people/anna")],
        projection_only=True,
    )
    res = writer.write_concept(rec, ingesting_uid="mdopp")
    assert res.skipped is False
    assert res.ref_kind == "event"
    assert res.okf_path == "" and res.embedded is False
    conn = projection.open_conn(db_path)
    # The event + its edge are projected; no per-photo concepts row / embedding.
    assert projection.row_count(conn, "events") == 1
    assert projection.row_count(conn, "event_entities") == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM concepts WHERE ref_kind = 'event'"
        ).fetchone()[0]
        == 0
    )
    conn.close()
    # No photo markdown materialized under okf/events/.
    assert not [p for p in (tmp_path / "notes").rglob("*.md") if "events" in p.parts]


# --- idempotency -------------------------------------------------------------


def test_reingest_unchanged_is_skipped(env):
    writer, db_path, _ = env
    rec = _person()
    first = writer.write_concept(rec, ingesting_uid="mdopp")
    second = writer.write_concept(_person(), ingesting_uid="mdopp")
    assert second.skipped is True
    assert second.embedded is False
    assert second.content_hash == first.content_hash
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 1
    assert projection.row_count(conn, "concepts") == 1
    assert projection.row_count(conn, "ingest_log") == 1
    conn.close()


def test_reingest_changed_reembeds_and_updates_hash(env):
    writer, db_path, tmp_path = env
    writer.write_concept(_person(), ingesting_uid="mdopp")
    changed = _person()
    changed.description = "An old friend."
    res = writer.write_concept(changed, ingesting_uid="mdopp")
    assert res.skipped is False and res.embedded is True
    conn = projection.open_conn(db_path)
    log = conn.execute("SELECT content_hash FROM ingest_log").fetchone()
    assert log["content_hash"] == res.content_hash
    conn.close()
    # The append-only JSONL sidecar carries the (re-)embedding work: two lines
    # (initial + re-embed) for the one embedding_id; the drain worker dedups by
    # keeping the last line per embedding_id (#597).
    queue_file = tmp_path / "okf_embedding_queue.jsonl"
    lines = [json.loads(ln) for ln in queue_file.read_text().splitlines() if ln]
    assert len({e["embedding_id"] for e in lines}) == 1
    assert lines[-1]["model"] == "nomic-embed-text"


# --- embedding enqueue is O(1) (append-only, no whole-file rewrite) (#597) ---


def test_enqueue_is_append_only_no_full_read(tmp_path, monkeypatch):
    # enqueue must NOT read/serialize the whole sidecar (the old O(n^2) bug):
    # make any whole-file read explode, then prove N enqueues still succeed and
    # each appends exactly one line without re-reading prior entries.
    from pathlib import Path as _P

    queue = PendingEmbeddingQueue(str(tmp_path / "solaris.db"))

    def _boom(self, *a, **k):
        raise AssertionError("enqueue re-read the whole sidecar (not O(1))")

    monkeypatch.setattr(_P, "read_text", _boom)
    for i in range(50):
        queue.enqueue(concept_id=f"c{i}", embedding_id=f"e{i}", text="t")
    monkeypatch.undo()  # restore read_text for the verification read below.
    path = tmp_path / "okf_embedding_queue.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln]
    assert len(lines) == 50
    assert json.loads(lines[0])["embedding_id"] == "e0"


def test_enqueue_rotates_legacy_dict_sidecar_aside(tmp_path):
    # An old whole-file dict-format .json (pre-#597) is rotated to .legacy on
    # first construction, since nothing drains it; the JSONL path starts clean.
    legacy = tmp_path / "okf_embedding_queue.json"
    legacy.write_text('{"e-old": {"concept_id": "c", "model": "m", "text": "t"}}')
    queue = PendingEmbeddingQueue(str(tmp_path / "solaris.db"))
    queue.enqueue(concept_id="c1", embedding_id="e1", text="t")
    assert not legacy.exists()
    assert (tmp_path / "okf_embedding_queue.json.legacy").exists()
    lines = (tmp_path / "okf_embedding_queue.jsonl").read_text().splitlines()
    assert [json.loads(ln)["embedding_id"] for ln in lines if ln] == ["e1"]


def test_default_scope_is_ingesting_resident(env):
    writer, db_path, _ = env
    writer.write_concept(_person(), ingesting_uid="lena")
    conn = projection.open_conn(db_path)
    assert conn.execute("SELECT resident_uid FROM entities").fetchone()[0] == "lena"
    conn.close()


def test_explicit_household_scope_wins(env):
    writer, db_path, _ = env
    rec = _person()
    rec.resident = "household"
    writer.write_concept(rec, ingesting_uid="mdopp")
    conn = projection.open_conn(db_path)
    assert (
        conn.execute("SELECT resident_uid FROM entities").fetchone()[0] == "household"
    )
    conn.close()
