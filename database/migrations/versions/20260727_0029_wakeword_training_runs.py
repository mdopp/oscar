"""add wakeword_training_runs queue table

Revision ID: 0029_wakeword_training_runs
Revises: 0028_engine_messages_conversation
Create Date: 2026-07-27

The handshake behind `trigger_wakeword_training` (#1066). The engine could never
start the microWakeWord training itself: the chat image is `python:3.12-slim`
with no TensorFlow, no GPU and no training script, and a pod container cannot
reach the host's podman. So the engine enqueues a row here and the
`solaris-wakeword-trainer` GPU companion — the same shape as the gatekeeper
claiming `enroll_requests` — claims it, runs the training and writes the result
back.

`status` walks queued → running → done|failed. `started_at` doubles as the claim
stamp: the claim is a single conditional UPDATE from `queued` to `running`, so
two trainers can never take the same run. `model_path` carries the produced
streaming `.tflite`; flashing it onto the Voice PE stays a separate ESPHome
phase, so a `done` row means "model built", not "wake word live".
"""

from __future__ import annotations

from alembic import op


revision = "0029_wakeword_training_runs"
down_revision = "0028_engine_messages_conversation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE wakeword_training_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            uid          TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'queued',
            requested_at TEXT NOT NULL DEFAULT (datetime('now')),
            started_at   TEXT,
            finished_at  TEXT,
            result       TEXT,
            model_path   TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS wakeword_training_runs_status_idx"
        " ON wakeword_training_runs (status, requested_at)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )
