"""Project document deadlines + dated to-do tasks into PER-RESIDENT calendars
(#doc-graph / #todo / #997).

A document carries dated facts — a `cancellation_deadline`, a `hu_date`, a
contract `end_date`. Rather than push notifications, we write each as an all-day
event with an advance alarm into a calendar, where the calendar app itself
reminds the resident (the passive, non-intrusive channel the plan chose).

A dated **task** is not an appointment, so it goes into the same collection as a
VTODO instead (#1127): a CalDAV task client (Tasks.org via DAVx5) then shows it
on the phone's to-do list with a checkbox, while the document deadlines stay
VEVENTs in the calendar view.

Solaris owns, the calendar mirrors (#997, option A / #1011): every resident's
Solaris calendar lives under the RESIDENT's OWN principal tree —
`{base}/{resident_uid}/solaris/` — a `solaris`-named calendar under each resident.
The resident reads/writes their own tree as themselves (owner_only), so they
subscribe on the phone with their OWN login — no shared service password. The
`solaris` service account is granted a narrow Radicale rights rule to WRITE only
`/<resident>/solaris/` (nothing else). A resident's private dated task lands only
in their own `/{resident_uid}/solaris/` collection; household-scoped document
deadlines and household tasks (`SHARED_UID`) go to `{base}/{SHARED_UID}/solaris/`.

**Authenticated CalDAV PUT** as the dedicated `solaris` DAV account, not a
filesystem mount: Radicale's rights scope it to the collections it may write.
The event UID is derived from `(entity_id, predicate)` / the task id, so a
re-sync overwrites in place. Non-ISO / unparseable dates are skipped. Disabled
(no-op) when the base URL / credentials are unset.
"""

from __future__ import annotations

from datetime import date, timedelta

from icalendar import Alarm, Calendar, Event, Todo

from solaris_chat.engine.ingest.dav_client import HttpDavClient
from solaris_chat.engine.knowledge import projection
from solaris_chat.logging import log

# The dated predicates that become calendar entries, with the German label the
# event summary uses. `start_date` is deliberately absent — a contract's start is
# not a deadline to be reminded about.
_DEADLINE_LABELS = {
    "cancellation_deadline": "Kündigungsfrist",
    "renewal_date": "Verlängerung",
    "hu_date": "TÜV/HU",
    "expiry_date": "Ablauf",
    "due_date": "Fällig",
    "end_date": "Vertragsende",
}
_UID_PREFIX = "solaris-deadline-"
_TASK_UID_PREFIX = "solaris-task-"
# Days before the date the calendar app raises its alarm.
_LEAD_DAYS = 14
# The calendar name under each resident's own principal: `{base}/{resident_uid}/
# {_CALENDAR}/` (option A / #1011). The resident owns their tree (subscribes as
# themselves); a narrow Radicale rights rule lets the `solaris` account WRITE only
# `/<resident>/solaris/`.
_CALENDAR = "solaris"


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def _build_event(uid: str, summary: str, description: str, day: date) -> str:
    cal = Calendar()
    cal.add("prodid", "-//solaris//deadlines//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", summary)
    if description:
        ev.add("description", description)
    ev.add("dtstart", day)  # a date (not datetime) → all-day VALUE=DATE
    ev.add("dtend", day + timedelta(days=1))
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", summary)
    alarm.add("trigger", timedelta(days=-_LEAD_DAYS))
    ev.add_component(alarm)
    cal.add_component(ev)
    return cal.to_ical().decode()


def _build_todo(uid: str, summary: str, day: date) -> str:
    """One VTODO — an open task with a DUE date (#1127).

    Same collection as the deadline VEVENTs, but a task component, so a CalDAV
    task client lists it with a checkbox instead of blocking a day in the
    calendar. The alarm carries `RELATED=END` because a VTODO's trigger anchors
    to DTSTART by default and a task only has a DUE.
    """
    cal = Calendar()
    cal.add("prodid", "-//solaris//tasks//EN")
    cal.add("version", "2.0")
    todo = Todo()
    todo.add("uid", uid)
    todo.add("summary", summary)
    todo.add("status", "NEEDS-ACTION")
    todo.add("due", day)  # a date (not datetime) → VALUE=DATE
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", summary)
    alarm.add("trigger", timedelta(days=-_LEAD_DAYS), parameters={"RELATED": "END"})
    todo.add_component(alarm)
    cal.add_component(todo)
    return cal.to_ical().decode()


