# oscar-db

OSCAR's Postgres schema as versioned alembic migrations + the small migrate-on-start sidecar image.

## Why migrations and not inline `CREATE TABLE` in the pod-yaml

`templates/oscar-brain/template.yml` used to drop a giant `CREATE TABLE IF NOT EXISTS …` block into `/docker-entrypoint-initdb.d/` on first boot. That works for a fresh install and does nothing on re-boot — but it's destructive when the schema changes: we'd have to drop the volume and re-init, or run ad-hoc SQL by hand. Phase-2 alone adds `gatekeeper_voice_embeddings`; Phase-3a adds `ingestion_classifications`; future bug-fixes will add indexes and columns. Versioned migrations are the standard answer.

## What ships

- `shared/oscar_db/migrations/` — alembic-managed `versions/` directory + `env.py` + `alembic.ini`. One **baseline** revision creates today's household-domain schema (`system_settings`, `cloud_audit`). Future revisions add Phase-3 domain tables (`books`, `records`, `documents`, …).
- `oscar_db.cli` — thin wrapper around alembic that reads `POSTGRES_DSN` from env, waits for Postgres to be reachable, then runs `alembic upgrade head`.
- `Dockerfile` builds `ghcr.io/mdopp/oscar-db-migrate` — a 50-MB Python+alembic image used as a sidecar in the oscar-brain pod.

## Container behaviour

The migrate container in oscar-brain runs once per pod start:

1. Waits up to 5 minutes for Postgres to accept connections.
2. Runs `alembic upgrade head` — idempotent, applies only pending revisions.
3. Logs the resulting head revision via `oscar_logging`.
4. Sleeps forever (so the pod stays healthy; the wakeup happens at the next pod restart).

If the migration fails the container exits non-zero — ServiceBay surfaces this in `get_container_logs(oscar-brain-migrate)` and the pod is unhealthy. **In that case nothing else in the pod should serve user traffic** because the schema is in an unknown state. Phase-1 doesn't enforce that gate; downstream containers happily try to run anyway. A Phase-2 follow-up will have HERMES + connectors block on a "migrations applied" readiness signal.

## Adding a new migration

```bash
cd shared/oscar_db
# Generate a new revision skeleton
alembic revision -m "add gatekeeper_voice_embeddings"
# Hand-edit shared/oscar_db/migrations/versions/<rev>_*.py
# Local test against a throw-away Postgres:
POSTGRES_DSN=postgresql://... alembic upgrade head
POSTGRES_DSN=postgresql://... alembic downgrade -1
```

We hand-write migration bodies; **don't** use `--autogenerate` because we don't have SQLAlchemy ORM models — autogenerate would produce empty diffs.

## CLI

```bash
# What the container ENTRYPOINT runs:
POSTGRES_DSN=postgresql://oscar:…@localhost:5432/oscar python -m oscar_db upgrade

# Diagnostics:
python -m oscar_db current   # print the current head revision
python -m oscar_db history   # print revision history
```

`upgrade` is alembic `upgrade head`. The other two are passthrough to alembic's standard subcommands; useful when debugging.

## Tests

Minimal. The `cli.py` wait-for-postgres logic is tested with a stubbed asyncpg; the actual migration is verified by applying it against a docker-managed throw-away Postgres in a follow-up (Phase-1 doesn't ship that integration test yet).

## Open follow-ups

- **Readiness gate**: HERMES + connectors should refuse to serve until the migrate container has logged its `migrate.complete` event. Phase 2.
- **`downgrade` support**: alembic supports it; the baseline revision doesn't yet have a clean down because it'd drop user data. Phase-2 migrations should always include a working `downgrade()`.
- **Schema drift CI test**: spin up an empty Postgres in CI, run migrations, dump the resulting schema, diff against a checked-in `schema.sql` snapshot. Catches accidental hand-edits to the production DB.
