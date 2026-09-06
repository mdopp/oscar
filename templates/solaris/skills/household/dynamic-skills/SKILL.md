---
name: solaris-dynamic-skills
description: Durable fact memory for residents (fact_store / note_write); a new skill is only ever drafted into the pending directory and promoted by an admin. Delivered as tools plus the SOUL pointer, not as prompt text.
kind: skill
scope: household
version: 4.0.0
author: Solaris
license: MIT
---

# Solaris — Gedächtnis und Skill-Entwürfe

Der Bewohnerteil erreicht die Bewohner als **Werkzeug**: `fact_store` /
`note_write` schreiben einen dauerhaften Fakt in den Vault; die SOUL trägt dazu
den Zeiger „proaktiv merken".

Der Skill-Teil bleibt bewusst ohne Bewohner-Zeiger: `draft_skill` legt nur
einen Entwurf unter `_pending/` ab, und Freigabe wie Promotion gehören dem
Admin-Profil (`file_skill_approval` / `check_skill_approval`). Ein Bewohnerzug
aktiviert nie eine Skill und führt nie erzeugten Code aus.

Kein Fließtext für den Haushalts-Prompt (#1291).
