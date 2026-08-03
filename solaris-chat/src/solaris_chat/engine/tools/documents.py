"""The document-extraction tool (#doc).

Asking an 8B model to hand-write correct YAML frontmatter for a `document` note
and then append a marker is fragile — it botched the note and only appended the
marker. So instead the model fills a STRUCTURED tool call (typed fields), and
CODE deterministically writes the note and marks the source companion done
(only on success). Filling typed tool arguments is something small models do
reliably; hand-authoring structured markdown is not.
"""

from __future__ import annotations

import json
from pathlib import Path

from solaris_chat import notes_search
from solaris_chat.engine.knowledge import safe_slug
from solaris_chat.engine.tools import Tool, Visibility
from solaris_chat.logging import log

_CLASSIFIED_MARKER = "<!-- classified -->"

# The document `category` vocabulary: the Dokumente doorways, and what a
# paperless document type converges onto (#1051).
CATEGORIES = [
    "insurance",
    "contract",
    "invoice",
    "utility",
    "employment",
    "pension",
    "health_insurance",
    "bank",
    "tax",
    "vehicle",
    "property",
    "warranty",
    "membership",
    "id_document",
    "legal",
    "family",
    "appliance",
    "other",
]

# The typed fields the model may fill (all optional strings). Kept explicit so the
# model has a concrete schema; dates are ISO YYYY-MM-DD by instruction.
_FIELDS: dict[str, str] = {
    "provider": "Anbieter/Firma",
    "policy_number": "Policen-/Vertragsnummer",
    "policyholder": "Versicherungsnehmer/Inhaber",
    "insurance_type": "Art (Haftpflicht/Hausrat/KFZ/…)",
    "premium_per_year": "Beitrag pro Jahr",
    "balance": "Guthaben/Saldo/Kontostand",
    "contract_sum": "Vertrags-/Bauspar-/Versicherungssumme",
    "interest_rate": "Zinssatz",
    "coverage": "Deckung/Leistung",
    "start_date": "Beginn (YYYY-MM-DD)",
    "end_date": "Ende (YYYY-MM-DD)",
    "renewal_date": "Verlängerung (YYYY-MM-DD)",
    "cancellation_deadline": "Kündigungsfrist-Datum (YYYY-MM-DD)",
    "cancellation_notice_period": "Kündigungsfrist (z.B. '3 Monate')",
    "employer": "Arbeitgeber",
    "salary": "Gehalt",
    "amount": "Betrag",
    "due_date": "Fällig (YYYY-MM-DD)",
    "expiry_date": "Ablauf (YYYY-MM-DD)",
    "hu_date": "HU/TÜV (YYYY-MM-DD)",
    "member_number": "Mitgliedsnummer",
    # Contact fields — become a linked organization/person contact (#doc-graph).
    "provider_phone": "Telefon des Anbieters",
    "provider_email": "E-Mail des Anbieters",
    "provider_address": "Anschrift des Anbieters",
    "contact_person": "Ansprechpartner/Betreuer (Name)",
}


def _fm_line(key: str, value: str) -> str:
    # Frontmatter values are single-line; strip newlines so a multi-line OCR
    # value can't break the YAML block.
    return f"{key}: {value.replace(chr(10), ' ').strip()}\n"


def build_document_tools(notes_dir: str, uid_getter) -> list[Tool]:
    root = Path(notes_dir).resolve()

    async def document_extract(args: dict) -> str:
        uid = uid_getter()
        src = str(args.get("source_document") or "").strip().lstrip("/")
        category = str(args.get("category") or "").strip()
        title = str(args.get("title") or "").strip()
        if not src or category not in CATEGORIES or not title:
            return json.dumps(
                {
                    "ok": False,
                    "error": "source_document, valid category, title required",
                }
            )

        # 1. Write the typed document note (frontmatter = the provided fields).
        # Name it after the SOURCE upload, not the title — so two uploads that get
        # the same title don't overwrite each other (#doc dedup).
        base = root if uid == notes_search.SHARED_UID else root / "users" / uid
        src_stem = src.rsplit("/", 1)[-1].removesuffix(".md") or safe_slug(title)
        doc_rel = f"okf/documents/{safe_slug(src_stem)}.md"
        doc_path = (base / doc_rel).resolve()
        if not str(doc_path).startswith(str(root) + "/"):
            return json.dumps({"ok": False, "error": "path outside vault"})
        fm = f"---\ntype: document\ntitle: {title}\ncategory: {category}\n"
        fm += _fm_line("source_document", src)
        for key in _FIELDS:
            val = str(args.get(key) or "").strip()
            if val:
                fm += _fm_line(key, val)
        fm += "---\n"
        try:
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text(fm, encoding="utf-8")
        except OSError as e:
            log.error("tools.document_extract.write_failed", error=str(e))
            return json.dumps({"ok": False, "error": "write failed"})

        # 2. Mark the source companion done — ONLY now that the note is written,
        #    so a failed extraction is retried next run instead of lost.
        companion = (root / src).resolve()
        if str(companion).startswith(str(root) + "/") and companion.is_file():
            try:
                text = companion.read_text(encoding="utf-8", errors="replace")
                if _CLASSIFIED_MARKER not in text:
                    companion.write_text(
                        text.rstrip("\n") + f"\n\n{_CLASSIFIED_MARKER}\n",
                        encoding="utf-8",
                    )
            except OSError as e:  # marking is best-effort; the note is already written.
                log.error("tools.document_extract.mark_failed", error=str(e))

        rel = str(doc_path.relative_to(root))
        log.info("tools.document_extract", document=rel, category=category)
        return json.dumps({"ok": True, "document": rel, "category": category})

    params = {
        "type": "object",
        "properties": {
            "source_document": {
                "type": "string",
                "description": "exakter vault-relativer Pfad der Companion-Notiz",
            },
            "category": {"type": "string", "enum": CATEGORIES},
            "title": {
                "type": "string",
                "description": "kurzer sprechender Titel, z.B. 'ERGO Rechtsschutz'",
            },
            **{k: {"type": "string", "description": v} for k, v in _FIELDS.items()},
        },
        "required": ["source_document", "category", "title"],
    }

    return [
        Tool(
            name="document_extract",
            description=(
                "Legt aus einem hochgeladenen Dokument eine strukturierte "
                "document-Notiz an (Versicherung/Vertrag/…). Nur Felder angeben, "
                "die wirklich im Text stehen; Datumsangaben als YYYY-MM-DD."
            ),
            parameters=params,
            handler=document_extract,
            visibility=Visibility.CONFIDENTIAL,
        )
    ]
