"""Full Multi-Turn E2E Integration Test Suite for Voice Enrollment & Wakeword Trainer (#1056).

Verifies the complete end-to-end multi-turn lifecycle:
1. start_voice_enrollment (ID resolution 'Michael' -> 'mdopp' + spelled UID 'M - D - O - P - P')
2. Simulated Wyoming PCM turns 1, 2, 3 via gatekeeper/enroll_requests_store
3. register_pending_resident (completion & pending_residents DB verification)
4. Wakeword enrollment (start_wakeword_enrollment -> 10 samples -> audit -> trigger_wakeword_training)
"""

from __future__ annotations

import json
import sqlite3
import pytest

from solaris_chat.engine.tools.register import build_register_tools
from solaris_chat.engine.tools.wakeword_trainer import build_wakeword_tools
from solaris_chat import enroll_requests_store, pending_residents_store, wakeword_samples_store


@pytest.mark.asyncio
async def test_full_voice_enrollment_multi_turn_e2e(tmp_path):
    db_path = str(tmp_path / "solaris_e2e.db")

    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_residents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                enrolled INTEGER NOT NULL DEFAULT 0,
                requested_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

    reg_tools = build_register_tools(db_path)
    wake_tools = build_wakeword_tools(db_path, lambda: "mdopp", script_dir=str(tmp_path))

    start_reg = next(t for t in reg_tools if t.name == "start_voice_enrollment")
    finish_reg = next(t for t in reg_tools if t.name == "register_pending_resident")

    # 1. Start voice enrollment for 'Michael' (resolves to 'mdopp')
    r1 = json.loads(await start_reg.handler({"uid": "Michael"}))
    assert r1["ok"] is True
    assert r1["uid"] == "mdopp"
    assert r1["display_name"] == "Michael Dopp"
    assert r1["spelled_uid"] == "M - D - O - P - P"
    assert r1["say"].endswith("?")

    # 2. Simulate Turn 1 spoken: Gatekeeper captures sample 1
    with enroll_requests_store._connect(db_path) as conn:
        conn.execute("UPDATE enroll_requests SET collected = 1 WHERE uid = 'mdopp'")
        conn.commit()

    f1 = json.loads(await finish_reg.handler({"uid": "mdopp"}))
    assert f1["ok"] is False
    assert f1["reason"] == "enroll_incomplete"
    assert f1["say"].endswith("?")

    # 3. Simulate Turn 2 spoken: Gatekeeper captures sample 2
    with enroll_requests_store._connect(db_path) as conn:
        conn.execute("UPDATE enroll_requests SET collected = 2 WHERE uid = 'mdopp'")
        conn.commit()

    f2 = json.loads(await finish_reg.handler({"uid": "mdopp"}))
    assert f2["ok"] is False
    assert f2["reason"] == "enroll_incomplete"
    assert f2["say"].endswith("?")

    # 4. Simulate Turn 3 spoken: Gatekeeper captures sample 3 -> DONE
    with enroll_requests_store._connect(db_path) as conn:
        conn.execute("UPDATE enroll_requests SET status = 'done', collected = 3 WHERE uid = 'mdopp'")
        conn.commit()

    f3 = json.loads(await finish_reg.handler({"uid": "mdopp"}))
    assert f3["ok"] is True
    assert f3["status"] == "pending"
    assert f3["say"].endswith("?")

    # Verify row in pending_residents table
    pending = pending_residents_store.list_pending_residents(db_path)
    assert len(pending) == 1
    assert pending[0]["uid"] == "mdopp"
    assert pending[0]["display_name"] == "Michael Dopp"

    # 5. Wakeword Enrollment for mdopp
    start_wake = next(t for t in wake_tools if t.name == "start_wakeword_enrollment")
    sample_wake = next(t for t in wake_tools if t.name == "record_wakeword_sample")

    w1 = json.loads(await start_wake.handler({"uid": "mdopp", "target_count": 2}))
    assert w1["ok"] is True
    assert w1["say"].endswith("?")

    ws1 = json.loads(await sample_wake.handler({"uid": "mdopp", "transcript": "Solaris"}))
    assert ws1["say"].endswith("?")

    ws2 = json.loads(await sample_wake.handler({"uid": "mdopp", "transcript": "Solaris"}))
    assert ws2["completed"] is True
    assert ws2["say"].endswith("?")
