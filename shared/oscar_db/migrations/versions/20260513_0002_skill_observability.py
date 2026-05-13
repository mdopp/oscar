"""skill_runs + skill_corrections + skill_edits

Revision ID: 0002_skill_observability
Revises: 0001_baseline
Create Date: 2026-05-13

Data fundament for user-creatable and self-improving skills (#39):

- skill_runs: one row per executed skill — utterance, response, outcome.
- skill_corrections: one row per detected "Nein, ich meinte…"-style
  correction, FK to the run it follows.
- skill_edits: one row per actual edit applied to skills-local/, both
  user-initiated (skill-author) and reviewer-autonomous, with git_sha
  for revert lookups.
"""

from __future__ import annotations

from alembic import op


revision = "0002_skill_observability"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_runs (
          id          UUID PRIMARY KEY,
          trace_id    UUID NOT NULL,
          uid         TEXT NOT NULL,
          endpoint    TEXT NOT NULL,
          skill_name  TEXT NOT NULL,
          utterance   TEXT NOT NULL,
          response    TEXT,
          outcome     TEXT NOT NULL CHECK (outcome IN ('ok','error','no_skill')),
          ts          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS skill_runs_skill_idx ON skill_runs (skill_name, ts DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS skill_runs_uid_idx ON skill_runs (uid, ts DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_corrections (
          id                   UUID PRIMARY KEY,
          run_id               UUID NOT NULL REFERENCES skill_runs(id) ON DELETE CASCADE,
          correction_utterance TEXT NOT NULL,
          ts                   TIMESTAMPTZ NOT NULL DEFAULT now(),
          status               TEXT NOT NULL CHECK (status IN ('pending','aggregated','edited','dismissed'))
                                 DEFAULT 'pending'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS skill_corrections_run_idx ON skill_corrections (run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS skill_corrections_status_idx ON skill_corrections (status, ts DESC)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_edits (
          id           UUID PRIMARY KEY,
          skill_name   TEXT NOT NULL,
          source       TEXT NOT NULL CHECK (source IN ('user','reviewer')),
          diff         TEXT NOT NULL,
          applied_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          reverted_at  TIMESTAMPTZ,
          git_sha      TEXT NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS skill_edits_skill_idx ON skill_edits (skill_name, applied_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS skill_edits CASCADE")
    op.execute("DROP TABLE IF EXISTS skill_corrections CASCADE")
    op.execute("DROP TABLE IF EXISTS skill_runs CASCADE")
