"""`complete` — the single MCP tool the cloud-LLM connector exposes."""

from __future__ import annotations

import time
import uuid
from typing import Literal

from oscar_logging import log
from pydantic import BaseModel, Field

from .. import audit
from ..config import settings
from ..pricing import cost_micro_usd
from ..providers import AnthropicProvider, GoogleProvider, Provider


def _provider(vendor: str) -> Provider:
    if vendor == "anthropic":
        return AnthropicProvider(
            api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url
        )
    if vendor == "google":
        return GoogleProvider(
            api_key=settings.google_api_key, base_url=settings.google_base_url
        )
    raise ValueError(f"Unknown vendor '{vendor}', expected 'anthropic' or 'google'.")


class CompleteInput(BaseModel):
    vendor: Literal["anthropic", "google"] = Field(
        ..., description="Cloud LLM provider to call."
    )
    model: str = Field(
        ...,
        description="Provider-side model id, e.g. 'claude-sonnet-4' or 'gemini-2.5-flash'.",
    )
    prompt: str = Field(
        ..., description="User-turn prompt. System instruction goes in `system`."
    )
    system: str | None = Field(None, description="Optional system instruction.")
    max_tokens: int = Field(
        1024,
        ge=1,
        le=8192,
        description="Maximum response tokens. Provider may produce less.",
    )
    router_score: float | None = Field(
        None,
        ge=0.0,
        le=1.0,
        description="Optional Gemma-1B router complexity score that justified the escalation.",
    )
    escalation_reason: str | None = Field(
        None,
        description="Free-text note about why HERMES routed here (e.g. 'multi-step-plan', 'long-context').",
    )


class CompleteOutput(BaseModel):
    text: str
    vendor: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_micro_usd: int | None


async def run(input: CompleteInput, ctx) -> CompleteOutput:
    meta = audit._coerce_meta(
        getattr(getattr(ctx, "request_context", None), "meta", None)
    )
    trace_id = meta.get("trace_id") or str(uuid.uuid4())
    uid = meta.get("uid") or "unknown"
    job_id = str(uuid.uuid4())

    log.info(
        "connector.call",
        event_type="cloud_llm.complete",
        trace_id=trace_id,
        uid=uid,
        vendor=input.vendor,
        model=input.model,
        prompt_chars=len(input.prompt),
        router_score=input.router_score,
        reason=input.escalation_reason,
    )

    provider = _provider(input.vendor)
    started = time.monotonic()
    try:
        result = await provider.complete(
            model=input.model,
            prompt=input.prompt,
            system=input.system,
            max_tokens=input.max_tokens,
        )
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.monotonic() - started) * 1000)
        log.error(
            "connector.external_error",
            event_type="cloud_llm.complete",
            trace_id=trace_id,
            vendor=input.vendor,
            model=input.model,
            latency_ms=latency_ms,
            error=str(exc),
        )
        raise

    latency_ms = int((time.monotonic() - started) * 1000)
    cost = cost_micro_usd(
        input.vendor, input.model, result.input_tokens, result.output_tokens
    )

    log.info(
        "connector.response",
        event_type="cloud_llm.complete",
        trace_id=trace_id,
        vendor=input.vendor,
        model=input.model,
        latency_ms=latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_micro_usd=cost,
    )

    await audit.record(
        settings.postgres_dsn,
        job_id=job_id,
        trace_id=trace_id,
        uid=uid,
        vendor=input.vendor,
        model=input.model,
        prompt=input.prompt
        if input.system is None
        else f"[SYSTEM]\n{input.system}\n[USER]\n{input.prompt}",
        response_text=result.text,
        prompt_tokens=result.input_tokens,
        response_tokens=result.output_tokens,
        latency_ms=latency_ms,
        cost_micro_usd=cost,
        router_score=input.router_score,
        escalation_reason=input.escalation_reason,
    )

    return CompleteOutput(
        text=result.text,
        vendor=input.vendor,
        model=input.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=latency_ms,
        cost_micro_usd=cost,
    )
