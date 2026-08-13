"""Document deadlines + dated tasks → PER-RESIDENT Solaris calendars (#doc-graph,
#997).

`document_deadlines_sync.sync_deadlines` reads each document's dated facts and
each resident's dated OPEN tasks and PUTs one all-day, alarmed object per item
into a `solaris` collection under the owner's OWN principal
(`{base}/{uid}/solaris/`, option A / #1011) — a document deadline as a VEVENT,
a task as a VTODO (#1127). These tests mock `put_item`/`ensure_calendar` and
prove the per-resident collection URLs, the component/alarm content, the stable
overwrite UID, owner-scoping (a resident's task never reaches another's
calendar), that non-deadline / unparseable dates are skipped, and the disabled
no-op.
"""

from __future__ import annotations

import sqlite3

from solaris_chat.engine import document_deadlines_sync
from solaris_chat.engine.document_deadlines_sync import (
    cascade_task_event,
    sync_deadlines,
)

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

_BASE = "https://caldav.example/dav"
_SHARED = document_deadlines_sync.projection.SHARED_UID
_CALENDAR = document_deadlines_sync._CALENDAR
# Document deadlines are household data → the `solaris` calendar under the
# household principal (option A / #1011).
_SHARED_URL = f"{_BASE}/{_SHARED}/{_CALENDAR}/"


def _doc(conn, eid, title, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'document', ?, 'mdopp', 'documents', 'h')",
        (eid, title),
    )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, 'mdopp', ?, ?, 0.6, 'd')",
            (f"{eid}-{i}", eid, pred, val),
        )


def _task(conn, eid, title, resident_uid, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'task', ?, ?, 'todo', 'h')",
        (eid, title, resident_uid),
    )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, 0.6, 't')",
            (f"{eid}-{i}", eid, resident_uid, pred, val),
        )


def _db(tmp_path, facts):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _doc(conn, "d1", "ERGO Rechtsschutz", facts)
    conn.commit()
    conn.close()
    return db


def _capture(monkeypatch):
    calls = []
    ensured = []

    async def fake_put(self, collection_url, uid, body, *, suffix, content_type):
        calls.append(
            {"url": collection_url, "uid": uid, "body": body, "suffix": suffix}
        )
        return collection_url + uid

    async def fake_ensure(self, collection_url):
        ensured.append(collection_url)

    monkeypatch.setattr(document_deadlines_sync.HttpDavClient, "put_item", fake_put)
    monkeypatch.setattr(
        document_deadlines_sync.HttpDavClient, "ensure_calendar", fake_ensure
    )
    return calls, ensured


async def test_deadline_puts_alarmed_all_day_event(tmp_path, monkeypatch):
    calls, ensured = _capture(monkeypatch)
    db = _db(
        tmp_path,
        [
            ("provider", "ERGO"),
            ("policy_number", "SV 072714970"),
            ("cancellation_deadline", "2026-12-15"),
        ],
    )
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out == {"written": 1, "skipped": 0, "failed": 0}
    c = calls[0]
    # A document deadline lands in the household principal's calendar.
    assert c["url"] == _SHARED_URL and c["suffix"] == ".ics"
    assert _SHARED_URL in ensured  # collection ensured before its PUTs
    assert c["uid"] == "solaris-deadline-d1-cancellation_deadline"
    # A document deadline stays a VEVENT — only tasks became VTODOs (#1127).
    assert "BEGIN:VEVENT" in c["body"] and "BEGIN:VTODO" not in c["body"]
    assert "SUMMARY:Kündigungsfrist: ERGO Rechtsschutz" in c["body"]
    assert "DTSTART;VALUE=DATE:20261215" in c["body"]
    assert "BEGIN:VALARM" in c["body"] and "TRIGGER:-P14D" in c["body"]
    assert "ERGO" in c["body"] and "SV 072714970" in c["body"]


async def test_multiple_deadlines_each_put(tmp_path, monkeypatch):
    calls, _ = _capture(monkeypatch)
    db = _db(
        tmp_path,
        [("cancellation_deadline", "2026-12-15"), ("hu_date", "2027-03-01")],
    )
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out["written"] == 2 and len(calls) == 2


async def test_unparseable_date_skipped(tmp_path, monkeypatch):
    calls, _ = _capture(monkeypatch)
    db = _db(tmp_path, [("cancellation_deadline", "zum Vertragsende")])
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out == {"written": 0, "skipped": 1, "failed": 0}
    assert calls == []


async def test_non_deadline_date_ignored(tmp_path, monkeypatch):
    calls, _ = _capture(monkeypatch)
    db = _db(tmp_path, [("start_date", "2026-01-01")])
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out == {"written": 0, "skipped": 0, "failed": 0}
    assert calls == []


