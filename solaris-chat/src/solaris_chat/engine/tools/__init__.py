"""Tool registry for the Solaris Engine.

Every tool is a hand-written, token-lean definition (~100-200 tokens) plus an
async handler. The Hermes-era 8.4k-token tool block is the single biggest
thing this engine exists to kill — keep definitions terse and resist
accumulating tools a profile doesn't need.

Every tool also declares a **visibility class** (#1130, ADR-12 / G-6): what its
result may reveal when the answer is spoken on a speaker. `tests/
test_tool_visibility.py` fails on a registered tool that declares none.
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
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


class Visibility(str, Enum):
    """What a tool's result may reveal on the speaker (ADR-12 / G-6).

    HOUSEHOLD    — heating, shopping list, family calendar: spoken freely. Also
                   the class of a tool that only acts or acknowledges (its
                   result carries no household data to leak).
    PERSONAL     — a resident's own notes/mail/photos: spoken only when
                   speaker-ID actually matched THIS utterance to a resident.
    CONFIDENTIAL — contracts, insurance, finances: never spoken. The voice path
                   answers with a pointer to the app instead of the content.
    """

    HOUSEHOLD = "haushalt"
    PERSONAL = "persoenlich"
    CONFIDENTIAL = "vertraulich"


# G-6: a tool that declares no class counts as confidential. The registry test
# fails on an undeclared tool, so this default is the belt to that lint's
# braces — a tool added without a class degrades safely instead of leaking.
DEFAULT_VISIBILITY = Visibility.CONFIDENTIAL

# The surface this turn arrived on. `CHANNEL_VOICE` for the /ollama facade (HA's
# Voice PE and the wyoming satellites); "" for the browser/API path, which sits
# behind the SSO session and is not gated here.
CHANNEL_VOICE = "voice"
current_channel: contextvars.ContextVar[str] = contextvars.ContextVar(
    "engine_channel", default=""
)

# Whether speaker-ID resolved THIS utterance to an enrolled resident. ADR-12:
# recognition sets the context, it is not authorization — far-field recognition
# is spoofable, so a match only ever unlocks PERSONAL, never CONFIDENTIAL.
current_speaker_matched: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "engine_speaker_matched", default=False
)

# Whether speaker-ID runs on this install at all (SOLARIS_SPEAKER_ID_ENABLED).
# With it off nothing can ever match, so gating PERSONAL would withhold a
# resident's own notes from every spoken turn forever: the class then collapses
# to HOUSEHOLD, i.e. the gate only bites where speaker recognition can satisfy
# it. CONFIDENTIAL is unaffected — it is never spoken, on or off.
current_speaker_id_enabled: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "engine_speaker_id_enabled", default=True
)

_POINTER_PERSONAL = (
    "Ich bin mir nicht sicher, wer gerade spricht — persönliche Sachen lese ich"
    " dann nicht laut vor. In der Solaris-App findest du es."
)
_POINTER_CONFIDENTIAL = (
    "Das gehört zu den vertraulichen Unterlagen — die lese ich nie über den"
    " Lautsprecher vor. In der Solaris-App findest du es."
)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    visibility: Visibility | None = None

    @property
    def visibility_class(self) -> Visibility:
        """The class that actually applies — G-6: undeclared means confidential."""
        if self.visibility is None:
            return DEFAULT_VISIBILITY
        return self.visibility

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


def voice_pointer(tool: Tool) -> str | None:
    """The pointer a voice turn gets instead of this tool's content (#1130).

    `None` when the tool may answer normally: off the voice path, a HOUSEHOLD
    tool, or a PERSONAL one either on an utterance speaker-ID actually matched
    or on an install where speaker-ID is off (it then counts as HOUSEHOLD). A
    CONFIDENTIAL tool never answers with content on a speaker, whoever is heard.
    """
    if current_channel.get() != CHANNEL_VOICE:
        return None
    visibility = tool.visibility_class
    if visibility is Visibility.HOUSEHOLD:
        return None
    if visibility is Visibility.PERSONAL and (
        current_speaker_matched.get() or not current_speaker_id_enabled.get()
    ):
        return None
    say = (
        _POINTER_PERSONAL
        if visibility is Visibility.PERSONAL
        else _POINTER_CONFIDENTIAL
    )
    return json.dumps(
        {
            "ok": False,
            "reason": "visibility_withheld_on_voice",
            "visibility": visibility.value,
            "say": say,
        },
        ensure_ascii=False,
    )


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
        withheld = voice_pointer(tool)
        if withheld is not None:
            return withheld
        try:
            return await tool.handler(arguments)
        except Exception as e:  # noqa: BLE001 — a tool error is model feedback,
            # not a turn-killer: the model sees it and can recover or apologize.
            return f'{{"error": "{type(e).__name__}: {str(e)[:200]}"}}'

    def names(self) -> list[str]:
        return list(self._tools)
