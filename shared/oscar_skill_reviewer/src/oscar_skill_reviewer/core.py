"""Aggregation + rate-limit checks for the autonomous reviewer.

Group-key normalization is intentionally crude: lower-case first 5
tokens of utterance + first 5 tokens of correction. Anything fancier
(stemming, embedding-based clustering) doesn't pay off until you've
seen real correction data. The k=3 threshold compensates for the
noisy grouping.
"""

from __future__ import annotations

import dataclasses
import re
import uuid

import asyncpg
from oscar_logging import log


K_THRESHOLD = 3
REVIEWER_RATE_LIMIT_S = 24 * 60 * 60
USER_INTERFERE_WINDOW_S = 24 * 60 * 60


_TOKEN_PREFIX_LEN = 5


@dataclasses.dataclass(frozen=True)
class CorrectionGroup:
    skill_name: str
    utterance_prefix: str
    correction_prefix: str
    count: int
    sample_run_id: str
    correction_ids: tuple[str, ...]
    sample_utterance: str
    sample_correction: str


def _normalize_prefix(text: str) -> str:
    """Lowercase first ~5 word tokens. Strips punctuation, collapses ws."""
    tokens = re.findall(r"\w+", text.lower())
    return " ".join(tokens[:_TOKEN_PREFIX_LEN])


async def aggregate_corrections(
    *, dsn: str, window_days: int = 14, k: int = K_THRESHOLD
) -> list[CorrectionGroup]:
    """Group pending corrections, return only groups with count >= k."""
    conn = await asyncpg.connect(dsn=dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT
              c.id AS correction_id,
              c.run_id,
              c.correction_utterance,
              r.skill_name,
              r.utterance
            FROM skill_corrections c
            JOIN skill_runs r ON r.id = c.run_id
            WHERE c.status = 'pending'
              AND c.ts > now() - ($1 || ' days')::interval
            ORDER BY c.ts ASC
            """,
            str(window_days),
        )
    finally:
        await conn.close()

    buckets: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        key = (
            row["skill_name"],
            _normalize_prefix(row["utterance"]),
            _normalize_prefix(row["correction_utterance"]),
        )
        buckets.setdefault(key, []).append(dict(row))

    groups: list[CorrectionGroup] = []
    for (skill_name, u_pref, c_pref), entries in buckets.items():
        if len(entries) < k:
            continue
        sample = entries[0]
        groups.append(
            CorrectionGroup(
                skill_name=skill_name,
                utterance_prefix=u_pref,
                correction_prefix=c_pref,
                count=len(entries),
                sample_run_id=str(sample["run_id"]),
                correction_ids=tuple(str(e["correction_id"]) for e in entries),
                sample_utterance=sample["utterance"],
                sample_correction=sample["correction_utterance"],
            )
        )
    log.info(
        "skill_reviewer.aggregate",
        window_days=window_days,
        k=k,
        eligible_groups=len(groups),
    )
    return groups


async def can_apply_now(*, dsn: str, skill_name: str) -> bool:
    """Rate-limit + user-interference check before an autonomous apply."""
    conn = await asyncpg.connect(dsn=dsn)
    try:
        row = await conn.fetchrow(
            """
            SELECT
              EXISTS (
                SELECT 1 FROM skill_edits
                WHERE skill_name = $1 AND source = 'reviewer'
                  AND applied_at > now() - ($2 || ' seconds')::interval
              ) AS reviewer_recent,
              EXISTS (
                SELECT 1 FROM skill_edits
                WHERE skill_name = $1 AND source = 'user'
                  AND applied_at > now() - ($3 || ' seconds')::interval
              ) AS user_recent
            """,
            skill_name,
            str(REVIEWER_RATE_LIMIT_S),
            str(USER_INTERFERE_WINDOW_S),
        )
    finally:
        await conn.close()
    blocked = bool(row["reviewer_recent"]) or bool(row["user_recent"])
    if blocked:
        log.info(
            "skill_reviewer.rate_limited",
            skill=skill_name,
            reviewer_recent=row["reviewer_recent"],
            user_recent=row["user_recent"],
        )
    return not blocked


async def mark_group_edited(*, dsn: str, group: CorrectionGroup) -> None:
    """Flag every correction id in `group` as `edited`."""
    if not group.correction_ids:
        return
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            "UPDATE skill_corrections SET status = 'edited' WHERE id = ANY($1::uuid[])",
            list(group.correction_ids),
        )
    finally:
        await conn.close()
    log.info(
        "skill_reviewer.marked_edited",
        skill=group.skill_name,
        count=len(group.correction_ids),
    )


async def mark_corrections_dismissed(*, dsn: str, correction_ids: list[str]) -> None:
    """Used when the constraint check rejects the group post-aggregation."""
    if not correction_ids:
        return
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            "UPDATE skill_corrections SET status = 'dismissed' "
            "WHERE id = ANY($1::uuid[])",
            correction_ids,
        )
    finally:
        await conn.close()


def _is_uuid(text: str) -> bool:
    try:
        uuid.UUID(text)
        return True
    except ValueError:
        return False
