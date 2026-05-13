"""HERMES client — same shape as gatekeeper/hermes.py.

Duplicated intentionally: each entry-point service owns its tiny client
to avoid a cross-service shared lib for two-method clients.
"""

from __future__ import annotations

import httpx
from oscar_logging import log


class HermesClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def converse(
        self, *, text: str, uid: str, endpoint: str, trace_id: str
    ) -> str:
        url = f"{self._base_url}/converse"
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload = {"text": text, "uid": uid, "endpoint": endpoint, "trace_id": trace_id}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            log.error(
                "signal_gateway.hermes.error",
                trace_id=trace_id,
                status=response.status_code,
                body=response.text[:500],
            )
            return ""
        data = response.json()
        return data.get("text") or data.get("response") or data.get("reply") or ""
