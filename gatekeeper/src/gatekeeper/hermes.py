"""Thin HTTP client for the HERMES conversation endpoint.

POSTs `(text, uid, endpoint, trace_id)` and returns the response text.
HERMES's actual schema is upstream — Phase 0 expects a `/converse`-style
JSON endpoint; adjust the path/keys when validating against a real HERMES
instance.
"""

from __future__ import annotations

import httpx
from gatekeeper.logging import log


class HermesClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def converse(
        self,
        *,
        text: str,
        uid: str,
        endpoint: str,
        trace_id: str,
    ) -> str:
        url = f"{self._base_url}/converse"
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload = {
            "text": text,
            "uid": uid,
            "endpoint": endpoint,
            "trace_id": trace_id,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            log.error(
                "gatekeeper.hermes.error",
                trace_id=trace_id,
                status=response.status_code,
                body=response.text[:500],
            )
            return ""
        data = response.json()
        # Tolerate a few shapes — HERMES upstream may pick its own response key.
        return data.get("text") or data.get("response") or data.get("reply") or ""
