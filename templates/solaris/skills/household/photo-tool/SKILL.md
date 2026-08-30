---
name: solaris-photo-tool
description: The .photo dot-command — upload a photo to Immich and search existing photos by person/caption/text.
kind: tool
scope: household
tool-id: photo
tool-label: Foto
command: .photo
tool-api-path: /api/photo
tool-compose-path: #/p/photo/new
tool-item-id-field: id
tool-cell-schema: {"id": "id", "title": "name", "meta": ["people"]}
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Fotos (`.photo`)

**Usage:** `.photo` öffnet eine Ablage — ein Bild wird zu Immich hochgeladen.
Text im Befehl durchsucht die bestehenden Fotos live (Person / Beschreibung /
Text).

A `.tool` expressed declaratively (ADR 0011, #1006): served at `/api/defs/tool`
so it joins the tool registry. The card stays on its inline builder
(`buildPhotoCard`) — the Immich upload dropzone isn't a plain create/list card.
The upload posts to `/api/photo` directly, so this tool declares no card action.
