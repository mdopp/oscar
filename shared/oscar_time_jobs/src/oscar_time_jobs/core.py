"""Async API for the `time_jobs` table.

Pool is injected (asyncpg or a fake one in tests) so the same code
runs against real Postgres + against a stub `FakePool` in the test
suite, with no integration setup.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from oscar_logging import log

from .rrule import next_after, parse_rrule


class _Connection(Protocol):
    async def execute(self, query: str, *args: Any) -> Any: ...
    async def fetch(self, query: str, *args: Any) -> list[Any]: ...
    async def fetchrow(self, query: str, *args: Any) -> Any: ...


class Pool(Protocol):
    async def acquire(self) -> _Connection: ...
    async def release(self, conn: _Connection) -> None: ...


@dataclass(frozen=True)
class NextFire:
    """Result of `add`: enough info for the caller to schedule a HERMES cron."""

    job_id: str
    fires_at: datetime
    kind: str
    label: str | None
    target_endpoint: str


# ---- public API ---------------------------------------------------------


async def add(
    pool: Pool,
    *,
    kind: str,
    owner_uid: str,
    target_endpoint: str,
    duration: str | None = None,
    at: datetime | None = None,
    rrule: str | None = None,
    label: str | None = None,
    now: datetime | None = None,
) -> NextFire:
    """Insert one armed row. Exactly one of (duration, at, rrule) must be set."""

    if kind not in ("timer", "alarm"):
        raise ValueError(f"kind must be 'timer' or 'alarm', got {kind!r}")
    if sum(x is not None for x in (duration, at, rrule)) != 1:
        raise ValueError("exactly one of duration / at / rrule must be provided")

    now = now or datetime.now(timezone.utc)
    duration_set: timedelta | None = None
    if duration is not None:
        duration_set = _parse_iso8601_duration(duration)
        fires_at = now + duration_set
    elif at is not None:
        fires_at = _aware(at)
    else:
        assert rrule is not None
        fires_at = next_after(parse_rrule(rrule), now)

    job_id = str(uuid.uuid4())

    conn = await pool.acquire()
    try:
        await conn.execute(
            """
            INSERT INTO time_jobs (
                id, kind, owner_uid, label,
                fires_at, rrule, duration_set,
                target_endpoint, state
            ) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, 'armed')
            """,
            job_id,
            kind,
            owner_uid,
            label,
            fires_at,
            rrule,
            duration_set,
            target_endpoint,
        )
    finally:
        await pool.release(conn)

    log.info(
        "skill.time_jobs.add",
        kind=kind,
        owner_uid=owner_uid,
        label=label,
        endpoint=target_endpoint,
        fires_at=fires_at.isoformat(),
        job_id=job_id,
    )
    return NextFire(
        job_id=job_id,
        fires_at=fires_at,
        kind=kind,
        label=label,
        target_endpoint=target_endpoint,
    )


async def cancel(
    pool: Pool, *, owner_uid: str, job_id: str | None = None, label: str | None = None
) -> int:
    """Mark matching armed/snoozed jobs as cancelled. Returns affected row count."""

    if not (job_id or label):
        raise ValueError("cancel needs job_id or label")

    conn = await pool.acquire()
    try:
        if job_id:
            rows = await conn.execute(
                """
                UPDATE time_jobs
                SET state = 'cancelled'
                WHERE id = $1::uuid AND owner_uid = $2 AND state IN ('armed','snoozed')
                """,
                job_id,
                owner_uid,
            )
        else:
            rows = await conn.execute(
                """
                UPDATE time_jobs
                SET state = 'cancelled'
                WHERE label = $1 AND owner_uid = $2 AND state IN ('armed','snoozed')
                """,
                label,
                owner_uid,
            )
    finally:
        await pool.release(conn)

    count = _affected_rows(rows)
    log.info(
        "skill.time_jobs.cancel",
        owner_uid=owner_uid,
        label=label,
        job_id=job_id,
        cancelled=count,
    )
    return count


async def list_for(
    pool: Pool, *, owner_uid: str, kind: str | None = None
) -> list[dict[str, Any]]:
    """Return currently-active jobs (armed + snoozed) for the owner."""

    conn = await pool.acquire()
    try:
        if kind:
            rows = await conn.fetch(
                """
                SELECT id, kind, label, fires_at, rrule, target_endpoint, state
                FROM time_jobs
                WHERE owner_uid = $1 AND kind = $2 AND state IN ('armed','snoozed')
                ORDER BY fires_at
                """,
                owner_uid,
                kind,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, kind, label, fires_at, rrule, target_endpoint, state
                FROM time_jobs
                WHERE owner_uid = $1 AND state IN ('armed','snoozed')
                ORDER BY fires_at
                """,
                owner_uid,
            )
    finally:
        await pool.release(conn)
    return [dict(r) for r in rows]


