"""Calendar + contacts ingest adapter (#207, docs/okf-write-contract.md §6).

The dav client is mocked (no live CalDAV/CardDAV server): a `FakeDavClient`
yields `CalEvent`/`Contact` dataclasses, so these cover the event→event and
contact→person mapping, the attendee→person `with` resolution, phone/email
facts, the idempotent skip and the incremental sync-token passthrough — without
touching the DAV protocol layer.

Schema is built from inlined DDL mirroring the #446 migration (importing alembic
from a solaris-chat test fails CI's clean env).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest
from defusedxml.common import EntitiesForbidden

from solaris_chat.engine.ingest import DavIngest
from solaris_chat.engine.ingest.dav_client import CalEvent, Contact
from solaris_chat.engine.knowledge import projection
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


class FakeDavClient:
    """A mocked read-only CalDAV/CardDAV source yielding canned dataclasses."""

    def __init__(
        self,
        *,
        events: list[CalEvent] | None = None,
        contacts: list[Contact] | None = None,
    ):
        self.events = events or []
        self.contacts = contacts or []
        self.event_tokens_seen: list[str] = []
        self.contact_tokens_seen: list[str] = []

    async def iter_events(self, *, sync_token: str = "") -> AsyncIterator[CalEvent]:
        self.event_tokens_seen.append(sync_token)
        for event in self.events:
            yield event

    async def iter_contacts(self, *, sync_token: str = "") -> AsyncIterator[Contact]:
        self.contact_tokens_seen.append(sync_token)
        for contact in self.contacts:
            yield contact


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "solaris.db")
    notes_dir = str(tmp_path / "notes")
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    writer = OkfWriter(db_path=db_path, notes_dir=notes_dir)
    return writer, db_path, tmp_path


def _event(**kw) -> CalEvent:
    base = dict(
        uid="ev-1",
        title="Team Dinner",
        start="2026-05-30T19:00:00",
    )
    base.update(kw)
    return CalEvent(**base)


def _contact(**kw) -> Contact:
    base = dict(uid="c-1", name="Anna Müller")
    base.update(kw)
    return Contact(**base)


def _run(client, writer, *, uid="mdopp", event_token="", contact_token=""):
    ingest = DavIngest(client, writer, ingesting_uid=uid)
    return asyncio.run(
        ingest.run(event_sync_token=event_token, contact_sync_token=contact_token)
    )


# --- contact -> person -------------------------------------------------------


def test_contact_maps_to_person_with_aliases_and_contact_uri(env):
    writer, db_path, tmp_path = env
    client = FakeDavClient(
        contacts=[
            _contact(
                aliases=["Anna", "Anni"],
                resource="carddav://book/c-1.vcf",
            )
        ]
    )
    stats = _run(client, writer)
    assert stats.contacts == 1 and stats.people_written == 1
    conn = projection.open_conn(db_path)
    person = conn.execute("SELECT * FROM entities WHERE type = 'person'").fetchone()
    assert person["canonical_name"] == "Anna Müller"
    aliases = {
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (person["id"],)
        )
    }
    assert {"Anna", "Anni"} <= aliases
    conn.close()
    text = (
        tmp_path / "notes" / "users" / "mdopp" / "okf" / "people" / "anna-mueller.md"
    ).read_text()
    assert "contact: carddav://book/c-1.vcf" in text


def test_contact_phone_and_email_become_facts(env):
    writer, db_path, _ = env
    client = FakeDavClient(
        contacts=[_contact(phones=["+49 89 123"], emails=["anna@example.org"])]
    )
    _run(client, writer)
    conn = projection.open_conn(db_path)
    facts = {
        (r["predicate"], r["value"])
        for r in conn.execute("SELECT predicate, value FROM facts")
    }
    assert ("phone", "+49 89 123") in facts
    assert ("email", "anna@example.org") in facts
    conn.close()


# --- event -> event + participant resolution ---------------------------------


def test_event_maps_to_event_with_when_and_kind(env):
    writer, db_path, tmp_path = env
    client = FakeDavClient(
        events=[_event(end="2026-05-30T21:00:00", location="Club X")]
    )
    stats = _run(client, writer)
    assert stats.events == 1 and stats.events_written == 1
    conn = projection.open_conn(db_path)
    event = conn.execute("SELECT * FROM events").fetchone()
    assert event["kind"] == "calendar" and event["ts"] == "2026-05-30T19:00:00"
    concept = conn.execute(
        "SELECT okf_path FROM concepts WHERE ref_kind = 'event'"
    ).fetchone()
    # Caldav events default to the ingesting resident -> private user path (#576).
    assert (
        concept["okf_path"] == "users/mdopp/okf/events/2026/2026-05-30-team-dinner.md"
    )
    conn.close()
    text = (
        tmp_path
        / "notes"
        / "users"
        / "mdopp"
        / "okf"
        / "events"
        / "2026"
        / "2026-05-30-team-dinner.md"
    ).read_text()
    assert "when: 2026-05-30T19:00:00/2026-05-30T21:00:00" in text
    assert "where: Club X" in text


def test_event_participant_resolves_to_ingested_contact(env):
    writer, db_path, _ = env
    # Contact ingested first; the event's attendee name must resolve to that
    # person entity via the `with -> [[people/anna-mueller]]` edge.
    client = FakeDavClient(
        contacts=[_contact(name="Anna Müller")],
        events=[_event(participants=["Anna Müller"])],
    )
    _run(client, writer)
    conn = projection.open_conn(db_path)
    person = conn.execute("SELECT id FROM entities WHERE type = 'person'").fetchone()
    edge = conn.execute("SELECT * FROM event_entities WHERE role = 'with'").fetchone()
    assert edge is not None and edge["entity_id"] == person["id"]
    conn.close()


def test_event_without_matching_contact_writes_no_dangling_edge(env):
    writer, db_path, _ = env
    # No contact for the attendee -> no person entity -> no event_entities edge
    # (the writer drops an unresolvable participant rather than inventing one).
    client = FakeDavClient(events=[_event(participants=["Unknown Person"])])
    stats = _run(client, writer)
    assert stats.events_written == 1
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "event_entities") == 0
    conn.close()


# --- incremental / idempotent ------------------------------------------------


def test_reingest_unchanged_is_skipped(env):
    writer, db_path, _ = env
    _run(FakeDavClient(events=[_event(etag="e1")]), writer)
    stats = _run(FakeDavClient(events=[_event(etag="e1")]), writer)
    assert stats.events == 1 and stats.skipped == 1 and stats.events_written == 0
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 1
    conn.close()


def test_changed_etag_reingests_no_dup(env):
    writer, db_path, _ = env
    _run(FakeDavClient(events=[_event(etag="e1")]), writer)
    stats = _run(FakeDavClient(events=[_event(etag="e2")]), writer)
    # New etag rides the body -> content_hash moves -> not skipped, no dup.
    assert stats.events_written == 1 and stats.skipped == 0
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 1
    conn.close()


def test_sync_tokens_are_passed_through(env):
    writer, _, _ = env
    client = FakeDavClient()
    _run(client, writer, event_token="ev-tok", contact_token="card-tok")
    assert client.event_tokens_seen == ["ev-tok"]
    assert client.contact_tokens_seen == ["card-tok"]


def test_bad_contact_is_skipped_and_the_rest_still_ingest(env):
    # A contact whose name is purely non-Latin -> safe_slug ValueError. Without
    # per-item isolation it would abort the whole run (#528); it must be skipped
    # and the following contact must still ingest.
    writer, db_path, _ = env
    client = FakeDavClient(
        contacts=[
            _contact(uid="c-bad", name="王芳"),
            _contact(uid="c-ok", name="Anna Müller", phones=["+49 89 1"]),
        ]
    )
    stats = _run(client, writer)
    assert stats.contacts == 2 and stats.skipped == 1 and stats.people_written == 1
    conn = projection.open_conn(db_path)
    names = {
        r["canonical_name"] for r in conn.execute("SELECT canonical_name FROM entities")
    }
    assert names == {"Anna Müller"}
    conn.close()


def test_empty_collections_are_a_noop(env):
    writer, db_path, _ = env
    stats = _run(FakeDavClient(), writer)
    assert stats == type(stats)()  # all-zero stats
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 0
    assert projection.row_count(conn, "entities") == 0
    conn.close()


# --- HttpDavClient: hand-parsing + read-only HTTP (all mocked) ----------------

from solaris_chat.engine.ingest.dav_client import (  # noqa: E402
    HttpDavClient,
    _propfind_members,
    parse_vcard,
    parse_vevent,
)


_ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
    "UID:ev-1\r\nSUMMARY:Team Dinner\r\nDTSTART:20260530T190000\r\n"
    "DTEND:20260530T210000\r\nLOCATION:Club X\r\n"
    "DESCRIPTION:Bring a dish\\, please\r\n"
    "ATTENDEE;CN=Anna Müller:mailto:anna@example.org\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)

# A folded DESCRIPTION-style continuation (RFC folding drops the fold space) is
# covered by the VEVENT; the vCard FN stays on one line.
_VCF = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c-1\r\nFN:Anna Müller\r\n"
    "N:Müller;Anna;;;\r\nNICKNAME:Anni\r\nTEL:+49 89 123\r\n"
    "EMAIL:anna@example.org\r\nEND:VCARD\r\n"
)


def test_parse_vevent_subset():
    ev = parse_vevent(_ICS, resource="cal/ev-1.ics", etag="e1")
    assert ev is not None
    assert ev.uid == "ev-1"
    assert ev.title == "Team Dinner"
    assert ev.start == "20260530T190000"
    assert ev.end == "20260530T210000"
    assert ev.location == "Club X"
    assert ev.description == "Bring a dish, please"  # unescaped
    assert ev.participants == ["Anna Müller"]
    assert ev.etag == "e1" and ev.resource == "cal/ev-1.ics"


def test_parse_vcard_subset():
    c = parse_vcard(_VCF, resource="book/c-1.vcf", etag="e2")
    assert c is not None
    assert c.uid == "c-1"
    assert c.name == "Anna Müller"  # folded continuation joined
    assert "Anni" in c.aliases
    assert c.phones == ["+49 89 123"]
    assert c.emails == ["anna@example.org"]


def test_parse_returns_none_without_uid():
    assert parse_vevent("BEGIN:VEVENT\nSUMMARY:x\nEND:VEVENT") is None
    assert parse_vcard("BEGIN:VCARD\nFN:x\nEND:VCARD") is None


def test_valarm_description_does_not_overwrite_event_description():
    # A nested VALARM's DESCRIPTION must not be folded into the VEVENT (#527).
    ics = (
        "BEGIN:VEVENT\r\n"
        "UID:abc123\r\n"
        "DTSTART:20260622T180000Z\r\n"
        "SUMMARY:Team meeting\r\n"
        "DESCRIPTION:Discuss Q3 roadmap\r\n"
        "BEGIN:VALARM\r\n"
        "TRIGGER:-PT15M\r\n"
        "ACTION:DISPLAY\r\n"
        "DESCRIPTION:Reminder: Team meeting\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
    )
    ev = parse_vevent(ics)
    assert ev is not None
    assert ev.description == "Discuss Q3 roadmap"


def test_nested_component_inside_valarm_keeps_vevent_properties():
    # #548: a VALARM with its own nested sub-component must not drive the nesting
    # counter negative; VEVENT-level DESCRIPTION/LOCATION/ATTENDEE after the
    # VALARM must survive (the guard is `nesting > 0`, END clamps at 0).
    ics = (
        "BEGIN:VEVENT\r\n"
        "UID:abc123\r\n"
        "DTSTART:20260622T180000Z\r\n"
        "SUMMARY:Team meeting\r\n"
        "BEGIN:VALARM\r\n"
        "TRIGGER:-PT15M\r\n"
        "BEGIN:X-FOO\r\n"
        "X-PROP:ignored\r\n"
        "END:X-FOO\r\n"
        "DESCRIPTION:alarm body\r\n"
        "END:VALARM\r\n"
        "DESCRIPTION:real event description\r\n"
        "LOCATION:Room 5\r\n"
        "ATTENDEE;CN=Anna:mailto:anna@example.com\r\n"
        "END:VEVENT\r\n"
    )
    ev = parse_vevent(ics)
    assert ev is not None
    assert ev.description == "real event description"
    assert ev.location == "Room 5"
    assert ev.participants == ["Anna"]


def test_extra_end_inside_event_does_not_drop_properties():
    # A stray/unbalanced END inside the VEVENT must clamp at 0, not go negative
    # (which would make `if nesting:` truthy and drop later props) (#548).
    ics = (
        "BEGIN:VEVENT\r\n"
        "UID:abc123\r\n"
        "SUMMARY:Meeting\r\n"
        "END:X-STRAY\r\n"
        "DESCRIPTION:still captured\r\n"
        "END:VEVENT\r\n"
    )
    ev = parse_vevent(ics)
    assert ev is not None
    assert ev.description == "still captured"


class _FakeResp:
    def __init__(self, *, text: str):
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def text(self):
        return self._text


class _FakeSession:
    """Records every HTTP method issued and serves canned PROPFIND/GET bodies."""

    methods: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _multistatus(self, base: str, suffix: str) -> str:
        href = base.rstrip("/") + f"/item{suffix}"
        return (
            '<?xml version="1.0"?>'
            '<multistatus xmlns="DAV:">'
            "<response><href>/coll/</href>"
            "<propstat><prop><resourcetype><collection/></resourcetype>"
            "</prop></propstat></response>"
            f"<response><href>{href}</href>"
            "<propstat><prop><getetag>etag-1</getetag><resourcetype/></prop>"
            "</propstat></response>"
            "</multistatus>"
        )

    def request(self, method, url, *, data=None, headers=None):
        _FakeSession.methods.append(method)
        suffix = ".vcf" if "contacts" in url else ".ics"
        return _FakeResp(text=self._multistatus(url, suffix))

    def get(self, url):
        _FakeSession.methods.append("GET")
        return _FakeResp(text=_ICS if url.endswith(".ics") else _VCF)


def _patch_session(monkeypatch):
    import solaris_chat.engine.ingest.dav_client as mod

    _FakeSession.methods = []
    monkeypatch.setattr(mod.aiohttp, "ClientSession", _FakeSession)


def test_http_client_iter_events_only_reads(monkeypatch):
    _patch_session(monkeypatch)
    client = HttpDavClient(caldav_url="https://radicale/cal/")
    events = asyncio.run(_collect(client.iter_events()))
    assert len(events) == 1 and events[0].uid == "ev-1"
    # PROPFIND + GET only — never a write method.
    assert set(_FakeSession.methods) <= {"PROPFIND", "GET"}
    assert "PUT" not in _FakeSession.methods
    assert "DELETE" not in _FakeSession.methods


def test_http_client_iter_contacts_only_reads(monkeypatch):
    _patch_session(monkeypatch)
    client = HttpDavClient(carddav_url="https://radicale/contacts/")
    contacts = asyncio.run(_collect(client.iter_contacts()))
    assert len(contacts) == 1 and contacts[0].name == "Anna Müller"
    assert set(_FakeSession.methods) <= {"PROPFIND", "GET"}


def test_propfind_multistatus_with_xxe_entity_is_refused():
    """A hostile DAV multistatus's external entity is refused, not resolved (#1225)."""
    xml = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE multistatus [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<multistatus xmlns="DAV:"><response><href>&xxe;</href>'
        "</response></multistatus>"
    )
    with pytest.raises(EntitiesForbidden):
        _propfind_members(xml, "https://radicale/cal/")


def test_http_client_inert_half_yields_nothing(monkeypatch):
    _patch_session(monkeypatch)
    # carddav_url unset -> iter_contacts is inert and issues NO HTTP at all.
    client = HttpDavClient(caldav_url="https://radicale/cal/")
    contacts = asyncio.run(_collect(client.iter_contacts()))
    assert contacts == []
    assert _FakeSession.methods == []


async def _collect(aiter):
    return [x async for x in aiter]
