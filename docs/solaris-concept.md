# Solaris Concept — Start Page, Knowledge Skeleton, Lean Prompts, App Navigation

> **Status:** accepted 2026-07-06. Companion to the frozen
> [`okf-write-contract.md`](okf-write-contract.md) — that contract defines the
> **write side** of household knowledge and stays frozen; this document defines
> the **read/curation side** it deferred ("gbrain"), plus three product
> workstreams: the favorites start page, the prompt optimization pass, and the
> app-style navigation. Implementation is tracked by the *Klar & Vernetzt* epic.

## 0. Why this exists

Four asks drive this concept:

- **(a) UX** must be clear, simple, distinct — one visual language, fewer
  special cases.
- **(b) Prompts** (SOUL.md, skills, tool descriptions) carry 3–4× duplicated
  flows and blow the stated ≤3k-token household budget; one optimization pass.
- **(c) Favorites**: "packe das Bürolicht auf meine persönliche Startseite"
  said in chat should pin that card to a URL-addressable start page, so routine
  actions become one tap instead of a voice round-trip.
- **(d) Knowledge**: the write side exists (OKF pipeline, four ingest
  adapters), but the skeleton that *brings knowledge together* was never
  built — no retrieval, no continuous capture, no curation. Metaphor:
  **Stenograph** (capture everything worth keeping) + **Bibliothekar**
  (periodically rewrite it so it is quickly findable).

## 1. Conversation invariants (workstream A)

These three conventions partially exist in code today. This section makes them
**binding invariants**: every prompt rewrite (§4) and every new dialog script
must preserve them, and the regression list (§4.3) covers them.

1. **Voice ends in `?` when the conversation can continue.** The Voice PE
   re-opens the mic only on a trailing question mark. Code backstop:
   `_question_pending` / `_as_question` (`engine/facade.py`). Every tool `say`
   script that expects an answer MUST end its line in `?`.
2. **Chat offers 2–4 short continuation suggestions per turn** (quick-reply
   chips, `_suggest_answers` in `engine/client.py`). Chat-only — never on the
   voice path, where it would add latency.
3. **`#` and `@` are the grouping keys.** `#topic` and `@person` mentions typed
   in conversation (anchors, mentions store) are first-class signals for
   grouping and refinding — the knowledge layer (§3) consumes them.

## 2. Favorites / start page (workstream C)

### What a favorite is

A favorite is one of the **cards that already exist in chat**, made permanent.
The start page reuses the chat card renderers 1:1 (`renderHaCard` device
cards, concept cards, action buttons) — chat and start page share one visual
language; there is no new tile system.

| kind | payload | rendering | tap |
|---|---|---|---|
| `entity` | `{entity_id}` | live HA device card | existing `/api/ha/call` |
| `action` | `{tool, args}` snapshot | labeled action button | `POST /api/favorites/{id}/run` |
| `link` | `{href}` | title card | navigate (`/c/<id>`, `#/p/<type>`) |

### Data model

New table (alembic migration in `database/`), following the
`topics`/`mentions` `owner_uid` precedent — favorites are **operational
state**, so they live in `solaris.db`, not the vault (write-contract §1):

```
favorites( id PK, owner_uid, kind CHECK IN ('action','entity','link'),
           label, payload JSON, position, created )
favorite_usage( owner_uid, kind, payload_hash, payload JSON, count, last_used )
```

`owner_uid = 'household'` is the shared page; every resident additionally has
a personal set.

### How things get pinned

- **Explicitly, by chat/voice** — one tool `pin_favorite(target?, scope?,
  remove?)` (`engine/tools/favorites.py`). The **handler decides
  deterministically** (code-side steering beats prompt steering on the small
  model — the enrollment fix proved this):
  1. `target` resolves in the entity registry → entity card.
  2. Otherwise ("pack das auf meine Startseite" right after an action) → the
     handler takes the session's most recent side-effectful tool call from the
     in-process trace recorder, filtered to the pinnable allowlist
     (`ha_call_service`, `play_music`, `play_radio`, `media_find_podcast`) →
     action card with the exact args that just ran. The model never
     reconstructs arguments.
  3. Scope defaults from `current_uid`: an identified resident pins
     personally; the anonymous `household` voice uid pins to the shared page.
     "unsere Startseite" → `scope: "household"`.
  4. `remove: true` covers "nimm X von meiner Startseite".
