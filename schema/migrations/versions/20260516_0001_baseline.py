"""baseline: system_settings + cloud_audit + voice_embeddings (SQLite)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-05-16

Phase 0 baseline for OSCAR's three SQLite tables. The voice_embeddings
table is created up front so Phase 2 enrolment can fill it without a
migration step; it stays empty until then.

Storage choice rationale: see schema/README.md.
"""

from __future__ import annotations

from alembic import op


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # system_settings — single-row global flags.
    # `value` is JSON-as-TEXT (SQLite has no JSONB; the json1 extension
    # is built into mainline SQLite and lets the skills query json_extract).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS system_settings (
          key        TEXT PRIMARY KEY,
          value      TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    op.execute(
        """
        INSERT INTO system_settings (key, value) VALUES
          ('debug_mode',
           '{"active": true, "verbose_until": null, "latency_annotations": false}')
        ON CONFLICT(key) DO NOTHING
        """
    )

    # cloud_audit — one row per Hermes cloud-LLM call. Append-only.
    # id stored as TEXT(uuid) for cross-store portability (vs SQLite's
    # implicit INTEGER PK rowid). Caller provides the UUID.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS cloud_audit (
          id                 TEXT PRIMARY KEY,
          ts                 TEXT NOT NULL DEFAULT (datetime('now')),
          trace_id           TEXT NOT NULL,
          uid                TEXT NOT NULL,
          vendor             TEXT NOT NULL,
          model              TEXT,
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

    # voice_embeddings — Phase 2; created up front so enrolment can fill
    # it without a follow-up migration. 256-d float32 stored as BLOB
    # (256 * 4 = 1024 bytes). Brute-force cosine in Python over 3–10
    # rows — no vector index, no SQLite extension required.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_embeddings (
          uid              TEXT PRIMARY KEY,
          embedding        BLOB NOT NULL,
          enrolled_at      TEXT NOT NULL DEFAULT (datetime('now')),
          enrolled_via     TEXT NOT NULL,
          sample_count     INTEGER NOT NULL DEFAULT 1,
          last_seen_at     TEXT
        )
        """
    )


def downgrade() -> None:
    # Baseline migration is one-way: a downgrade would destroy audit
    # history and voice enrolments. Drop the SQLite file and re-run
    # upgrade instead.
    raise NotImplementedError(
        "Baseline migration is one-way. Delete oscar.db and re-run upgrade."
    )
