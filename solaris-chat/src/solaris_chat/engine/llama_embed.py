"""Embeddings client for the second llama-server instance (#1332).

`nomic-embed-text` used to be served by Ollama, and that was the last reason
the service existed. It is now a small `llama-server --embeddings` of its own
(templates/llama, `llama-embed.service`, loopback 11436) speaking the OpenAI
`POST /v1/embeddings` shape.

`embed(model, inputs) -> list[list[float]]` returns one vector per input, in
order — the shape the two call sites want (the OKF drain worker and the vault's
semantic search).

**No prefix, deliberately.** nomic-embed-text v1.5's model card describes
`search_document:` / `search_query:` prefixes, but Ollama never applied them,
so every vector in `okf_vectors` was computed on the raw text. Adding one here
would move new vectors away from ~46k stored ones — search would not fail, it
would quietly get worse. Same reason the server pins f16 and mean pooling.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from solaris_chat.engine.llama_server import LlamaServerError


class LlamaEmbed:
    def __init__(self, base_url: str, timeout: float = 120.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout, sock_read=timeout)

    async def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        """`POST /v1/embeddings` — embed a batch of texts, one vector per input."""
        body = {"model": model, "input": inputs}
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                f"{self._base_url}/v1/embeddings", json=body
            ) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:500]
                    raise LlamaServerError(
                        f"llama-server /v1/embeddings {resp.status}: {detail}"
                    )
                data = await resp.json()
        rows: list[dict[str, Any]] = data.get("data") or []
        # Sorted by `index`: the OpenAI schema does not promise request order,
        # and the caller zips these against its own batch.
        rows.sort(key=lambda r: r.get("index") or 0)
        return [r.get("embedding") or [] for r in rows]
