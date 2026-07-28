"""voice_embeddings: many rows per resident instead of one averaged row

Revision ID: 0030_voice_embeddings_multi
Revises: 0029_wakeword_training_runs
Create Date: 2026-07-28

The baseline gave `voice_embeddings` a `uid TEXT PRIMARY KEY`, which allows
exactly one fingerprint per resident, so enrolment averaged every sample into a
single vector. One voice does not form one cloud though: close to the device vs.
across the room, morning vs. evening, quiet vs. TV each sit apart, and their mean
lands in the valley between them — far from all of them. Measured on the box,
five live turns of one speaker against his own 3-sample profile scored 0.74,
0.66, 0.54, 0.42, 0.30 against a 0.55 threshold (#1084).

So the primary key becomes a surrogate `id` and `uid` merely gets an index. The
resolver needs no change: `speaker.cosine_match` already picks the best row
across all candidates.

Existing rows are carried over verbatim — a resident whose profile was enrolled
before this migration keeps it, and only gains the ability to add more.
"""

from __future__ import annotations

from alembic import op


revision = "0030_voice_embeddings_multi"
down_revision = "0029_wakeword_training_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE voice_embeddings_multi (
          id               INTEGER PRIMARY KEY AUTOINCREMENT,
          uid              TEXT NOT NULL,
          embedding        BLOB NOT NULL,
          enrolled_at      TEXT NOT NULL DEFAULT (datetime('now')),
          enrolled_via     TEXT NOT NULL,
          sample_count     INTEGER NOT NULL DEFAULT 1,
          last_seen_at     TEXT
        )
        """
    )
    op.execute(
        """
        INSERT INTO voice_embeddings_multi
            (uid, embedding, enrolled_at, enrolled_via, sample_count, last_seen_at)
        SELECT uid, embedding, enrolled_at, enrolled_via, sample_count, last_seen_at
        FROM voice_embeddings
        """
    )
    op.execute("DROP TABLE voice_embeddings")
    op.execute("ALTER TABLE voice_embeddings_multi RENAME TO voice_embeddings")
    op.execute(
        "CREATE INDEX IF NOT EXISTS voice_embeddings_uid_idx ON voice_embeddings (uid)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade would have to discard all but one row per resident. Delete solaris.db and re-run upgrade if needed."
    )
