# ADR 0009 — Two composer surfaces: `/control` and `.tool`, and the `.tool` create-and-find pattern

**Status:** Accepted

## Context

The chat composer is the one place a resident interacts with Solaris. Besides a
plain message (which goes to the model), we grew two prefix surfaces and needed a
crisp, teachable rule for *what belongs where* — and, more importantly, a single
reusable **pattern for handling data** so every new "thing" (task, note, document,
contact, device, photo …) behaves the same and doesn't sprawl into bespoke UIs
(ADR 0007: no new surface).

## Decision

Two prefixes, named for what they touch:

- **`/` — Control (Chat & Solaris).** Browser-local commands *about Solaris and
  the chat itself*: `/new`, `/clear`, `/search`, `/context`, `/skills`, `/soul`,
  `/tools`, `/persona`, `/thinking`, `/commands`, plus admin `/model` `/voice`.
  They never round-trip the model (except user prompt-templates). Menu heading:
  **"Control — Chat & Solaris"**.
- **`.` — Tool (Erfassen & Finden).** *Capture and find your data.* Each `.tool`
  opens a **live card in the chat transcript** that fills as you type. Today:
  `.task`, `.note`, `.doc`, `.contacts` (and `.home`, `.photo` planned). Menu
  heading: **"Tools — Erfassen & Finden"**. Composer placeholder:
  `Nachricht · /control · .tool`.

Skill-kind defs are **not** exposed as `/` aliases — a skill has no editable
body, so a `/`-alias could only send its own trigger string as a turn, which is
text and not a skill invocation. They fire on a natural question instead. Since
#1309 no shipped skill declares a `command:` either: a declaration the `/` pool
cannot route is an advertised command that answers "Unknown command".

### The `.tool` create-and-find pattern (the load-bearing part)

A `.tool`'s text argument does **double duty**, both shown in the one live card:

1. **Create preview** — the argument fills a "new <thing>" card as you type.
2. **Live filter** — the *same* argument narrows the existing items of that kind
   (case-insensitive substring), shown beneath the create card.

No argument (or the keyword `list`) → empty create + the full list. **Enter** or
the card's primary button **creates** the item and freezes the card into a
confirmation that stays in the transcript. Recognizers enrich the argument in
place: a `TT.MM.JJJJ` date lifts into a due field (`.task`); `#`/`@` are linked;
`.contacts` parses `@`→email, digits→phone, else name.

The write is deterministic (a POST to `/api/action-callback`, **not** the model),
so capture never depends on an LLM. Each `.tool` has a matching **list endpoint**
for the filter (`/api/portal/tasks`, `/api/portal/notes/search`,
`/api/portal/persons`, `/api/portal/documents`). Entities are projection-only
where that fits (tasks, contacts — ADR 0002) or full documents.

### Adding a new `.tool` (recipe)

1. Add `[".x", "…"]` to `DOT_COMMANDS` and a `DC_HEAD.x` label (`static/index.html`).
2. `buildXCard(el)` — the create form; `updateCard` branch — live preview + filter
   render; `submit` branch — POST the create action, then `freeze()` the card.
3. Backend: register an `x.add`/`x.classify` action (create) and a list endpoint
   (filter). Keep the create off the model.

## Consequences

- One mental model — "`/` controls Solaris, `.` handles your stuff" — surfaced in
  the menu headings, the placeholder, and `/help`'s intro.
- "Type to create, type to find" is uniform across data types and mobile-friendly
  (no tab-hunting; the keyboard's `.`/`/` are one tap).
- Data capture is deterministic and offline-safe; the model is for conversation,
  not form-filling.
- New data types cost a card builder + one action + one list endpoint — no new
  page (ADR 0007). The `/` surface only ever lists commands that actually work in
  the current role.
