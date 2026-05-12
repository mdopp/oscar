"""baseline: system_settings, time_jobs, gateway_identities, cloud_audit

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-12

Mirrors the inline init SQL that used to live in
templates/oscar-brain/template.yml. Same `IF NOT EXISTS` idempotency
so applying this against an already-initialised DB is harmless.
"""

from __future__ import annotations

from alembic import op


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS system_settings (
          key        TEXT PRIMARY KEY,
          value      JSONB NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO system_settings (key, value) VALUES
          ('debug_mode', '{"active": true, "verbose_until": null, "latency_annotations": false}'::jsonb)
        ON CONFLICT (key) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS time_jobs (
          id              UUID PRIMARY KEY,
          kind            TEXT NOT NULL CHECK (kind IN ('timer','alarm')),
          owner_uid       TEXT NOT NULL,
          label           TEXT,
          fires_at        TIMESTAMPTZ NOT NULL,
          rrule           TEXT,
          duration_set    INTERVAL,
          target_endpoint TEXT NOT NULL,
          hermes_cron_id  TEXT,
          state           TEXT NOT NULL CHECK (state IN ('armed','firing','snoozed','done','cancelled')),
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS time_jobs_fires_idx ON time_jobs (state, fires_at) "
        "WHERE state IN ('armed','snoozed')"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS time_jobs_owner_idx ON time_jobs (owner_uid, state)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_identities (
          gateway      TEXT NOT NULL,
          external_id  TEXT NOT NULL,
          uid          TEXT NOT NULL,
          display_name TEXT,
          verified_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_by   TEXT NOT NULL,
          PRIMARY KEY (gateway, external_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS gateway_identities_uid_idx ON gateway_identities (uid)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cloud_audit (
          id                 UUID PRIMARY KEY,
          ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
          trace_id           UUID NOT NULL,
          uid                TEXT NOT NULL,
          vendor             TEXT NOT NULL,
          prompt_hash        TEXT NOT NULL,
          prompt_length      INTEGER NOT NULL,
          response_length    INTEGER NOT NULL,
          latency_ms         INTEGER NOT NULL,
          cost_usd_micro     INTEGER,
          router_score       REAL,
          escalation_reason  TEXT,
          prompt_fulltext    TEXT,
          response_fulltext  TEXT
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS cloud_audit_ts_idx ON cloud_audit (ts DESC)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS cloud_audit_uid_idx ON cloud_audit (uid, ts DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS cloud_audit_trace_idx ON cloud_audit (trace_id)"
    )


def downgrade() -> None:
    # Phase-1 baseline doesn't support a clean down — it would destroy user
    # data. Phase-2+ migrations should provide working downgrade() paths.
    raise NotImplementedError(
        "Phase-1 baseline migration is one-way; would destroy production data. "
        "Drop the database and re-run upgrade instead."
    )
