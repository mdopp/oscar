"""Quick-reply choices tool (#555).

When the model poses a question with a small, discrete answer set, it calls
`offer_choices(options)`; the engine drains the turn's choices into a
`quick_replies` event (parallel to the `ha_cards` drain in client.py) and the
SPA renders them as tappable chips directly above the input. A contextvar sink
so the tool (built once per profile) attributes the choices to the running turn.
"""

from __future__ import annotations

import contextvars
import json
from typing import Any

from solaris_chat.engine.tools import Tool, Visibility

# The turn's offered quick-reply options. The engine sets this per turn and
# drains it at turn end into the `quick_replies` event.
choice_sink: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "quick_reply_sink", default=None
)

_MAX_OPTIONS = 4


def _clean(options: Any) -> list[str]:
    """Trimmed, de-duped, non-empty option strings capped at four (#555)."""
    if not isinstance(options, list):
        return []
    out: list[str] = []
    for o in options:
        text = str(o).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= _MAX_OPTIONS:
            break
    return out


def build_choice_tools() -> list[Tool]:
    async def offer_choices(args: dict[str, Any]) -> str:
        options = _clean(args.get("options"))
        if not options:
            return json.dumps({"error": "no valid options"})
        sink = choice_sink.get()
        if sink is not None:
            sink.clear()
            sink.extend(options)
        return json.dumps({"offered": options}, ensure_ascii=False)

    return [
        Tool(
            name="offer_choices",
            description=(
                "Bietet 2–4 kurze, antippbare Antwortoptionen an, wenn du eine"
                " Frage mit wenigen festen Antworten stellst — stelle die Frage"
                " normal im Text UND rufe dieses Tool mit den Optionen (z.B."
                " 'Garage öffnen?' ⇒ ['ja','nein']). Pflicht vor"
                " sicherheitsrelevanten Aktionen (Garage, Tür, Schloss, Alarm): du"
                " fragst damit NUR — in diesem Zug kein Aktions-Tool rufen, auf die"
                " Antwort warten. Nicht für offene Fragen."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-4 kurze Antwortoptionen",
                    }
                },
                "required": ["options"],
            },
            handler=offer_choices,
            visibility=Visibility.HOUSEHOLD,
        ),
    ]
