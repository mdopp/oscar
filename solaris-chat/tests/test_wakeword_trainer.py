"""Tests for Bidirectional Wakeword Trainer & System-UID Resident Resolver (#1056)."""

from __future__ import annotations

import json
import sqlite3

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


def _create_queue_table(db_path: str) -> None:
    """The `wakeword_training_runs` table as migration 0029 creates it — the
    engine never creates it itself, so a test that wants the queue present has
    to stand it up the way schema-init does."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS wakeword_training_runs ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'queued',"
            " requested_at TEXT NOT NULL DEFAULT (datetime('now')),"
            " started_at TEXT, finished_at TEXT, result TEXT, model_path TEXT)"
        )
        conn.commit()


def _gatekeeper_captured(db_path, uid):
    """Simulate the gatekeeper writing one `.wav` and bumping the counter.

    The counter is the gatekeeper's alone — captured audio is what counts. The
    tool used to increment it too, so every spoken turn counted twice and the
    dialog finished at five real recordings claiming ten.
    """
    from solaris_chat import wakeword_requests_store

    return wakeword_requests_store.record_sample(db_path, uid)


@pytest.mark.asyncio
async def test_bidirectional_wakeword_trainer_flow(tmp_path):
    db_path = str(tmp_path / "solaris_test.db")
    tools = build_wakeword_tools(db_path, lambda: "household")

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
    _gatekeeper_captured(db_path, "alex")
    s1 = json.loads(await sample_tool.handler({"uid": "alex", "transcript": "Solaris"}))
    assert s1["remaining"] == 1
    assert s1["say"].endswith("?")

    _gatekeeper_captured(db_path, "alex")
    s2 = json.loads(await sample_tool.handler({"uid": "alex", "transcript": "Solaris"}))
    assert s2["completed"] is True
    assert s2["say"].endswith("?")

    # 3. Trigger training — a queued row is the whole handshake; the engine
    #    cannot run TensorFlow itself.
    _create_queue_table(db_path)
    t1 = json.loads(await trigger_tool.handler({"uid": "alex"}))
    assert t1["training_queued"] is True
    assert "Danke, Alex" in t1["say"]
    assert t1["say"].rstrip().endswith("?")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT uid, status, started_at FROM wakeword_training_runs WHERE id = ?",
            (t1["run_id"],),
        ).fetchone() == ("alex", "queued", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", ["queued", "running"])
async def test_voice_does_not_queue_a_second_run_beside_a_pending_one(
    tmp_path, pending
):
    """One model wakes the box for everyone, so a run already in flight covers
    these recordings too — by voice, by tap, by another resident (#1089). The
    reply has to say that instead of claiming a fresh run started."""
    db_path = str(tmp_path / "solaris_test.db")
    _create_queue_table(db_path)
    tools = build_wakeword_tools(db_path, lambda: "household")
    trigger_tool = next(t for t in tools if t.name == "trigger_wakeword_training")

    first = json.loads(await trigger_tool.handler({"uid": "alex"}))
    assert first["training_queued"] is True
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE wakeword_training_runs SET status = ?", (pending,))
        conn.commit()

    second = json.loads(await trigger_tool.handler({"uid": "marco"}))

    assert second["training_queued"] is False
    assert second["already_running"] is True
    assert second["run_id"] == first["run_id"]
    assert "läuft aber schon" in second["say"]
    assert "eingeplant" not in second["say"]
    assert second["say"].rstrip().endswith("?")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT id, uid, status FROM wakeword_training_runs ORDER BY id"
        ).fetchall() == [(1, "alex", pending)]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["done", "failed"])
async def test_voice_queues_again_once_the_previous_run_finished(tmp_path, terminal):
    db_path = str(tmp_path / "solaris_test.db")
    _create_queue_table(db_path)
    tools = build_wakeword_tools(db_path, lambda: "household")
    trigger_tool = next(t for t in tools if t.name == "trigger_wakeword_training")

    await trigger_tool.handler({"uid": "alex"})
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE wakeword_training_runs SET status = ?", (terminal,))
        conn.commit()

    again = json.loads(await trigger_tool.handler({"uid": "alex"}))

    assert again["training_queued"] is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT id, uid, status FROM wakeword_training_runs ORDER BY id"
        ).fetchall() == [(1, "alex", terminal), (2, "alex", "queued")]


@pytest.mark.asyncio
async def test_training_not_queued_when_table_missing(tmp_path):
    """Without the 0029 queue table there is nothing for the GPU trainer to
    claim. Claiming a run that was never queued would leave the resident
    waiting for a model nobody is building."""
    db_path = str(tmp_path / "solaris_test.db")
    tools = build_wakeword_tools(db_path, lambda: "household")
    trigger_tool = next(t for t in tools if t.name == "trigger_wakeword_training")

    res = json.loads(await trigger_tool.handler({"uid": "alex"}))
    assert res["training_queued"] is False
    assert res["run_id"] is None
    assert "eingeplant" not in res["say"]
    assert "nicht einplanen" in res["say"]
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

    tools = build_wakeword_tools(db_path, lambda: "alex")
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


@pytest.mark.asyncio
async def test_only_the_gatekeeper_counts_a_sample(tmp_path):
    """Captured audio is what counts. When the gatekeeper also started writing
    the `.wav` and bumping the counter, the tool's own increment made every
    spoken turn count twice — the dialog would finish at five real recordings
    while claiming ten."""
    from solaris_chat import wakeword_requests_store

    db_path = str(tmp_path / "count.db")
    tools = build_wakeword_tools(db_path, lambda: "household")
    start_tool = next(t for t in tools if t.name == "start_wakeword_enrollment")
    sample_tool = next(t for t in tools if t.name == "record_wakeword_sample")

    await start_tool.handler({"uid": "alex", "target_count": 10})

    # A turn the gatekeeper did not capture must not advance anything.
    res = json.loads(
        await sample_tool.handler({"uid": "alex", "transcript": "Solaris"})
    )
    assert res["ok"] is False
    assert res["reason"] == "not_captured"
    assert res["say"].rstrip().endswith("?")
    assert wakeword_requests_store.get_request(db_path, "alex")["collected_count"] == 0

    # One capture, one count — the tool reports it without adding a second.
    _gatekeeper_captured(db_path, "alex")
    res = json.loads(
        await sample_tool.handler({"uid": "alex", "transcript": "Solaris"})
    )
    assert res["collected"] == 1
    assert res["remaining"] == 9
    assert wakeword_requests_store.get_request(db_path, "alex")["collected_count"] == 1
