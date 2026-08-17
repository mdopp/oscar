"""Import Google Contacts ``.vcf`` exports into the acting user's Radicale
address book (CardDAV collection ``<user>/contacts/``).

Google exports one ``.vcf`` (``Takeout/Contacts/*.vcf``, vCard 3.0) containing
many ``VCARD``s. CardDAV storage wants one file per card, so we split them. Each
card gets a stable ``UID`` (kept from the source when present, otherwise derived
from the card content) so re-imports overwrite instead of duplicating.

Two write paths, same report shape: ``do_import`` writes Radicale's on-disk
storage directly, while ``import_to_dav`` ``PUT``s each card to the owner's
CardDAV collection over HTTP (reusing ``HttpDavClient.put_item`` with
``text/vcard``, the sibling of the calendar importer's CalDAV PUT) — a plain
authenticated write that avoids the Radicale userns-uid caveat. The written
cards are projected to OKF person entities on the next nightly ``DavIngest`` run
(``source="carddav"``); no new ingest code is added.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import vobject

from solaris_chat.logging import log

from ....ingest.dav_client import HttpDavClient
from .. import ImportPlan, radicale_store

_NS = uuid.UUID("6f1a1c2e-9b1e-4b7a-9c2d-000000000002")
_CONTACTS_COLLECTION = "contacts"


def _iter_cards(vcf_text: str):
    for card in vobject.readComponents(vcf_text):
        yield card


def _values(card, name: str) -> list[str]:
    return [
        str(line.value).strip()
        for line in card.contents.get(name, [])
        if str(line.value or "").strip()
    ]


def _identity(card) -> str:
    """Stable per-person key for a card that carries no source ``UID`` (#1190).

    Hashing the whole serialization keyed the card on *every* field, so a
    re-export with one phone number added produced a second UID and a duplicate
    card. Name plus the card's strongest self-contained identifier survives
    edits to the rest of the card; the identifier is chosen by sort order, not
    by file order, so a reordered export keeps the same key.
    """
    fn = next(iter(_values(card, "fn")), "").lower()
    mails = sorted(v.lower() for v in _values(card, "email"))
    if mails:
        return f"{fn}|{mails[0]}"
    tels = sorted("".join(c for c in v if c.isdigit()) for v in _values(card, "tel"))
    if tels:
        return f"{fn}|{tels[0]}"
    return fn


def _ensure_fn(card) -> None:
    """vCard 3.0 requires ``FN``; without it ``serialize()`` raises (#1189).

    A "saved a phone number, no name" Google export has none, so label the card
    with the number itself — the way Google Contacts shows it — instead of
    losing the entry.
    """
    if _values(card, "fn"):
        return
    label = next(iter(_values(card, "email") + _values(card, "tel")), "")
    if label:
        card.add("fn").value = label


def _ensure_uid(card) -> str:
    uid = None
    if hasattr(card, "uid") and card.uid.value:
        uid = str(card.uid.value)
    if not uid:
        uid = f"import-{uuid.uuid5(_NS, _identity(card))}"
        card.add("uid").value = uid
    return uid


def _decode(vcf_bytes: bytes) -> str:
    return vcf_bytes.decode("utf-8", errors="replace")


def _names(vcf_text: str, limit=5):
    out = []
    for i, card in enumerate(_iter_cards(vcf_text)):
        if i >= limit:
            break
        out.append(str(card.fn.value) if hasattr(card, "fn") else "(ohne Namen)")
    return out


def _count(vcf_text: str) -> int:
    return sum(1 for _ in _iter_cards(vcf_text))


def preview(filename: str, vcf_bytes: bytes) -> dict:
    text = _decode(vcf_bytes)
    return {
        "type": "contacts",
        "cards": _count(text),
        "samples": _names(text),
    }


def do_import(radicale_data: Path, user: str, filename: str, vcf_bytes: bytes) -> dict:
    text = _decode(vcf_bytes)
    written = 0
    skipped = 0
    with radicale_store.storage_lock(radicale_data):
        coll = radicale_store.ensure_collection(
            radicale_data,
            user,
            _CONTACTS_COLLECTION,
            tag="VADDRESSBOOK",
            displayname="Kontakte",
        )
        for card in _iter_cards(text):
            try:
                _ensure_fn(card)
                uid = _ensure_uid(card)
                radicale_store.write_item(coll, uid, card.serialize(), "vcf")
            except Exception as exc:  # noqa: BLE001 — one bad card must not cost the rest (#1189).
                skipped += 1
                log.warn("import.contacts.card_failed", file=filename, error=str(exc))
                continue
            written += 1
    return {
        "type": "contacts",
        "written": written,
        "skipped": skipped,
        "target": f"{user}/{_CONTACTS_COLLECTION}",
    }


async def import_to_dav(
    client: HttpDavClient, collection_url: str, filename: str, vcf_bytes: bytes
) -> dict:
    """PUT each card to a Radicale CardDAV collection over HTTP.

    ``collection_url`` is the owner's address-book collection URL (e.g.
    ``https://…/dav/<user>/contacts/``); each card is PUT as ``<uid>.vcf`` with
    a ``text/vcard`` content type. Returns the same report shape as
    ``do_import`` so the two write paths are interchangeable.
    """
    text = _decode(vcf_bytes)
    written = 0
    skipped = 0
    for card in _iter_cards(text):
        try:
            _ensure_fn(card)
            uid = _ensure_uid(card)
            await client.put_item(
                collection_url,
                uid,
                card.serialize(),
                suffix=".vcf",
                content_type="text/vcard; charset=utf-8",
            )
        except Exception as exc:  # noqa: BLE001 — one bad card must not cost the rest (#1189).
            skipped += 1
            log.warn("import.contacts.card_failed", file=filename, error=str(exc))
            continue
        written += 1
    return {
        "type": "contacts",
        "written": written,
        "skipped": skipped,
        "target": collection_url,
    }


class ContactsImporter:
    """Registrable ``contacts`` importer kind.

    Parses a Takeout ``.vcf`` (``plan``) and PUTs each card to the owner's
    Radicale CardDAV collection (``run``); ``DavIngest`` (``source="carddav"``)
    projects them to OKF person entities on the next nightly run. ``run`` carries
    out the CardDAV write given a client + collection URL supplied in the plan.
    """

    kind = "contacts"

    def detect(self, manifest) -> list[dict]:
        return [{"kind": self.kind, "type": "contacts"}]

    def plan(self, archive, selections) -> ImportPlan:
        vcf_bytes = archive["vcf_bytes"]
        filename = archive.get("filename", "")
        return ImportPlan(
            kind=self.kind,
            writes=[{"filename": filename, "vcf_bytes": vcf_bytes}],
            summary=preview(filename, vcf_bytes),
        )

    async def run(self, plan: ImportPlan, progress) -> list[dict]:
        client: HttpDavClient = progress["client"]
        collection_url: str = progress["collection_url"]
        reports = []
        for write in plan.writes:
            reports.append(
                await import_to_dav(
                    client, collection_url, write["filename"], write["vcf_bytes"]
                )
            )
        return reports
