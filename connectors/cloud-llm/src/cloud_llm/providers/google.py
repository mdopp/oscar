"""Google Gemini provider — POST /v1beta/models/{model}:generateContent."""

from __future__ import annotations

import httpx

from .base import Provider, ProviderResult


class GoogleProvider(Provider):
    name = "google"

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
                "GOOGLE_API_KEY is empty — Google provider not configured."
            )

        payload: dict = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self._base_url}/models/{model}:generateContent",
                params={"key": self._api_key},
                headers={"content-type": "application/json"},
                json=payload,
            )
        response.raise_for_status()
        data = response.json()

        candidates = data.get("candidates", [])
        text = ""
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata", {})
        return ProviderResult(
            text=text,
            input_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)),
        )
