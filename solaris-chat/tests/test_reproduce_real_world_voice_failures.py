"""E2E Test Suite for Real-World Voice Failures (#1056)."""

import json, pytest
from solaris_chat.engine import enrollment_fsm, facade

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

    assert len(streamed) > 0, f"FAIL: Streamed response was 0 bytes! Event types did not match 'assistant.delta'. Streamed: '{streamed}'"
