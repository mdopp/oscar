"""Sync Solaris-owned contacts to the phone book as vCards (#doc-graph / #996).

Two kinds of contact leave Solaris this way:

* **Provider organizations** — every document's provider is an `organization`
  entity carrying contact facts (phone/email/address/contact_person), see
  `ingest/obsidian.py`. They are household-wide, so they all land in ONE book.
* **Personal persons** — a `.contacts` person the resident created in Solaris
  (`entities.source = 'contact'`). These are per-resident (`resident_uid`), so
  each resident's persons land under **their own** principal and nobody else's:
  `{base}/{resident_uid}/solaris-contacts/`. Persons that came IN from CardDAV
  (`source = 'carddav'`) are deliberately NOT written back — they already exist
  in the resident's own address book, and re-projecting them is exactly the
  duplicate class #1028 exists to clean up.

**Authenticated CardDAV MKCOL+PUT**, not a filesystem mount: the sync runs as a
dedicated `solaris` DAV account (its own LLDAP identity), and the converged
Radicale rights let that account touch only `<resident>/solaris` and
`<resident>/solaris-contacts` — no raw storage access, no other collection of a
resident's, no loosened perms. The vCard UID is derived from the entity id, so a
re-sync overwrites the same card instead of duplicating, and never touches a
human's own contacts (different UIDs). Disabled (no-op) when the collection URL /
credentials are unset.
"""

from __future__ import annotations

import vobject

from solaris_chat.engine.ingest.dav_client import HttpDavClient
from solaris_chat.engine.knowledge import projection
from solaris_chat.logging import log

# vCard UIDs that mark a card as Solaris-owned, so a re-sync overwrites the same
# resource and a human's own contacts (different UIDs) are never touched.
_UID_PREFIX = "solaris-provider-"
_PERSON_UID_PREFIX = "solaris-person-"
_CONTACT_PREDICATES = ("phone", "email", "address", "contact_person")
# The address book name under each resident's own principal — the literal the
# `[solaris-contacts]` Radicale rights rule grants (templates/solaris/post-deploy.py).
_COLLECTION = "solaris-contacts"
_BOOK_NAME = "Solaris"


def _contact_facts(conn, entity_id: str) -> dict[str, str]:
    """The entity's contact facts, highest-confidence value winning per predicate."""
    out: dict[str, str] = {}
    for f in conn.execute(
        "SELECT predicate, value FROM facts WHERE subject_entity_id = ?"
        " ORDER BY confidence DESC",
        (entity_id,),
    ).fetchall():
        if f["predicate"] in _CONTACT_PREDICATES and f["predicate"] not in out:
            out[f["predicate"]] = f["value"]
    return out


def _add_contact_props(card, contact: dict[str, str]) -> None:
    if contact.get("phone"):
        card.add("tel").value = contact["phone"]
    if contact.get("email"):
        card.add("email").value = contact["email"]
    if contact.get("address"):
        card.add("adr").value = vobject.vcard.Address(street=contact["address"])


def _build_vcard(entity_id: str, name: str, contact: dict[str, str]) -> str:
    card = vobject.vCard()
    card.add("fn").value = name
    card.add("n").value = vobject.vcard.Name(family=name)
    card.add("org").value = [name]
    _add_contact_props(card, contact)
    note = "Anbieter aus deinen Solaris-Dokumenten."
    if contact.get("contact_person"):
        note = f"Ansprechpartner: {contact['contact_person']}. {note}"
    card.add("note").value = note
    card.add("uid").value = _UID_PREFIX + entity_id
    return card.serialize()


def _build_person_vcard(entity_id: str, name: str, contact: dict[str, str]) -> str:
    given, _, family = name.rpartition(" ")
    card = vobject.vCard()
    card.add("fn").value = name
    card.add("n").value = vobject.vcard.Name(family=family, given=given)
    _add_contact_props(card, contact)
    card.add("note").value = "Kontakt aus Solaris."
    card.add("uid").value = _PERSON_UID_PREFIX + entity_id
    return card.serialize()


def _collection_url(base_url: str, resident_uid: str) -> str:
    return f"{base_url.rstrip('/')}/{resident_uid}/{_COLLECTION}/"


async def sync_contacts(
    db_path: str,
    collection_url: str,
    username: str,
    password: str,
    base_url: str = "",
    household_uid: str = "",
) -> dict[str, int]:
    """PUT the provider orgs and each resident's `.contacts` persons as vCards.

    Orgs go to `collection_url` (the household book); persons go to their OWN
    resident's `{base_url}/{resident_uid}/solaris-contacts/`, so one resident's
    personal contacts are never written into another's principal. A person owned
    by the household sentinel routes to `household_uid` like the deadlines sync,
    since `household` is not a Radicale principal.

    Returns `{written, failed}`. Never raises — a sync failure must not abort the
    night run. Each half is a no-op when its URL or the credentials are unset."""
    if not (username and password) or not (collection_url or base_url):
        return {"written": 0, "failed": 0}
    client = HttpDavClient(carddav_username=username, carddav_password=password)
    written = failed = 0
    # target collection URL → [(vcard uid, body)].
    per_collection: dict[str, list[tuple[str, str]]] = {}
    conn = projection.open_conn(db_path)
    try:
        if collection_url:
            for o in conn.execute(
                "SELECT id, canonical_name FROM entities WHERE type = 'organization'"
                " ORDER BY canonical_name"
            ).fetchall():
                per_collection.setdefault(collection_url, []).append(
                    (
                        _UID_PREFIX + o["id"],
                        _build_vcard(
                            o["id"], o["canonical_name"], _contact_facts(conn, o["id"])
                        ),
                    )
                )
        if base_url:
            for p in conn.execute(
                "SELECT id, canonical_name, resident_uid FROM entities"
                " WHERE type = 'person' AND source = 'contact'"
                " ORDER BY canonical_name"
            ).fetchall():
                owner = p["resident_uid"]
                if owner == projection.SHARED_UID:
                    owner = household_uid or projection.SHARED_UID
                per_collection.setdefault(_collection_url(base_url, owner), []).append(
                    (
                        _PERSON_UID_PREFIX + p["id"],
                        _build_person_vcard(
                            p["id"], p["canonical_name"], _contact_facts(conn, p["id"])
                        ),
                    )
                )
    finally:
        conn.close()
    for url, cards in per_collection.items():
        try:
            await client.ensure_addressbook(url, _BOOK_NAME)
        except Exception as e:  # noqa: BLE001 — a missing book fails its PUTs below.
            log.error("engine.contacts_sync.ensure_failed", url=url, error=str(e))
        for uid, body in cards:
            try:
                await client.put_item(
                    url,
                    uid,
                    body,
                    suffix=".vcf",
                    content_type="text/vcard; charset=utf-8",
                )
                written += 1
            except Exception as e:  # noqa: BLE001 — one bad card mustn't stop the sync.
                log.error("engine.contacts_sync.card_failed", uid=uid, error=str(e))
                failed += 1
    log.info("engine.contacts_sync", written=written, failed=failed)
    return {"written": written, "failed": failed}
