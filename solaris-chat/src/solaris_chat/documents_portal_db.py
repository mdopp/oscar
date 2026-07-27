"""DB-backed document category views (#doc).

An uploaded document is extracted into a `document` entity carrying typed facts
(`category`, `provider`, `policy_number`, `cancellation_deadline`, …) — see
`engine/ingest/obsidian.py::_ingest_document`. The markdown renderer can't do
tables, so the Notizen "documents" page renders these DB-backed instead: one
doorway per category, and a table per category built straight from the
`entities`+`facts` projection (mirrors `notes_portal_db.py`).

Owner scope is the entity's `resident_uid`: the caller sees their own documents
plus the shared household pool, never another resident's (matches
`projection.entity_facts` scoping). Each fact carries its `confidence`, so the
UI can flag an unconfirmed (agent-extracted, 0.6) value vs. a human-confirmed
(1.0) one; per predicate the highest-confidence value wins.

Returns ``None`` when the projection is absent (fresh install / migration not
run) so the caller can degrade gracefully.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from solaris_chat import notes_search

_REQUIRED_TABLES = ("entities", "facts")


def _connect(db_path: str) -> sqlite3.Connection | None:
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        have = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    except sqlite3.OperationalError:
        conn.close()
        return None
    if not set(_REQUIRED_TABLES).issubset(have):
        conn.close()
        return None
    return conn


def _scope(uid: str) -> tuple[str, list[str]]:
    """`resident_uid` owner scope: the caller's own documents + shared household."""
    return "e.resident_uid IN (?, ?)", [uid, notes_search.SHARED_UID]


def categories(db_path: str, uid: str) -> dict[str, int] | None:
    """`{category: count}` for the caller's document doorways (own ∪ shared)."""
    conn = _connect(db_path)
    if conn is None:
        return None
    scope, params = _scope(uid)
    try:
        rows = conn.execute(
            "SELECT f.value AS category, COUNT(DISTINCT e.id) AS n"
            " FROM entities e JOIN facts f ON f.subject_entity_id = e.id"
            f" WHERE e.type = 'document' AND f.predicate = 'category' AND {scope}"  # noqa: S608
            " GROUP BY f.value ORDER BY n DESC, category",
            params,
        ).fetchall()
    finally:
        conn.close()
    return {r["category"]: r["n"] for r in rows}


def search(db_path: str, uid: str, q: str) -> list[dict[str, Any]] | None:
    """Documents (own ∪ shared) whose title or category matches `q` (LIKE),
    for the `.doc` filter. Empty `q` → the full owner-scoped document list.
    Each row is `{entity_id, title, category}` — the lightweight shape the
    `.doc` list needs."""
    conn = _connect(db_path)
    if conn is None:
        return None
    scope, params = _scope(uid)
    like = f"%{q.strip()}%"
    try:
        rows = conn.execute(
            "SELECT DISTINCT e.id AS entity_id, e.canonical_name AS title,"
            " (SELECT value FROM facts WHERE subject_entity_id = e.id"
            "  AND predicate = 'category' LIMIT 1) AS category"
            " FROM entities e"
            " LEFT JOIN facts f ON f.subject_entity_id = e.id"
            "  AND f.predicate = 'category'"
            f" WHERE e.type = 'document' AND {scope}"  # noqa: S608
            " AND (e.canonical_name LIKE ? OR f.value LIKE ?)"
            " ORDER BY e.canonical_name",
            [*params, like, like],
        ).fetchall()
    finally:
        conn.close()
    return [
        {"entity_id": r["entity_id"], "title": r["title"], "category": r["category"]}
        for r in rows
    ]


_CONTACT_PREDICATES = ("phone", "email", "address", "contact_person")