async def test_task_lands_in_owning_residents_calendar(tmp_path, monkeypatch):
    # #997 owner-scoping: a resident's private dated task is written ONLY under
    # that resident's own DAV context, and a household task under SHARED_UID —
    # a resident's task must never reach another resident's calendar.
    calls, ensured = _capture(monkeypatch)
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _task(
        conn,
        "t-lena",
        "Gynäkologe",
        "lena",
        [("status", "open"), ("due", "2026-09-01"), ("title_text", "Gynäkologe")],
    )
    _task(
        conn,
        "t-shared",
        "Müll rausbringen",
        _SHARED,
        [("status", "open"), ("due", "2026-09-02"), ("title_text", "Müll rausbringen")],
    )
    conn.commit()
    conn.close()
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out == {"written": 2, "skipped": 0, "failed": 0}
    by_uid = {c["uid"]: c for c in calls}
    lena_url = f"{_BASE}/lena/{_CALENDAR}/"
    # Both tasks reach the phone as to-dos, not as appointments (#1127).
    for c in calls:
        assert "BEGIN:VTODO" in c["body"] and "BEGIN:VEVENT" not in c["body"]
        assert "STATUS:NEEDS-ACTION" in c["body"]
    assert "DUE;VALUE=DATE:20260901" in by_uid["solaris-task-t-lena"]["body"]
    # Lena's task → only Lena's calendar; the household task → only SHARED.
    assert by_uid["solaris-task-t-lena"]["url"] == lena_url
    assert by_uid["solaris-task-t-shared"]["url"] == _SHARED_URL
    # Owner-safety: Lena's task never PUT to the household (B's) URL, and vice versa.
    assert not any(
        c["uid"] == "solaris-task-t-lena" and c["url"] == _SHARED_URL for c in calls
    )
    assert not any(
        c["uid"] == "solaris-task-t-shared" and c["url"] == lena_url for c in calls
    )
    # Each owner's collection ensured before its PUTs.
    assert lena_url in ensured and _SHARED_URL in ensured
    # And Lena's private title never leaks into the household calendar's bodies.
    assert all("Gynäkologe" not in c["body"] for c in calls if c["url"] == _SHARED_URL)


async def test_household_items_routed_to_primary_resident(tmp_path, monkeypatch):
    # #1011: `household` isn't a real Radicale principal (writing under it 409s),
    # so with a configured primary resident the household document deadline AND a
    # household task are routed to THAT resident's own `/uid/solaris/` calendar.
    calls, ensured = _capture(monkeypatch)
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _doc(conn, "d1", "ERGO Rechtsschutz", [("cancellation_deadline", "2026-12-15")])
    _task(
        conn,
        "t-shared",
        "Müll rausbringen",
        _SHARED,
        [("status", "open"), ("due", "2026-09-02"), ("title_text", "Müll rausbringen")],
    )
    conn.commit()
    conn.close()
    out = await sync_deadlines(db, _BASE, "solaris", "pw", household_uid="mdopp")
    assert out == {"written": 2, "skipped": 0, "failed": 0}
    mdopp_url = f"{_BASE}/mdopp/{_CALENDAR}/"
    # Both the household deadline and the household task land on mdopp's calendar,
    # and nothing is written under the principal-less `household` uid.
    assert all(c["url"] == mdopp_url for c in calls)
    assert _SHARED_URL not in ensured
    by_uid = {c["uid"]: c for c in calls}
    assert set(by_uid) == {
        "solaris-deadline-d1-cancellation_deadline",
        "solaris-task-t-shared",
    }
    # Same collection, two component types: the deadline stays a VEVENT, the
    # task is a VTODO (#1127).
    assert "BEGIN:VEVENT" in by_uid["solaris-deadline-d1-cancellation_deadline"]["body"]
    assert "BEGIN:VTODO" in by_uid["solaris-task-t-shared"]["body"]


async def test_resolved_task_not_written(tmp_path, monkeypatch):
    calls, _ = _capture(monkeypatch)
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _task(
        conn,
        "t-done",
        "erledigt",
        "lena",
        [("status", "done"), ("due", "2026-09-01"), ("title_text", "erledigt")],
    )
    conn.commit()
    conn.close()
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out == {"written": 0, "skipped": 0, "failed": 0}
    assert calls == []


async def test_disabled_when_unconfigured(tmp_path, monkeypatch):
    calls, ensured = _capture(monkeypatch)
    db = _db(tmp_path, [("cancellation_deadline", "2026-12-15")])
    assert await sync_deadlines(db, "", "solaris", "pw") == {
        "written": 0,
        "skipped": 0,
        "failed": 0,
    }
    assert calls == [] and ensured == []


