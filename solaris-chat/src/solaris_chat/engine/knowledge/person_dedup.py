"""Cross-source person dedup + human-confirmed merge (#994, ADR 0010).

The persons/contacts SSOT (`entities` of `type='person'`) accretes duplicates:
the same human arrives from `.contacts`, a CalDAV sync, and chat `@`-mentions as
three separate entities. This module finds *likely* duplicates and, on explicit
confirmation, merges them onto one primary — re-pointing the secondary's
aliases/facts/event edges and recording an audit/undo trail.

Two invariants make this safe to ship (merging two humans is DESTRUCTIVE and
irreversible without care):

  1. **Conservative detection.** A candidate needs a shared *contact key* (a
     normalized phone or email — the strongest cross-source person signal) AND
     compatible names. A name-only match is never offered: two distinct "Anna
     Meyer"s must not be auto-merged. False-merge = irreversible data loss, so
     detection biases hard toward precision over recall. The mutating
     `merge_persons` re-checks that same predicate — a caller can't reach past
     detection with a hand-built pair.
  2. **Same-owner only.** Detection AND every mutation are scoped to persons the
     caller owns (`resident_uid = uid`); the shared-household sentinel is
     excluded on both sides. This is load-bearing, not tidiness: a merge
     re-points aliases/facts onto the primary *without* rewriting
     `facts.resident_uid`, and the people surfaces (`person_directory`) read an
     entity's facts and aliases unscoped. A private↔household merge would
     therefore publish one resident's private phone number and aliases to the
     whole household; mirrored, it would delete the shared contact for everyone
     unasked. Same-owner pairs make both impossible. Cross-resident and
     private↔shared merges are a deliberate non-goal here.

Merge itself is never automatic: `find_merge_candidates` surfaces pairs for the
UI to confirm, `preview_merge` is a no-write dry-run, and only `merge_persons`
(called on explicit confirmation) mutates. Every merge writes a `person_merges`
row recording the secondary's provenance, a snapshot of what moved, and exactly
which aliases/event edges the merge ADDED to the primary — so `undo_merge`
removes precisely those again and the merge really is reversible.

Scope caveat: the secondary's DB teardown goes through
`projection.delete_note_by_okf_path` (its `concepts` row + `okf_vectors`
embedding + entity), so the merged-away duplicate leaves RAG and the DB
projection. Its markdown file in the vault is deliberately left on disk — it is
the human-editable artifact and unlinking it would make the undo a lie.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from typing import Any

from solaris_chat.notes_search import SHARED_UID

from . import projection


# Contact facts that carry a person's identity across sources; a shared one of
# these is the merge signal. Kept small on purpose — an address or a birthday is
# too weak (shared households, common dates) to be a person key.
_CONTACT_PREDICATES = ("phone", "email")


def _normalize_name(name: str) -> str:
    """A person's name comparison key: casefold, fold diacritics, drop
    punctuation, collapse whitespace. Empty when nothing alphanumeric survives —
    then names never match (an unnamed contact isn't a name signal).

    NFKD + dropping combining marks folds `ï`→`i` for EVERY script. A character
    class that whitelists some diacritics instead would *split* the others, and
    the fragment then satisfies the token-subset rule (`Ana Müller` reading as a
    subset of `Anaïs Müller`)."""
    folded = unicodedata.normalize("NFKD", name.casefold())
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", stripped))


def _normalize_phone(phone: str) -> str:
    """Digits only, with a leading German 0 folded to +49 so `0177…` and
    `+49177…` are one key. Empty (unmatchable) below 6 digits — a fragment isn't
    a reliable person key."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "49" + digits[1:]
    return digits if len(digits) >= 6 else ""


def _normalize_email(email: str) -> str:
    """Lowercased, trimmed. Empty (unmatchable) unless it looks like an address."""
    e = email.strip().lower()
    return e if "@" in e and "." in e.split("@")[-1] else ""


# Suffixes that distinguish two people who otherwise share a full name.
_GENERATION_SUFFIXES = frozenset(
    {"jr", "jun", "junior", "sr", "sen", "senior", "ii", "iii", "iv"}
)


def _names_compatible(a: str, b: str) -> bool:
    """True when two normalized names could be the same person: equal, or one is
    a token-subset of the other sharing at least TWO tokens.

    Two disjoint full names ("anna meyer" vs "anna schmidt") are not compatible
    even sharing a contact key — that's the false-merge trap. Neither is a
    one-token overlap: on a family landline "meyer" ⊂ "anna meyer" would make
    the surname the only hurdle, so a single-token name has to match exactly. An
    empty name is never a match (a pair needs the contact key AND a name
    signal)."""
    if not a or not b:
        return False
    ta, tb = set(a.split()), set(b.split())
    if ta == tb:
        return True
    if not ((ta <= tb or tb <= ta) and len(ta & tb) >= 2):
        return False
    # A generational suffix is the one token that asserts these are DIFFERENT
    # people: "thomas meyer" vs "thomas meyer jr" is father and son, and on a
    # shared family landline the contact key matches too, so the subset rule
    # alone would offer them as duplicates.
    return not (ta ^ tb) <= _GENERATION_SUFFIXES


def _merge_reason(
    name_a: str, keys_a: set[str], name_b: str, keys_b: set[str]
) -> list[str]:
    """The shared contact keys that justify merging two persons, or `[]` when the
    conservative predicate fails. The single definition of "likely duplicate" —
    detection and the mutating merge both go through it."""
    shared = keys_a & keys_b
    if not shared or not _names_compatible(
        _normalize_name(name_a), _normalize_name(name_b)
    ):
        return []
    return sorted(shared)


def _person_keys(conn: sqlite3.Connection, entity_id: str, uid: str) -> set[str]:
    """The normalized contact keys (phone/email) recorded for a person that the
    caller may see, across ALL sources — so a phone from `.contacts` and the same
    phone from CalDAV collide even though they were written under different
    `source`s. Identity-scoped like `projection.entity_facts`."""
    keys: set[str] = set()
    for f in conn.execute(
        "SELECT predicate, value FROM facts"
        " WHERE subject_entity_id = ? AND resident_uid IN (?, ?)",
        (entity_id, uid, SHARED_UID),
    ).fetchall():
        if f["predicate"] == "phone":
            k = _normalize_phone(f["value"])
            if k:
                keys.add("phone:" + k)
        elif f["predicate"] == "email":
            k = _normalize_email(f["value"])
            if k:
                keys.add("email:" + k)
    return keys


def _person_aliases(conn: sqlite3.Connection, entity_id: str) -> list[str]:
    return [
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias",
            (entity_id,),
        ).fetchall()
    ]


