"""add ha_notice_backlog table (notification catch-up, #1284)

Revision ID: 0034_ha_notice_backlog
Revises: 0033_voice_uid_stash_room
Create Date: 2026-08-30

The `ha` event kind was in-process pub/sub with no backlog, so a notice
published while the app was not listening — the screen-off case the channel
exists for — was lost rather than delayed. This table is the few-hours backlog
`GET /napi/notifications?since=…` replays from.

Deliberately thin and deliberately short-lived: one row per emitted event, the
payload stored verbatim as JSON so the catch-up can never drift from the shape
on the stream. `solaris_chat.notice_backlog` prunes by age (6 hours) and by row
count per stream on every write — these rows name residents and describe their
home, so retention is a privacy property, not a capacity one. `notice_backlog`
degrades to "no backlog" when this table is missing, so the engine is safe to
deploy before the migration lands.
"""

from __future__ import annotations

from alembic import op


revision = "0034_ha_notice_backlog"
down_revision = "0033_voice_uid_stash_room"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `id` AUTOINCREMENT is the *order*: two notices can share a millisecond
    # stamp, and pruning deletes the newest rows of a stream, which without
    # AUTOINCREMENT would let a rowid be handed out twice. Millisecond stamps
    # because a cursor at second resolution drops a second notice published in
    # the same second as the one the client last saw.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ha_notice_backlog (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          target_uid TEXT NOT NULL,
          created_at TEXT NOT NULL
                     DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
          payload    TEXT NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ha_notice_backlog_target_idx "
        "ON ha_notice_backlog (target_uid, id)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )
