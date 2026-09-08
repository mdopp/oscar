# ADR 0011 — `.tool` plugins (like `/`-commands) and a card single-source-of-truth

**Status:** Accepted

## Context

Two surfaces grew very differently:

- **`/`-commands are already a plugin system.** A maintainer drops
  `templates/solaris/skills/<id>/SKILL.md` with `kind: command` +
  `command: /foo`; the server file-scans it (`skills.py` `KINDS =
  (skill, command, hook, scheduler)`, `list_defs`) and exposes it at
  `/api/defs/command`; the client fetches it at init (`loadCommandPool`) and it
  appears in the menu. **One file, no code change, no rebuild.**
- **`.tools` are 100% hardcoded.** Adding one touches ~7 regions —
  `DOT_COMMANDS`, `DC_HEAD`, the `ensureCard` dispatch, a `build*Card`, an
  `updateCard` branch (all inline in the single-file `static/index.html`), plus a
  server list endpoint and an `action_cards.register(...)` — and needs a rebuild
  + restart. There is no folder, registry, or manifest.

Separately, on **card reuse**: the device card renderer `renderHaCard` /
`renderHaWidget` is *already* a single source of truth — every surface (start
page, `.home`, chat, device detail, picker, action re-render) calls the same
function; `renderActionCard` likewise. But each `.tool`'s **list rows**
(task-row, contact-row, note-row, …) are rebuilt inline in its own `render*List`,
duplicated and not shared with the concept pages.

Client constraint: the UI is one inline-`<script>` `index.html`, vanilla JS, **no
bundler, no module system, no `eval`** (validated by `node --check`). A plugin
therefore cannot cleanly inject its own JS.

## Decision

**1. `.tools` become plugins via the existing defs system — a new `kind: tool`.**
A `.tool` is a `templates/solaris/skills/<id>/SKILL.md` with frontmatter:
`tool-id`, `tool-label`, `tool-api-path` (+ optional `tool-search-path` and
`tool-compose-path`), `tool-actions: [action.id, …]`, and a
**`tool-cell-schema`** (see below). An entry of `tool-actions` may also be an
object `{id, title}` (#1382) carrying the label a chooser shows for that action;
a bare id means "untitled". The catalog serves both views of the same list, in
declaration order: `tool-actions` stays the bare-id list every consumer already
reads, and `tool-actions-titled` carries `[{id, title}]` with `title` null when
untitled — always objects, so a consumer parses one shape. The titles do not
move into `tool-actions` itself because a consumer that reads that list as plain
strings drops an object entry silently and would end up with no action at all. `tool-compose-path` (#1213) is the tool's own
statement that it HAS a create path, and the deep link that opens it —
`#/p/<tool-id>/new`; a consumer that can't render a card, such as an Android 1×1
tile, offers a "create" tile only for tools that declare one. `tool-item-id-field`
(#1256) is the same statement for ONE entry: it names the item field that carries
the id, so `#/p/<tool-id>/item/<item-id>` addresses a single row and a consumer
never has to probe `entity_id`/`id`/`uid` for one; a tool that declares no id
field has no item route. Its sibling
`#/?tool=<tool-id>` opens the chat with that tool's card already open. All three
resolve against the catalog, so a new `.tool` needs no app update; an
unresolvable tool-id — or an item id that no longer exists — lands on
`#/p/start`. The
server auto-lists it at `/api/defs/tool` (mirroring `/api/defs/command`) and
**auto-registers its declared actions** on load; the client fetches it at init
and dispatches via a **tool registry** instead of the hardcoded
`DOT_COMMANDS`/`ensureCard` if/else. Adding a `.tool` drops from ~7 regions +
rebuild to **one `SKILL.md`** (plus its backend endpoint if it needs a new one).

**2. Cards are a single source of truth; a plugin brings a declarative schema,
not JS.** The shared renderers stay THE components — `renderHaCard`/
`renderHaWidget` (devices), `renderActionCard` (actions), and a **new generic
`renderListCell(item, schema)`** extracted from the duplicated `.tool` list rows
and reused by `.tool` cards *and* concept pages. A plugin describes its card/cell
**declaratively** (`tool-cell-schema`: which field is the title, which are meta,
which buttons map to which `action.id`); the shared components render it. So card
changes live in **one place**, and a plugin composes from the same cards rather
than shipping bespoke card code. (Chosen over server-rendered fragments and a
build-time-JS step: schema-driven keeps the SSOT and the single-file/no-build
simplicity. A server-rendered fragment remains a possible future escape hatch for
a genuinely custom card, explicitly out of scope here.)

**3. No plugin JS.** Given the single-file/no-bundler client, a plugin never
injects JavaScript. Everything it customizes is declarative metadata (frontmatter
+ cell-schema) + server-side action handlers/endpoints. This bounds what a plugin
can do (generic, schema-driven UI) in exchange for safety and one code path.

Delivered in verifiable slices (card-SSOT refactor first — a pure internal
no-behaviour-change extraction — then the `kind: tool` defs + registry, then
migrating the existing six `.tools`), each box-verified. Tracked as issues; this
ADR pins the target.

## Consequences

- Adding a `.tool` mirrors adding a `/`-command: one declarative file, no client
  code, no rebuild for the wiring (a brand-new backend still needs its endpoint).
- One card SSOT: `renderHaCard`/`renderActionCard`/`renderListCell` are the only
  card builders; a change to a card is a change in one place, everywhere — the
  maintainer's stated goal.
- Plugins are declarative + safe (no arbitrary JS); the trade-off is that plugin
  cards are schema-driven, not pixel-bespoke. A future server-fragment path can
  lift that ceiling if needed.
- The existing `.task/.note/.doc/.contacts/.photo/.home/.energy` migrate onto the
  registry incrementally; until migrated they keep working via the current inline
  path (no big-bang).
- Reuses the existing `/api/defs/{kind}` + `SKILL.md` machinery (ADR 0009's `.tool`
  surface, ADR 0007's no-new-surface) — no new storage, no new transport.