def find_merge_candidates(conn: sqlite3.Connection, uid: str) -> list[dict[str, Any]]:
    """Likely-duplicate person pairs among the persons `uid` OWNS, for the UI to
    CONFIRM — never auto-merged.

    A pair is offered only when the two persons share a normalized contact key
    (phone/email) AND their names are compatible (`_names_compatible`). Both
    sides must have the same `resident_uid` — the shared-household sentinel is
    excluded, so neither a cross-resident nor a private↔shared pair is ever
    surfaced (module docstring, invariant 2). Each candidate is
    `{primary, secondary, reason, primary_name, secondary_name}`; entity ids are
    uuid4/content hashes, so `sorted()` only makes the pair stable across calls —
    which side becomes the primary carries no meaning and is the resident's call
    to confirm."""
    rows = conn.execute(
        "SELECT id, canonical_name, resident_uid FROM entities"
        " WHERE type = 'person' AND resident_uid = ?"
        " ORDER BY id",
        (uid,),
    ).fetchall()
    persons = [
        {
            "id": r["id"],
            "name": r["canonical_name"],
            "keys": _person_keys(conn, r["id"], uid),
        }
        for r in rows
    ]
    out: list[dict[str, Any]] = []
    for i, a in enumerate(persons):
        for b in persons[i + 1 :]:
            reason = _merge_reason(a["name"], a["keys"], b["name"], b["keys"])
            if not reason:
                continue
            primary, secondary = sorted((a, b), key=lambda p: p["id"])
            out.append(
                {
                    "primary": primary["id"],
                    "secondary": secondary["id"],
                    "primary_name": primary["name"],
                    "secondary_name": secondary["name"],
                    "reason": reason,
                }
            )
    return out


def _owned_person(
    conn: sqlite3.Connection, entity_id: str, uid: str
) -> sqlite3.Row | None:
    """The person row iff `uid` OWNS it. The shared-household sentinel is
    excluded on purpose — see the module docstring, invariant 2: "own ∪ shared"
    is a common pot both residents write through, not a boundary between them."""
    return conn.execute(
        "SELECT id, canonical_name, resident_uid, source FROM entities"
        " WHERE id = ? AND type = 'person' AND resident_uid = ?",
        (entity_id, uid),
    ).fetchone()