- **Explicitly, by tap** — every card rendered in the chat log gets a small ☆
  pin affordance posting the same favorite payload.
- **Implicitly — "Häufig genutzt"**: side-effectful tool dispatches on the
  allowlist increment `favorite_usage` per `owner_uid`. The page shows the top
  recurring actions as ordinary cards in an automatic "Häufig genutzt"
  section — directly usable, and one tap (☆) promotes them to a permanent
  favorite. **No silent auto-pinning**: frequency changes must never reshuffle
  the curated sections.

### Safety

Confirm-gated actions (`engine/confirm.py`: locks, garage covers, …) are
**refused at pin time** — a one-tap tile would bypass the deterministic
confirmation gate. Defense in depth: `POST /api/favorites/{id}/run` re-checks
the same policy server-side and returns 403.

### The page

`/p/start`, built exactly like the proven energy portal page
(`portal_energy` → `openPortal` → `renderEnergyPage`):

- `GET /api/portal/start` resolves the caller via `resolve_uid` and returns
  `{personal, household, frequent}`; entity cards are enriched with live HA
  state on read.
- **One URL serves everyone their own page**: `https://<host>/p/start` sits
  behind the same Authelia forward-auth as everything else; the `Remote-User`
  header selects the personal section. Bookmark it, add it to the home screen —
  no per-user paths, no token scheme.
- Layout ("klar, einfach, deutlich"): three labeled sections — **Meine
  Favoriten**, **Haushalt**, **Häufig genutzt** — as a large-touch-target card
  grid. A single **Bearbeiten** toggle reveals remove (✕) and up/down per
  card; no drag library, no per-card menus. Action taps show a brief result
  toast.

### Playlists are not favorites

"Füge das meiner Playlist hinzu" is media *content*, not an action launcher:
a playlist has order, playback, shuffle — semantics Jellyfin owns. It becomes
`playlist_add(track?)` on the media tools, writing a real **Jellyfin
playlist** via the existing REST client; without an argument it takes the
currently/last-played track from the trace recorder (same trick as the pin
tool). The bridge to this concept: the playlist itself is pinnable as an
ordinary `action` favorite (`play_music` with the playlist argument).

## 3. Knowledge skeleton — Stenograph + Bibliothekar (workstream D)

The write contract froze the shape of stored knowledge. What was never built
is the loop around it:

```
capture (Stenograph)  →  project + embed (pipeline)  →  curate (Bibliothekar)  →  retrieve (one search tool)
        §3.2                     §3.1 / §3.4                    §3.3                        §3.1
```

### 3.1 Retrieval backbone

The single biggest gap: embeddings are enqueued
(`okf_embedding_queue.jsonl`) but nothing drains them; no vector store, no
semantic search, and the OKF projection is invisible to the agent (only web
concept pages and the music tools read it).

- **Embedding call**: `embed(model, inputs)` in `engine/llama_embed.py`,
  hitting `POST /v1/embeddings` with `nomic-embed-text` (768-dim, ~274 MB —
  noise next to the chat model) on its own small `llama-server --embeddings`
  instance. Historically this was Ollama's `/api/embed`; #1332 moved it, on
  the same weights and the same raw text, so the vectors carried over.
- **Vector store**: a plain table in `solaris.db` —
  `okf_vectors(embedding_id PK, concept_id, model, dim, vector BLOB float32,
  updated)` — plus **numpy brute-force cosine top-k**. At household scale
  (even 20k concepts × 768 dims ≈ 60 MB, ~10 ms per query) an ANN index or the
  sqlite-vec extension buys nothing and would drag a loadable extension into
  the schema-init sidecar. Vectors stay **derived and rebuildable** (contract
  §1): drop the table, the drain refills it.
