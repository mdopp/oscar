"""add the person_merges audit/undo trail for cross-source person merge (#994)

Revision ID: 0030_person_merges
Revises: 0029_wakeword_training_runs
Create Date: 2026-07-27

Merging two `person` entities re-points the secondary's aliases/facts/event
edges onto the primary and deletes the secondary (ADR 0010, #994). Merging two
humans is DESTRUCTIVE and would be irreversible without a trail, so every merge
records one `person_merges` row capturing the secondary's provenance (its id,
canonical_name, resident_uid, source) plus a JSON `snapshot` of the
aliases/facts/event edges that moved AND of exactly which aliases/event edges
the merge added to the primary. That row makes a merge auditable and lets
`undo_merge` restore both sides — remove precisely what was added, put the
secondary back — so a false-merge is recoverable rather than a hard data loss.
`merged_by` is the authorization anchor: only the resident who made a merge may
undo it. Merge is human-confirmed (never auto-applied); this table is the safety
net behind the confirmation gate.
"""

from __future__ import annotations

from alembic import op


revision = "0030_person_merges"
down_revision = "0029_wakeword_training_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS person_merges (
          id                 TEXT PRIMARY KEY,
          primary_entity_id  TEXT NOT NULL,
          secondary_entity_id TEXT NOT NULL,
          secondary_name     TEXT NOT NULL,
          secondary_resident_uid TEXT NOT NULL,
          secondary_source   TEXT NOT NULL,
          snapshot           TEXT NOT NULL,
          merged_by          TEXT NOT NULL,
          merged_at          TEXT NOT NULL DEFAULT (datetime('now')),
          undone_at          TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS person_merges_primary_idx "
        "ON person_merges (primary_entity_id)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )
