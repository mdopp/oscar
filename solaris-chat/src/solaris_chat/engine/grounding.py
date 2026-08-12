"""Hard facts are rendered from record fields, never generated (#1129, G-1).

Two stages, both deterministic:

1. **Formatter path.** `notes_read` hands a `type: document` note back as the
   STRUCTURED record its frontmatter already is, plus the readout this module
   renders from those fields — instead of raw markdown the model has to
   re-narrate.
2. **Checker.** At the end of a turn that retrieved such a record, every number
   and every date in the produced answer is held against the retrieval context.
   One that isn't in it discards the answer, and the record is read out verbatim.

`gemma4:e4b` is the right call for 1.3s, but a 4B model occasionally misstates an
amount or a date, and for an insurance sum or a cancellation deadline that is not
a cosmetic defect. The check is string work over text already in hand — no extra
model pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from solaris_chat.engine import date_parse
from solaris_chat.engine.tools.documents import FIELDS
from solaris_chat.logging import log

# The resident-facing label of each typed field. Derived from the model-facing
# schema descriptions so there is one field vocabulary, with the format hints
# ("(YYYY-MM-DD)", "(z.B. '3 Monate')") dropped — those are for the model.
LABELS = {k: re.sub(r"\s*\([^)]*\)", "", v).strip() for k, v in FIELDS.items()}

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\s*(.*)\Z", re.DOTALL)
# A number is a digit run, optionally grouped/decimal-separated: 240, 1.200,50.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_THOUSANDS = re.compile(r"\d{1,3}(?:\.\d{3})+")

_LEAD_IN = "Ich lese dir das lieber genau so vor, wie es im Dokument steht:"


def parse_document(text: str) -> tuple[dict[str, str], str] | None:
    """A `type: document` note as ``(record, body)``, or None for any other note.

    The formatter path is for the typed life-document records `document_extract`
    writes; prose notes keep going back as prose.
    """
    m = _FRONTMATTER.match(text)
    if m is None:
        return None
    record: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            record[key.strip()] = value.strip()
    if record.get("type") != "document":
        return None
    return record, m.group(2).strip()


def _render_value(value: str) -> str:
    """A value that IS one date reads out German; everything else stands."""
    spans = date_parse.find_dates(value)
    if len(spans) == 1 and spans[0][0] == 0 and spans[0][1] == len(value):
        return spans[0][2].strftime("%d.%m.%Y")
    return value


def render(record: dict[str, str]) -> str:
    """The readout of a document record: its fields, one per line, no prose."""
    lines = [f"{record.get('title') or 'Dokument'}:"]
    for key, label in LABELS.items():
        value = (record.get(key) or "").strip()
        if value:
            lines.append(f"- {label}: {_render_value(value)}")
    return "\n".join(lines)


def _norm_number(raw: str) -> str:
    """German number text to a comparable value: 1.200,50 and 1200.5 are one."""
    text = raw
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif _THOUSANDS.fullmatch(text):
        text = text.replace(".", "")
    try:
        return format(Decimal(text).normalize(), "f")
    except InvalidOperation:
        return raw


def _numbers_and_days(text: str) -> tuple[set[str], set[date]]:
    spans = date_parse.find_dates(text)
    # Blank the date literals first, so their digits aren't also read as amounts.
    masked = list(text)
    for start, end, _ in spans:
        masked[start:end] = " " * (end - start)
    return (
        {_norm_number(m.group(0)) for m in _NUMBER.finditer("".join(masked))},
        {day for _, _, day in spans},
    )


@dataclass(frozen=True)
class Grounding:
    """What a turn retrieved: the record readouts, and every number and date the
    retrieval context holds."""

    readouts: list[str]
    numbers: frozenset[str]
    days: frozenset[date]


def context_from_turn(messages: list[dict[str, Any]]) -> Grounding | None:
    """The current turn's retrieval context, or None if it retrieved no record.

    The checker arms only when a tool handed back a structured record this turn.
    On every other turn the numbers in an answer are the resident's own or a
    device's, and holding those against a record would be nonsense.
    """
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            start = i
            break
    outputs = [
        str(m.get("content") or "") for m in messages[start:] if m.get("role") == "tool"
    ]
    readouts: list[str] = []
    for output in outputs:
        try:
            payload = json.loads(output)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("verbatim"):
            readouts.append(str(payload["verbatim"]))
    if not readouts:
        return None
    numbers, days = _numbers_and_days("\n".join(outputs))
    return Grounding(readouts, frozenset(numbers), frozenset(days))


def ungrounded(answer: str, context: Grounding) -> list[str]:
    """The numbers and dates in `answer` that the retrieval context doesn't hold."""
    numbers, days = _numbers_and_days(answer)
    return sorted(numbers - context.numbers) + [
        day.isoformat() for day in sorted(days - context.days)
    ]


def ground(answer: str, context: Grounding) -> str:
    """`answer` when every number and date in it came from the retrieval context,
    otherwise the record read out verbatim."""
    missing = ungrounded(answer, context)
    if not missing:
        return answer
    log.warn("grounding.answer_discarded", ungrounded=missing[:5])
    return "\n\n".join([_LEAD_IN, *context.readouts])
