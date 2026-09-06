"""Deterministic 'merk dir …' capture (#621).

The SOUL's second-brain section asks Solaris to proactively STORE a memorable
statement ('merk dir X', a durable fact) via fact_store/note_write. gemma4:e4b
obeys that discretionary instruction only sometimes — it confirms
conversationally ('Klar, merke ich mir.') but calls no store tool, so nothing
is durably remembered. For the second brain's core promise "usually" is not
enough, so the engine ENFORCES it: when the user's turn is an explicit
remember-this request and the model dispatched no store tool, the loop stores
the content in code.

This module owns the policy — is a turn a remember-this request, and what is
the fact to store — matching the confirm-gate's split (policy here, the hook in
client.py). Detection is a small, case-insensitive trigger-phrase set; the fact
is the text after the trigger, so 'Merk dir, dass das Auto in der Tiefgarage
steht.' stores 'das Auto in der Tiefgarage steht'.

That same phrase set also ROUTES the turn (#1336): the model is not asked to
pick `fact_store`, it is made to — see `routed_pass()`.
"""

from __future__ import annotations

import re
from typing import Any

# Explicit remember-this openers. Deliberately narrow: only a clear directive to
# STORE (not every declarative fact) triggers the code path, so a normal chat
# turn is never silently written to the vault. German
# 'merk/notier/behalt/denk daran/vergiss nicht' + English 'remember/note', an
# optional 'bitte'/'kannst du (dir)' lead-in, an optional pronoun
# (dir/euch/mir/es), then a separator (comma/colon/space) and an optional
# 'dass'/'that' — all stripped so only the fact itself is captured. The
# separator is what keeps 'merkwürdig' and 'Notizen' out: a trigger must end on
# whitespace, not run into the next syllable.
_TRIGGER_RE = re.compile(
    r"^\s*(?:bitte\s+)?(?:kannst du\s+(?:dir|mir|euch)?\s*)?"
    r"(?:merke?n?|notiere?|behalte?|remember|note"
    r"|denke?\s+da?ran|vergiss\s+nicht)"
    r"(?:\s+(?:dir|euch|mir|es|it))?"
    r"\s*[:,]?\s+"
    r"(?:dass\s+|that\s+)?"
    r"(?P<fact>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)

_PRONOUNS: frozenset[str] = frozenset({"dir", "euch", "mir", "es", "it"})

FACT_STORE = "fact_store"

# llama.cpp reads `tool_choice` as a STRING — "auto"/"none"/"required" only
# (`common_chat_tool_choice_parse_oaicompat`); OpenAI's named-function object is
# not parsed, it is dropped and the turn silently runs as "auto". So a routed
# turn asks for "required" and ships fact_store as the ONLY tool, which is the
# same guarantee in the shape this server honours.
_REQUIRED = "required"


def wants_remember(text: str) -> str | None:
    """The fact to store when `text` is an explicit remember-this request, else None.

    Returns the trailing content with the trigger phrase and an optional
    'dass'/'that' stripped and surrounding punctuation trimmed; None when the
    turn is not a remember-this directive or carries no content to store."""
    m = _TRIGGER_RE.match(text or "")
    if not m:
        return None
    fact = m.group("fact").strip().strip(".,;:! \t\n")
    # A bare "merk dir" leaves only the pronoun as the fact — nothing to store.
    if fact.lower() in _PRONOUNS:
        return None
    return fact or None


def routed_pass(
    text: str, tools: list[dict[str, Any]] | None
) -> tuple[list[dict[str, Any]] | None, str]:
    """`(tools, tool_choice)` for the first model pass of this turn.

    #1336: for "merk dir, dass der Müll dienstags kommt" gemma4:e4b answers
    "Notiert: …" with an empty `tool_calls` list on 5 of 5 box runs — with the
    original tool description and with the rewritten one, so the model treats
    remembering as conversation and no wording changes that. The intent is
    therefore ROUTED rather than suggested: a matching turn goes out with
    fact_store alone under a forced tool_choice, so the call has to be produced.
    Every other turn is returned untouched.
    """
    if not tools or not wants_remember(text):
        return tools, ""
    forced = [t for t in tools if (t.get("function") or {}).get("name") == FACT_STORE]
    return (forced, _REQUIRED) if forced else (tools, "")