- **Drain worker** (`engine/knowledge/embed_worker.py`): atomically rename the
  queue to `.draining`, dedup last-line-wins per `embedding_id`, batch-call
  `embed()`, upsert `okf_vectors`, delete the drained file. Runs at boot,
  after every ingest run, and periodically. No knobs.
- **One retrieval tool, not two**: semantic + structured retrieval **folds
  into `notes_search`** — tool *choice* is the small model's weakest skill, so
  a second overlapping search tool would degrade routing. `notes_search` keeps
  its name and signature (plus optional `after`/`before` for temporal
  questions) and internally merges and dedups:
  1. the existing fuzzy vault-note hits,
  2. semantic top-k over `okf_vectors` joined through `concepts` →
     `entities`/`events`,
  3. exact/alias hits via `entity_aliases`,
  4. boosted/filtered by `#topic` / `@person` anchors (§1.3),
  all filtered to `resident_uid IN (caller, 'household')` — the same
  default-deny scoping the notes tools already enforce.

  This is the moment "Wen habe ich letzte Woche gesehen?" becomes answerable:
  the `events` + `event_entities` schema has supported it since migration
  0016; it just had no reader.

### 3.2 Stenograph (capture) — hybrid

- **Immediate lane (exists, unchanged)**: explicit "merk dir das" →
  `fact_store` writes a dated, owner-scoped fact file into the vault. Vault
  files are carried into OKF/embeddings by the recurring Obsidian re-ingest —
  one write path, no parallel store.
- **Nightly lane (new)**: a **code cron** — not a prompt-only scheduler job,
  because extraction needs the day's transcripts as input, and the compactor
  already solved exactly this: `compaction.py`'s extract pass (an LLM turn
  instructed to call `fact_store`) runs today only when a session goes stale
  or hits the context cap. The Stenograph reuses that extraction machinery
  and iterates **all sessions with `engine_messages` activity since the last
  run** (one deep-profile extraction turn per active session; ephemeral guest
  sessions excluded by design). Output = the same owner-scoped fact files.
- Extraction prompts instruct tagging with `#topic` / `@person` anchors so the
  Bibliothekar can consolidate along them.
- **Voice coverage requires the voice-visibility fix** (§5, issue 2e): voice
  turns must land in the resident's household chat session; then the
  Stenograph sees them via `engine_messages` with no extra path.

### 3.3 Bibliothekar (curation) — safety contract

A nightly deep-profile job that rewrites knowledge for findability. Its
contract, binding for the implementation:

1. **Edits only vault files, never the projection.** `entities`/`facts`/
   `concepts`/vectors follow automatically on the next re-ingest + drain —
   the write contract's invariant (files = truth, db = rebuildable projection)
   stays intact.
2. **Never deletes.** Duplicate entities are merged by adding aliases to the
   canonical file and rewriting the duplicate into a stub with `merged_into:`
   frontmatter. Consolidated fact files get a `consolidated: true` frontmatter
   stamp, not removal. Every run appends to `notes/okf/log.md` (reserved by
   contract §2).
3. **Bounded input per run**: only concepts changed since the last run plus
   unconsolidated fact files older than ~3 days — never a full-vault rewrite.
4. Tasks, in order: consolidate loose fact files into per-person/per-topic OKF
   notes (using the `#`/`@` anchors) → dedup/alias entities → refresh stale
   one-line descriptions. Re-embedding happens for free: content change →
   `content_hash` change → writer enqueue → drain.

### 3.4 Nightly pipeline

One code cron `knowledge-night-run` in `engine/crons.py` (~02:30) sequences:

```
stenograph → run_ingest() → bibliothekar → run_ingest(obsidian-only) → embed drain
```

This also fixes the boot-only ingest (today new photos/events land only on
restart). Existing crons (daily-chronicle 23:59, chat-compactor 04:15,
problem-summarizer Mon 04:30) are untouched and slot around it. One
mechanism, one nightly log trail.

