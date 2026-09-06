# Knowledge system

Solaris remembers. Conversations, notes, photos, calendar, contacts, music and
email flow into one household knowledge graph you can ask questions about —
"Wen habe ich letzte Woche gesehen?", "Empfiehl mir ein Buch." — and re-find
by `#topic` and `@person`.

The metaphor is **Stenograph** (capture everything worth keeping) +
**Bibliothekar** (periodically rewrite it so it is quickly findable). This doc
covers the loop and how you use it; for the frozen write-side shape see
[`okf-write-contract.md`](../okf-write-contract.md); for the design rationale
see [`solaris-concept.md`](../solaris-concept.md) §3; for where it sits in the
4-layer architecture see [`solaris-architecture.md`](../../solaris-architecture.md) §3.

Ingestion of external sources (Obsidian, Immich, calendar, contacts, music,
email, messenger exports) has its own doc: [ingest.md](ingest.md).

## What it does

```
capture (Stenograph) → project + embed → curate (Bibliothekar) → retrieve (one search tool)
```

- **Captures** durable facts out of your conversations every night, and pulls
  in your other on-box sources (photos, calendar, contacts, music, mail).
- **Stores** them as **OKF concept files** in the vault (people, events,
  places, books, songs, …) — files are the source of truth. A rebuildable
  **projection** in `solaris.db` (`entities`, `entity_aliases`, `facts`,
  `events`, `event_entities`, `concepts`) makes them queryable, and an
  `okf_vectors` table makes them semantically searchable.
- **Curates** them nightly: consolidates loose fact files into per-person /
  per-topic notes, dedups and aliases entities, refreshes stale one-line
  descriptions. Never deletes.
- **Retrieves** through **one** tool — `notes_search` — that blends fuzzy vault
  hits, entity/alias hits, date-range event hits, and semantic top-k.

## How to use it

- **Remember something now:** „Merk dir, dass …" → a dated, owner-scoped fact
  file (`fact_store`).
- **Ask across everything:** just ask. The engine calls `notes_search`
  internally; temporal questions ("letzte Woche") use the `events` range.
- **Group & re-find:** type `#topic` / `@person` in chat. Those anchors boost
  and filter retrieval and drive the nightly consolidation.
- **Browse & curate the vault** in the [Notizen tab](notes-tab.md) (`/p/notes`)
  — the human-in-the-loop surface for what the Bibliothekar does automatically.
- **Concept pages** `/c/<id>` render an OKF concept (its description + body,
  Relationships shown as links).

## How it works (brief)

- **Stenograph** (`engine/crons.py::_stenograph`) reuses the compactor's
  extraction pass: one deep-model turn per session with activity since the last
  watermark, instructed to call `fact_store` (tagging `#topic`/`@person`).
  Ephemeral guest sessions are excluded. Voice turns land in the resident's
  household chat, so the Stenograph sees them with no extra path.
- **Bibliothekar** (`engine/crons.py::_bibliothekar`) is a nightly deep-model
  curation job with a binding safety contract: it edits **only vault files**
  (the projection follows on re-ingest), **never deletes** (merge = alias +
  `merged_into:` stub; consolidate = `consolidated: true` stamp), works on a
  **bounded input** (concepts changed since last run + fact files older than
  ~3 days), and appends every run to `notes/okf/log.md`.
- **Embeddings**: writes enqueue `okf_embedding_queue.jsonl`; the drain worker
  (`engine/knowledge/embed_worker.py`) batches `nomic-embed-text` calls and
  upserts `okf_vectors` (float32 BLOB). Search is **numpy brute-force cosine**
  top-k — at household scale an ANN index buys nothing. Vectors are derived and
  rebuildable: drop the table, the drain refills it.
- **`notes_search`** (`engine/tools/notes.py`) merges: fuzzy vault-note hits,
  `entity_aliases` exact hits, `events` range hits (`after`/`before`), and —
  only when the direct hits are thin — semantic top-k over `okf_vectors`. All
  filtered to `caller ∪ household` (default-deny).

### The nightly pipeline

One code cron, `knowledge-night-run` (~02:30, `engine/crons.py`), sequences:

```
stenograph → run_ingest() → embed drain → bibliothekar → re-ingest (obsidian) → embed drain
```

It also fixes the old boot-only ingest (new photos/events used to land only on
restart). The other crons slot around it: `daily-chronicle` 23:59,
`chat-compactor` 04:15, `problem-summarizer` Mon 04:30.

## Config / env

| env | purpose |
|---|---|
| `NOTES_DIR` | Obsidian vault (Syncthing-synced); OKF subtree at `notes/okf/` |
| `SOLARIS_DB_PATH` | the `solaris.db` projection + vectors + cron stamps |
| `OLLAMA_URL` | embedding backend (`nomic-embed-text` managed) — chat generation moved to llama-server in #1318; embeddings move too once #1332 lands |

The `solaris.db` knowledge tables are created by Alembic migrations in
`database/` — `0016_okf_knowledge_index` (entities/facts/events/concepts) and
`0018_okf_vectors`. Per-source ingest env is documented in [ingest.md](ingest.md).
