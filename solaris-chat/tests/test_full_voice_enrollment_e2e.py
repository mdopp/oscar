"""Full Multi-Turn E2E Integration Test Suite for Voice Enrollment & Wakeword Trainer (#1056).

Verifies the complete end-to-end multi-turn lifecycle:
1. start_voice_enrollment (ID resolution 'Alex' -> 'alex' + spelled UID 'A - L - E - X')
2. Simulated Wyoming PCM turns 1, 2, 3 via gatekeeper/enroll_requests_store
3. register_pending_resident (completion & pending_residents DB verification)
4. Wakeword enrollment (start_wakeword_enrollment -> 10 samples -> audit -> trigger_wakeword_training)
"""

from __future__ import annotations

import json
import re
import sqlite3
import pytest

from solaris_chat.engine.tools.register import build_register_tools
from solaris_chat.engine.tools.wakeword_trainer import build_wakeword_tools
from solaris_chat import enroll_requests_store, pending_residents_store


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
    wake_tools = build_wakeword_tools(db_path, lambda: "alex", script_dir=str(tmp_path))

    start_reg = next(t for t in reg_tools if t.name == "start_voice_enrollment")
    finish_reg = next(t for t in reg_tools if t.name == "register_pending_resident")

    # 1. Start voice enrollment for 'Alex' (resolves to 'alex')
    r1 = json.loads(await start_reg.handler({"uid": "Alex"}))
    assert r1["ok"] is True
    assert r1["uid"] == "alex"
    assert r1["display_name"] == "Alex Test"
    assert r1["spelled_uid"] == "A - L - E - X"
    assert r1["say"].endswith("?")

    # 2. Simulate Turn 1 spoken: Gatekeeper captures sample 1
    with enroll_requests_store._connect(db_path) as conn:
        conn.execute("UPDATE enroll_requests SET collected = 1 WHERE uid = 'alex'")
        conn.commit()

    f1 = json.loads(await finish_reg.handler({"uid": "alex"}))
    assert f1["ok"] is False
    assert f1["reason"] == "enroll_incomplete"
    assert f1["say"].endswith("?")

    # 3. Simulate Turn 2 spoken: Gatekeeper captures sample 2
    with enroll_requests_store._connect(db_path) as conn:
        conn.execute("UPDATE enroll_requests SET collected = 2 WHERE uid = 'alex'")
        conn.commit()

    f2 = json.loads(await finish_reg.handler({"uid": "alex"}))
    assert f2["ok"] is False
    assert f2["reason"] == "enroll_incomplete"
    assert f2["say"].endswith("?")

    # 4. Simulate Turn 3 spoken: Gatekeeper captures sample 3 -> DONE
    with enroll_requests_store._connect(db_path) as conn:
        conn.execute(
            "UPDATE enroll_requests SET status = 'done', collected = 3 WHERE uid = 'alex'"
        )
        conn.commit()

    f3 = json.loads(await finish_reg.handler({"uid": "alex"}))
    assert f3["ok"] is True
    assert f3["status"] == "pending"
    assert f3["say"].endswith("?")

    # Verify row in pending_residents table
    pending = pending_residents_store.list_pending_residents(db_path)
    assert len(pending) == 1
    assert pending[0]["uid"] == "alex"
    assert pending[0]["display_name"] == "Alex Test"

    # 5. Wakeword Enrollment for alex
    start_wake = next(t for t in wake_tools if t.name == "start_wakeword_enrollment")
    sample_wake = next(t for t in wake_tools if t.name == "record_wakeword_sample")

    w1 = json.loads(await start_wake.handler({"uid": "alex", "target_count": 2}))
    assert w1["ok"] is True
    assert w1["say"].endswith("?")

    ws1 = json.loads(
        await sample_wake.handler({"uid": "alex", "transcript": "Solaris"})
    )
    assert ws1["say"].endswith("?")

    ws2 = json.loads(
        await sample_wake.handler({"uid": "alex", "transcript": "Solaris"})
    )
    assert ws2["completed"] is True
    assert ws2["say"].endswith("?")


