"""Generic audit query — one entry-point, switches per `stream` name.

Each stream has its own SQL builder so the public API stays uniform but
each table can use the indexes it actually has. PII (prompt/response
fulltext on cloud_audit) is masked unless `OSCAR_DEBUG_MODE=true`.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Protocol


class _Connection(Protocol):
    async def fetch(self, query: str, *args: Any) -> list[Any]: ...


class Pool(Protocol):
    async def acquire(self) -> _Connection: ...
    async def release(self, conn: _Connection) -> None: ...


def _debug_active() -> bool:
    return os.environ.get("OSCAR_DEBUG_MODE", "false").lower() in ("true", "1", "yes")


def supported_streams() -> list[str]:
    return ["cloud_audit"]


async def query(
    pool: Pool,
    *,
    stream: str,
    since: datetime | None = None,
    until: datetime | None = None,
    uid: str | None = None,
    vendor: str | None = None,
    trace_id: str | None = None,
    min_cost_micro_usd: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return rows from the named audit stream, newest first."""

    if stream not in supported_streams():
        raise ValueError(f"unknown stream {stream!r}; supported: {supported_streams()}")
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")

    builder = _BUILDERS[stream]
    sql, args = builder(
        since=since,
        until=until,
        uid=uid,
        vendor=vendor,
        trace_id=trace_id,
        min_cost_micro_usd=min_cost_micro_usd,
        limit=limit,
    )

    conn = await pool.acquire()
    try:
        rows = await conn.fetch(sql, *args)
    finally:
        await pool.release(conn)

    return [_postprocess(stream, dict(r)) for r in rows]


# ---- per-stream SQL builders -----------------------------------------


def _build_cloud_audit(
    *, since, until, uid, vendor, trace_id, min_cost_micro_usd, limit, **_
):
    where: list[str] = []
    args: list[Any] = []

    def add(clause: str, value: Any) -> None:
        args.append(value)
        where.append(clause.format(idx=len(args)))

    if since is not None:
        add("ts >= ${idx}", _aware(since))
    if until is not None:
        add("ts <= ${idx}", _aware(until))
    if uid is not None:
        add("uid = ${idx}", uid)
    if vendor is not None:
        add("vendor LIKE ${idx} || ':%'", vendor)
    if trace_id is not None:
        add("trace_id = ${idx}::uuid", trace_id)
    if min_cost_micro_usd is not None:
        add("cost_usd_micro >= ${idx}", min_cost_micro_usd)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT
            id, ts, trace_id, uid, vendor,
            prompt_hash, prompt_length, response_length,
            latency_ms, cost_usd_micro,
            router_score, escalation_reason,
            prompt_fulltext, response_fulltext
        FROM cloud_audit
        {where_sql}
        ORDER BY ts DESC
        LIMIT {limit}
    """
    return sql, args


_BUILDERS = {
    "cloud_audit": _build_cloud_audit,
}


# ---- post-processing ------------------------------------------------------


def _postprocess(stream: str, row: dict[str, Any]) -> dict[str, Any]:
    if stream == "cloud_audit" and not _debug_active():
        # Strip fulltext fields outside debug mode (PII).
        row["prompt_fulltext"] = None
        row["response_fulltext"] = None
        row["_pii_redacted"] = True
    return row


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
