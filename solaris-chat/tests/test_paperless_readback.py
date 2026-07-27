"""Paperless → OKF read-back of a confirmed correspondent/document type (#1051).

The paperless REST client is faked (no live instance): these cover the id→name
resolution, the document-type→`category` mapping, the join from a paperless
document back to its OKF `document` note — and, above all, the no-op on the
state the real instance is in today (documents stored, nothing confirmed yet).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from solaris_chat import documents_portal_db
from solaris_chat.engine.ingest import ObsidianIngest, paperless_readback
from solaris_chat.engine.ingest.obsidian_reader import VaultObsidianReader
from solaris_chat.engine.ingest.paperless_readback import read_back
from solaris_chat.engine.knowledge.writer import OkfWriter
from tests.test_obsidian_ingest import _SCHEMA  # shared projection schema


class FakePaperless:
    """Serves canned `/api/documents/` + lookup lists to the read-back."""

    def __init__(self, documents, correspondents=None, document_types=None):
        self.documents = documents
        self.names = {
            "correspondents": correspondents or {},
            "document_types": document_types or {},
        }

    async def list_documents(self):
        return self.documents

    async def list_names(self, resource):
        return self.names[resource]


def _doc(doc_id, filename, *, correspondent=None, document_type=None):
    return {
        "id": doc_id,
        "original_file_name": filename,
        "title": Path(filename).stem,
        "correspondent": correspondent,
        "document_type": document_type,
    }


def _note(vault: Path, rel: str, **frontmatter) -> Path:
    """A `document` note as the extraction tool writes it."""
    fm = {"type": "document", "title": "ERGO Rechtsschutz", **frontmatter}
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "---\n" + "".join(f"{k}: {v}\n" for k, v in fm.items()) + "---\n"
    path.write_text(body, encoding="utf-8")
    return path


def _logged(monkeypatch) -> list[dict]:
    records: list[dict] = []
    monkeypatch.setattr(
        paperless_readback.log,
        "info",
        lambda msg, **kw: records.append({"msg": msg, **kw}),
    )
    return records


def _run(vault: Path, client) -> int:
    return asyncio.run(read_back(str(vault), client))


def test_nothing_confirmed_is_a_clean_no_op(tmp_path, monkeypatch):
    # The live instance today: documents stored, but no correspondent, document
    # type or lookup defined at all. The pass must touch nothing and say so.
    note = _note(
        tmp_path,
        "users/mdopp/okf/documents/scan.md",
        category="insurance",
        source_document="users/mdopp/uploads/scan.md",
    )
    before = note.read_text(encoding="utf-8")
    records = _logged(monkeypatch)

    assert _run(tmp_path, FakePaperless([_doc(1, "scan.pdf")])) == 0

    assert note.read_text(encoding="utf-8") == before
    summary = [r for r in records if r["msg"] == "engine.ingest.paperless_readback"]
    assert summary == [
        {
            "msg": "engine.ingest.paperless_readback",
            "documents": 1,
            "confirmed": 0,
            "changed": 0,
            "unmatched": 0,
        }
    ]
    # Nothing confirmed → not one per-document line to review.
    assert not [
        r for r in records if r["msg"] == "engine.ingest.paperless_readback_doc"
    ]


def test_confirmed_values_resolve_to_names_and_the_owning_note(tmp_path, monkeypatch):
    _note(
        tmp_path,
        "users/mdopp/okf/documents/scan.md",
        category="other",
        source_document="users/mdopp/uploads/scan.md",
    )
    records = _logged(monkeypatch)
    client = FakePaperless(
        [_doc(7, "scan.pdf", correspondent=3, document_type=5)],
        correspondents={3: "ERGO Versicherung AG"},
        document_types={5: "Versicherungen"},
    )

    assert _run(tmp_path, client) == 1

    line = [r for r in records if r["msg"] == "engine.ingest.paperless_readback_doc"]
    assert line == [
        {
            "msg": "engine.ingest.paperless_readback_doc",
            "paperless_id": 7,
            "file": "scan",
            "correspondent": "ERGO Versicherung AG",
            "document_type": "Versicherungen",
            "category": "insurance",
            "note": "users/mdopp/okf/documents/scan.md",
            "applied": "category",
        }
    ]


@pytest.mark.parametrize(
    "document_type,category",
    [
        ("Versicherungen", "insurance"),  # the German doorway name Solaris shows
        ("Verträge & Abos", "contract"),  # punctuation + umlaut folded
        ("Strom, Gas & Wasser", "utility"),
        ("Geräte", "appliance"),
        ("invoice", "invoice"),  # the OKF key itself
        ("health insurance", "health_insurance"),
        ("Kontoauszug", ""),  # a name Solaris has no category for → left alone
    ],
)
def test_document_type_maps_onto_the_category_vocabulary(document_type, category):
    assert paperless_readback._category_for(document_type) == category


def test_confirmed_type_rewrites_the_notes_category(tmp_path):
    note = _note(
        tmp_path,
        "users/mdopp/okf/documents/scan.md",
        category="other",
        provider="ERGO",
        source_document="users/mdopp/uploads/scan.md",
    )
    client = FakePaperless(
        [_doc(7, "scan.pdf", document_type=5)], document_types={5: "Versicherungen"}
    )

    _run(tmp_path, client)

    text = note.read_text(encoding="utf-8")
    assert "category: insurance" in text
    assert "category: other" not in text
    # Only that one field moves — the extraction's other fields stay.
    assert "provider: ERGO" in text
    assert "source_document: users/mdopp/uploads/scan.md" in text


def test_rewrite_is_idempotent(tmp_path):
    note = _note(
        tmp_path,
        "users/mdopp/okf/documents/scan.md",
        category="insurance",
        source_document="users/mdopp/uploads/scan.md",
    )
    before = note.read_text(encoding="utf-8")
    client = FakePaperless(
        [_doc(7, "scan.pdf", document_type=5)], document_types={5: "Versicherungen"}
    )

    _run(tmp_path, client)
    _run(tmp_path, client)

    # Already converged → the note is not rewritten at all.
    assert note.read_text(encoding="utf-8") == before


def test_unmappable_type_leaves_the_category_alone(tmp_path, monkeypatch):
    note = _note(
        tmp_path,
        "users/mdopp/okf/documents/scan.md",
        category="insurance",
        source_document="users/mdopp/uploads/scan.md",
    )
    records = _logged(monkeypatch)
    client = FakePaperless(
        [_doc(7, "scan.pdf", document_type=5)], document_types={5: "Kontoauszug"}
    )

    _run(tmp_path, client)

    assert "category: insurance" in note.read_text(encoding="utf-8")
    # The log names the type Solaris couldn't place, so an alias can be added.
    line = [r for r in records if r["msg"] == "engine.ingest.paperless_readback_doc"][0]
    assert line["document_type"] == "Kontoauszug"
    assert line["category"] == "" and line["applied"] == ""


def test_category_is_added_to_a_note_that_has_none(tmp_path):
    note = _note(
        tmp_path,
        "users/mdopp/okf/documents/scan.md",
        source_document="users/mdopp/uploads/scan.md",
    )
    client = FakePaperless(
        [_doc(7, "scan.pdf", document_type=5)], document_types={5: "Rechnungen"}
    )

    _run(tmp_path, client)

    assert "category: invoice" in note.read_text(encoding="utf-8")
    assert note.read_text(encoding="utf-8").startswith("---\ntype: document\n")


def test_document_without_a_note_is_counted_unmatched(tmp_path, monkeypatch):
    records = _logged(monkeypatch)
    client = FakePaperless(
        [_doc(9, "fremd.pdf", document_type=1)],
        document_types={1: "Rechnungen"},
    )

    assert _run(tmp_path, client) == 1

    summary = [r for r in records if r["msg"] == "engine.ingest.paperless_readback"][0]
    assert summary["confirmed"] == 1 and summary["unmatched"] == 1
    line = [r for r in records if r["msg"] == "engine.ingest.paperless_readback_doc"][0]
    assert line["note"] == "" and line["category"] == "invoice"


def test_a_stem_two_residents_share_is_not_matched(tmp_path, monkeypatch):
    # Two residents can upload their own `scan.pdf`; converging onto either note
    # would be a coin flip, so an ambiguous stem matches nothing.
    _note(
        tmp_path,
        "users/mdopp/okf/documents/scan.md",
        source_document="users/mdopp/uploads/scan.md",
    )
    _note(
        tmp_path,
        "users/lena/okf/documents/scan.md",
        source_document="users/lena/uploads/scan.md",
    )
    records = _logged(monkeypatch)
    client = FakePaperless(
        [_doc(3, "scan.pdf", document_type=1)], document_types={1: "Rechnungen"}
    )

    assert _run(tmp_path, client) == 1
    line = [r for r in records if r["msg"] == "engine.ingest.paperless_readback_doc"][0]
    assert line["note"] == ""


def test_client_failure_degrades_to_zero(tmp_path, monkeypatch):
    class Boom(FakePaperless):
        async def list_documents(self):
            raise TimeoutError()

    errors: list[dict] = []
    monkeypatch.setattr(
        paperless_readback.log,
        "error",
        lambda msg, **kw: errors.append({"msg": msg, **kw}),
    )
    assert _run(tmp_path, Boom([])) == 0
    assert errors[0]["msg"] == "engine.ingest.paperless_readback_failed"
    assert errors[0]["error"] == "TimeoutError()"


def test_the_dokumente_doorway_follows_the_confirmed_type(tmp_path):
    """End to end: the confirmed type moves the document to the right doorway —
    and to ONE doorway, which a second paperless-sourced `category` fact would
    not (the document would then be listed under both)."""
    db_path = str(tmp_path / "solaris.db")
    notes_dir = tmp_path / "notes"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    writer = OkfWriter(db_path=db_path, notes_dir=str(notes_dir))

    def ingest():
        ObsidianIngest(
            VaultObsidianReader(str(notes_dir)),
            writer,
            db_path=db_path,
            ingesting_uid="mdopp",
        ).run()
        return documents_portal_db.categories(db_path, "mdopp")

    _note(
        notes_dir,
        "users/mdopp/okf/documents/scan.md",
        category="other",
        source_document="users/mdopp/uploads/scan.md",
    )
    assert ingest() == {"other": 1}

    client = FakePaperless(
        [_doc(7, "scan.pdf", document_type=5)], document_types={5: "Versicherungen"}
    )
    _run(notes_dir, client)

    assert ingest() == {"insurance": 1}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