def _facts(conn, entity_id: str) -> dict[str, str]:
    """The document's facts, highest-confidence value winning per predicate."""
    out: dict[str, str] = {}
    for f in conn.execute(
        "SELECT predicate, value FROM facts WHERE subject_entity_id = ?"
        " ORDER BY confidence DESC",
        (entity_id,),
    ).fetchall():
        if f["predicate"] not in out:
            out[f["predicate"]] = f["value"]
    return out


def calendar_collection_url(base_url: str, resident_uid: str) -> str:
    """The resident's Solaris calendar collection — `{base}/{uid}/solaris/`.

    Under the RESIDENT's own principal (option A / #1011): the resident owns their
    tree and subscribes as themselves; the `solaris` account writes here via a
    narrow rights rule granting it `/<resident>/solaris/` only.
    """
    return f"{base_url.rstrip('/')}/{resident_uid}/{_CALENDAR}/"


def calendar_target_uid(owner_uid: str, household_uid: str) -> str:
    """Which resident's calendar a task lands in: its own owner, except a
    household task (`SHARED_UID`) which routes to the primary resident when set."""
    if owner_uid == projection.SHARED_UID:
        return household_uid or projection.SHARED_UID
    return owner_uid


async def sync_deadlines(
    db_path: str,
    base_url: str,
    username: str,
    password: str,
    household_uid: str = "",
) -> dict[str, int]:
    """PUT each resident's dated OPEN tasks + the household's document deadlines
    into their PER-RESIDENT Solaris calendar (`{base}/{resident_uid}/solaris/`).

    Household-wide items (shared document deadlines + household tasks) carry the
    `SHARED_UID` sentinel, which is NOT a real Radicale principal — writing under
    it 409s where no `household` principal exists. So when `household_uid` is set
    (the operator's primary resident), those items are routed to that resident's
    own calendar instead; when empty, the `SHARED_UID` behaviour is unchanged.

    Returns `{written, skipped, failed}`. Never raises. Disabled (no-op) when the
    base URL / credentials are unset.
    """
    if not (base_url and username and password):
        return {"written": 0, "skipped": 0, "failed": 0}
    # Where household-scoped items land: the primary resident's own calendar when
    # configured, else the (principal-less) SHARED_UID as before.
    shared_target = household_uid or projection.SHARED_UID
    client = HttpDavClient(caldav_username=username, caldav_password=password)
    written = skipped = failed = 0
    # owner resident_uid → [(uid, ics)]; an event only ever lands under its own
    # owner's key, so a resident's task can never cross into another's calendar.
    per_resident: dict[str, list[tuple[str, str]]] = {}
    conn = projection.open_conn(db_path)
    try:
        # Household document deadlines → the household principal's calendar.
        docs = conn.execute(
            "SELECT id, canonical_name FROM entities WHERE type = 'document'"
        ).fetchall()
        for doc in docs:
            facts = _facts(conn, doc["id"])
            desc = " · ".join(
                x
                for x in (facts.get("provider", ""), facts.get("policy_number", ""))
                if x
            )
            for pred, label in _DEADLINE_LABELS.items():
                raw = facts.get(pred, "")
                if not raw:
                    continue
                day = _parse_iso(raw)
                if day is None:
                    skipped += 1
                    continue
                uid = f"{_UID_PREFIX}{doc['id']}-{pred}"
                summary = f"{label}: {doc['canonical_name']}"
                per_resident.setdefault(shared_target, []).append(
                    (uid, _build_event(uid, summary, desc, day))
                )
        # Dated to-do tasks (#todo, #997, #1127): an OPEN task with an ISO `due`
        # becomes a VTODO in ITS OWNER's collection — a private resident's task
        # under their own uid, a household task under SHARED_UID. A resolved task
        # simply stops being written (its item lingers until re-synced away —
        # refined by cascade-on-change, see the TODO below).
        tasks = conn.execute(
            "SELECT id, canonical_name, resident_uid FROM entities WHERE type = 'task'"
        ).fetchall()
        for task in tasks:
            facts = _facts(conn, task["id"])
            if facts.get("status", "open") != "open":
                continue
            day = _parse_iso(facts.get("due", ""))
            if day is None:
                continue
            uid = f"{_TASK_UID_PREFIX}{task['id']}"
            summary = f"Aufgabe: {facts.get('title_text', task['canonical_name'])}"
            # A private task lands in its owner's calendar; a household task
            # (SHARED_UID) is routed to the primary resident like the deadlines.
            owner = calendar_target_uid(task["resident_uid"], shared_target)
            per_resident.setdefault(owner, []).append(
                (uid, _build_todo(uid, summary, day))
            )
    finally:
        conn.close()
    for resident_uid, events in per_resident.items():
        collection_url = calendar_collection_url(base_url, resident_uid)
        try:
            await client.ensure_calendar(collection_url)
        except Exception as e:  # noqa: BLE001 — a missing collection fails its PUTs below.
            log.error(
                "engine.deadlines_sync.ensure_failed",
                resident=resident_uid,
                error=str(e),
            )
        for uid, ics in events:
            try:
                await client.put_item(
                    collection_url,
                    uid,
                    ics,
                    suffix=".ics",
                    content_type="text/calendar; charset=utf-8",
                )
                written += 1
            except Exception as e:  # noqa: BLE001 — one bad event mustn't stop the sync.
                log.error("engine.deadlines_sync.event_failed", uid=uid, error=str(e))
                failed += 1
    log.info("engine.deadlines_sync", written=written, skipped=skipped, failed=failed)
    return {"written": written, "skipped": skipped, "failed": failed}


