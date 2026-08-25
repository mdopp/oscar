"""voice_uid_stash: widen the correlation key with the originating room

Revision ID: 0033_voice_uid_stash_room
Revises: 0032_engine_timers_room
Create Date: 2026-08-25

The stash row was keyed on the whisper transcript alone. Two residents who say
the identical sentence inside the ~30s stash window therefore shared one row:
the second write overwrote the first's `{uid, matched}` before it was consumed,
and the PERSONAL visibility gate handed resident A's turn resident B's identity
(#1218).

`room` is the disambiguator the two sides already share on the satellite path:
the gatekeeper resolves the originating room and injects it as the `[room: X]`
prefix the facade parses back out, so both ends compute the same string. It is
part of the primary key, so two rooms saying the same thing are two rows.

On the HA-STT path the gatekeeper's peer is HA, not the satellite, so it has no
room and writes `''`. There the key is unchanged and a collision is resolved by
failing closed — the writer degrades a live same-transcript/different-uid row to
`guest`/not-matched, and the reader refuses an ambiguous multi-row hit — rather
than by handing over the other resident's identity.
"""

from __future__ import annotations

from alembic import op


revision = "0033_voice_uid_stash_room"
down_revision = "0032_engine_timers_room"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite can't alter a primary key, so the table is rebuilt. The rows carried
    # over are at most a few seconds of scratch state; they land in room ''.
    op.execute(
        """
        CREATE TABLE voice_uid_stash_new (
            transcript TEXT NOT NULL,
            room       TEXT NOT NULL DEFAULT '',
            uid        TEXT NOT NULL,
            matched    INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (transcript, room)
        )
        """
    )
    op.execute(
        """
        INSERT INTO voice_uid_stash_new (transcript, room, uid, matched, created_at)
        SELECT transcript, '', uid, matched, created_at FROM voice_uid_stash
        """
    )
    op.execute("DROP TABLE voice_uid_stash")
    op.execute("ALTER TABLE voice_uid_stash_new RENAME TO voice_uid_stash")


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )
