# Architecture Decision Records

An **ADR** captures one architectural decision: its context, the decision itself,
and the consequences we accept by taking it. They are append-only history — when a
decision changes, we add a new ADR that supersedes the old one rather than editing
it away. They exist so the *why* survives longer than the person who decided it.

These ADRs pin the rules for **where knowledge lives, why, and how we avoid holding
the same thing twice** — the substrate under the Google-Takeout import (epic #860),
the music/OKF substrate rework (#873), and everything that feeds the household
knowledge graph.

| ADR | Decision | One line |
|---|---|---|
| [0001](0001-import-transport-vs-semantics.md) | Import = transport (ServiceBay) vs semantics (Solaris) | ServiceBay moves bytes onto the box; Solaris turns bytes into knowledge. No second transport. |
| [0002](0002-provenance-decides-substrate.md) | Provenance decides the substrate | Externally re-ingestable → SQLite only; self-originated → markdown note is truth. |
| [0003](0003-one-entity-source-tagged-facts.md) | One entity, source-tagged facts | One album/artist = one node; every source contributes only `source`-tagged facts. |
| [0004](0004-derived-lists-are-queries.md) | Derived lists are queries | Wishlist / shopping / "just rip" is a live query over facts, never a stored list. |
| [0005](0005-lean-rag-no-cover-art.md) | Lean RAG, no cover art | Embeddings at album/artist granularity; no album cover images, only your own sleeve photo. |
| [0006](0006-import-as-capability-plugins.md) | Import is a capability, sources are plugins | `Importer` protocol + registry; Takeout is the first source; the LLM classifies. |
| [0007](0007-frontend-no-new-surface.md) | Frontend: no new surface | No new tab; reuse action-cards + the one Posteingang inbox. |
| [0008](0008-documents-live-in-paperless.md) | Documents live in paperless-ngx | External DMS owns documents; Solaris ingests projection-only. |
| [0009](0009-command-surfaces-control-and-tool.md) | `/control` vs `.tool` + create-and-find | `/` controls Solaris; `.` captures/finds data — the arg both fills a card AND filters. |
| [0010](0010-one-person-entity-all-surfaces.md) | One person entity; every people-surface reads it | One `person` node per human (source-tagged facts + aliases); `.contacts`, Personen doorway and `@`-mentions all read it; orgs stay separate. |
| [0011](0011-tool-plugins-and-card-ssot.md) | `.tool` plugins (like `/`-commands) + card SSOT | `.tools` become `kind: tool` SKILL.md defs (auto-listed, registry-dispatched); cards are one SSOT (`renderHaCard`/`renderActionCard`/`renderListCell`); a plugin brings a declarative schema, not JS. |
| [0012](0012-paperless-stores-solaris-extracts.md) | paperless stores, Solaris extracts | The `gemma4:12b` vision pass stays permanently as the document text source; paperless's own OCR stays disabled. Supersedes ADR 0008's "retire the extractor". |
| [0013](0013-thinking-command-retired.md) | `/thinking` retires with the `think` switch | Zone 1 has one model and no per-turn thinking, so the command loses its capability. Supersedes that entry in ADR 0009's command list. |

## Two numbering spaces

These `ADR 000x` records are **not** the same series as the `ZA-01…ZA-18` decisions in
[`../solaris-zielarchitektur.md`](../solaris-zielarchitektur.md). ADRs say how the
substrate and the surface are built; ZA says where a workload runs and who may see it.
`ADR 0003` and `ZA-03` are unrelated documents — always write the space out.
Section 11.1 of the Zielarchitektur lists where the two touch.

See [`../data-flow.md`](../data-flow.md) for the human-facing overview: the
consolidated requirements, the capability map (which data source flows to which
store, over which capability, and what already exists), and the phased build plan.
