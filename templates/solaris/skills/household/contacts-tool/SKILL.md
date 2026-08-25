---
name: solaris-contacts-tool
description: The .contacts dot-command — add a personal contact and search existing ones; tap a row to correct it.
kind: tool
scope: household
tool-id: contacts
tool-label: Kontakt
command: .contacts
tool-api-path: /api/portal/persons
tool-compose-path: #/p/contacts/new
tool-actions: contact.add, person.update
tool-cell-schema: {"title": "name", "meta": ["phone", "email"]}
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Kontakte (`.contacts`)

**Usage:** `.contacts <Name|@Mail|Tel>` legt eine Person an *und* filtert die
bestehenden Kontakte live. `@` ⇒ E-Mail, nur Zahlen ⇒ Telefon, sonst Name. Ein
Tipp auf einen Kontakt öffnet die Bearbeitung — Anlegen·Finden·Bearbeiten (#967).

A `.tool` expressed declaratively (ADR 0011, #1006): the server auto-registers
`contact.add` + `person.update` from this def and serves the surface at
`/api/defs/tool`. The card stays on its inline builder (`buildContactsCard`) —
the split name/email/phone edit form isn't a plain generic list row.
