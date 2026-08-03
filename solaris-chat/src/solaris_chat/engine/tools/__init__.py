"""Tool registry for the Solaris Engine.

Every tool is a hand-written, token-lean definition (~100-200 tokens) plus an
async handler. The Hermes-era 8.4k-token tool block is the single biggest
thing this engine exists to kill — keep definitions terse and resist
accumulating tools a profile doesn't need.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

Handler = Callable[[dict[str, Any]], Awaitable[str]]

# Calibrated against the box measurement of 2026-08-03: a household turn whose
# system prompt + tool schemas came to ~28.5k characters reported 7810
# prompt_eval_count tokens from Ollama (gemma4:e4b, German prose + JSON
# schemas). Good enough to attribute a prefill to its parts; the authoritative
# total is always Ollama's own prompt_eval_count.
_CHARS_PER_TOKEN = 3.65


def estimate_tokens(chars: int) -> int:
    """Rough token count for a character count — cheap enough for the hot path.

    Takes characters rather than text so a caller can attribute a part it only
    knows as a length difference (e.g. the prompt minus its named blocks)."""
    return round(chars / _CHARS_PER_TOKEN)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler

    def definition(self) -> dict[str, Any]:
        """The Ollama `tools` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Toolbox:
    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}

    async def prepare(self) -> None:
        """Hook for toolboxes that fetch definitions remotely (MCP); awaited
        once per turn before `definitions()` is read. No-op here."""

    def definitions(self) -> list[dict[str, Any]]:
        return [t.definition() for t in self._tools.values()]

    def schema_chars(self) -> int:
        """Characters this toolbox contributes to the prefill — the serialized
        `tools` payload, which every turn re-processes."""
        return len(json.dumps(self.definitions(), ensure_ascii=False))

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f'{{"error": "unknown tool: {name}"}}'
        try:
            return await tool.handler(arguments)
        except Exception as e:  # noqa: BLE001 — a tool error is model feedback,
            # not a turn-killer: the model sees it and can recover or apologize.
            return f'{{"error": "{type(e).__name__}: {str(e)[:200]}"}}'

    def names(self) -> list[str]:
        return list(self._tools)
