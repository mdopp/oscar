"""Research-synthesis tool (#574, slice 1).

`research(query)` does the gather + trust-rank + cite in Python so the small
hot-path model only has to phrase the answer — prompt-driven multi-tool
synthesis is unreliable on gemma4:e4b. It fans out to the EXISTING query paths
(the `notes_search` keyword grep in `tools/notes.py` and the `web_search`
backend in `tools/web.py`), tags each hit with a `source_kind`, assigns a trust
rank (household notes outrank the web), dedups, sorts by rank, and caps the
result. The model gets clean pre-ranked material plus a one-line instruction to
answer directly from it and cite each source used.

Trust ordering is an explicit, extensible constant — slices 2+ add OKF/HA tiers
above the web by inserting their `source_kind` into `_TRUST_RANK`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from solaris_chat.engine.tools import Tool, Visibility
from solaris_chat.engine.tools.notes import build_notes_tools
from solaris_chat.engine.tools.web import _ddgs_search, _tavily_search

# Lower number = more trusted. The structured OKF store (entities/facts, e.g. the
# music library via music_query, #588) is exact and household-owned, so it
# outranks the notes grep and the web; notes still outrank the web.
_TRUST_RANK = {
    "okf": 1,
    "notes": 2,
    "web": 3,
}

_MAX_SOURCES = 8

_NOTE = "Beantworte die Frage aus diesen Quellen und zitiere jede genutzte Quelle."


def build_research_tools(
    *,
    notes_dir: str = "",
    uid_getter=lambda: "household",
    tavily_api_key: str = "",
) -> list[Tool]:
    """The `research` tool, gated on having at least one query path.

    Wired alongside `build_web_tools` in `profiles.py`; web availability is the
    gate (the web fan-out is always present, notes is added when a vault exists).
    """
    notes_search = None
    if notes_dir:
        for tool in build_notes_tools(notes_dir, uid_getter):
            if tool.name == "notes_search":
                notes_search = tool.handler
                break

    async def _notes_hits(query: str) -> list[dict[str, Any]]:
        if notes_search is None:
            return []
        raw = await notes_search({"query": query})
        try:
            hits = json.loads(raw)
        except (ValueError, TypeError):
            return []
        out = []
        for h in hits if isinstance(hits, list) else []:
            path = str(h.get("path") or "")
            if not path:
                continue
            out.append(
                {
                    "kind": "notes",
                    "title": path,
                    "ref": path,
                    "snippet": str(h.get("snippet") or "").strip(),
                    "score": float(h.get("score") or 0.0),
                }
            )
        return out

    async def _web_hits(query: str) -> list[dict[str, Any]]:
        if tavily_api_key:
            raw = await _tavily_search(tavily_api_key, query)
        else:
            raw = await _ddgs_search(query)
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            return []
        out = []
        for r in body.get("results") or []:
            url = str(r.get("url") or "")
            if not url:
                continue
            out.append(
                {
                    "kind": "web",
                    "title": str(r.get("title") or url),
                    "ref": url,
                    "snippet": str(r.get("snippet") or "").strip(),
                }
            )
        return out

    async def research(args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return json.dumps({"sources": [], "note": _NOTE}, ensure_ascii=False)

        notes_hits, web_hits = await asyncio.gather(
            _notes_hits(query), _web_hits(query)
        )

        seen: set[str] = set()
        ranked: list[dict[str, Any]] = []
        for hit in [*notes_hits, *web_hits]:
            ref = hit["ref"]
            if ref in seen:
                continue
            seen.add(ref)
            ranked.append({"rank": _TRUST_RANK[hit["kind"]], **hit})

        # Trust tier first (cross-tier stays trust-first); within a tier the
        # notes' relevance score (#591) breaks the tie, higher first.
        ranked.sort(key=lambda h: (h["rank"], -float(h.get("score") or 0.0)))
        return json.dumps(
            {"sources": ranked[:_MAX_SOURCES], "note": _NOTE},
            ensure_ascii=False,
        )

    return [
        Tool(
            name="research",
            description=(
                "Recherchiert eine Wissensfrage: sammelt Quellen aus Notizen"
                " und Web, sortiert nach Vertrauenswürdigkeit und liefert sie"
                " mit Quellenangaben zum direkten Beantworten."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=research,
            visibility=Visibility.PERSONAL,
        ),
    ]