### 3.5 New ingestion adapters

All follow the existing `engine/ingest/` pattern (health-probe, incremental
cursor via `projection.get_cursor`, OKF writer, idempotent by
`content_hash`). Priority: chat/voice (§3.2, no adapter needed) → email →
messenger exports.

- **Email — IMAP** (`ingest/imap.py`, stdlib `imaplib`). The filter is
  **structural, not a knob**: the adapter reads only a configured
  folder/label (e.g. "Solaris") — the user curates by labeling mail; the
  adapter never sees the rest. **Per-person accounts from v1**: each
  env-configured account maps to a `resident_uid`; credentials come from the
  process env like every adapter (contract §6). Cursor = IMAP UID per folder.
  Output: OKF `event` concepts (kind `email`, sender/subject/date
  frontmatter, body text verbatim) — distillation is the Bibliothekar's job,
  not the adapter's.
- **Messenger exports — drop folder** (`ingest/exports.py`). **One adapter,
  per-format parsers**: the lifecycle (scan folder → detect format → parse to
  messages → write event concepts → move file to `processed/`) is identical
  across sources; only parsing differs, so a parser registry beats three
  adapters. Drop location = the Syncthing-synced vault:
  `notes/users/<uid>/inbox/exports/` (personal) and `notes/inbox/exports/`
  (household) — path-based ownership reuses the existing default-deny scoping
  and gives a from-any-device drop path with zero new infrastructure.
  Parsers, in delivery order:
  1. **WhatsApp** — the official "Chat exportieren" `.txt`/`.zip` (media
     omitted in v1),
  2. **Signal** — signal-cli one-shot JSON / Desktop export,
  3. **SMS/RCS** — JSON from an Android export app (e.g. the FOSS *SMS
     Import/Export*, which can schedule automatic exports).
- **RCS / SMS / Google Messages, honestly**: there is **no official API**.
  Google Messages for Web is unofficial, ToS-gray and pairing-fragile — not a
  foundation for a household system. The export-app path above is the
  pragmatic answer and keeps the messenger story uniform.
- **Matrix (optional, later — live upgrade)**: a self-hosted homeserver +
  mautrix bridges (`whatsapp`, `signal`, `gmessages`) log in as linked
  devices and mirror every conversation into Matrix rooms in real time; a
  Matrix-client ingest adapter would feed the **same** message→OKF path as the
  export parsers — the ingest side is transport-agnostic by design, so this
  is a drop-in upgrade, not a rework. Trade-offs to accept before building:
  3–4 extra containers, bridge maintenance (they break when providers change
  protocols), and the WhatsApp ban risk (whatsmeow is unofficial). Not
  scheduled.

### 3.6 Privacy & scoping

Nothing new to invent: every OKF row carries `resident_uid` (contract §1),
retrieval is default-deny (`caller ∪ household`), personal exports and
per-person IMAP accounts scope by construction (vault path / account
mapping). Messenger exports and email are personal by default; sharing stays
an explicit act (dropping into the household inbox).

## 4. Prompt optimization (workstream B)

Guiding principle, proven by the enrollment fix (`register.py`): **on the
small household model, deterministic tool-side steering is the single source
of truth per flow; the SOUL keeps trigger→tool pointers only.**

### 4.1 Changes

1. **Delete the dead duplicate** `templates/solaris/SOUL.md` — byte-identical
   to `templates/solaris/skills/household/SOUL.md`, and only the latter is
   provisioned (`post-deploy.py`). Two copies = a maintenance hazard.
2. **Single-language German SOUL.** Today the top third and headings are
   English, the operational body German; the runtime is German — code-switching
   costs budget and adds variance on a small model.
3. **Music/radio**: the dialog scripts in SOUL (~450 tokens) move into
   structured tool results with `say` fields (the enrollment pattern); the
   SOUL section shrinks to ~3 lines (library → `play_music`, radio →
   `play_radio`, confirm only the returned title). The six over-budget tool
   descriptions (`play_music`, `play_radio`, `media_find_podcast`,
   `music_query`, `offer_choices`, `start_voice_enrollment`) are trimmed back
   inside the ~100–200-token budget `engine/tools/__init__.py` declares.
