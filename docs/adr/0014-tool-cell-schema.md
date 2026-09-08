# `tool-cell-schema` — the renderer-agnostic cell contract (ADR 0014, #1022)

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
`tool-actions` frontmatter, as bare strings; an `{id, title}` object never
appears *here*. No inline JS or handlers live in the schema.

### Titled actions — the contract (#1382, part B of #1381)

An action carries a label so a chooser can name it. The label lives **once**,
at the tool, joined to the row by its id:

- **Authoring.** `tool-actions` takes either the comma list it always took, or a
  one-line JSON array whose entries are ids and/or objects, mixable:
  `tool-actions: [{"id": "model.lease.1h", "title": "1 Stunde"}, {"id": "model.release", "title": "Freigeben"}]`.
  A bare string means "untitled". Titles are finished German text — the same
  server-says/client-shows rule as `status_text`.
- **Emission.** `/api/defs/tool` and `/napi/defs/tool` serve two views of that
  one list, same order: `tool-actions` unchanged (bare ids), plus
  `tool-actions-titled`: `[{"id": …, "title": … | null}]` — **always objects**,
  `title` null when untitled, the key always present (empty list when the tool
  declares no action), so a consumer parses one shape and needs no per-entry
  type test.
- **Why not titles inside `tool-actions`.** A consumer that reads that list as
  plain strings drops a JSON object entry *silently* (`ToolDefs.stringList` in
  mdopp/solaris-android keeps only `String`s, and `parseDef` then filters the
  schema's actions against it). Objects there would leave every already-installed
  app with `actions = []` — no button in *any* tool, no error. Additive key
  instead: an old consumer ignores what it doesn't know.
- **Resolution.** A consumer that supports the chooser walks the schema's
  `actions` in declaration order and offers **every** id whose
  `tool-action-params` mapping the row can fill — not only the first. The entry's
  label is that id's `title`, or the consumer's fixed wording when null.

### One action id per tool — in force until the app ships the chooser

`actions` is a list at the **tool**, not at the row, and today's consumer
resolves **exactly one** entry per row: the first id whose `tool-action-params`
mapping that row can fill wins (`ToolCells.resolveAction` in
mdopp/solaris-android). So **a second id fillable from the same row fields is
unreachable** — every row runs the first one, the button reacts, something
happens, and it is the same something everywhere. That is a bug that passes a
click-test.

This rule therefore **stays in force**: a shipped `tool-cell-schema` declares
**one** action id. A second choice belongs in the **rows**: give each row its
own fields (`{"profile": "$profile", "hours": "$hours"}`) so one id does
different things per row — see the `.model` tool for the worked example. Ids
that only chat or the PWA calls stay out of the schema; they may live in
`tool-actions`.

**Lifted when solaris-android ≥ `<version>`** — the version that renders the
titled chooser (contract handed over at solarisbay#1374, awaiting that number).
From then on a schema may declare several ids, and the author owes one ordering
duty: an app older than that version still runs the **first** fillable id, so
the safest, least surprising action goes first (for `.model`: the shortest
lease, never "until tomorrow").

## `tool-action-params` — where an action's params come from

An action id alone is not enough to call `POST /api/action-callback`, which wants
`{"action_id": …, "params": {…}}` with action-specific params. `tool-action-params`
declares that mapping per action id, so a renderer builds the callback body from
the row it already drew:

```json
{ "task.set_status": { "entity_id": "$id", "status": "done" } }
```

Each value is a **source**: `$field` reads the item field of that name off the
rendered row, anything else is a **literal**. The `$` marker is what keeps the two
apart — `{"status": "done"}` would otherwise be ambiguous. Values are flat
strings only, so the whole map stays one line in the pack's no-PyYAML frontmatter
parser.

Every id in a schema's `actions` role **must** be declared here; the lint rejects
a def that offers a button no consumer can wire.

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
{ "title": "title", "meta": ["due"], "actions": ["task.set_status"] }
```

- `title` → the task's `title` field is the primary line.
- `meta` → the task's `due` field is the detail line.
- `actions` → the row's tap target, its params declared in `tool-action-params`.

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