async def cascade_task_event(
    db_path: str,
    entity_id: str,
    base_url: str,
    username: str,
    password: str,
    household_uid: str = "",
    deleted: bool = False,
) -> None:
    """Re-sync the SINGLE VTODO for one task, immediately (#997, #1127).

    Called on task add/update/set_status so the phone's task list isn't stale
    until the nightly `sync_deadlines`. Reads the task's current facts and either
    PUTs its `solaris-task-<id>` VTODO (still OPEN with a parseable ISO `due`) or
    DELETEs it (resolved, or the due date removed/unparseable, or the task
    gone). Routing +
    the deterministic UID match the nightly full sync exactly, so a re-PUT
    overwrites in place. Never raises. Disabled (no-op) when the base URL /
    credentials are unset.

    `deleted=True` forces the DELETE branch for a task that is ABOUT to be
    removed (#1244): the row must still be there, because the collection is
    routed off its owner, but it must not be re-PUT on the way out.
    """
    if not (base_url and username and password):
        return
    client = HttpDavClient(caldav_username=username, caldav_password=password)
    conn = projection.open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT canonical_name, resident_uid FROM entities"
            " WHERE id = ? AND type = 'task'",
            (entity_id,),
        ).fetchone()
        facts = _facts(conn, entity_id) if row is not None else {}
    finally:
        conn.close()
    uid = f"{_TASK_UID_PREFIX}{entity_id}"
    day = None
    if not deleted and row is not None and facts.get("status", "open") == "open":
        day = _parse_iso(facts.get("due", ""))
    target = calendar_target_uid(
        row["resident_uid"] if row is not None else projection.SHARED_UID,
        household_uid,
    )
    collection_url = calendar_collection_url(base_url, target)
    try:
        if day is None:
            await client.delete_item(collection_url, uid, suffix=".ics")
        else:
            summary = f"Aufgabe: {facts.get('title_text', row['canonical_name'])}"
            await client.ensure_calendar(collection_url)
            await client.put_item(
                collection_url,
                uid,
                _build_todo(uid, summary, day),
                suffix=".ics",
                content_type="text/calendar; charset=utf-8",
            )
    except Exception as e:  # noqa: BLE001 — a stale event self-heals at the nightly sync.
        log.error("engine.deadlines_sync.cascade_failed", uid=uid, error=str(e))


async def cascade_task_event_configured(
    db_path: str, entity_id: str, *, deleted: bool = False
) -> None:
    """`cascade_task_event` with the DAV config drawn from the running settings —
    the one-liner the task write paths call after a DB change. No-op when unset."""
    from solaris_chat.config import settings

    await cascade_task_event(
        db_path,
        entity_id,
        settings.deadlines_sync_url_base,
        settings.sync_dav_username,
        settings.sync_dav_password,
        settings.household_calendar_uid,
        deleted=deleted,
    )