4. **Enrollment**: the SOUL section shrinks to ~5 lines — the tools already
   enforce the ordering guarantee the prose restates.
5. **"Act, don't announce" once**: `_TOOL_DISCIPLINE` (`engine/client.py`,
   pinned last — position is load-bearing) stays the enforcement point; the
   SOUL keeps one echo line; the third restatement goes.
6. **Skills**: merge `guest-onboarding` + `resident-registration` +
   `self-enrollment` into one `enrollment` pack (household skills are
   panel/docs content, not runtime-injected — zero behavior risk). Hoist the
   shared service↔container model out of `admin-diagnose`/`admin-logs` into
   the admin SOUL once (the three admin skills fold ~2.2k tokens into every
   admin turn today).

### 4.2 Target

Household system prompt (SOUL + registry + tool discipline) measurably
**≤3k tokens** (the budget `engine/profiles.py` already states), asserted by
a token-count test.

### 4.3 Regression list (box-verified before/after every prompt PR)

Canonical German utterances; each PR touching SOUL/tool descriptions runs
them on the box and diffs behavior:

| # | Utterance / flow | Expected |
|---|---|---|
| 1 | "Spiele Musik von <artist>" | plays, artist set, title empty |
| 2 | "Spiele Musik" | random song, both args empty |
| 3 | "Spiele Radio" (no favorite yet) | asks for station/device, line ends `?` |
| 4 | "Stelle einen Timer auf 10 Minuten" | timer set, bare confirmation |
| 5 | "Wer bin ich?" | resident name, nothing else |
| 6 | "Wie spät ist es?" | 24h time with minutes, no date |
| 7 | Voice enrollment happy path | 5-step flow, scripted `say` lines, each question ends `?` |
| 8 | "Öffne das Garagentor" | confirmation gate fires, never direct |
| 9 | Any conversational voice reply | ends in `?` when a follow-up is expected |
| 10 | Any chat turn | 2–4 short quick-reply suggestions render |

## 5. App navigation & voice visibility (workstream A)

- **`/` keeps opening the chat** — Solaris stays talk-first; the start page is
  one tap away.
- **Mobile** (CSS media query, same markup): the current header moves to a
  **bottom tab bar** — 🏠 **Zuhause** (the pinned household chat) · 💬
  **Chats** (session list/history) · ⭐ **Favoriten** (`#/p/start`). Same
  hash-router targets; no new routing model.
- **PWA**: `manifest.json` (`display: standalone`, icons, theme color) +
  `apple-mobile-web-app-capable` meta → fullscreen via Add-to-Home-Screen on
  mobile; the app becomes installable as a chromeless window on desktop as a
  side effect.
- **Desktop**: layout unchanged; the rail gains a ⭐ Favoriten link and
  consistent German labels.
- **Voice visibility**: voice conversations are not visible in the household
  chat today, but belong there — the spoken exchange and the typed exchange
  are the same conversation. Fixing this (issue 2e; starting points
  `engine/facade.py` respond-vs-respond_session paths,
  `store.ensure_household_session`) also guarantees the Stenograph (§3.2)
  sees voice turns.

## 6. Phasing

Three mutually independent phases; within phase 3 two independent tracks.

```
Phase 1 (prompts):    1a SOUL duplicate → 1b dedup/trim ≤3k → 1c skill merges
Phase 2 (favorites):  2a store+pin tool → 2b /p/start page → 2d bottom bar+PWA
                      2c playlist_add   → 2e voice into household chat
Phase 3 (knowledge):  3a embed drain+vectors → 3b unified notes_search
                      3c nightly pipeline (needs 2e for voice coverage)
                        → 3d Bibliothekar → 3e IMAP → 3f export adapter
```

Every template/skill/chat-affecting PR is verified by deploying onto the box
(house rule); prompt PRs additionally run the §4.3 regression list.
