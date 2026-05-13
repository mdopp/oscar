"""Async write-side for skill_runs + skill_corrections.

The detector is pattern-based, not ML — we lean on the fact that German
and English correction openings are extremely consistent ("Nein, ich
meinte…", "Stopp", "Nicht das"). False positives are mostly harmless: a
spurious 'pending' correction sits in the table until the reviewer (#41)
gates it out via the k=3 aggregation.
"""

from __future__ import annotations

import re
import uuid
from typing import Iterable

import asyncpg
from oscar_logging import log


NEGATION_PREFIXES: tuple[str, ...] = (
    "nein",
    "no",
    "stopp",
    "stop",
    "doch nicht",
    "lass das",
    "falsch",
    "moment",
    "quatsch",
    "wait",
    "nicht das",
)


_CORRECTION_WINDOW_S = 30


def looks_like_correction(
    utterance: str, prefixes: Iterable[str] = NEGATION_PREFIXES
) -> bool:
    """Pure check — does the utterance start with a negation prefix?"""
    text = utterance.strip().lower()
    if not text:
        return False
    for prefix in prefixes:
        # Match the prefix as a *word* (or at start of text with optional comma).
        # Avoids matching "stopper" or "neinhilft" for the rare edge cases.
        pattern = rf"^{re.escape(prefix)}(?:\b|[,!.\s])"
        if re.match(pattern, text):
            return True
    return False


async def append_run(
    dsn: str,
    *,
    trace_id: str,
    uid: str,
    endpoint: str,
    skill_name: str,
    utterance: str,
    response: str | None,
    outcome: str,
) -> str:
    """Insert one skill_runs row, return the new UUID."""
    if outcome not in ("ok", "error", "no_skill"):
        raise ValueError(f"invalid outcome {outcome!r}")
    run_id = str(uuid.uuid4())
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            """
            INSERT INTO skill_runs
              (id, trace_id, uid, endpoint, skill_name, utterance, response, outcome)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8)
            """,
            run_id,
            trace_id,
            uid,
            endpoint,
            skill_name,
            utterance,
            response,
            outcome,
        )
    finally:
        await conn.close()
    log.info(
        "skill_runs.append",
        trace_id=trace_id,
        uid=uid,
        skill=skill_name,
        outcome=outcome,
        run_id=run_id,
    )
    return run_id


async def detect_correction(
    dsn: str,
    *,
    uid: str,
    endpoint: str,
    utterance: str,
    window_s: int = _CORRECTION_WINDOW_S,
) -> str | None:
    """If `utterance` is a correction of a recent skill_run, write a row.

    Returns the new skill_corrections.id, or None if no correction was logged.
    """
    if not looks_like_correction(utterance):
        return None
    conn = await asyncpg.connect(dsn=dsn)
    try:
        row = await conn.fetchrow(
            """
            SELECT id, skill_name, ts FROM skill_runs
            WHERE uid = $1 AND endpoint = $2
              AND ts > now() - ($3 || ' seconds')::interval
            ORDER BY ts DESC
            LIMIT 1
            """,
            uid,
            endpoint,
            str(window_s),
        )
        if row is None:
            return None
        corr_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO skill_corrections (id, run_id, correction_utterance)
            VALUES ($1::uuid, $2::uuid, $3)
            """,
            corr_id,
            row["id"],
            utterance,
        )
    finally:
        await conn.close()
    log.info(
        "skill_runs.correction",
        run_id=str(row["id"]),
        skill=row["skill_name"],
        uid=uid,
        correction_id=corr_id,
    )
    return corr_id
