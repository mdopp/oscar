"""Structured + semantic merge in notes_search (#651).

The OKF knowledge-index schema is owned by the alembic migration in `database/`;
importing alembic from a solaris-chat test fails CI's clean env, so the fixture
mirrors the DDL directly (same idiom as test_okf_writer.py).
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from solaris_chat import notes_index
from solaris_chat.engine.tools.notes import build_notes_tools

# Mirrors 0016_okf_knowledge_index + 0018_okf_vectors.
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
CREATE TABLE okf_vectors (
  embedding_id TEXT PRIMARY KEY, concept_id TEXT NOT NULL, model TEXT NOT NULL,
  dim INTEGER NOT NULL, vector BLOB NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
"""


def _okf_file(root, rel: str, title: str, resident: str = "household") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nresident: {resident}\n---\n\n# {title}\n\nBody.\n")


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "solaris.db")
    root = tmp_path / "notes"
    root.mkdir()
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path, root


def _add_entity(db_path, ent_id, name, resident, aliases, okf_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'person', ?, ?, 'test', 'h')",
        (ent_id, name, resident),
    )
    for a in aliases:
        conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?)", (ent_id, a)
        )
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash)"
        " VALUES (?, ?, 'entity', ?, 'h')",
        (f"c-{ent_id}", ent_id, okf_path),
    )
    conn.commit()
    conn.close()


def _search_tool(root, db_path, uid, embed=None):
    for tool in build_notes_tools(str(root), lambda: uid, db_path=db_path, embed=embed):
        if tool.name == "notes_search":
            return tool
    raise AssertionError("notes_search tool missing")


async def _search(root, db_path, uid, query, embed=None, **kw):
    # The FTS index (#830) is what notes_search now queries for keyword candidates;
    # on the box the boot backfill fills it — mirror that here before searching.
    notes_index.backfill(db_path, str(root))
    tool = _search_tool(root, db_path, uid, embed)
    return json.loads(await tool.handler({"query": query, **kw}))


# ---- alias-exact -------------------------------------------------------------


async def test_alias_exact_hit_found(env):
    db_path, root = env
    _okf_file(root, "okf/people/anna.md", "Anna")
    _add_entity(db_path, "e1", "Anna", "household", ["Aennchen"], "okf/people/anna.md")
    hits = await _search(root, db_path, "household", "Aennchen")
    assert [h["path"] for h in hits] == ["okf/people/anna.md"]


async def test_alias_of_other_resident_not_found(env):
    db_path, root = env
    _okf_file(root, "users/bob/okf/people/x.md", "Geheim", resident="bob")
    _add_entity(db_path, "e1", "Geheim", "bob", ["Xaver"], "users/bob/okf/people/x.md")
    # caller 'anna' must never see bob's private alias hit.
    hits = await _search(root, db_path, "anna", "Xaver")
    assert hits == []


# ---- events after/before -----------------------------------------------------


async def test_events_between_returns_participants(env):
    db_path, root = env
    _okf_file(root, "okf/events/ev1.md", "Treffen")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (id, ts, resident_uid, kind, source)"
        " VALUES ('ev1', '2026-06-30T18:00', 'household', 'meeting', 'test')"
    )
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES ('p1', 'person', 'Anna', 'household', 't', 'h')"
    )
    conn.execute(
        "INSERT INTO event_entities (event_id, entity_id, role)"
        " VALUES ('ev1', 'p1', 'participant')"
    )
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash)"
        " VALUES ('c1', 'ev1', 'event', 'okf/events/ev1.md', 'h')"
    )
    conn.commit()
    conn.close()
    hits = await _search(
        root, db_path, "household", "wen", after="2026-06-28", before="2026-07-05"
    )
    ev = next(h for h in hits if h["path"] == "okf/events/ev1.md")
    assert "Anna" in ev["snippet"]
    assert ev["date"] == "2026-06-30T18:00"


