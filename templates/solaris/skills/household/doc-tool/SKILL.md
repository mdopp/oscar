---
name: solaris-doc-tool
description: The .doc dot-command — upload a document/Takeout, sort it into a category, and search existing documents.
kind: tool
scope: household
tool-id: doc
tool-label: Dokument
command: .doc
tool-api-path: /api/portal/documents/search
tool-compose-path: #/p/doc/new
tool-actions: doc.classify
tool-cell-schema: {"title": "title", "meta": ["category"]}
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Dokumente (`.doc`)

**Usage:** `.doc` öffnet eine Ablage: ein Bild/PDF wird gescannt und einsortiert
(Kategorie + Schlagwörter), ein Takeout-`.zip`/`.json` startet den Import. Text
im Befehl durchsucht die bestehenden Dokumente live.

A `.tool` expressed declaratively (ADR 0011, #1006): the server auto-registers
`doc.classify` from this def and serves the surface at `/api/defs/tool`. The card
stays on its inline builder (`buildDocCard`) — the upload dropzone + classify
flow isn't a plain create/list card the generic renderer covers.
