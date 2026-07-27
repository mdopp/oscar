"""scope the model-visible history to one conversation

Revision ID: 0028_engine_messages_conversation
Revises: 0027_concepts_refid_index
Create Date: 2026-07-27

Every voice turn preloaded the whole durable "Zuhause" session: `store.history`
reads `WHERE session_id = ? ORDER BY seq` with no limit and no ageing, and the
session id is a stable uuid5 of the uid, so the row set only ever grows. On the
box that was 141 messages / ~5.6k est-tokens spanning a day, 12 of them
identity statements ("… wurde als … erkannt") left over from failed enrolment
attempts — the model kept imitating them. `truncate_session_head` never fired
because it only trims at ~40% of the context window.

`conversation_id` scopes what the prompt sees (HA supplies one per Assist
conversation; an idle gap covers the callers that don't). `in_prompt` marks
rows the model must never see again — the enrolment dialog scripts, which the
`say` short-circuit writes into the shared session and which then replay into
every later prompt.

Both columns are added with a constant default, so SQLite rewrites no rows.
Existing rows keep `conversation_id = NULL`, which can never equal a stamped
id: they drop out of every future prompt without a single UPDATE against real
user data, while the browser history (`store.get_session`) still shows them.
"""

from __future__ import annotations

from alembic import op


revision = "0028_engine_messages_conversation"
down_revision = "0027_concepts_refid_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE engine_messages ADD COLUMN conversation_id TEXT")
    op.execute(
        "ALTER TABLE engine_messages ADD COLUMN in_prompt INTEGER NOT NULL DEFAULT 1"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS engine_messages_conv_idx"
        " ON engine_messages (session_id, conversation_id, seq)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )
