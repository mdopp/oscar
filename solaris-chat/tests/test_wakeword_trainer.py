"""Tests for Bidirectional Wakeword Trainer & System-UID Resident Resolver (#1056)."""

from __future__ import annotations

import json
import pytest

from solaris_chat.engine.tools.wakeword_trainer import (
    build_wakeword_tools,
    parse_spelled_uid,
    resolve_resident_identity,
)


def test_spelled_uid_and_identity_resolver():
    assert parse_spelled_uid("M - A - R - C - O") == "marco"
    assert parse_spelled_uid("A - L - E - X") == "alex"

    uid, display_name, spelled = resolve_resident_identity("Alex")
    assert uid == "alex"
    assert display_name == "Alex Test"
    assert spelled == "A - L - E - X"

    uid2, display_name2, spelled2 = resolve_resident_identity("A - L - E - X")
    assert uid2 == "alex"
    assert display_name2 == "Alex Test"
    assert spelled2 == "A - L - E - X"


@pytest.mark.asyncio
async def test_bidirectional_wakeword_trainer_flow(tmp_path):
    db_path = str(tmp_path / "solaris_test.db")
    (tmp_path / "train-micro-wake-word.py").write_text("import sys; sys.exit(0)\n")
    tools = build_wakeword_tools(db_path, lambda: "household", script_dir=str(tmp_path))

    start_tool = next(t for t in tools if t.name == "start_wakeword_enrollment")
    sample_tool = next(t for t in tools if t.name == "record_wakeword_sample")
    trigger_tool = next(t for t in tools if t.name == "trigger_wakeword_training")

    # 1. Start enrollment for 'Alex' -> resolves to system UID 'alex'
    res1 = json.loads(await start_tool.handler({"uid": "Alex", "target_count": 2}))
    assert res1["ok"] is True
    assert res1["uid"] == "alex"
    assert res1["display_name"] == "Alex Test"
    assert res1["say"].endswith("?")

    # 2. Record samples addressing Alex
    s1 = json.loads(await sample_tool.handler({"uid": "alex", "transcript": "Solaris"}))
    assert s1["remaining"] == 1
    assert s1["say"].endswith("?")

    s2 = json.loads(await sample_tool.handler({"uid": "alex", "transcript": "Solaris"}))
    assert s2["completed"] is True
    assert s2["say"].endswith("?")

    # 3. Trigger training
    t1 = json.loads(await trigger_tool.handler({"uid": "alex"}))
    assert t1["training_started"] is True
    assert "Danke, Alex" in t1["say"]


@pytest.mark.asyncio
async def test_training_not_started_when_script_missing(tmp_path):
    """The chat image ships no training scripts, so the launch usually fails.
    Claiming a run that never started would leave the resident waiting for a
    model nobody is building."""
    db_path = str(tmp_path / "solaris_test.db")
    tools = build_wakeword_tools(
        db_path, lambda: "household", script_dir=str(tmp_path / "absent")
    )
    trigger_tool = next(t for t in tools if t.name == "trigger_wakeword_training")

    res = json.loads(await trigger_tool.handler({"uid": "alex"}))
    assert res["training_started"] is False
    assert "gestartet" not in res["say"]
    assert "nicht starten" in res["say"]
    assert res["say"].rstrip().endswith("?")


@pytest.mark.asyncio
async def test_delete_never_touches_another_residents_sample(tmp_path):
    """The delete tool used to grab the first suspicious sample across ALL
    residents and decrement whoever happened to be speaking. On a household box
    that lets one resident erase another's recording and corrupts both counters.
    Observed live: deleting for `verifyww` removed `sample_mdopp_10`."""
    from solaris_chat import wakeword_requests_store, wakeword_samples_store

    db_path = str(tmp_path / "solaris_test.db")
    wakeword_requests_store.start_request(db_path, "alex", 10)
    wakeword_requests_store.start_request(db_path, "other", 10)
    wakeword_samples_store.add_sample(
        db_path,
        sample_id="sample_other_1",
        wakeword_id="solaris",
        filename=str(tmp_path / "other.wav"),
        resident_uid="other",
        intended_phrase="Solaris",
        stt_transcript="voellig anderes",
        is_valid=0,
    )

    tools = build_wakeword_tools(db_path, lambda: "alex", script_dir=str(tmp_path))
    delete_tool = next(t for t in tools if t.name == "delete_wakeword_sample")

    # No sample of alex's own is suspicious -> nothing to delete, and the other
    # resident's sample must survive.
    res = json.loads(await delete_tool.handler({"uid": "alex"}))
    assert res["deleted_sample_id"] == ""
    assert wakeword_samples_store.get_sample(db_path, "sample_other_1") is not None

    # Naming it explicitly is refused too.
    res2 = json.loads(
        await delete_tool.handler({"uid": "alex", "sample_id": "sample_other_1"})
    )
    assert res2["ok"] is False
    assert wakeword_samples_store.get_sample(db_path, "sample_other_1") is not None
