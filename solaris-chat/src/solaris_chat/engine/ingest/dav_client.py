"""Read-only CalDAV/CardDAV source client for the calendar+contacts adapter.

The adapter depends on the `DavClient` Protocol, not the concrete dav client —
so tests inject a fake and the live path uses `HttpDavClient` (a thin read-only
wrapper over a CalDAV/CardDAV server, `CALDAV_URL`/`CARDDAV_URL` +
`*_USERNAME`/`*_PASSWORD`). Read-only on the source: only `PROPFIND`/`REPORT`
collection reads, never a `PUT`/`DELETE`.

The dataclasses are the normalized subset the adapter maps from; the concrete
client folds the raw iCalendar/vCard payloads into them so the adapter never
touches DAV protocol quirks. A fake client returns the same dataclasses
directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urljoin

import re

import aiohttp
from defusedxml import ElementTree as ET

# Characters unsafe in a DAV resource path segment; the UID becomes the filename.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _dav_name(uid: str) -> str:
    """A filesystem/URL-safe, non-empty resource name from a calendar UID."""
    cleaned = _UNSAFE_NAME.sub("-", (uid or "").strip()).strip("-.")
    return cleaned or "item"


@dataclass(frozen=True)
class CalEvent:
    """The normalized subset of a CalDAV VEVENT the adapter ingests.

    `participants` are the attendee display names (resolved against contact
    person entities by the writer); `etag` rides the change key so an unchanged
    re-ingest is skipped.
    """

    uid: str
    title: str
    start: str  # ISO-8601 start (DTSTART).
    end: str = ""  # ISO-8601 end (DTEND), empty for an all-day/open event.
    description: str = ""
    location: str = ""
    participants: list[str] = field(default_factory=list)
    resource: str = ""  # canonical CalDAV resource URI.
    etag: str = ""  # server etag — the change key for content_hash.


@dataclass(frozen=True)
class Contact:
    """The normalized subset of a CardDAV vCard the adapter ingests.

    `phones`/`emails` project to `facts` (predicate `phone`/`email`) for later
    resolution; `etag` is the change key.
    """

    uid: str
    name: str
    aliases: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    resource: str = ""  # canonical CardDAV resource URI.
    etag: str = ""  # server etag — the change key for content_hash.


class DavClient(Protocol):
    """Read-only CalDAV/CardDAV access the adapter needs. Injectable for tests.

    Either half may be inert (its source URL unset); an inert iterator just
    yields nothing, so an operator can enable only calendar or only contacts.
    """

    def iter_events(self, *, sync_token: str = "") -> AsyncIterator[CalEvent]:
        """Yield calendar events, optionally only those changed since
        `sync_token` (the incremental CalDAV sync-collection token)."""
        ...

    def iter_contacts(self, *, sync_token: str = "") -> AsyncIterator[Contact]:
        """Yield contacts, optionally only those changed since `sync_token`
        (the incremental CardDAV sync-collection token)."""
        ...


# --- iCalendar / vCard hand-parsing ------------------------------------------
#
# Both formats are RFC 5545 / RFC 6350 line-based "PROP[;params]:value" with a
# 75-octet folding (a continuation line starts with a space/tab) and a small set
# of escaped chars (\n \, \; \\). We only need a handful of properties, so a
# hand-parse of that subset is far lighter than pulling in icalendar/vobject.

_DAV_NS = "{DAV:}"


def _unfold(text: str) -> list[str]:
    """Join RFC-5545/6350 folded continuation lines into logical lines."""
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _split_line(line: str) -> tuple[str, dict[str, str], str] | None:
    """Split a logical line into (name, params, value). None for a blank line."""
    if not line or ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return name, params, value


def parse_vevent(body: str, *, resource: str = "", etag: str = "") -> CalEvent | None:
    """Fold the first VEVENT in an iCalendar body into a CalEvent."""
    uid = title = start = end = description = location = ""
    participants: list[str] = []
    in_event = False
    # A VEVENT can contain nested components (VALARM); their properties (e.g. a
    # VALARM DESCRIPTION) must not be folded into the event itself (#527).
    nesting = 0
    for line in _unfold(body):
        parsed = _split_line(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "BEGIN" and value.upper() == "VEVENT":
            in_event = True
            continue
        if name == "END" and value.upper() == "VEVENT":
            break
        if not in_event:
            continue
        if name == "BEGIN":
            nesting += 1
            continue
        if name == "END":
            # Clamp at 0: a stray/extra END (proprietary extensions, lenient
            # servers) must not drive nesting negative, which would make
            # `nesting > 0` falsy-vs-truthy slip and drop VEVENT props (#548).
            nesting = max(0, nesting - 1)
            continue
        if nesting > 0:
            continue
        if name == "UID":
            uid = value.strip()
        elif name == "SUMMARY":
            title = _unescape(value)
        elif name == "DTSTART":
            start = value.strip()
        elif name == "DTEND":
            end = value.strip()
        elif name == "DESCRIPTION":
            description = _unescape(value)
        elif name == "LOCATION":
            location = _unescape(value)
        elif name == "ATTENDEE":
            cn = params.get("CN")
            if cn:
                participants.append(_unescape(cn))
    if not uid:
        return None
    return CalEvent(
        uid=uid,
        title=title,
        start=start,
        end=end,
        description=description,
        location=location,
        participants=participants,
        resource=resource,
        etag=etag,
    )


def parse_vcard(body: str, *, resource: str = "", etag: str = "") -> Contact | None:
    """Fold the first VCARD in a vCard body into a Contact."""
    uid = name = ""
    aliases: list[str] = []
    phones: list[str] = []
    emails: list[str] = []
    for line in _unfold(body):
        parsed = _split_line(line)
        if not parsed:
            continue
        prop, _params, value = parsed
        if prop == "UID":
            uid = value.strip()
        elif prop == "FN":
            name = _unescape(value)
        elif prop == "NICKNAME":
            aliases += [a.strip() for a in value.split(",") if a.strip()]
        elif prop == "N":
            # N is family;given;additional;prefix;suffix — given+family is a
            # useful alias for resolution alongside FN.
            fields = [_unescape(f).strip() for f in value.split(";")]
            given = fields[1] if len(fields) > 1 else ""
            family = fields[0] if fields else ""
            joined = " ".join(p for p in (given, family) if p)
            if joined:
                aliases.append(joined)
        elif prop == "TEL":
            tel = value.strip()
            if tel:
                phones.append(tel)
        elif prop == "EMAIL":
            mail = value.strip()
            if mail:
                emails.append(mail)
    if not uid or not name:
        return None
    aliases = [a for a in dict.fromkeys(aliases) if a and a != name]
    return Contact(
        uid=uid,
        name=name,
        aliases=aliases,
        phones=phones,
        emails=emails,
        resource=resource,
        etag=etag,
    )


# A PROPFIND that asks for getetag + the content-type so we can tell calendar
# resources (.ics) from contact resources (.vcf). Depth:1 enumerates the
# collection's member resources.
_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<propfind xmlns="DAV:"><prop>'
    "<getetag/><getcontenttype/><resourcetype/>"
    "</prop></propfind>"
)


def _propfind_members(xml: str, base_url: str) -> list[tuple[str, str]]:
    """Parse a PROPFIND multistatus into [(absolute href, etag)] for the
    non-collection member resources (skip the collection self-entry)."""
    root = ET.fromstring(xml)
    members: list[tuple[str, str]] = []
    for resp in root.findall(f"{_DAV_NS}response"):
        href_el = resp.find(f"{_DAV_NS}href")
        if href_el is None or not (href_el.text or "").strip():
            continue
        href = href_el.text.strip()
        is_collection = (
            resp.find(f".//{_DAV_NS}resourcetype/{_DAV_NS}collection") is not None
        )
        if is_collection:
            continue
        etag_el = resp.find(f".//{_DAV_NS}getetag")
        etag = (etag_el.text or "").strip() if etag_el is not None else ""
        members.append((urljoin(base_url, href), etag))
    return members


class HttpDavClient:
    """CalDAV/CardDAV client over aiohttp — read-only for ingest (PROPFIND +
    GET), with a single write path (`put_item`) for importing calendar objects.

    Either half is inert when its URL is unset — `iter_events` yields nothing
    without `caldav_url`, `iter_contacts` nothing without `carddav_url` — so an
    operator can enable only calendar or only contacts. The ingest iterators
    never mutate the source; only `put_item` (used by the Takeout calendar
    importer) issues a PUT.
    """

    def __init__(
        self,
        *,
        caldav_url: str = "",
        caldav_username: str = "",
        caldav_password: str = "",
        carddav_url: str = "",
        carddav_username: str = "",
        carddav_password: str = "",
        timeout: float = 30.0,
    ):
        self._caldav_url = caldav_url
        self._caldav_auth = _basic_auth(caldav_username, caldav_password)
        self._carddav_url = carddav_url
        self._carddav_auth = _basic_auth(carddav_username, carddav_password)
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def iter_events(self, *, sync_token: str = "") -> AsyncIterator[CalEvent]:
        if not self._caldav_url:
            return
        async for resource, etag, body in self._iter_resources(
            self._caldav_url, self._caldav_auth, ".ics"
        ):
            event = parse_vevent(body, resource=resource, etag=etag)
            if event is not None:
                yield event

    async def iter_contacts(self, *, sync_token: str = "") -> AsyncIterator[Contact]:
        if not self._carddav_url:
            return
        async for resource, etag, body in self._iter_resources(
            self._carddav_url, self._carddav_auth, ".vcf"
        ):
            contact = parse_vcard(body, resource=resource, etag=etag)
            if contact is not None:
                yield contact

    async def put_item(
        self,
        collection_url: str,
        uid: str,
        body: str,
        *,
        suffix: str = ".ics",
        content_type: str = "text/calendar; charset=utf-8",
    ) -> str:
        """PUT one object into a CalDAV/CardDAV collection (RFC 4791 §5.3.2).

        The resource href is ``<collection_url>/<uid><suffix>``; the UID-derived
        name makes a re-PUT of the same item overwrite rather than duplicate.
        Defaults write a calendar object (``.ics`` / ``text/calendar``); the
        contacts importer passes ``suffix=".vcf"`` / ``content_type="text/vcard"``
        for a CardDAV card. Auth follows the target: CardDAV credentials for a
        ``.vcf`` write, CalDAV otherwise. Returns the resource URL written.
        """
        resource_url = urljoin(
            collection_url if collection_url.endswith("/") else collection_url + "/",
            f"{_dav_name(uid)}{suffix}",
        )
        auth = self._carddav_auth if suffix == ".vcf" else self._caldav_auth
        async with aiohttp.ClientSession(timeout=self._timeout, auth=auth) as session:
            async with session.put(
                resource_url,
                data=body.encode("utf-8"),
                headers={"Content-Type": content_type},
            ) as resp:
                resp.raise_for_status()
        return resource_url

    async def delete_item(
        self, collection_url: str, uid: str, *, suffix: str = ".ics"
    ) -> None:
        """DELETE one UID-named object from a collection. A 404 (already gone) is
        not an error, so removing an event that was never written is a no-op."""
        resource_url = urljoin(
            collection_url if collection_url.endswith("/") else collection_url + "/",
            f"{_dav_name(uid)}{suffix}",
        )
        auth = self._carddav_auth if suffix == ".vcf" else self._caldav_auth
        async with aiohttp.ClientSession(timeout=self._timeout, auth=auth) as session:
            async with session.delete(resource_url) as resp:
                if resp.status != 404:
                    resp.raise_for_status()

    async def ensure_calendar(self, collection_url: str) -> None:
        """MKCALENDAR the collection so a following PUT lands (RFC 4791 §5.3.1).

        An existing collection answers 405 (Method Not Allowed) or 409
        (Conflict, observed against docker.io/tomsquest/docker-radicale on the
        household box) — both mean "already there", so neither is an error,
        matching `ensure_addressbook` below. Any other non-2xx raises. Uses
        the CalDAV credentials.
        """
        url = collection_url if collection_url.endswith("/") else collection_url + "/"
        async with aiohttp.ClientSession(
            timeout=self._timeout, auth=self._caldav_auth
        ) as session:
            async with session.request("MKCALENDAR", url) as resp:
                if resp.status not in (405, 409):
                    resp.raise_for_status()

    async def ensure_addressbook(self, collection_url: str, displayname: str) -> None:
        """MKCOL a CardDAV ADDRESS BOOK so a following vCard PUT lands (#996).

        A plain MKCOL makes a bare WebDAV collection Radicale won't accept cards
        into, so the body carries the `{DAV:}resourcetype` that tags it
        `VADDRESSBOOK` — the address-book counterpart of `ensure_calendar`'s
        MKCALENDAR. An existing collection answers 405 (Radicale) or 409
        (resource-must-be-null) — both mean "already there", so neither is an
        error. Uses the CardDAV credentials.
        """
        url = collection_url if collection_url.endswith("/") else collection_url + "/"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<mkcol xmlns="DAV:" xmlns:CR="urn:ietf:params:xml:ns:carddav">'
            "<set><prop><resourcetype><collection/><CR:addressbook/></resourcetype>"
            f"<displayname>{displayname}</displayname>"
            "</prop></set></mkcol>"
        )
        async with aiohttp.ClientSession(
            timeout=self._timeout, auth=self._carddav_auth
        ) as session:
            async with session.request(
                "MKCOL",
                url,
                data=body.encode("utf-8"),
                headers={"Content-Type": "application/xml; charset=utf-8"},
            ) as resp:
                if resp.status not in (405, 409):
                    resp.raise_for_status()

    async def _iter_resources(
        self, url: str, auth: aiohttp.BasicAuth | None, suffix: str
    ) -> AsyncIterator[tuple[str, str, str]]:
        async with aiohttp.ClientSession(timeout=self._timeout, auth=auth) as session:
            async with session.request(
                "PROPFIND",
                url,
                data=_PROPFIND_BODY,
                headers={"Depth": "1", "Content-Type": "application/xml"},
            ) as resp:
                resp.raise_for_status()
                multistatus = await resp.text()
            for href, etag in _propfind_members(multistatus, url):
                if suffix and not href.lower().endswith(suffix):
                    continue
                async with session.get(href) as resp:
                    resp.raise_for_status()
                    body = await resp.text()
                yield href, etag, body


def _basic_auth(username: str, password: str) -> aiohttp.BasicAuth | None:
    return aiohttp.BasicAuth(username, password) if username else None