def merge_refusal(
    conn: sqlite3.Connection, primary_id: str, secondary_id: str, uid: str
) -> str | None:
    """Why this pair may NOT be merged, or ``None`` when it may.

    `"same_person"` (the two ids are equal), `"not_in_scope"` (the caller doesn't
    own both persons) or `"not_a_duplicate"` (the pair fails the very predicate
    `find_merge_candidates` uses, so it was never offered)."""
    if primary_id == secondary_id:
        return "same_person"
    p = _owned_person(conn, primary_id, uid)
    s = _owned_person(conn, secondary_id, uid)
    if p is None or s is None:
        return "not_in_scope"
    if not _merge_reason(
        p["canonical_name"],
        _person_keys(conn, primary_id, uid),
        s["canonical_name"],
        _person_keys(conn, secondary_id, uid),
    ):
        return "not_a_duplicate"
    return None


def preview_merge(
    conn: sqlite3.Connection, primary_id: str, secondary_id: str, uid: str
) -> dict[str, Any] | None:
    """A no-write dry-run of merging `secondary` into `primary`: what the merged
    person would carry. Owner-gated on BOTH persons (returns ``None`` if the
    caller doesn't own both, so cross-resident and private↔shared are refused
    here too).

    Returns `{primary, secondary, name, aliases, facts, keys}` — the union of the
    two persons' aliases and their combined contact keys — for the confirmation
    card to show before the resident commits."""
    p = _owned_person(conn, primary_id, uid)
    s = _owned_person(conn, secondary_id, uid)
    if p is None or s is None or primary_id == secondary_id:
        return None
    aliases = sorted(
        set(_person_aliases(conn, primary_id))
        | set(_person_aliases(conn, secondary_id))
    )
    keys = sorted(
        _person_keys(conn, primary_id, uid) | _person_keys(conn, secondary_id, uid)
    )
    facts = [
        {"predicate": f["predicate"], "value": f["value"], "source": f["source"]}
        for f in conn.execute(
            "SELECT predicate, value, source FROM facts"
            " WHERE subject_entity_id IN (?, ?) AND resident_uid IN (?, ?)"
            " ORDER BY predicate, value",
            (primary_id, secondary_id, uid, SHARED_UID),
        ).fetchall()
    ]
    return {
        "primary": primary_id,
        "secondary": secondary_id,
        "name": p["canonical_name"],
        "aliases": aliases,
        "facts": facts,
        "keys": keys,
    }


def _snapshot(conn: sqlite3.Connection, entity_id: str, uid: str) -> dict[str, Any]:
    """A restorable snapshot of the secondary before merge: its aliases, facts,
    and event edges. Stored in the undo trail so `undo_merge` can recreate the
    entity and its rows. Facts are identity-scoped like every other read here."""
    return {
        "aliases": _person_aliases(conn, entity_id),
        "facts": [
            dict(r)
            for r in conn.execute(
                "SELECT id, resident_uid, predicate, value, confidence, source"
                " FROM facts WHERE subject_entity_id = ? AND resident_uid IN (?, ?)",
                (entity_id, uid, SHARED_UID),
            ).fetchall()
        ],
        "event_entities": [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, role FROM event_entities WHERE entity_id = ?",
                (entity_id,),
            ).fetchall()
        ],
    }


def _delete_person(conn: sqlite3.Connection, entity_id: str) -> None:
    """Tear the merged-away person down through the projection's own full delete
    so its `concepts` row and `okf_vectors` embedding go too — otherwise the
    duplicate keeps surfacing in Notizen and RAG after the merge. A person with
    no projected note has no `concepts` row; then the entity row is the whole
    delete."""
    row = conn.execute(
        "SELECT okf_path FROM concepts WHERE ref_kind = 'entity' AND ref_id = ?",
        (entity_id,),
    ).fetchone()
    if row is None:
        conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
    else:
        projection.delete_note_by_okf_path(conn, row["okf_path"])


