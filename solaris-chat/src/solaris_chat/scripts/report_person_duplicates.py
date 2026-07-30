"""Report — never merge — the person entities that look like the same human (#994).

ADR 0010 §1 convergence stops NEW duplicates being minted (`writer.write_concept`
now runs the name/alias match for person records, `.contacts` no longer mints a
random identity key per create). It is forward-only: the duplicates already in a
box's projection stay exactly as they are. This script is how you see that legacy
set — read-only, on the box, against the live `.db`:

    python -m solaris_chat.scripts.report_person_duplicates --db /opt/data/solaris.db

It opens nothing to a resident and mutates nothing: no merge, no delete, no
rename, no new table. Deciding what to do with the pairs it prints is a separate,
explicitly-reviewed slice — merging two humans is destructive when wrong.

The predicate is salvaged verbatim from the closed draft PR #1028
(`sec/issue-994-person-dedup-merge`, `engine/knowledge/person_dedup.py:66-137`):
NFKD diacritic folding, the token-subset rule with its ≥2-shared-tokens floor,
the generational-suffix veto, and the same-owner scope. That predicate survived a
hostile maintainer review; the merge/undo machinery around it did not, and is not
reproduced here.

Two evidence classes, reported separately because they are NOT equally strong:

  * ``shared_contact_key`` — the two persons share a normalized phone or email
    AND their names are compatible. The strong cross-source signal (the same
    human arriving from `.contacts` and from CardDAV).
  * ``same_name`` — normalized names are EQUAL and there is no shared contact
    key. Weaker on purpose, and the reason this class exists: the `.contacts`
    duplicates the convergence fix prevents carry no phone or email at all, so
    ``shared_contact_key`` cannot see them. Two genuinely different people who
    share a name land here too — this class needs human eyes per pair.

Both classes are same-owner only (`resident_uid = uid`, the shared-household
sentinel excluded on both sides). That is a privacy boundary, not tidiness: the
people surfaces read an entity's facts and aliases unscoped, so treating a
private person and a household one as "the same" is how one resident's private
number reaches the whole household.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import unicodedata
from typing import Any

from solaris_chat.engine.knowledge import projection
from solaris_chat.notes_search import SHARED_UID


# Contact facts that carry a person's identity across sources; a shared one of
# these is the merge signal. Kept small on purpose — an address or a birthday is
# too weak (shared households, common dates) to be a person key.
_CONTACT_PREDICATES = ("phone", "email")

# Suffixes that distinguish two people who otherwise share a full name.
_GENERATION_SUFFIXES = frozenset(
    {"jr", "jun", "junior", "sr", "sen", "senior", "ii", "iii", "iv"}
)


def normalize_name(name: str) -> str:
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


def normalize_phone(phone: str) -> str:
    """Digits only, with a leading German 0 folded to +49 so `0177…` and
    `+49177…` are one key. Empty (unmatchable) below 6 digits — a fragment isn't
    a reliable person key."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "49" + digits[1:]
    return digits if len(digits) >= 6 else ""


def normalize_email(email: str) -> str:
    """Lowercased, trimmed. Empty (unmatchable) unless it looks like an address."""
    e = email.strip().lower()
    return e if "@" in e and "." in e.split("@")[-1] else ""


