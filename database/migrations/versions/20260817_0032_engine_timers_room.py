"""engine_timers: remember the room the timer was set in

Revision ID: 0032_engine_timers_room
Revises: 0031_voice_uid_stash_matched
Create Date: 2026-08-17

A fired timer used to announce on every `assist_satellite` in the house, so a
private reminder ("Arzttermin um 15 Uhr") was read out to everyone present
(#1187). Speaker-ID is off in production, so "the person who asked" is not
knowable — the room the request came in on (the facade's `[room: X]` marker) is
the strongest available signal, and it now rides the row so the scheduler can
ring only that room's satellite.

`DEFAULT ''` is the app/browser-set case: no originating room, so the
announcement falls back to house-wide *without* the label.
"""

from __future__ import annotations

from alembic import op


revision = "0032_engine_timers_room"
down_revision = "0031_voice_uid_stash_matched"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE engine_timers ADD COLUMN room TEXT NOT NULL DEFAULT ''")


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )
