"""Library tests against a FakePool — no real Postgres required."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("OSCAR_COMPONENT", "test")

from oscar_time_jobs import add, cancel, fire, list_for
from oscar_time_jobs.core import _parse_iso8601_duration


# ---- FakePool ----------------------------------------------------------


class _FakeConn:
    def __init__(self, store: dict):
        self.store = store

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO time_jobs"):
            job_id, kind, uid, label, fires_at, rrule, duration_set, endpoint = args
            self.store[job_id] = {
                "id": job_id,
                "kind": kind,
                "owner_uid": uid,
                "label": label,
                "fires_at": fires_at,
                "rrule": rrule,
                "duration_set": duration_set,
                "target_endpoint": endpoint,
                "state": "armed",
            }
            return "INSERT 0 1"
        if q.startswith("UPDATE time_jobs SET state = 'cancelled' WHERE id ="):
            job_id, uid = args
            row = self.store.get(job_id)
            if row and row["owner_uid"] == uid and row["state"] in ("armed", "snoozed"):
                row["state"] = "cancelled"
                return "UPDATE 1"
            return "UPDATE 0"
        if q.startswith("UPDATE time_jobs SET state = 'cancelled' WHERE label ="):
            label, uid = args
            count = 0
            for row in self.store.values():
                if (
                    row["label"] == label
                    and row["owner_uid"] == uid
                    and row["state"] in ("armed", "snoozed")
                ):
                    row["state"] = "cancelled"
                    count += 1
            return f"UPDATE {count}"
        if q.startswith("UPDATE time_jobs SET state = 'armed', fires_at ="):
            new_fires, job_id = args
            self.store[job_id]["state"] = "armed"
            self.store[job_id]["fires_at"] = new_fires
            return "UPDATE 1"
        if q.startswith("UPDATE time_jobs SET state = 'done' WHERE id ="):
            (job_id,) = args
            self.store[job_id]["state"] = "done"
            return "UPDATE 1"
        raise AssertionError(f"FakeConn: unhandled execute: {q!r}")

    async def fetchrow(self, query: str, *args):
        if "WHERE id = $1::uuid" in query:
            return self.store.get(args[0])
        raise AssertionError(f"FakeConn: unhandled fetchrow: {query!r}")

    async def fetch(self, query: str, *args):
        q = " ".join(query.split())
        if "owner_uid = $1 AND kind = $2" in q:
            uid, kind = args
            return [
                row
                for row in self.store.values()
                if row["owner_uid"] == uid
                and row["kind"] == kind
                and row["state"] in ("armed", "snoozed")
            ]
        if "owner_uid = $1" in q:
            (uid,) = args
            return [
                row
                for row in self.store.values()
                if row["owner_uid"] == uid and row["state"] in ("armed", "snoozed")
            ]
        raise AssertionError(f"FakeConn: unhandled fetch: {q!r}")


class FakePool:
    def __init__(self):
        self.store: dict = {}

    async def acquire(self):
        return _FakeConn(self.store)

    async def release(self, _conn):
        return None


# ---- tests --------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_timer_inserts_armed_row():
    pool = FakePool()
    now = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    result = await add(
        pool,
        kind="timer",
        owner_uid="michael",
        target_endpoint="voice-pe:office",
        duration="PT5M",
        label="Pizza",
        now=now,
    )
    assert result.kind == "timer"
    assert result.fires_at == datetime(2026, 5, 12, 10, 5, tzinfo=timezone.utc)
    row = pool.store[result.job_id]
    assert row["state"] == "armed"
    assert row["label"] == "Pizza"


@pytest.mark.asyncio
async def test_add_rejects_no_time_arg():
    pool = FakePool()
    with pytest.raises(ValueError):
        await add(
            pool, kind="timer", owner_uid="michael", target_endpoint="voice-pe:office"
        )


@pytest.mark.asyncio
async def test_fire_one_shot_moves_to_done():
    pool = FakePool()
    now = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    res = await add(
        pool,
        kind="timer",
        owner_uid="michael",
        target_endpoint="signal:+49150",
        duration="PT5M",
        label="Tee",
        now=now,
    )
    payload = await fire(pool, job_id=res.job_id)
    assert payload["ok"] is True
    assert payload["target_endpoint"] == "signal:+49150"
    assert "Tee" in payload["message"]
    assert pool.store[res.job_id]["state"] == "done"


@pytest.mark.asyncio
async def test_fire_rrule_re_arms():
    pool = FakePool()
    now = datetime(2026, 5, 12, 6, 30, tzinfo=timezone.utc)
    res = await add(
        pool,
        kind="alarm",
        owner_uid="michael",
        target_endpoint="voice-pe:bedroom",
        rrule="FREQ=DAILY;BYHOUR=6;BYMINUTE=30;BYSECOND=0",
        label="Wake",
        now=now,
    )
    # Force the row's state to firing semantics: call fire at "now"
    payload = await fire(pool, job_id=res.job_id, now=now)
    assert payload["ok"] is True
    # rrule jobs re-arm with a future fires_at
    assert pool.store[res.job_id]["state"] == "armed"
    assert pool.store[res.job_id]["fires_at"] > now


@pytest.mark.asyncio
async def test_fire_voice_pe_flags_pending():
    pool = FakePool()
    res = await add(
        pool,
        kind="timer",
        owner_uid="michael",
        target_endpoint="voice-pe:office",
        duration="PT1M",
    )
    payload = await fire(pool, job_id=res.job_id)
    assert payload["delivery_pending"] == "voice-pe"


@pytest.mark.asyncio
async def test_cancel_by_label_only_owners_rows():
    pool = FakePool()
    a = await add(
        pool,
        kind="timer",
        owner_uid="michael",
        target_endpoint="signal:+49",
        duration="PT5M",
        label="Pizza",
    )
    b = await add(
        pool,
        kind="timer",
        owner_uid="anna",
        target_endpoint="signal:+50",
        duration="PT5M",
        label="Pizza",
    )
    count = await cancel(pool, owner_uid="michael", label="Pizza")
    assert count == 1
    assert pool.store[a.job_id]["state"] == "cancelled"
    assert pool.store[b.job_id]["state"] == "armed"


@pytest.mark.asyncio
async def test_list_filters_by_owner_and_state():
    pool = FakePool()
    a = await add(
        pool,
        kind="timer",
        owner_uid="michael",
        target_endpoint="signal:+49",
        duration="PT5M",
        label="Pizza",
    )
    b = await add(
        pool,
        kind="alarm",
        owner_uid="michael",
        target_endpoint="voice-pe:office",
        at=datetime(2026, 5, 13, 7, tzinfo=timezone.utc),
    )
    await fire(pool, job_id=a.job_id)  # one-shot → done

    rows = await list_for(pool, owner_uid="michael")
    ids = [r["id"] for r in rows]
    assert b.job_id in ids
    assert a.job_id not in ids  # done jobs are excluded


def test_parse_iso8601_duration_examples():
    from datetime import timedelta

    assert _parse_iso8601_duration("PT5M") == timedelta(minutes=5)
    assert _parse_iso8601_duration("PT1H30M") == timedelta(hours=1, minutes=30)
    assert _parse_iso8601_duration("P1D") == timedelta(days=1)
    with pytest.raises(ValueError):
        _parse_iso8601_duration("5 minutes")
