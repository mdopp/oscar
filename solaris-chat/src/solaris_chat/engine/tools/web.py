"""Web search/extract tools.

Backends, in order: Tavily (when a key is configured) — else the keyless
`ddgs` DuckDuckGo client, the same backend the Hermes-era box used (its
post-deploy installed ddgs; no Tavily key was ever configured). ddgs is a
sync library, so it runs in the default executor to keep the loop free.
`web_extract` fetches the page and strips tags — good enough for "read me
that article"; a readability pass can come later if extraction quality
ever matters.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import aiohttp

from solaris_chat.engine.tools import Tool, Visibility

_TIMEOUT = aiohttp.ClientTimeout(total=20)


def build_web_tools(tavily_api_key: str = "") -> list[Tool]:
    async def search(args: dict[str, Any]) -> str:
        query = str(args.get("query") or "")
        if tavily_api_key:
            return await _tavily_search(tavily_api_key, query)
        return await _ddgs_search(query)

    async def extract(args: dict[str, Any]) -> str:
        url = str(args.get("url") or "")
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(
                url, headers={"User-Agent": "Mozilla/5.0 (Solaris)"}
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        text = re.sub(r"(?s)<(script|style).*?</\1>", " ", html)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return json.dumps({"url": url, "content": text[:8000]}, ensure_ascii=False)

    return [
        Tool(
            name="web_search",
            description="Sucht im Web nach aktuellen Informationen.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=search,
            visibility=Visibility.HOUSEHOLD,
        ),
        Tool(
            name="web_extract",
            description="Lädt den Textinhalt einer Webseite.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=extract,
            visibility=Visibility.HOUSEHOLD,
        ),
    ]


async def _ddgs_search(query: str) -> str:
    def _run() -> list[dict[str, Any]]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=5))

    results = await asyncio.get_event_loop().run_in_executor(None, _run)
    return json.dumps(
        {
            "results": [
                {
                    "title": r.get("title"),
                    "url": r.get("href"),
                    "snippet": (r.get("body") or "")[:300],
                }
                for r in results
            ]
        },
        ensure_ascii=False,
    )


async def _tavily_search(api_key: str, query: str) -> str:
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
        async with client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": 5,
                "include_answer": True,
            },
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
    return json.dumps(
        {
            "answer": body.get("answer"),
            "results": [
                {
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "snippet": (r.get("content") or "")[:300],
                }
                for r in (body.get("results") or [])[:5]
            ],
        },
        ensure_ascii=False,
    )
