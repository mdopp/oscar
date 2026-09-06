"""Client for the Solaris Engine's Ollama-PROTOCOL facade.

The engine (solaris-chat) exposes `/ollama/api/chat` — stateless, the caller
owns the conversation history. The gatekeeper keeps a short rolling history
per conversation (keyed by the uid or the originating satellite) so a
follow-up like "und im Schlafzimmer?" still has its context, without any
server-side session bookkeeping.

A history key must name *one person*. Speaker-ID routes every voice it cannot
match to the single `guest` sentinel, which is not a person — so a turn whose
speaker was not identified is run stateless (`remember=False`) and leaves no
history behind. Before #1289 those turns all shared one bucket, and the next
unmatched speaker was prompted with the previous one's exchange.

Every turn runs on `solaris`, the engine's household profile — the facade's
only conversational model since `solaris-deep` was retired (#1121). The engine
does its own tool dispatch server-side — the reply is plain text, ready for
TTS.
"""

from __future__ import annotations

from collections import deque
from typing import Any

import httpx
from gatekeeper.logging import log

MODEL = "solaris"

# Per-conversation rolling history: enough for short voice follow-ups, small
# enough that the facade's prefill stays lean.
_MAX_HISTORY = 12


class SolarisClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._transport = transport
        self._history: dict[str, deque[dict[str, str]]] = {}

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def converse(
        self,
        *,
        text: str,
        uid: str,
        endpoint: str,
        trace_id: str,
        remember: bool,
        location: str | None = None,
    ) -> str:
        # Inject the satellite's resolved room as an out-of-band context hint
        # (#313): the hint rides as a bracketed prefix the model reads but
        # doesn't speak.
        if location:
            text = f"[room: {location}]\n{text}"
        history: deque[dict[str, str]] | None = None
        if remember:
            history = self._history.setdefault(
                uid or endpoint, deque(maxlen=_MAX_HISTORY)
            )
        messages = [*(history or ()), {"role": "user", "content": text}]
        body: dict[str, Any] = {
            "model": MODEL,
            "messages": messages,
            "stream": False,
            "user": uid,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/api/chat", json=body, headers=self._headers()
                )
        except httpx.HTTPError as e:
            log.error("gatekeeper.solaris.unreachable", trace_id=trace_id, error=str(e))
            return ""
        if response.status_code >= 400:
            log.error(
                "gatekeeper.solaris.error",
                trace_id=trace_id,
                status=response.status_code,
                body=response.text[:500],
            )
            return ""
        reply = _extract_reply(response.json())
        if reply and history is not None:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
        return reply


def _extract_reply(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    msg = body.get("message")
    if isinstance(msg, dict):
        return str(msg.get("content") or "")
    return ""
