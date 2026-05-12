"""Anthropic Claude provider — POST /v1/messages."""

from __future__ import annotations

import httpx

from .base import Provider, ProviderResult


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, api_key: str, base_url: str):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        system: str | None,
        max_tokens: int,
    ) -> ProviderResult:
        if not self._api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is empty — Anthropic provider not configured."
            )

        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self._base_url}/messages",
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
        response.raise_for_status()
        data = response.json()

        text_parts = [
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ]
        usage = data.get("usage", {})
        return ProviderResult(
            text="".join(text_parts),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )
