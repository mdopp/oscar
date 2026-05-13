"""skill_drafts: two-phase confirm storage.

Lifecycle:
  pending → confirmed   (on user "/ja", apply runs, row stays for audit)
          → cancelled   (user "/nein")
          → expired     (TTL hit, swept by reviewer cron or `expire_drafts`)

We don't auto-apply expired drafts. The author flow always goes back
through the user.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import uuid
from typing import Any

import asyncpg
from oscar_logging import log

from .apply import ApplyResult, apply_edit
from .validation import validate_edit


DRAFT_DEFAULT_TTL_S = 30 * 60  # 30 min for user-initiated drafts
DRAFT_REVIEWER_TTL_S = 24 * 60 * 60  # 24 h for reviewer-initiated drafts


async def create_draft(
    *,
    dsn: str,
    uid: str,
    skill_name: str,
    proposed_md: str,
    current_md: str | None,
    source: str,
    reason: str | None,
    ttl_s: int | None = None,
) -> str:
    """Validate up-front, then stash for human confirmation. Returns draft id."""
    if source not in ("user", "reviewer"):
        raise ValueError(f"invalid source {source!r}")
    validate_edit(proposed_md, current_md)
    draft_id = str(uuid.uuid4())
    ttl = ttl_s or (
        DRAFT_REVIEWER_TTL_S if source == "reviewer" else DRAFT_DEFAULT_TTL_S
    )
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl)
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            """
            INSERT INTO skill_drafts
              (id, uid, skill_name, proposed_md, current_md, source, reason, expires_at)
            VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8)
            """,
            draft_id,
            uid,
            skill_name,
            proposed_md,
            current_md,
            source,
            reason,
            expires_at,
        )
    finally:
        await conn.close()
    log.info(
        "skill_author.draft.created",
        draft_id=draft_id,
        uid=uid,
        skill=skill_name,
        source=source,
        expires_at=expires_at.isoformat(),
    )
    return draft_id


async def confirm_draft(
    *, dsn: str, skills_local: pathlib.Path, draft_id: str
) -> ApplyResult:
    """Apply a pending draft. Marks it confirmed before the apply runs."""
    conn = await asyncpg.connect(dsn=dsn)
    try:
        row = await conn.fetchrow(
            "SELECT * FROM skill_drafts WHERE id = $1::uuid", draft_id
        )
        if row is None:
            raise LookupError(f"draft {draft_id} not found")
        if row["status"] != "pending":
            raise ValueError(f"draft {draft_id} is {row['status']!r}, not pending")
        if row["expires_at"].replace(tzinfo=dt.timezone.utc) < dt.datetime.now(
            dt.timezone.utc
        ):
            await conn.execute(
                "UPDATE skill_drafts SET status = 'expired' WHERE id = $1::uuid",
                draft_id,
            )
            raise ValueError(f"draft {draft_id} has expired")
        await conn.execute(
            "UPDATE skill_drafts SET status = 'confirmed' WHERE id = $1::uuid",
            draft_id,
        )
    finally:
        await conn.close()

    return await apply_edit(
        dsn=dsn,
        skills_local=skills_local,
        skill_name=row["skill_name"],
        proposed_md=row["proposed_md"],
        source=row["source"],
        reason=row["reason"],
    )


async def cancel_draft(*, dsn: str, draft_id: str) -> None:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            "UPDATE skill_drafts SET status = 'cancelled' "
            "WHERE id = $1::uuid AND status = 'pending'",
            draft_id,
        )
    finally:
        await conn.close()
    log.info("skill_author.draft.cancelled", draft_id=draft_id)


async def expire_drafts(*, dsn: str) -> int:
    """Sweep pending drafts past their expiry. Returns count expired."""
    conn = await asyncpg.connect(dsn=dsn)
    try:
        result = await conn.execute(
            "UPDATE skill_drafts SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at < now()"
        )
    finally:
        await conn.close()
    # asyncpg returns 'UPDATE <count>'
    count = int(result.split()[1]) if result.startswith("UPDATE ") else 0
    if count:
        log.info("skill_author.draft.expired", count=count)
    return count


async def list_pending(*, dsn: str, uid: str | None = None) -> list[dict[str, Any]]:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        if uid:
            rows = await conn.fetch(
                """
                SELECT id, uid, skill_name, source, reason, created_at, expires_at
                FROM skill_drafts
                WHERE status = 'pending' AND uid = $1
                ORDER BY created_at DESC
                """,
                uid,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, uid, skill_name, source, reason, created_at, expires_at
                FROM skill_drafts
                WHERE status = 'pending'
                ORDER BY created_at DESC
                """
            )
    finally:
        await conn.close()
    return [dict(r) for r in rows]
