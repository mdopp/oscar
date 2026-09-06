"""Leading internal-hint blocks the proxy/gatekeeper inject into a user turn.

A resident's sentence never reaches the engine bare: the chat proxy prepends a
wall-clock line and, where they apply, a topic/ephemeral guard; the voice
gatekeeper prepends the room. Two consumers need the sentence without them —
the messages API renders history for a human (#309) and `engine/remember.py`
matches a remember-this directive that is anchored at the start of the text
(#1336) — so the prefix set lives here once instead of in a copy per caller.

What is actually injected:
  server.topic_turn_text  -> "[Aktuelle Zeit: ...]", "[Active topic: ... #topic/<slug>]",
                             the "[Temporary/incognito ...]" ephemeral guard,
                             "[Extract this to a note #topic/<slug> (...)]"
  voice gatekeeper.engine -> "[room: <location>]" (#312/#313)
Each rides as a leading bracketed block; topic_turn_text joins them with "\n\n",
the voice room hint with "\n". `[uid:...]` lives on the title (marker.py), but a
leading one is stripped too for safety. Only LEADING hints are removed so a hint
the resident actually typed mid-message survives.
"""

from __future__ import annotations

import re

HINT_PREFIX_RE = re.compile(
    r"^\[(?:Aktuelle Zeit:|Temporary/incognito|Active topic:|Extract this to a note|room:|uid:)[^\]]*\]\s*",
    re.IGNORECASE,
)


def strip_internal_hints(content: str) -> str:
    """Drop leading internal-hint prefixes from a user message.

    Strips each consecutive leading bracketed hint block, then the whitespace it
    was joined with, leaving the resident's actual text. On the messages API this
    is display-only — what was sent to the engine is unchanged.
    """
    prev = None
    while content != prev:
        prev = content
        content = HINT_PREFIX_RE.sub("", content, count=1)
    return content
