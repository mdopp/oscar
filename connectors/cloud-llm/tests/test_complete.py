"""Tests for the cloud-LLM `complete` tool.

We mock the upstream HTTP API via pytest-httpx and stub the audit-writer
so no Postgres is needed. The point is to pin: response parsing, the
audit payload shape, and that a 5xx upstream raises and logs.
"""

import os
import re
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CONNECTORS_BEARER", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic")
os.environ.setdefault("GOOGLE_API_KEY", "test-google")
os.environ.setdefault("POSTGRES_DSN", "postgresql://oscar:test@localhost:5432/oscar")

from cloud_llm.tools.complete import CompleteInput, run


class _Ctx:
    class _Rc:
        meta = {"trace_id": "11111111-1111-1111-1111-111111111111", "uid": "michael"}

    request_context = _Rc()


@pytest.mark.asyncio
async def test_anthropic_happy_path(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r".*/v1/messages"),
        json={
            "content": [{"type": "text", "text": "The capital of France is Paris."}],
            "usage": {"input_tokens": 25, "output_tokens": 8},
        },
    )

    with patch("cloud_llm.tools.complete.audit.record", new=AsyncMock()) as mock_audit:
        result = await run(
            CompleteInput(
                vendor="anthropic",
                model="claude-sonnet-4",
                prompt="What is the capital of France?",
                router_score=0.7,
                escalation_reason="multi-step-plan",
            ),
            _Ctx(),
        )
        assert "Paris" in result.text
        assert result.input_tokens == 25
        assert result.output_tokens == 8
        assert result.cost_micro_usd is not None
        # Audit-writer received the prompt + response and the routing metadata.
        mock_audit.assert_awaited_once()
        kwargs = mock_audit.await_args.kwargs
        assert kwargs["vendor"] == "anthropic"
        assert kwargs["uid"] == "michael"
        assert kwargs["router_score"] == 0.7
        assert kwargs["escalation_reason"] == "multi-step-plan"


@pytest.mark.asyncio
async def test_google_happy_path(httpx_mock):
    httpx_mock.add_response(
        url=re.compile(r".*/models/gemini-2.5-flash:generateContent.*"),
        json={
            "candidates": [{"content": {"parts": [{"text": "42 is the answer."}]}}],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 5},
        },
    )

    with patch("cloud_llm.tools.complete.audit.record", new=AsyncMock()):
        result = await run(
            CompleteInput(vendor="google", model="gemini-2.5-flash", prompt="hi"),
            _Ctx(),
        )
        assert "42" in result.text
        assert result.input_tokens == 12


@pytest.mark.asyncio
async def test_upstream_5xx_raises(httpx_mock):
    httpx_mock.add_response(status_code=503, text="overloaded")

    with patch("cloud_llm.tools.complete.audit.record", new=AsyncMock()) as mock_audit:
        with pytest.raises(Exception):
            await run(
                CompleteInput(vendor="anthropic", model="claude-sonnet-4", prompt="hi"),
                _Ctx(),
            )
        # On error we don't write to cloud_audit — the request never completed.
        mock_audit.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_vendor_rejected():
    with pytest.raises(Exception):
        await run(
            CompleteInput.model_validate(
                {"vendor": "openai", "model": "gpt-4o", "prompt": "hi"}
            ),
            _Ctx(),
        )
