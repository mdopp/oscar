"""Paperless → Solaris read-back of a confirmed correspondent/document type (#1051).

The #931 adapter is push-only: Solaris stores documents in paperless and never
reads anything back. But paperless already owns a mature correction UI, so once
a resident fixes a document's **correspondent** and **document type** there,
those two values are the truth and Solaris has to converge onto them — nothing
new to build on the Solaris side.

This module is that read path, run on the existing ingest cron right after the
push: read every paperless document plus the `correspondents`/`document_types`
lookups, resolve each id to its name, and record what that means for the
document's OKF note.

Convergence goes through the vault, not around it: the confirmed values map onto
the `category:` / `provider:` frontmatter of the `document` note the extraction
agent wrote — the SAME fields it fills — so the existing obsidian ingest projects
them into the one `category` fact the Dokumente doorways read and the one
`provider_key` link the phone-book groups by. Writing a second, paperless-sourced
`category` fact instead would list the document under TWO doorways at once (both
facts survive, and `categories()` groups by value), and a parallel field would
fork the SSOT. The frontmatter write lands in the projection on the NEXT cycle
(obsidian runs before paperless in `run_ingest`) — a cron cycle late is fine for
a correction the resident just made in another UI.

One-way and value-gated: a null correspondent/document type is left alone and
nothing is ever written back to paperless, so an instance where the resident has
confirmed nothing yet (the state today: 18 documents, none carrying either field)
is a clean no-op that only logs what it saw.

Caveat from #1051's own review: paperless has no separate "human-confirmed" flag,
so a value its matching algorithm auto-assigned reads exactly like one a resident
set by hand. Today nothing can auto-assign (the instance defines no
correspondents or document types at all, and the #1050 AI classifier only
*suggests* into the review pane); if auto-matching is ever switched on, the
confirmation signal has to be defined before this converges those values.
"""

from __future__ import annotations

import re
from pathlib import Path

from solaris_chat.engine.tools.documents import CATEGORIES
from solaris_chat.logging import log

from .paperless_client import PaperlessClient

# A paperless document type is free text the resident types into paperless's UI,
# while `category` is a fixed OKF vocabulary the Dokumente doorways label + sort
# by. Accept the vocabulary's own keys and the German doorway names Solaris shows
# for them (index.html `DOCUMENT_CATEGORY_META`) — a type named anything else is
# logged with an empty category and left alone rather than guessed at.
_TYPE_CATEGORY = {
    "versicherungen": "insurance",
    "vertrage abos": "contract",
    "rechnungen": "invoice",
    "strom gas wasser": "utility",
    "arbeitsvertrag": "employment",
    "altersvorsorge": "pension",
    "krankenkasse": "health_insurance",
    "bank finanzen": "bank",
    "steuern": "tax",
    "fahrzeug": "vehicle",
    "immobilie wohnen": "property",
    "garantien belege": "warranty",
    "mitgliedschaften": "membership",
    "ausweise": "id_document",
    "recht vorsorge": "legal",
    "familie kinder": "family",
    "gerate": "appliance",
    "sonstiges": "other",
}

_FENCE = re.compile(r"\A---\n(?P<fm>.*?)\n---\n", re.DOTALL)
_UMLAUTS = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "s"})


def _normalized(name: str) -> str:
    """A document-type name reduced to its lookup key: casefolded, umlauts
    folded, punctuation (`&`, `-`, `/`) collapsed to single spaces."""
    folded = name.casefold().translate(_UMLAUTS)
    return " ".join(re.findall(r"[a-z0-9_]+", folded))


def _category_for(document_type: str) -> str:
    key = _normalized(document_type)
    if key.replace(" ", "_") in CATEGORIES:
        return key.replace(" ", "_")
    return _TYPE_CATEGORY.get(key, "")


def _fm_value(frontmatter: str, key: str) -> str:
    for line in frontmatter.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip() == key:
            return value.strip().strip("'\"")
    return ""


