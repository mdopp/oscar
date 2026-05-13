"""skill_drafts: two-phase confirm storage for skill-author + reviewer

Revision ID: 0003_skill_drafts
Revises: 0002_skill_observability
Create Date: 2026-05-13

Drafts live for ~30 minutes between "OSCAR proposes" and user "/ja".
For reviewer-source drafts, expires_at is set high (24 h) because the
notification flow may need user confirmation later.
"""

from __future__ import annotations

from alembic import op


revision = "0003_skill_drafts"
down_revision = "0002_skill_observability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_drafts (
          id          UUID PRIMARY KEY,
          uid         TEXT NOT NULL,
          skill_name  TEXT NOT NULL,
          proposed_md TEXT NOT NULL,
          current_md  TEXT,
          source      TEXT NOT NULL CHECK (source IN ('user','reviewer')),
          reason      TEXT,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          expires_at  TIMESTAMPTZ NOT NULL,
          status      TEXT NOT NULL CHECK (status IN ('pending','confirmed','expired','cancelled'))
                        DEFAULT 'pending'
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS skill_drafts_pending_idx "
        "ON skill_drafts (status, expires_at) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS skill_drafts_uid_idx ON skill_drafts (uid, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS skill_drafts CASCADE")
