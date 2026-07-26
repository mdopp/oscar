"""Tests for the Wakeword Improvement Tools (#1056)."""

from __future__ annotations

import json
import sqlite3
import pytest

from solaris_chat.engine.tools.wakeword_trainer import build_wakeword_tools
from solaris_chat import wakeword_requests_store


@pytest.mark.asyncio
async def test_wakeword_trainer_flow(tmp_path):
    db_path = str(tmp_path / "solaris_test.db")
    tools = build_wakeword_tools(db_path, lambda: "mdopp", script_dir=str(tmp_path))

    start_tool = next(t for t in tools if t.name == "start_wakeword_enrollment")
    sample_tool = next(t for t in tools if t.name == "record_wakeword_sample")
    trigger_tool = next(t for t in tools if t.name == "trigger_wakeword_training")

    # 1. Start enrollment
    res1_raw = await start_tool.handler({"target_count": 3})
    res1 = json.loads(res1_raw)
    assert res1["ok"] is True
    assert res1["target_count"] == 3
    assert "Probe 1" in res1["say"]

    # 2. Record samples 1, 2, 3
    s1 = json.loads(await sample_tool.handler({}))
    assert s1["remaining"] == 2
    assert "Noch 2 Mal" in s1["say"]

    s2 = json.loads(await sample_tool.handler({}))
    assert s2["remaining"] == 1
    assert "1 Probe" in s2["say"]

    s3 = json.loads(await sample_tool.handler({}))
    assert s3["remaining"] == 0
    assert s3["completed"] is True
    assert "GPU-Training" in s3["say"]

    # 3. Trigger GPU training
    t_res = json.loads(await trigger_tool.handler({}))
    assert t_res["ok"] is True
    assert t_res["training_started"] is True
    assert "GPU-Training" in t_res["say"]