def names_compatible(a: str, b: str) -> bool:
    """True when two normalized names could be the same person: equal, or one is
    a token-subset of the other sharing at least TWO tokens.

    Two disjoint full names ("anna meyer" vs "anna schmidt") are not compatible
    even sharing a contact key — that's the false-match trap. Neither is a
    one-token overlap: on a family landline "meyer" ⊂ "anna meyer" would make the
    surname the only hurdle, so a single-token name has to match exactly. An
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


def shared_key_reason(
    name_a: str, keys_a: set[str], name_b: str, keys_b: set[str]
) -> list[str]:
    """The shared contact keys that make two persons look like one human, or `[]`
    when the conservative predicate fails."""
    shared = keys_a & keys_b
    if not shared or not names_compatible(
        normalize_name(name_a), normalize_name(name_b)
    ):
        return []
    return sorted(shared)


def person_keys(conn: sqlite3.Connection, entity_id: str, uid: str) -> set[str]:
    """The normalized contact keys (phone/email) recorded for a person that the
    caller may see, across ALL sources — so a phone from `.contacts` and the same
    phone from CardDAV collide even though they were written under different
    `source`s. Identity-scoped like `projection.entity_facts`."""
    keys: set[str] = set()
    rows = conn.execute(
        "SELECT predicate, value FROM facts"
        " WHERE subject_entity_id = ? AND resident_uid IN (?, ?)"
        f" AND predicate IN ({','.join('?' for _ in _CONTACT_PREDICATES)})",
        (entity_id, uid, SHARED_UID, *_CONTACT_PREDICATES),
    ).fetchall()
    for f in rows:
        if f["predicate"] == "phone":
            k = normalize_phone(f["value"])
            if k:
                keys.add("phone:" + k)
        else:
            k = normalize_email(f["value"])
            if k:
                keys.add("email:" + k)
    return keys


def find_duplicate_persons(conn: sqlite3.Connection, uid: str) -> list[dict[str, Any]]:
    """Suspected duplicate person pairs among the persons `uid` OWNS. Read-only.

    Both sides must have the same `resident_uid` — the shared-household sentinel
    is excluded, so neither a cross-resident nor a private↔shared pair is ever
    reported (module docstring). Each pair is `{a, b, a_name, b_name, evidence,
    detail}` with `evidence` in `("shared_contact_key", "same_name")`; the ids
    are ordered only to make the listing stable across runs, which side would be
    canonical is not decided here."""
    rows = conn.execute(
        "SELECT id, canonical_name FROM entities"
        " WHERE type = 'person' AND resident_uid = ? ORDER BY id",
        (uid,),
    ).fetchall()
    persons = [
        {
            "id": r["id"],
            "name": r["canonical_name"],
            "keys": person_keys(conn, r["id"], uid),
        }
        for r in rows
    ]
    out: list[dict[str, Any]] = []
    for i, a in enumerate(persons):
        for b in persons[i + 1 :]:
            shared = shared_key_reason(a["name"], a["keys"], b["name"], b["keys"])
            if shared:
                evidence, detail = "shared_contact_key", shared
            elif normalize_name(a["name"]) and normalize_name(
                a["name"]
            ) == normalize_name(b["name"]):
                evidence, detail = "same_name", [normalize_name(a["name"])]
            else:
                continue
            out.append(
                {
                    "a": a["id"],
                    "b": b["id"],
                    "a_name": a["name"],
                    "b_name": b["name"],
                    "evidence": evidence,
                    "detail": detail,
                }
            )
    return out


def residents(conn: sqlite3.Connection) -> list[str]:
    """Every `resident_uid` that owns a person entity, the household sentinel
    included — household persons are checked against each other, never against a
    resident's private ones."""
    rows = conn.execute(
        "SELECT DISTINCT resident_uid FROM entities WHERE type = 'person'"
        " ORDER BY resident_uid"
    ).fetchall()
    return [r["resident_uid"] for r in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="path to the Solaris projection .db")
    ap.add_argument(
        "--uid",
        default="",
        help="report only this resident (default: every owner of a person entity)",
    )
    args = ap.parse_args(argv)

    conn = projection.open_conn(args.db)
    try:
        uids = [args.uid] if args.uid else residents(conn)
        total = 0
        for uid in uids:
            pairs = find_duplicate_persons(conn, uid)
            total += len(pairs)
            if not pairs:
                continue
            print(f"{uid}: {len(pairs)} suspected duplicate pair(s)")
            for p in pairs:
                print(
                    f"  [{p['evidence']}] {p['a_name']!r} ({p['a'][:8]})"
                    f" ~ {p['b_name']!r} ({p['b'][:8]}) — {', '.join(p['detail'])}"
                )
        print(f"total: {total} suspected pair(s) — nothing was changed")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
