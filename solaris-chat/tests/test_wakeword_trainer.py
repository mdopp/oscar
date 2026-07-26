"""Tests for Bidirectional Wakeword Trainer & Spelling Parser (#1056)."""

from __future__ annotations

import json
import pytest

from solaris_chat.engine.tools.wakeword_trainer import build_wakeword_tools, parse_spelled_uid


def test_spelled_uid_parser():
    assert parse_spelled_uid("M - A - R - C - O") == "marco"
    assert parse_spelled_uid("M, A, R, C, O") == "marco"
    assert parse_spelled_uid("Michael") == "michael"
    assert parse_spelled_uid("C - A - R - O - L - A") == "carola"


@pytest.mark.asyncio
async def test_bidirectional_wakeword_trainer_flow(tmp_path):
    db_path = str(tmp_path / "solaris_test.db")
    tools = build_wakeword_tools(db_path, lambda: "household", script_dir=str(tmp_path))

    start_tool = next(t for t in tools if t.name == "start_wakeword_enrollment")
    sample_tool = next(t for t in tools if t.name == "record_wakeword_sample")
    trigger_tool = next(t for t in tools if t.name == "trigger_wakeword_training")

    # 1. Start enrollment with spelled UID 'M - A - R - C - O'
    res1 = json.loads(await start_tool.handler({"uid": "M - A - R - C - O", "target_count": 2}))
    assert res1["ok"] is True
    assert res1["uid"] == "marco"
    assert "Marco" in res1["say"]

    # 2. Record samples addressing Marco
    s1 = json.loads(await sample_tool.handler({"uid": "marco", "transcript": "Solaris"}))
    assert s1["remaining"] == 1

    s2 = json.loads(await sample_tool.handler({"uid": "marco", "transcript": "Solaris"}))
    assert s2["completed"] is True

    # 3. Trigger training
    t1 = json.loads(await trigger_tool.handler({"uid": "marco"}))
    assert t1["training_started"] is True
    assert "Danke, Marco" in t1["say"]