def _document_notes(notes_dir: str) -> dict[str, Path]:
    """`{upload stem: document note}` — the join key back to paperless.

    A document note records the companion it was extracted from
    (`source_document: users/<uid>/uploads/<stem>.md`), and the file pushed to
    paperless is that same stem, so paperless's `original_file_name` stem matches
    it. A stem two notes claim is dropped: an ambiguous match must not converge
    onto the wrong resident's document."""
    root = Path(notes_dir)
    notes: dict[str, Path] = {}
    ambiguous: set[str] = set()
    for note in sorted(
        {*root.glob("users/*/okf/documents/*.md"), *root.glob("okf/documents/*.md")}
    ):
        try:
            text = note.read_text(encoding="utf-8")
        except OSError as e:
            log.error("engine.ingest.paperless_readback_note_failed", error=str(e))
            continue
        fence = _FENCE.match(text)
        source = _fm_value(fence.group("fm"), "source_document") if fence else ""
        stem = Path(source).stem if source else note.stem
        if stem in notes:
            ambiguous.add(stem)
        notes[stem] = note
    for stem in ambiguous:
        notes.pop(stem, None)
    return notes


def _with_field(text: str, key: str, value: str) -> str | None:
    """`text` with its frontmatter `key` set to `value`, or None when the note
    already carries that value (or has no frontmatter fence to write into)."""
    fence = _FENCE.match(text)
    if fence is None:
        return None
    frontmatter = fence.group("fm")
    if _fm_value(frontmatter, key) == value:
        return None
    lines = frontmatter.splitlines()
    # Frontmatter values are single-line — a name with a newline in it would
    # otherwise break the block for every other field.
    replacement = f"{key}: {' '.join(value.split())}"
    for i, line in enumerate(lines):
        name, sep, _ = line.partition(":")
        if sep and name.strip() == key:
            lines[i] = replacement
            break
    else:
        lines.append(replacement)
    return "---\n" + "\n".join(lines) + "\n---\n" + text[fence.end() :]


def _apply(note: Path, fields: dict[str, str]) -> list[str]:
    """Write `fields` into the note's frontmatter; return the keys changed."""
    try:
        text = note.read_text(encoding="utf-8")
    except OSError as e:
        log.error("engine.ingest.paperless_readback_note_failed", error=str(e))
        return []
    applied: list[str] = []
    for key, value in fields.items():
        updated = _with_field(text, key, value)
        if updated is not None:
            text = updated
            applied.append(key)
    if not applied:
        return []
    try:
        note.write_text(text, encoding="utf-8")
    except OSError as e:
        log.error("engine.ingest.paperless_readback_write_failed", error=str(e))
        return []
    return applied


async def read_back(notes_dir: str, client: PaperlessClient) -> int:
    """Converge every paperless document's confirmed document type into its OKF
    note. Returns the number of documents carrying a confirmed value. Never
    raises — the read-back is advisory, the push must not be affected."""
    try:
        documents = await client.list_documents()
        correspondents = await client.list_names("correspondents")
        types = await client.list_names("document_types")
    except Exception as e:  # noqa: BLE001 — a read-back failure must not break ingest.
        log.error("engine.ingest.paperless_readback_failed", error=repr(e))
        return 0

    notes = _document_notes(notes_dir)
    confirmed = 0
    changed = 0
    unmatched = 0
    for doc in documents:
        correspondent = correspondents.get(doc.get("correspondent") or 0, "")
        document_type = types.get(doc.get("document_type") or 0, "")
        if not correspondent and not document_type:
            continue
        confirmed += 1
        stem = Path(str(doc.get("original_file_name") or doc.get("title") or "")).stem
        note = notes.get(stem)
        if note is None:
            unmatched += 1
        category = _category_for(document_type) if document_type else ""
        applied = _apply(note, {"category": category}) if note and category else []
        changed += bool(applied)
        log.info(
            "engine.ingest.paperless_readback_doc",
            paperless_id=doc.get("id"),
            file=stem,
            correspondent=correspondent,
            document_type=document_type,
            category=category,
            note=str(note.relative_to(notes_dir)) if note is not None else "",
            applied=",".join(applied),
        )
    log.info(
        "engine.ingest.paperless_readback",
        documents=len(documents),
        confirmed=confirmed,
        changed=changed,
        unmatched=unmatched,
    )
    return confirmed