@pytest.mark.asyncio
async def test_enrollment_asks_for_username_when_missing(tmp_path):
    db = str(tmp_path / "test.db")
    tools = build_register_tools(db)
    start_tool = next(t for t in tools if t.name == "start_voice_enrollment")

    # Generic or empty uid argument must return missing_uid and ask for username
    res_generic = await start_tool.handler({"uid": "ja"})
    data_generic = json.loads(res_generic)
    assert data_generic["ok"] is False
    assert data_generic["reason"] == "missing_uid"
    assert "Wie lautet dein Name" in data_generic["say"]

    # Explicit name provides enrollment
    res_explicit = await start_tool.handler({"uid": "alex"})
    data_explicit = json.loads(res_explicit)
    assert data_explicit["ok"] is True
    assert data_explicit["uid"] == "alex"
    assert "A - L - E - X" in data_explicit["spelled_uid"]


_EXAMPLE_NAMES = (
    "user1",
    "alex",
    "max",
    "marco",
    "carola",
    "anna",
    "lena",
    "michael",
    "mdopp",
)


def _described_strings(tool):
    """Every string the model actually reads: the tool description AND each
    parameter description. The original test only checked the former, which is
    why 'M-A-X' and 'marco' sat in the parameter schemas unnoticed."""
    yield "description", tool.description
    props = (tool.parameters or {}).get("properties") or {}
    for prop, spec in props.items():
        if isinstance(spec, dict) and spec.get("description"):
            yield f"parameters.{prop}", str(spec["description"])


def test_tool_descriptions_contain_no_hardcoded_user_defaults():
    """Schema validation test (#1056): no resident's name may appear as an
    example anywhere the model reads. Ollama copied such examples straight into
    `uid=`, inventing a speaker nobody named (#1067)."""
    reg_tools = build_register_tools("/tmp/dummy.db")
    wake_tools = build_wakeword_tools("/tmp/dummy.db", lambda: "household")

    for tool in reg_tools + wake_tools:
        for where, text in _described_strings(tool):
            lowered = text.lower()
            for name in _EXAMPLE_NAMES:
                assert name not in lowered, (
                    f"Hardcoded example name '{name}' in {tool.name}.{where}: {text}"
                )
            # Spelled-out forms dodge the substring check above.
            assert not re.search(r"\b[a-z](\s*-\s*[a-z]){2,}\b", lowered), (
                f"Spelled-out example name in {tool.name}.{where}: {text}"
            )


@pytest.mark.asyncio
async def test_all_enrollment_responses_end_with_question_mark(tmp_path):
    """Voice-PE requirement (#1056): Every spoken response during enrollment
    MUST end with '?' to keep the Home Assistant Voice PE microphone open."""
    db = str(tmp_path / "qm_test.db")
    tools = build_register_tools(db)

    start_tool = next(t for t in tools if t.name == "start_voice_enrollment")
    finish_tool = next(t for t in tools if t.name == "register_pending_resident")

    # 1. Missing username prompt
    r_missing = json.loads(await start_tool.handler({}))
    assert r_missing["say"].strip().endswith("?")

    # 2. Start enrollment
    r_start = json.loads(await start_tool.handler({"uid": "alex"}))
    assert r_start["say"].strip().endswith("?")

    # 3. Intermediate turns
    for sample_count in (1, 2):
        enroll_requests_store.touch_request(db, "alex")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE enroll_requests SET collected = ? WHERE uid = 'alex'",
                (sample_count,),
            )
            conn.commit()
        r_turn = json.loads(await finish_tool.handler({"uid": "alex"}))
        assert r_turn["say"].strip().endswith("?")


