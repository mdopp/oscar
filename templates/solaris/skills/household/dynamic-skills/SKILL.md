---
name: solaris-dynamic-skills
description: Use when a resident asks Solaris to learn a new capability or write down a fact/note — the self-enhancement loop. Writes facts to the vault and drafts new skills into a pending directory for admin approval.
kind: skill
scope: household
version: 3.1.0
author: Solaris
license: MIT
---

# Solaris — Dynamic Skills & Knowledge Self-Enhancement

Two jobs: write durable facts into the notes vault, and draft a brand-new skill
into the **pending** directory for an admin to approve. Solaris never
auto-activates a skill it wrote and never runs generated scripts.

## When to use

- "Merk dir, dass der Gartenschlüssel unter dem blauen Topf liegt."
- "Remember that …" / "Schreib dir auf, dass …"
- "Kannst du lernen, X zu tun?" / "Learn how to …" (drafts a new skill).

Out of scope: searching notes (`solaris-notes-search`), the dated journal
(`solaris-daily-chronicle`), media ingestion (`media-ingestion-multimodal`).

## 1. Writing facts

When a resident shares a household fact/preference, write it down so it is indexed:

1. Compose a clean Markdown block (tags + date). If the turn carries
   `[Active topic: <name> #topic/<slug>]`, add that `#topic/<slug>` to the block's
   tags (slug may be hierarchical); omit otherwise.
2. Read the existing file with `notes_read` if present.
3. **Wiki-link only clear named entities** the fact is *about* — people
   (`[[Oma Erna]]`), places (`[[Garten]]`), and named topics that already have a
   note. Do not link common nouns/verbs/dates. Check the vault first
   (`grep -ril "<Entity>" /opt/data/notes/`); render links inline in the body, not
   in frontmatter. When in doubt, don't link.
4. Write with `note_write` (or `append=true` for edits): general facts →
   `/opt/data/notes/SOUL.md`; topic facts → `/opt/data/notes/fact_<topic>.md`.
   Write only under `/opt/data/notes/`.
5. For a newly linked **person/place** with no note yet, create a minimal stub
   (`people/<Name>.md` / `places/<Name>.md`, idempotent, name+type only):
   ```markdown
   ---
   type: <person|place>
   tags:
     - solaris/stub
     - type/<person|place>
   created_at: {{timestamp}}
   ---

   # {{Entity}}

   > Automatisch angelegter Knoten. Wird ergänzt, sobald mehr bekannt ist.
   ```
   Don't stub abstract topics — a `fact_<topic>.md` *is* the topic's note.
6. Confirm: *"Ich habe mir das notiert in <filename>."*

## 2. Drafting a new skill (admin-promotion gate)

When a resident asks for a new capability, draft the skill **into the pending
directory only**. Hard rules — do not skip:

- **Use `draft_skill` — never `note_write`.** `draft_skill` is the only tool that
  reaches the pending directory; the notes tools write into the vault, where an
  admin never looks for a skill.
- **Never write to the active pack** `/data/skills/<slug>/…` — that would make the
  skill live with no human review (a prompt-injection risk). `draft_skill` can't.
- **Do not execute anything.** The draft is the SKILL.md text and nothing else —
  no scripts are written and none are run.
- **Do not promote the draft yourself** — filing the approval and the promotion is
  the admin profile's job (`file_skill_approval` / `check_skill_approval`). This
  session's job ends at "drafted the SKILL.md".
- **Use a safe `<slug>`** — lowercase letters/digits/dashes; no `/`, `..`, leading
  dots, or whitespace.

Sequence: call `draft_skill` with the `slug` and the complete SKILL.md text as
`content` (`name: solaris-custom-<slug>`, a clear router `description`,
`version: 1.0.0`). On `{"ok": true}` tell the resident: *"Ich habe einen Entwurf
für die Skill `<slug>` unter den ausstehenden Skills abgelegt. Sag einem Admin
Bescheid — sobald er sie freigibt, lerne ich sie."* On `{"ok": false}` say the
draft could not be filed and stop.

On admin approval the **engine** moves `_pending/<slug>/` → `<slug>/` (the active
pack is read live, so the move *is* the reload — no restart); on denial/expiry the
engine deletes the draft.

## Guards

- **One tool per destination**: pending skills → `draft_skill`; notes →
  `note_write` under `/opt/data/notes/`. Neither can reach the other's place.
- **No silent self-activation**: `draft_skill` writes only under `_pending/`; the
  active pack is the admin's to fill, never this session's.
- **No generated scripts**: a draft is prose the admin reads, not code to run.
- **Error recovery**: surface a failed draft to the resident and stop; never fall
  back to `note_write`.
