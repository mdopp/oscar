"""voice_uid_stash: carry the speaker-ID verdict explicitly

Revision ID: 0031_voice_uid_stash_matched
Revises: 0030_voice_embeddings_multi
Create Date: 2026-08-13

The stash row used to carry only *who* (`uid`). Whether speaker-ID had actually
recognised that voice was inferred by the reader from the row existing plus the
uid not being the `guest` sentinel — one string compare, in another image, was
the whole distance between an unknown voice and the PERSONAL tool class (#1152).

`matched` carries the claim itself: 1 only when the gatekeeper matched an
enrolled resident, 0 for the unknown-speaker row that routes to the guest
profile. `DEFAULT 0` is the fail-closed half of it — a writer that predates this
column (older gatekeeper image, deployed independently of the engine) inserts
rows that read as "not matched", never as a match.
"""

from __future__ import annotations

from alembic import op


revision = "0031_voice_uid_stash_matched"
down_revision = "0030_voice_embeddings_multi"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE voice_uid_stash ADD COLUMN matched INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )
