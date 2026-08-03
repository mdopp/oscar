"""`calendar_create` — write an appointment straight into the resident's Radicale
calendar (#1125, ADR-09).

Solaris already speaks CalDAV: `ingest.dav_client` is the client and
`document_deadlines_sync` already PUTs VEVENTs into `{base}/{uid}/solaris/`.
Routing an appointment through Home Assistant's `calendar.create_event` would be a
second CalDAV implementation over the same Radicale server, one more hop in the
latency budget, and would tie a core function to HA's restart cycle — so this
writes directly, reusing the collection layout the deadline sync defines.

The date is never model-generated (G-1, #1126): the model passes the resident's own
words and `date_parse` resolves them against the current household time. An
ambiguous expression comes back as a question, not a guessed date.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import uuid4

from icalendar import Calendar, Event

from solaris_chat.engine import date_parse
from solaris_chat.engine.document_deadlines_sync import (
    calendar_collection_url,
    calendar_target_uid,
)
from solaris_chat.engine.ingest.dav_client import HttpDavClient
from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.tools import Tool
from solaris_chat.logging import log

_UID_PREFIX = "solaris-event-"
_DEFAULT_MINUTES = 60


def build_event_ics(uid: str, title: str, resolution: date_parse.Resolution) -> str:
    """One VEVENT — timed (default an hour) when a time was named, else all-day."""
    cal = Calendar()
    cal.add("prodid", "-//solaris//calendar//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", title)
    start = resolution.start()
    if start is None:
        ev.add("dtstart", resolution.day)  # a date → all-day VALUE=DATE
        ev.add("dtend", resolution.day + timedelta(days=1))
    else:
        ev.add("dtstart", start)
        ev.add("dtend", start + timedelta(minutes=_DEFAULT_MINUTES))
    cal.add_component(ev)
    return cal.to_ical().decode()


def build_calendar_tools(uid_getter) -> list[Tool]:
    """`uid_getter()` supplies the current turn's resident uid — the appointment
    lands in that resident's own Solaris calendar."""

    async def calendar_create(args: dict[str, Any]) -> str:
        from solaris_chat.config import settings

        title = str(args.get("title") or "").strip()
        if not title:
            return json.dumps({"ok": False, "reason": "kein_titel"})
        resolution = date_parse.resolve(str(args.get("when") or ""))
        if resolution.question:
            return json.dumps(
                {"ok": False, "reason": "unklar", "say": resolution.question},
                ensure_ascii=False,
            )
        base = settings.deadlines_sync_url_base
        username = settings.sync_dav_username
        password = settings.sync_dav_password
        if not (base and username and password):
            return json.dumps(
                {
                    "ok": False,
                    "reason": "kalender_nicht_konfiguriert",
                    "say": "Ich habe noch keinen Kalender, in den ich schreiben kann.",
                },
                ensure_ascii=False,
            )
        target = calendar_target_uid(
            uid_getter() or projection.SHARED_UID, settings.household_calendar_uid
        )
        url = calendar_collection_url(base, target)
        event_uid = f"{_UID_PREFIX}{uuid4().hex[:12]}"
        client = HttpDavClient(caldav_username=username, caldav_password=password)
        try:
            await client.ensure_calendar(url)
            await client.put_item(
                url,
                event_uid,
                build_event_ics(event_uid, title, resolution),
                suffix=".ics",
                content_type="text/calendar; charset=utf-8",
            )
        except Exception as e:  # noqa: BLE001 — the resident gets a plain answer.
            log.error("engine.calendar_create.failed", uid=event_uid, error=str(e))
            return json.dumps(
                {
                    "ok": False,
                    "reason": "kalender_nicht_erreichbar",
                    "say": "Ich konnte den Termin gerade nicht eintragen.",
                },
                ensure_ascii=False,
            )
        when_label = date_parse.format_de(resolution.day, resolution.at)
        return json.dumps(
            {
                "ok": True,
                "uid": event_uid,
                "day": resolution.day.isoformat(),
                "say": f"Eingetragen: {title} am {when_label} ✓",
            },
            ensure_ascii=False,
        )

    return [
        Tool(
            name="calendar_create",
            description=(
                "Trägt einen Termin in den Kalender ein ('trag … ein', 'mach mir"
                " einen Termin', 'Zahnarzt am Donnerstag'). when bekommt die"
                " Zeitangabe WÖRTLICH so, wie sie gesagt wurde ('morgen',"
                " 'nächsten Dienstag', 'Donnerstag 15 Uhr') — rechne selbst KEIN"
                " Datum aus. Kommt ein 'say'-Feld zurück, lies es vor."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "when": {
                        "type": "string",
                        "description": "Zeitangabe wörtlich, z.B. 'Donnerstag 15 Uhr'",
                    },
                },
                "required": ["title", "when"],
            },
            handler=calendar_create,
        ),
    ]
