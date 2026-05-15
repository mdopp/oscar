# OSCAR schema

Alembic migrations for the three OSCAR-owned tables, kept in a single SQLite file (`oscar.db`) in the `oscar-household` template's volume.

| Table | Purpose | Phase |
|---|---|---|
| `system_settings` | Single-row global flags (`debug_mode.active`, `debug_mode.verbose_until`, `debug_mode.latency_annotations`). Read by every component on every audit event. | 0 |
| `cloud_audit` | Append-only — one row per Hermes cloud-LLM call: timestamp, uid, trace_id, vendor, model, prompt/response hash + length, cost, router score, escalation reason. Full text gated by `system_settings.debug_mode`. | 0 |
| `voice_embeddings` | 256-d ECAPA-TDNN voice embedding per LLDAP `uid`, plus enrolment metadata. 3–10 rows; brute-force cosine in Python — no vector index. | 2 (table created up-front, populated in Phase 2) |

## Storage choice

SQLite. Rationale: OSCAR's tables are a few-rows-of-config job (tens of audit rows per day; one settings row; ten embedding rows). Hermes itself uses SQLite for Honcho + FTS5. Phase 3a re-opens the storage choice when domain collections (`books`, `records`, `documents`, `audiobooks`, `experiences`) land. See [`../oscar-architecture.md`](../oscar-architecture.md) → "The schema".

Migrations are written with hand-rolled SQL via `op.execute(...)`, so a future move to Postgres (if Phase 3a calls for it) is a one-day swap of the DDL strings.

## Running the migration

The `oscar-household` ServiceBay template runs the migration container on every pod start, against `/var/lib/oscar/oscar.db` in the bind-mounted volume. Idempotent.

Manual run (development):

```bash
cd schema
alembic -x dburl=sqlite:////tmp/oscar.db upgrade head
```

## Image

Built from `Dockerfile`; published as `ghcr.io/mdopp/oscar-household-init:latest` by `.github/workflows/build-images.yml`.
