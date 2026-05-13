"""Thin async wrapper around signal-cli-rest-api.

Only the two endpoints we actually need:
- GET /v1/receive/<number> — pulls pending messages (long-poll)
- POST /v2/send         — sends a text message to one recipient

Anything fancier (groups, attachments, reactions) is intentionally out
of scope for Phase 1.
"""

from __future__ import annotations

from typing import Any

import httpx


class SignalRestError(Exception):
    """Non-2xx response from signal-cli-rest-api."""


class SignalRestClient:
    def __init__(self, base_url: str, account: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._account = account
        self._timeout = timeout

    async def receive(self) -> list[dict[str, Any]]:
        url = f"{self._base_url}/v1/receive/{self._account}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url)
        if response.status_code == 200:
            data = response.json()
            return data if isinstance(data, list) else []
        raise SignalRestError(
            f"receive failed: {response.status_code} {response.text[:200]}"
        )

    async def send(self, recipient: str, text: str) -> None:
        url = f"{self._base_url}/v2/send"
        payload = {"number": self._account, "recipients": [recipient], "message": text}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=payload)
        if response.status_code >= 400:
            raise SignalRestError(
                f"send failed: {response.status_code} {response.text[:200]}"
            )

    async def health(self) -> dict[str, Any]:
        url = f"{self._base_url}/v1/about"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
        if response.status_code == 200:
            return {"ok": True, "details": response.json()}
        return {"ok": False, "status": response.status_code}