def contacts(db_path: str, uid: str) -> list[dict[str, Any]] | None:
    """The phone-book: every `organization` (a document's provider) in scope with
    its contact facts and the documents grouped under it (#doc-graph).

    Documents join to their org by the normalized `provider_key` fact (written on
    both the document and the org, see `_ingest_provider_org`), so provider name
    variants (`… GmbH & Co. KG` vs `… KG`) group under one contact. Contact facts
    follow the same highest-confidence-per-predicate rule as `category_view`, so a
    corrected phone number wins over the agent-extracted one."""
    conn = _connect(db_path)
    if conn is None:
        return None
    scope, params = _scope(uid)
    try:
        orgs = conn.execute(
            "SELECT DISTINCT e.id AS id, e.canonical_name AS name"
            f" FROM entities e WHERE e.type = 'organization' AND {scope}"  # noqa: S608
            " ORDER BY e.canonical_name",
            params,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for org in orgs:
            contact: dict[str, dict[str, Any]] = {}
            provider_key = ""
            for f in conn.execute(
                "SELECT predicate, value, confidence FROM facts"
                " WHERE subject_entity_id = ? ORDER BY confidence DESC",
                (org["id"],),
            ).fetchall():
                if f["predicate"] == "provider_key" and not provider_key:
                    provider_key = f["value"]
                if (
                    f["predicate"] in _CONTACT_PREDICATES
                    and f["predicate"] not in contact
                ):
                    contact[f["predicate"]] = {
                        "value": f["value"],
                        "confidence": f["confidence"],
                    }
            # The org's documents: any `document` sharing its normalized provider_key.
            docs = conn.execute(
                "SELECT DISTINCT e.id AS id, e.canonical_name AS title,"
                " (SELECT value FROM facts WHERE subject_entity_id = e.id"
                "  AND predicate = 'category' LIMIT 1) AS category"
                " FROM entities e JOIN facts f ON f.subject_entity_id = e.id"
                f" WHERE e.type = 'document' AND f.predicate = 'provider_key'"  # noqa: S608
                f" AND f.value = ? AND {scope}"
                " ORDER BY e.canonical_name",
                [provider_key, *params],
            ).fetchall()
            out.append(
                {
                    "entity_id": org["id"],
                    "name": org["name"],
                    "contact": contact,
                    "documents": [
                        {
                            "entity_id": d["id"],
                            "title": d["title"],
                            "category": d["category"],
                        }
                        for d in docs
                    ],
                }
            )
    finally:
        conn.close()
    return out


def person_contacts(db_path: str, uid: str) -> list[dict[str, Any]] | None:
    """Personal contacts (`person` entities carrying an email/phone fact — created
    via the `.contacts` command), for the `.contacts` filter.

    A view over `person_directory` (the single person + alias model, ADR 0010):
    the same rows — carrying `aliases` — narrowed to persons that have an email or
    phone, so `.contacts` and `@`-mentions resolve against one alias→person path."""
    directory = person_directory(db_path, uid)
    if directory is None:
        return None
    return [p for p in directory if p["email"] or p["phone"]]


def person_directory(db_path: str, uid: str) -> list[dict[str, Any]] | None:
    """Every `person` entity in scope (own ∪ shared household), for the people
    surfaces (Personen doorway + `@`-mention autocomplete/resolution) — ADR 0010.

    Unlike `person_contacts` (persons *with* an email/phone fact, for `.contacts`),
    this returns ALL person entities, so a person with no contact facts and no chat
    mentions still shows up. Each row is
    `{id, name (canonical_name), aliases: [...], email, phone}`.

    Aliases come from the `entity_aliases` table (the resolve/dedup mechanism);
    an `alias` predicate fact is also folded in if one exists — none do yet, so
    that arm is defensive (ADR 0010 §3 target, no writer this slice)."""
    conn = _connect(db_path)
    if conn is None:
        return None
    scope, params = _scope(uid)
    try:
        rows = conn.execute(
            "SELECT e.id AS id, e.canonical_name AS name"
            f" FROM entities e WHERE e.type = 'person' AND {scope}"  # noqa: S608
            " ORDER BY e.canonical_name",
            params,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            aliases: list[str] = [
                a["alias"]
                for a in conn.execute(
                    "SELECT alias FROM entity_aliases WHERE entity_id = ?"
                    " ORDER BY alias",
                    (r["id"],),
                ).fetchall()
                # the canonical name is stored as a self-alias; drop it here.
                if a["alias"] != r["name"]
            ]
            contact: dict[str, str] = {}
            for f in conn.execute(
                "SELECT predicate, value FROM facts WHERE subject_entity_id = ?"
                " ORDER BY confidence DESC",
                (r["id"],),
            ).fetchall():
                if f["predicate"] == "alias" and f["value"] not in aliases:
                    aliases.append(f["value"])
                elif (
                    f["predicate"] in ("email", "phone")
                    and f["predicate"] not in contact
                ):
                    contact[f["predicate"]] = f["value"]
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "aliases": aliases,
                    "email": contact.get("email", ""),
                    "phone": contact.get("phone", ""),
                }
            )
    finally:
        conn.close()
    return out


def _vorgang_key(facts: dict[str, dict[str, Any]]) -> str:
    """The Vorgang identifier for a document: its `policy_number` fact,
    normalized (case/whitespace-folded) so `KR 74536202` and `kr74536202`
    group as one Vorgang. Empty when the document carries no such identifier —
    it then stands alone, never folded into a group."""
    val = (facts.get("policy_number") or {}).get("value") or ""
    return "".join(val.split()).casefold()


def category_view(db_path: str, uid: str, category: str) -> list[dict[str, Any]] | None:
    """The table rows for one category: per document, its title + fact map.

    Each fact is `{value, confidence}`; per predicate the highest-confidence
    value wins (a human-confirmed 1.0 beats the agent-extracted 0.6), so a
    corrected field displays as authoritative.

    Each row also carries a `vorgang` key (the normalized `policy_number`, empty
    when absent) so the portal can group documents that share a policy/contract
    identifier — a renewal, its invoice and a cancellation letter — into one
    Vorgang instead of scattering them as unrelated rows."""
    conn = _connect(db_path)
    if conn is None:
        return None
    scope, params = _scope(uid)
    try:
        docs = conn.execute(
            "SELECT DISTINCT e.id AS id, e.canonical_name AS title"
            " FROM entities e JOIN facts f ON f.subject_entity_id = e.id"
            " WHERE e.type = 'document' AND f.predicate = 'category'"
            f" AND f.value = ? AND {scope}"  # noqa: S608
            " ORDER BY e.canonical_name",
            [category, *params],
        ).fetchall()
        rows: list[dict[str, Any]] = []
        for doc in docs:
            facts: dict[str, dict[str, Any]] = {}
            # confidence DESC → NULL last in sqlite, so the first row per
            # predicate is the highest-confidence (confirmed) value.
            for f in conn.execute(
                "SELECT predicate, value, confidence FROM facts"
                " WHERE subject_entity_id = ? ORDER BY confidence DESC",
                (doc["id"],),
            ).fetchall():
                if f["predicate"] not in facts:
                    facts[f["predicate"]] = {
                        "value": f["value"],
                        "confidence": f["confidence"],
                    }
            rows.append(
                {
                    "entity_id": doc["id"],
                    "title": doc["title"],
                    "facts": facts,
                    "source_document": facts.get("source_document", {}).get("value"),
                    "vorgang": _vorgang_key(facts),
                }
            )
    finally:
        conn.close()
    return rows