async def fire(
    pool: Pool, *, job_id: str, now: datetime | None = None
) -> dict[str, Any]:
    """Mark a job as firing, return a delivery payload, advance the state.

    For one-shot jobs (no rrule): state goes armed → firing → done.
    For RRULE jobs: state stays armed, fires_at advances to next occurrence.
    """

    now = now or datetime.now(timezone.utc)
    conn = await pool.acquire()
    try:
        row = await conn.fetchrow(
            "SELECT id, kind, owner_uid, label, fires_at, rrule, target_endpoint, state FROM time_jobs WHERE id = $1::uuid",
            job_id,
        )
        if row is None:
            log.warn("skill.time_jobs.fire.unknown", job_id=job_id)
            return {"ok": False, "reason": "unknown_job", "job_id": job_id}
        if row["state"] not in ("armed", "snoozed"):
            log.info("skill.time_jobs.fire.skipped", job_id=job_id, state=row["state"])
            return {"ok": False, "reason": f"state_{row['state']}", "job_id": job_id}

        message = _format_fire_message(row)

        if row["rrule"]:
            next_fire = next_after(parse_rrule(row["rrule"]), now, inclusive=False)
            await conn.execute(
                "UPDATE time_jobs SET state = 'armed', fires_at = $1 WHERE id = $2::uuid",
                next_fire,
                job_id,
            )
            advance = next_fire.isoformat()
        else:
            await conn.execute(
                "UPDATE time_jobs SET state = 'done' WHERE id = $1::uuid",
                job_id,
            )
            advance = None
    finally:
        await pool.release(conn)

    payload: dict[str, Any] = {
        "ok": True,
        "kind": row["kind"],
        "label": row["label"],
        "target_endpoint": row["target_endpoint"],
        "message": message,
        "next_fire": advance,
    }
    if row["target_endpoint"].startswith("voice-pe:"):
        payload["delivery_pending"] = "voice-pe"
    log.info(
        "skill.time_jobs.fire",
        job_id=job_id,
        kind=row["kind"],
        endpoint=row["target_endpoint"],
        rrule_repeat=advance is not None,
    )
    return payload


# ---- helpers ------------------------------------------------------------


_ISO_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _parse_iso8601_duration(text: str) -> timedelta:
    """Tiny ISO-8601 duration parser — covers the subset our skills emit (PT5M, PT1H30M, P1D, …)."""
    m = _ISO_DURATION.match(text)
    if not m or text in ("P", "PT"):
        raise ValueError(f"unrecognised ISO-8601 duration: {text!r}")
    days, hours, minutes, seconds = (int(x) if x else 0 for x in m.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _affected_rows(execute_result: Any) -> int:
    # asyncpg's execute returns a status string like 'UPDATE 1'. Fakes may return ints.
    if isinstance(execute_result, int):
        return execute_result
    if isinstance(execute_result, str):
        parts = execute_result.split()
        if parts and parts[-1].isdigit():
            return int(parts[-1])
    return 0


def _format_fire_message(row: Any) -> str:
    label = row["label"]
    if row["kind"] == "timer":
        return f"Dein {label}-Timer ist um." if label else "Dein Timer ist um."
    return f"{label} — es ist Zeit." if label else "Es ist Zeit."
