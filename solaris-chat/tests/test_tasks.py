"""Aufgaben (to-do) — task entity CRUD, chat tools, and dated→calendar (#todo)."""

from __future__ import annotations

import json
import sqlite3

from solaris_chat.engine import document_deadlines_sync as deadlines
from solaris_chat.engine import tasks
from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.tools import tasks_tools
from solaris_chat.engine.tools.tasks_tools import build_tasks_tools

# entities/facts/concepts/ingest_log are what write_concept + the reads touch.
_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (entity_id, alias));
CREATE INDEX entity_aliases_alias_idx ON entity_aliases (alias);
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
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
"""


def _env(tmp_path):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db, str(tmp_path / "notes")


def test_create_list_and_complete(tmp_path):
    db, notes = _env(tmp_path)
    tid = tasks.create_task(
        db_path=db,
        notes_dir=notes,
        uid="mdopp",
        title="Rechnung bezahlen",
        due="2026-08-01",
    )
    open_ = tasks.list_tasks(db, "mdopp")
    assert len(open_) == 1
    t = open_[0]
    assert t["id"] == tid and t["title"] == "Rechnung bezahlen"
    assert (
        t["due"] == "2026-08-01" and t["status"] == "open" and t["source"] == "manual"
    )

    # Complete it → gone from the default (open-only) list, present with done=True.
    assert tasks.set_status(db_path=db, uid="mdopp", entity_id=tid, status="done")
    assert tasks.list_tasks(db, "mdopp") == []
    done = tasks.list_tasks(db, "mdopp", include_done=True)
    assert len(done) == 1 and done[0]["status"] == "done"


def test_resolved_at_stamped_and_cleared(tmp_path):
    db, notes = _env(tmp_path)
    tid = tasks.create_task(db_path=db, notes_dir=notes, uid="mdopp", title="X")
    tasks.set_status(db_path=db, uid="mdopp", entity_id=tid, status="done")
    done = tasks.list_tasks(db, "mdopp", include_done=True)[0]
    assert done["status"] == "done" and done["resolved_at"]  # stamped
    # Re-opening clears the resolution timestamp.
    tasks.set_status(db_path=db, uid="mdopp", entity_id=tid, status="open")
    reopened = tasks.list_tasks(db, "mdopp")[0]
    assert reopened["status"] == "open" and reopened["resolved_at"] == ""


def test_update_changes_title_and_due(tmp_path):
    db, notes = _env(tmp_path)
    tid = tasks.create_task(
        db_path=db, notes_dir=notes, uid="mdopp", title="alt", due="2026-08-01"
    )
    assert tasks.update(
        db_path=db, uid="mdopp", entity_id=tid, title="neu", due="2026-09-09"
    )
    t = tasks.list_tasks(db, "mdopp")[0]
    assert t["title"] == "neu" and t["due"] == "2026-09-09" and t["status"] == "open"

    # Clearing the due date drops it; status is preserved.
    assert tasks.update(db_path=db, uid="mdopp", entity_id=tid, title="neu", due="")
    assert tasks.list_tasks(db, "mdopp")[0]["due"] == ""

    # Not visible to another resident → no-op False.
    assert not tasks.update(
        db_path=db, uid="lena", entity_id=tid, title="fremd", due=""
    )
    assert tasks.list_tasks(db, "mdopp")[0]["title"] == "neu"


def test_owner_scoped(tmp_path):
    db, notes = _env(tmp_path)
    tasks.create_task(db_path=db, notes_dir=notes, uid="mdopp", title="privat")
    tasks.create_task(db_path=db, notes_dir=notes, uid="household", title="geteilt")
    # lena sees only the shared household task, not mdopp's private one.
    titles = {t["title"] for t in tasks.list_tasks(db, "lena")}
    assert titles == {"geteilt"}
    # mdopp sees own + shared.
    assert {t["title"] for t in tasks.list_tasks(db, "mdopp")} == {"privat", "geteilt"}


def test_open_dated_first(tmp_path):
    db, notes = _env(tmp_path)
    tasks.create_task(db_path=db, notes_dir=notes, uid="mdopp", title="undatiert")
    tasks.create_task(
        db_path=db, notes_dir=notes, uid="mdopp", title="später", due="2026-12-01"
    )
    tasks.create_task(
        db_path=db, notes_dir=notes, uid="mdopp", title="bald", due="2026-08-01"
    )
    order = [t["title"] for t in tasks.list_tasks(db, "mdopp")]
    assert order == ["bald", "später", "undatiert"]  # dated-by-due first, undated last


async def test_chat_tools_roundtrip(tmp_path):
    db, notes = _env(tmp_path)
    tools = {t.name: t for t in build_tasks_tools(db, lambda: "mdopp", notes_dir=notes)}
    add = json.loads(await tools["task_add"].handler({"title": "Milch kaufen"}))
    assert add["ok"] and add["title"] == "Milch kaufen"
    listed = json.loads(await tools["task_list"].handler({}))
    assert [t["title"] for t in listed["tasks"]] == ["Milch kaufen"]
    done = json.loads(await tools["task_done"].handler({"title": "milch"}))
    assert done["ok"] and done["status"] == "done"
    assert json.loads(await tools["task_list"].handler({}))["tasks"] == []


def _rows(db, sql, params=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def test_delete_removes_every_projection_row_and_the_ingest_marker(tmp_path):
    db, notes = _env(tmp_path)
    tid = tasks.create_task(
        db_path=db, notes_dir=notes, uid="mdopp", title="Testeintrag", due="2026-09-01"
    )
    keep = tasks.create_task(db_path=db, notes_dir=notes, uid="mdopp", title="bleibt")
    # A task carries no aliases/event edges of its own; seed both so the
    # child-first cascade is actually exercised (FKs are ON in projection conns).
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias) VALUES (?, 'alias')", (tid,)
    )
    conn.execute(
        "INSERT INTO events (id, ts, resident_uid, kind, source)"
        " VALUES ('e1', '2026-09-01', 'mdopp', 'note', 'task')"
    )
    conn.execute(
        "INSERT INTO event_entities (event_id, entity_id, role)"
        " VALUES ('e1', ?, 'subject')",
        (tid,),
    )
    conn.commit()
    conn.close()
    assert _rows(db, "SELECT 1 FROM ingest_log WHERE source = 'task'") != []

    assert tasks.delete_task(db_path=db, uid="mdopp", entity_id=tid)

    assert _rows(db, "SELECT 1 FROM entities WHERE id = ?", (tid,)) == []
    assert _rows(db, "SELECT 1 FROM facts WHERE subject_entity_id = ?", (tid,)) == []
    assert _rows(db, "SELECT 1 FROM entity_aliases WHERE entity_id = ?", (tid,)) == []
    assert _rows(db, "SELECT 1 FROM event_entities WHERE entity_id = ?", (tid,)) == []
    # Only the deleted task's idempotency marker went — the other task keeps its
    # own, so a re-created task with a fresh key still writes.
    assert len(_rows(db, "SELECT 1 FROM ingest_log WHERE source = 'task'")) == 1
    assert [t["id"] for t in tasks.list_tasks(db, "mdopp", include_done=True)] == [keep]


def test_delete_is_owner_scoped(tmp_path):
    db, notes = _env(tmp_path)
    tid = tasks.create_task(db_path=db, notes_dir=notes, uid="mdopp", title="privat")
    assert not tasks.delete_task(db_path=db, uid="lena", entity_id=tid)
    assert len(tasks.list_tasks(db, "mdopp")) == 1


def test_resolve_task_reports_candidates_instead_of_guessing(tmp_path):
    db, notes = _env(tmp_path)
    tasks.create_task(db_path=db, notes_dir=notes, uid="mdopp", title="Milch kaufen")
    tasks.create_task(db_path=db, notes_dir=notes, uid="mdopp", title="Brot kaufen")
    unknown = tasks.resolve_task(db, "mdopp", entity_id="nichtda")
    assert unknown["error"] and set(unknown["candidates"]) == {
        "Milch kaufen",
        "Brot kaufen",
    }
    assert tasks.resolve_task(db, "mdopp", title="kaufen")["error"] == "mehrdeutig"
    assert tasks.resolve_task(db, "mdopp", title="milch")["title"] == "Milch kaufen"


async def test_task_delete_tool_refuses_without_confirmation(tmp_path, monkeypatch):
    db, notes = _env(tmp_path)
    tid = tasks.create_task(db_path=db, notes_dir=notes, uid="mdopp", title="Milch")
    monkeypatch.setattr(
        tasks_tools,
        "cascade_task_event_configured",
        _no_cascade("must not cascade an unconfirmed delete"),
    )
    tools = {t.name: t for t in build_tasks_tools(db, lambda: "mdopp", notes_dir=notes)}
    out = json.loads(await tools["task_delete"].handler({"id": tid}))
    assert out["ok"] is False and out["needs_confirmation"] is True
    # Nothing removed — the row is still there.
    assert [t["id"] for t in tasks.list_tasks(db, "mdopp")] == [tid]
    # An unknown id is an error naming the candidates, never a silent success.
    unknown = json.loads(
        await tools["task_delete"].handler({"id": "nope", "confirmed": True})
    )
    assert unknown["ok"] is False and unknown["candidates"] == ["Milch"]
    assert [t["id"] for t in tasks.list_tasks(db, "mdopp")] == [tid]


def _no_cascade(msg: str):
    async def _boom(*a, **kw):
        raise AssertionError(msg)

    return _boom


async def test_task_delete_tool_cascades_caldav_before_the_row_goes(
    tmp_path, monkeypatch
):
    db, notes = _env(tmp_path)
    tid = tasks.create_task(
        db_path=db, notes_dir=notes, uid="mdopp", title="Milch", due="2026-09-01"
    )
    seen: list[tuple[str, bool, bool]] = []

    async def _record(db_path, entity_id, *, deleted=False):
        still_there = _rows(
            db_path, "SELECT 1 FROM entities WHERE id = ?", (entity_id,)
        )
        seen.append((entity_id, deleted, bool(still_there)))

    monkeypatch.setattr(tasks_tools, "cascade_task_event_configured", _record)
    tools = {t.name: t for t in build_tasks_tools(db, lambda: "mdopp", notes_dir=notes)}
    out = json.loads(
        await tools["task_delete"].handler({"title": "milch", "confirmed": True})
    )
    assert out["ok"] and out["deleted"] == "Milch"
    # The VTODO was removed with the row still present (owner routing) …
    assert seen == [(tid, True, True)]
    # … and the task is gone from every projection table afterwards.
    assert tasks.list_tasks(db, "mdopp", include_done=True) == []
    assert _rows(db, "SELECT 1 FROM facts WHERE subject_entity_id = ?", (tid,)) == []


async def test_dated_task_becomes_vtodo(tmp_path, monkeypatch):
    db, notes = _env(tmp_path)
    # Household-scoped: only shared tasks reach the shared Fristen collection.
    tid = tasks.create_task(
        db_path=db,
        notes_dir=notes,
        uid=projection.SHARED_UID,
        title="TÜV Termin",
        due="2026-09-15",
    )
    tasks.create_task(
        db_path=db, notes_dir=notes, uid=projection.SHARED_UID, title="ohne Datum"
    )

    put: list[tuple[str, str]] = []

    async def _fake_put(self, collection_url, uid, body, **kw):
        put.append((uid, body))
        return "ok"

    monkeypatch.setattr(deadlines.HttpDavClient, "put_item", _fake_put)
    out = await deadlines.sync_deadlines(db, "http://dav/cal", "solaris", "pw")
    assert out["written"] == 1  # only the dated task
    uid, ics = put[0]
    assert uid == f"solaris-task-{tid}"
    assert "TÜV Termin" in ics and "VALARM" in ics
    assert "BEGIN:VTODO" in ics and "BEGIN:VEVENT" not in ics


async def test_completed_task_not_written_to_calendar(tmp_path, monkeypatch):
    db, notes = _env(tmp_path)
    tid = tasks.create_task(
        db_path=db, notes_dir=notes, uid="mdopp", title="erledigt", due="2026-09-15"
    )
    tasks.set_status(db_path=db, uid="mdopp", entity_id=tid, status="done")
    monkeypatch.setattr(
        deadlines.HttpDavClient,
        "put_item",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not PUT")),
    )
    out = await deadlines.sync_deadlines(db, "http://dav/cal", "solaris", "pw")
    assert out["written"] == 0
