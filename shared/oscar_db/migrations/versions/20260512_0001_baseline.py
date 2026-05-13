"""baseline: system_settings + cloud_audit

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-12 (updated post-Hermes reset)

Household-domain tables only. Skill-management, timer/alarm, and
gateway_identities tables that earlier drafts had are now Hermes-owned
(skill versioning, cron scheduler, native messaging-gateway pairing).
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
