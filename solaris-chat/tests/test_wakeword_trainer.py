"""Tests for Bidirectional Wakeword Trainer & System-UID Resident Resolver (#1056)."""

from __future__ annotations

import json
import pytest

from solaris_chat.engine.tools.wakeword_trainer import (
    build_wakeword_tools,
    parse_spelled_uid,
    resolve_resident_identity,
)


def test_spelled_uid_and_identity_resolver():
    assert parse_spelled_uid("M - A - R - C - O") == "marco"
    assert parse_spelled_uid("M - D - O - P - P") == "mdopp"

    uid, display_name = resolve_resident_identity("Michael")
    assert uid == "mdopp"
    assert display_name == "Michael"

    uid2, display_name2 = resolve_resident_identity("M - D - O - P - P")
    assert uid2 == "mdopp"
    assert display_name2 == "Michael"


@pytest.mark.asyncio
async def test_bidirectional_wakeword_trainer_flow(tmp_path):
    db_path = str(tmp_path / "solaris_test.db")
    tools = build_wakeword_tools(db_path, lambda: "household", script_dir=str(tmp_path))

    start_tool = next(t for t in tools if t.name == "start_wakeword_enrollment")
    sample_tool = next(t for t in tools if t.name == "record_wakeword_sample")
    trigger_tool = next(t for t in tools if t.name == "trigger_wakeword_training")

    # 1. Start enrollment for 'Michael' -> resolves to system UID 'mdopp'
    res1 = json.loads(await start_tool.handler({"uid": "Michael", "target_count": 2}))
    assert res1["ok"] is True
    assert res1["uid"] == "mdopp"
    assert res1["display_name"] == "Michael"
    assert res1["say"].endswith("?")

    # 2. Record samples addressing Michael
    s1 = json.loads(await sample_tool.handler({"uid": "mdopp", "transcript": "Solaris"}))
    assert s1["remaining"] == 1
    assert s1["say"].endswith("?")

    s2 = json.loads(await sample_tool.handler({"uid": "mdopp", "transcript": "Solaris"}))
    assert s2["completed"] is True
    assert s2["say"].endswith("?")

    # 3. Trigger training
    t1 = json.loads(await trigger_tool.handler({"uid": "mdopp"}))
    assert t1["training_started"] is True
    assert "Danke, Michael" in t1["say"]
