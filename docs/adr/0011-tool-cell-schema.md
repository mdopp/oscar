# `tool-cell-schema` — the renderer-agnostic cell contract (ADR 0011, #1022)

A `.tool`'s `tool-cell-schema` maps an item's **fields** to **semantic roles** —
never to markup. This is the promise that one `SKILL.md` drives both the PWA card
(`renderListCell`, a DOM renderer) *and* a non-browser consumer (the Android
widget, which renders with **RemoteViews**: no HTML/CSS, no JS, a fixed set of
view types, click → `PendingIntent`). "Declarative" here means
**renderer-agnostic**, not "DOM-declarative".

The server lints every shipped tool def against this contract
(`skills.cell_schema_violations`); a def that leaks browser-only assumptions
fails the check rather than silently breaking a native consumer.

## Roles (the closed vocabulary)

Each key is a role; its value is the item field(s) the renderer reads. No HTML
strings, CSS class names, DOM handlers, or `<template>` snippets — a value is a
plain field name.

| Role | Value | Renders as (DOM / RemoteViews) |
|------|-------|--------------------------------|
| `title` | one field | primary line |
| `subtitle` | one field | secondary line |
| `meta` | list of fields | muted detail line(s) |
| `badge` / `state` | one field | small status chip |
| `icon` | one field | leading glyph |
| `actions` | list of `action.id`s | tap targets → the declared `tool-actions` |

`actions` references **`action.id` only** — the ids the def already lists in its
`tool-actions` frontmatter. No inline JS or handlers live in the schema.

Anything outside this vocabulary is **custom, browser-only**: it must be flagged
as such so a native renderer can skip or degrade gracefully — it is not accepted
in a shipped tool def.

## Field types

The value behind a field is one of a small, closed set both renderers implement:
text, number, boolean/state, timestamp/relative-time, icon, button. A DOM
renderer may add presentation (e.g. the `.task` `due` field gets a `📅` prefix),
but that is the renderer's choice from the field's semantics — it is not encoded
in the schema.

## Example

The reference `.task` tool (`templates/solaris/skills/household/task-tool/`):

```json
{ "title": "title", "meta": ["due"] }
```

- `title` → the task's `title` field is the primary line.
- `meta` → the task's `due` field is the detail line.

A richer example using more roles:

```json
{
  "title": "name",
  "subtitle": "role",
  "meta": ["phone", "email"],
  "badge": "state",
  "actions": ["contact.add", "person.update"]
}
```

Every value is a field name or a declared `action.id`; nothing is markup — so the
same schema renders as a DOM card and as a RemoteViews widget.
