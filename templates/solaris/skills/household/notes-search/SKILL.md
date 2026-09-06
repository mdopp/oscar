---
name: solaris-notes-search
description: Read-only keyword + frontmatter retrieval over the household Obsidian vault. Delivered to residents as the notes_search / notes_read tools plus the SOUL pointer, not as prompt text.
kind: skill
scope: household
version: 3.0.0
author: Solaris
license: MIT
---

# Solaris — Notizsuche

Die Fähigkeit erreicht die Bewohner als **Werkzeug**: `notes_search` sucht den
Vault (Stichwort, Frontmatter, `#topic/<slug>`, `after`/`before`) und
`notes_read` liest den Treffer. Nur lesend; die Rangfolge, die Themenanker und
die Vault-Grenze stecken im Werkzeug.

Kein Fließtext für den Haushalts-Prompt (#1291): die SOUL trägt nur den Zeiger
„erst suchen, dann antworten".