def merge_persons(
    conn: sqlite3.Connection,
    *,
    primary_id: str,
    secondary_id: str,
    uid: str,
) -> str | None:
    """Merge `secondary` into `primary` — call ONLY on explicit confirmation.

    Refuses unless `merge_refusal` clears the pair: the caller must OWN both
    persons and the pair must still satisfy the same duplicate predicate
    `find_merge_candidates` applies, so a hand-built pair can't reach past
    detection. Re-points the secondary's aliases, facts, and event edges onto the
    primary, records a `person_merges` audit/undo row (the secondary's
    provenance, a snapshot of what moved, and exactly what was ADDED to the
    primary), then tears the secondary down. Returns the merge-record id, or
    ``None`` when refused.

    The caller commits. The secondary's markdown file in the vault is left on
    disk (module docstring)."""
    s = _owned_person(conn, secondary_id, uid)
    if s is None or merge_refusal(conn, primary_id, secondary_id, uid) is not None:
        return None

    snapshot = _snapshot(conn, secondary_id, uid)

    # Aliases the primary already carried are NOT ours to remove on undo, so
    # record only the ones this merge actually added (rowcount 0 = ignored dup),
    # plus the secondary's canonical name so `@`-mentions of the old spelling
    # still resolve.
    added_aliases: list[str] = []
    for alias in dict.fromkeys([s["canonical_name"], *snapshot["aliases"]]):
        cur = conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (primary_id, alias),
        )
        if cur.rowcount:
            added_aliases.append(alias)
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (secondary_id,))

    conn.execute(
        "UPDATE facts SET subject_entity_id = ? WHERE subject_entity_id = ?",
        (primary_id, secondary_id),
    )
    # Event edges: point at the primary, but INSERT OR IGNORE can't dedup via an
    # UPDATE, so delete-then-reinsert to respect the (event, entity, role) PK.
    added_events: list[dict[str, Any]] = []
    for e in snapshot["event_entities"]:
        cur = conn.execute(
            "INSERT OR IGNORE INTO event_entities (event_id, entity_id, role)"
            " VALUES (?, ?, ?)",
            (e["event_id"], primary_id, e["role"]),
        )
        if cur.rowcount:
            added_events.append(e)
    conn.execute("DELETE FROM event_entities WHERE entity_id = ?", (secondary_id,))
    _delete_person(conn, secondary_id)

    snapshot["added_aliases"] = added_aliases
    snapshot["added_event_entities"] = added_events
    merge_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO person_merges"
        " (id, primary_entity_id, secondary_entity_id, secondary_name,"
        "  secondary_resident_uid, secondary_source, snapshot, merged_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            merge_id,
            primary_id,
            secondary_id,
            s["canonical_name"],
            s["resident_uid"],
            s["source"],
            json.dumps(snapshot),
            uid,
        ),
    )
    return merge_id


def undo_merge(conn: sqlite3.Connection, merge_id: str, uid: str) -> bool:
    """Reverse a merge from its audit row, restoring BOTH sides: the aliases and
    event edges the merge added come back off the primary, the moved facts go
    back, and the secondary person is recreated. Returns ``False`` if the record
    is missing, already undone, or wasn't made by this caller.

    Only `merged_by` may undo. Gating on the secondary's owner instead would let
    any resident undo someone else's merge whenever the secondary was shared."""
    row = conn.execute(
        "SELECT * FROM person_merges WHERE id = ? AND undone_at IS NULL",
        (merge_id,),
    ).fetchone()
    if row is None or row["merged_by"] != uid:
        return False
    if conn.execute(
        "SELECT 1 FROM entities WHERE id = ?", (row["secondary_entity_id"],)
    ).fetchone():
        return False  # already restored / id in use
    snapshot = json.loads(row["snapshot"])
    conn.execute(
        "INSERT INTO entities"
        " (id, type, canonical_name, resident_uid, source, content_hash)"
        " VALUES (?, 'person', ?, ?, ?, '')",
        (
            row["secondary_entity_id"],
            row["secondary_name"],
            row["secondary_resident_uid"],
            row["secondary_source"],
        ),
    )
    for alias in snapshot["added_aliases"]:
        conn.execute(
            "DELETE FROM entity_aliases WHERE entity_id = ? AND alias = ?",
            (row["primary_entity_id"], alias),
        )
    for alias in dict.fromkeys([row["secondary_name"], *snapshot["aliases"]]):
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (row["secondary_entity_id"], alias),
        )
    # The merge MOVED the secondary's facts onto the primary (UPDATE, not copy),
    # so each snapshot fact id still exists under the primary — re-point it back.
    # A row that's since been deleted/re-ingested is re-inserted from the snapshot.
    for f in snapshot["facts"]:
        cur = conn.execute(
            "UPDATE facts SET subject_entity_id = ? WHERE id = ?",
            (row["secondary_entity_id"], f["id"]),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO facts"
                " (id, subject_entity_id, resident_uid, predicate, value, confidence, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f["id"],
                    row["secondary_entity_id"],
                    f["resident_uid"],
                    f["predicate"],
                    f["value"],
                    f["confidence"],
                    f["source"],
                ),
            )
    for e in snapshot["added_event_entities"]:
        conn.execute(
            "DELETE FROM event_entities"
            " WHERE event_id = ? AND entity_id = ? AND role = ?",
            (e["event_id"], row["primary_entity_id"], e["role"]),
        )
    for e in snapshot["event_entities"]:
        conn.execute(
            "INSERT OR IGNORE INTO event_entities (event_id, entity_id, role)"
            " VALUES (?, ?, ?)",
            (e["event_id"], row["secondary_entity_id"], e["role"]),
        )
    conn.execute(
        "UPDATE person_merges SET undone_at = datetime('now') WHERE id = ?",
        (merge_id,),
    )
    return True
