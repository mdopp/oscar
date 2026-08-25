---
name: solaris-note-tool
description: The .note dot-command — save a quick note and search existing notes; tap a hit to open it.
kind: tool
scope: household
tool-id: note
tool-label: Notiz
command: .note
tool-compose-path: #/p/note/new
tool-actions: note.add
tool-cell-schema: {"title": "label"}
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Notizen (`.note`)

**Usage:** `.note <Text>` legt eine Notiz an *und* durchsucht die bestehenden
Notizen live (ab 3 Zeichen). Ein Tipp auf einen Treffer öffnet die Notiz im
Betrachter — das Anlegen·Finden·Öffnen-Muster.

A `.tool` expressed declaratively (ADR 0011, #1006): the server auto-registers
`note.add` from this def and serves the surface at `/api/defs/tool`. The card
stays on its inline builder (`buildNoteCard`) — the fuzzy note search
de-duplicates an upload's raw companion against its extracted OKF note and opens
the OKF note, behaviour the generic list card doesn't express.
