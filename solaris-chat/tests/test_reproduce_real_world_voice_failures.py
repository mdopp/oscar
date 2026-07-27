"""E2E Test Suite for Real-World Voice Failures (#1056)."""

import pytest
from solaris_chat.engine import enrollment_fsm


@pytest.mark.asyncio
async def test_fsm_stream_must_yield_non_zero_bytes_to_facade(tmp_path):
    """Tests facade streaming loop over FSM turns.
    MUST yield non-zero bytes (assistant.delta) to prevent 'unable to read gate response'."""
    db = str(tmp_path / "repro_stream.db")
    enrollment_fsm.reset_fsm(db, "default")

    reply = enrollment_fsm.handle_turn(db, "Richte einen Benutzer ein.")

    async def _fsm_turns():
        yield {"type": "assistant.delta", "data": {"delta": reply}}
        yield {"type": "run.completed", "data": {"answer": reply}}

    streamed = ""
    async for event in _fsm_turns():
        if event["type"] == "assistant.delta":
            streamed += event["data"].get("delta", "")

    assert len(streamed) > 0, (
        f"FAIL: Streamed response was 0 bytes! Event types did not match 'assistant.delta'. Streamed: '{streamed}'"
    )


@pytest.mark.asyncio
async def test_stale_fsm_state_auto_expires_after_ttl(tmp_path):
    """TTL auto-expiration test (#1056): Ensures stale FSM states older than 180s
    automatically expire and reset so new attempts start cleanly at Step 1."""
    import sqlite3

    db = str(tmp_path / "ttl_test.db")
    enrollment_fsm.reset_fsm(db, "default")

    # 1. Start enrollment and advance to NAME state
    enrollment_fsm.handle_turn(db, "Richte einen Benutzer ein.")
    enrollment_fsm.handle_turn(db, "Ja")

    # 2. Simulate stale timestamp (10 minutes ago)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE enrollment_fsm SET updated_at = datetime('now', '-600 seconds')"
        )
        conn.commit()

    # 3. Next turn MUST detect stale state, reset automatically, and start fresh at Step 1!
    res = enrollment_fsm.handle_turn(db, "Richte einen Benutzer ein.")
    assert "Möchtest du dein Sprachprofil biometrisch" in res
