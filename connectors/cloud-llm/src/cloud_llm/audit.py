"""cloud_audit Postgres writer.

Fail-safe: any audit-write error is logged but never propagated to the
caller — we'd rather the LLM call return a useful response to the user
than block on audit-storage problems. The operational stdout log keeps
a trail when this happens, so we can detect chronic audit-write failures
via ServiceBay-MCP `get_container_logs`.
"""

from __future__ import annotations

import os
from typing import Any

import asyncpg
from oscar_logging import log


_DEBUG = os.environ.get("OSCAR_DEBUG_MODE", "false").lower() in ("true", "1", "yes")


async def record(
    dsn: str,
    *,
    job_id: str,
    trace_id: str,
    uid: str,
    vendor: str,
    model: str,
    prompt: str,
    response_text: str,
    prompt_tokens: int,
    response_tokens: int,
    latency_ms: int,
    cost_micro_usd: int | None,
    router_score: float | None,
    escalation_reason: str | None,
) -> None:
    """Insert one row into cloud_audit. Body fields filled only in debug mode."""

    prompt_hash = _sha256(prompt)
    body_prompt = prompt if _DEBUG else None
    body_response = response_text if _DEBUG else None

    try:
        conn = await asyncpg.connect(dsn=dsn, timeout=5)
    except Exception as exc:  # noqa: BLE001
        log.error("cloud_audit.connect_failed", trace_id=trace_id, error=str(exc))
        return

    try:
        await conn.execute(
            """
            INSERT INTO cloud_audit (
                id, trace_id, uid, vendor,
                prompt_hash, prompt_length, response_length,
                latency_ms, cost_usd_micro,
                router_score, escalation_reason,
                prompt_fulltext, response_fulltext
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4,
                $5, $6, $7,
                $8, $9,
                $10, $11,
                $12, $13
            )
            """,
            job_id,
            trace_id,
            uid,
            f"{vendor}:{model}",
            prompt_hash,
            prompt_tokens,
            response_tokens,
            latency_ms,
            cost_micro_usd,
            router_score,
            escalation_reason,
            body_prompt,
            body_response,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("cloud_audit.insert_failed", trace_id=trace_id, error=str(exc))
    finally:
        await conn.close()


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _coerce_meta(meta: Any) -> dict:
    """Best-effort extraction of metadata from MCP request context."""
    if meta is None:
        return {}
    if isinstance(meta, dict):
        return meta
    if hasattr(meta, "model_dump"):
        return meta.model_dump()
    return {}