async def test_events_outside_range_excluded(env):
    db_path, root = env
    _okf_file(root, "okf/events/ev1.md", "Treffen")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (id, ts, resident_uid, kind, source)"
        " VALUES ('ev1', '2026-01-01T10:00', 'household', 'meeting', 'test')"
    )
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash)"
        " VALUES ('c1', 'ev1', 'event', 'okf/events/ev1.md', 'h')"
    )
    conn.commit()
    conn.close()
    hits = await _search(
        root, db_path, "household", "wen", after="2026-06-28", before="2026-07-05"
    )
    assert all(h["path"] != "okf/events/ev1.md" for h in hits)


# ---- anchor boost ------------------------------------------------------------


async def test_topic_anchor_boosts_ordering(env):
    db_path, root = env
    # Both mention 'urlaub' in the body only (equal fuzzy); the #urlaub anchor
    # lifts the topic-tagged note above the plain one.
    (root / "tagged.md").write_text("# A\n\n#topic/urlaub\nurlaub\n")
    (root / "plain.md").write_text("# B\n\nurlaub urlaub\n")
    hits = await _search(root, db_path, "household", "urlaub #urlaub")
    order = [h["path"] for h in hits]
    assert order.index("tagged.md") < order.index("plain.md")


# ---- semantic branch (PR 2) --------------------------------------------------


class _FakeEmbed:
    def __init__(self, vector, delay=0.0):
        self._vector = vector
        self._delay = delay

    async def embed(self, model, inputs):
        import asyncio

        if self._delay:
            await asyncio.sleep(self._delay)
        return [self._vector]


def _add_vector(db_path, embedding_id, ref_id, ref_kind, vector):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE concepts SET embedding_id = ? WHERE ref_id = ? AND ref_kind = ?",
        (embedding_id, ref_id, ref_kind),
    )
    conn.execute(
        "INSERT INTO okf_vectors (embedding_id, concept_id, model, dim, vector)"
        " VALUES (?, ?, 'nomic-embed-text', ?, ?)",
        (
            embedding_id,
            ref_id,
            len(vector),
            np.asarray(vector, dtype=np.float32).tobytes(),
        ),
    )
    conn.commit()
    conn.close()


async def test_semantic_hit_when_fuzzy_sparse(env):
    db_path, root = env
    _okf_file(root, "okf/people/anna.md", "Anna")
    _add_entity(db_path, "e1", "Anna", "household", [], "okf/people/anna.md")
    _add_vector(db_path, "emb1", "e1", "entity", [1.0, 0.0, 0.0])
    embed = _FakeEmbed([1.0, 0.0, 0.0])
    # 'kletterfreundin' matches nothing fuzzy/alias → semantic branch runs.
    hits = await _search(root, db_path, "household", "kletterfreundin", embed=embed)
    assert any(h["path"] == "okf/people/anna.md" for h in hits)


async def test_semantic_skipped_when_enough_fuzzy_hits(env):
    db_path, root = env
    for i in range(3):
        (root / f"n{i}.md").write_text(f"# Urlaub {i}\n\nurlaub urlaub urlaub\n")
    _okf_file(root, "okf/people/anna.md", "Anna")
    _add_entity(db_path, "e1", "Anna", "household", [], "okf/people/anna.md")
    _add_vector(db_path, "emb1", "e1", "entity", [1.0, 0.0, 0.0])

    class _Boom:
        async def embed(self, model, inputs):
            raise AssertionError("semantic branch must be skipped")

    hits = await _search(root, db_path, "household", "urlaub", embed=_Boom())
    assert len(hits) >= 3


async def test_semantic_timeout_degrades(env):
    db_path, root = env
    _okf_file(root, "okf/people/anna.md", "Anna")
    _add_entity(db_path, "e1", "Anna", "household", [], "okf/people/anna.md")
    _add_vector(db_path, "emb1", "e1", "entity", [1.0, 0.0, 0.0])
    slow = _FakeEmbed([1.0, 0.0, 0.0], delay=5.0)
    # embed exceeds the 2s guard → degrade to structured (empty) result.
    hits = await _search(root, db_path, "household", "kletterfreundin", embed=slow)
    assert hits == []