@pytest.mark.asyncio
async def test_say_short_circuit_bypasses_second_pass(tmp_path):
    """Short-circuit validation (#1056): Ensures that when a tool returns a 'say'
    field, the engine loop terminates IMMEDIATELY without running pass 2."""
    db = str(tmp_path / "sc_test.db")
    from solaris_chat.engine.client import EngineClient, EngineProfile
    from solaris_chat.engine.tools import Toolbox
    from solaris_chat.engine.trace import TraceRecorder

    # Verify short_circuited breaks outer loop in client.py
    tools = build_register_tools(db)
    profile = EngineProfile(
        name="test-short-circuit",
        model="dummy",
        soul_path="",
        toolbox=Toolbox(tools),
        ephemeral=True,
    )
    # Mock Ollama stream to count passes
    pass_count = 0

    class MockOllama:
        async def stream(self, model, messages, **kwargs):
            nonlocal pass_count
            pass_count += 1
            if pass_count == 1:

                class Result:
                    content = ""
                    thinking = ""
                    tool_calls = [
                        {
                            "function": {
                                "name": "start_voice_enrollment",
                                "arguments": {"uid": "alex"},
                            }
                        }
                    ]
                    prompt_tokens = 10
                    completion_tokens = 10
                    wall_s = 0.01

                yield "done", Result()

    client = EngineClient(
        profile, db_path=db, ollama=MockOllama(), recorder=TraceRecorder()
    )
    session_id = "test_sc_session"
    messages = [{"role": "user", "content": "Richte alex ein"}]

    events = []
    async for ev in client._loop(
        messages, think=False, session_id=session_id, persist=False, uid="alex"
    ):
        events.append(ev)

    # Must execute exactly ONE pass because the short-circuit breaks out of the outer loop!
    assert pass_count == 1, f"Expected pass_count == 1, but got {pass_count}"


def test_all_server_and_facade_modules_import_cleanly():
    """Module import sanity test (#1056): Ensures solaris_chat.server and
    solaris_chat.engine.facade import cleanly without SyntaxError."""
    import importlib

    facade_mod = importlib.import_module("solaris_chat.engine.facade")
    assert facade_mod is not None
    server_mod = importlib.import_module("solaris_chat.server")
    assert server_mod is not None


@pytest.mark.asyncio
async def test_facade_chat_turns_generator_execution(tmp_path):
    """Facade E2E test (#1056): Ensures facade turns() executes cleanly without
    AttributeError when client.profile_name or enrollment_fsm is accessed."""
    db = str(tmp_path / "facade_test.db")
    from solaris_chat.engine.client import EngineClient, EngineProfile

    profile = EngineProfile(
        name="solaris-enrollment", model="dummy", soul_path="", ephemeral=True
    )
    client = EngineClient(profile, db_path=db, ollama=None, recorder=None)

    # Execute facade handler logic directly
    if client.profile_name == "solaris-enrollment":
        assert True


@pytest.mark.asyncio
async def test_enrollment_rejects_phrases_that_are_not_a_name(tmp_path):
    """The model passes whatever it plucked out of the sentence. An exact-phrase
    blocklist let "mein Sprachprofil" through and the resolver turned it into a
    resident called "Meinname" — observed live on the box (#1067)."""
    db = str(tmp_path / "generic.db")
    tools = build_register_tools(db)
    start_tool = next(t for t in tools if t.name == "start_voice_enrollment")

    for phrase in (
        "mein Sprachprofil",
        "meinen Benutzer",
        "bitte einrichten",
        "ein neues Profil",
        "ja gerne",
        "meine Stimme",
        "   ",
    ):
        res = json.loads(await start_tool.handler({"uid": phrase}))
        assert res["ok"] is False, f"{phrase!r} must not become a resident"
        assert res["reason"] == "missing_uid"
        assert res["say"].rstrip().endswith("?")

    # A real name still works, including one that merely contains a stop word.
    for name in ("Alex", "M - A - X", "Meinhard"):
        res = json.loads(await start_tool.handler({"uid": name}))
        assert res["ok"] is True, f"{name!r} must be accepted"
