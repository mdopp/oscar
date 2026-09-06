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
"""

from __future__ import annotations

import re

# Explicit remember-this openers. Deliberately narrow: only a clear directive to
# STORE (not every declarative fact) triggers the code path, so a normal chat
# turn is never silently written to the vault. German 'merk/notier/behalt' +
# English 'remember/note', an optional 'bitte'/'kannst du' lead-in, an optional
# pronoun (dir/euch/mir/es), then a separator (comma/colon/space) and an optional
# 'dass'/'that' — all stripped so only the fact itself is captured.
_TRIGGER_RE = re.compile(
    r"^\s*(?:bitte\s+)?(?:kannst du\s+)?"
    r"(?:merke?|notiere?|behalte?|remember|note)"
    r"(?:\s+(?:dir|euch|mir|es|it))?"
    r"\s*[:,]?\s+"
    r"(?:dass\s+|that\s+)?"
    r"(?P<fact>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)

_PRONOUNS: frozenset[str] = frozenset({"dir", "euch", "mir", "es", "it"})


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