def _capture_cascade(monkeypatch):
    """Record PUT/DELETE/ensure so cascade tests can assert the single-event op."""
    puts, deletes, ensured = [], [], []

    async def fake_put(self, collection_url, uid, body, *, suffix, content_type):
        puts.append({"url": collection_url, "uid": uid, "body": body})
        return collection_url + uid

    async def fake_delete(self, collection_url, uid, *, suffix=".ics"):
        deletes.append({"url": collection_url, "uid": uid})

    async def fake_ensure(self, collection_url):
        ensured.append(collection_url)

    monkeypatch.setattr(document_deadlines_sync.HttpDavClient, "put_item", fake_put)
    monkeypatch.setattr(
        document_deadlines_sync.HttpDavClient, "delete_item", fake_delete
    )
    monkeypatch.setattr(
        document_deadlines_sync.HttpDavClient, "ensure_calendar", fake_ensure
    )
    return puts, deletes, ensured


def _one_task(tmp_path, resident_uid, facts):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _task(conn, "t1", "Zahnarzt", resident_uid, facts)
    conn.commit()
    conn.close()
    return db


async def test_cascade_add_with_due_puts(tmp_path, monkeypatch):
    # add-with-due → single PUT of the task's deterministic UID into its owner's
    # collection, ensured first, as an open VTODO (#1127).
    puts, deletes, ensured = _capture_cascade(monkeypatch)
    db = _one_task(
        tmp_path,
        "lena",
        [("status", "open"), ("due", "2026-09-01"), ("title_text", "Zahnarzt")],
    )
    await cascade_task_event(db, "t1", _BASE, "solaris", "pw")
    lena_url = f"{_BASE}/lena/{_CALENDAR}/"
    assert deletes == []
    assert len(puts) == 1
    assert puts[0]["url"] == lena_url and puts[0]["uid"] == "solaris-task-t1"
    assert "BEGIN:VTODO" in puts[0]["body"]
    assert "BEGIN:VEVENT" not in puts[0]["body"]
    assert "STATUS:NEEDS-ACTION" in puts[0]["body"]
    assert "SUMMARY:Aufgabe: Zahnarzt" in puts[0]["body"]
    assert "DUE;VALUE=DATE:20260901" in puts[0]["body"]
    # A VTODO's trigger anchors to DTSTART by default; a task only has a DUE.
    assert "TRIGGER;RELATED=END:-P14D" in puts[0]["body"]
    assert lena_url in ensured


async def test_cascade_status_done_deletes(tmp_path, monkeypatch):
    # status→done → the VTODO is DELETEd, not written, even though it still has a
    # due date — that's the round-trip that clears it off the phone (#1127).
    puts, deletes, _ = _capture_cascade(monkeypatch)
    db = _one_task(
        tmp_path,
        "lena",
        [("status", "done"), ("due", "2026-09-01"), ("title_text", "Zahnarzt")],
    )
    await cascade_task_event(db, "t1", _BASE, "solaris", "pw")
    assert puts == []
    assert len(deletes) == 1
    assert deletes[0]["url"] == f"{_BASE}/lena/{_CALENDAR}/"
    assert deletes[0]["uid"] == "solaris-task-t1"


async def test_cascade_due_change_reputs(tmp_path, monkeypatch):
    # due-change → re-PUT under the SAME UID so the calendar app overwrites in place.
    puts, deletes, _ = _capture_cascade(monkeypatch)
    db = _one_task(
        tmp_path,
        "lena",
        [("status", "open"), ("due", "2026-10-15"), ("title_text", "Zahnarzt")],
    )
    await cascade_task_event(db, "t1", _BASE, "solaris", "pw")
    assert deletes == []
    assert len(puts) == 1 and puts[0]["uid"] == "solaris-task-t1"
    assert "DUE;VALUE=DATE:20261015" in puts[0]["body"]


async def test_cascade_household_task_routes_to_primary(tmp_path, monkeypatch):
    # A household task cascades to the primary resident's calendar (#1011 routing),
    # matching the nightly sync.
    puts, _, _ = _capture_cascade(monkeypatch)
    db = _one_task(
        tmp_path,
        _SHARED,
        [("status", "open"), ("due", "2026-09-01"), ("title_text", "Müll")],
    )
    await cascade_task_event(db, "t1", _BASE, "solaris", "pw", household_uid="mdopp")
    assert len(puts) == 1
    assert puts[0]["url"] == f"{_BASE}/mdopp/{_CALENDAR}/"


async def test_cascade_disabled_when_unconfigured(tmp_path, monkeypatch):
    puts, deletes, ensured = _capture_cascade(monkeypatch)
    db = _one_task(
        tmp_path,
        "lena",
        [("status", "open"), ("due", "2026-09-01"), ("title_text", "Zahnarzt")],
    )
    await cascade_task_event(db, "t1", "", "solaris", "pw")
    assert puts == [] and deletes == [] and ensured == []
