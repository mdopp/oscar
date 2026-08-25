"""Deterministic confirmation gate for sensitive HA actions (#570).

`offer_choices` + the tool-discipline prompt (u64/#558) ask the model to
confirm before opening/unlocking the house, but gemma4:e4b obeys it only
sometimes — "Garagentor öffnen" sometimes executes with a bare "Klar.". For a
safety feature "usually" is not enough, so the engine ENFORCES it in code: a
`ha_call_service` on a sensitive target is intercepted at dispatch, not run, and
held until the user's next reply confirms it.

This module owns the policy (what is sensitive, what counts as yes/no) and the
per-session stash of the pending action; the loop in client.py calls `gate()`
before dispatching a tool and consumes a stashed action at the top of a turn.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

# How long a held sensitive action stays confirmable (#1183). A confirmation is
# answered in the same breath as the question; the household session is SHARED
# (one row for the whole box while speaker-ID is off), so anything later is a
# different conversation — possibly a different resident — and an incidental
# "ja"/"genau" in it must not open the garage. Monotonic, so a clock step can
# neither extend the window nor close it early.
PENDING_TTL_S = 120.0

# A ha_call_service is SENSITIVE when its target can open or unsecure the house.
# Two axes so the set is explicit and easy to extend: the whole domain (any lock
# action is sensitive — locking re-secures, but unlock is the danger and gating
# both keeps the rule trivial), or a specific opening/disarming service.
SENSITIVE_DOMAINS: frozenset[str] = frozenset({"lock", "alarm_control_panel"})
SENSITIVE_SERVICES: frozenset[str] = frozenset(
    {
        "open",
        "unlock",
        "alarm_disarm",
    }
)

# A `cover` is the house's perimeter only when its device_class is a garage door,
# entrance door or gate — those get gated; blinds/shades/curtains/awnings/windows
# must NOT (don't annoy the daily blind). The gate keys on the cover's
# device_class, so a `cover` call is sensitive only for an OPENING/MOVING service
# on a perimeter class. close_cover stays ungated (re-securing), and an
# unresolvable device_class fails SAFE for these open-direction services.
SENSITIVE_COVER_CLASSES: frozenset[str] = frozenset({"garage", "door", "gate"})
COVER_OPEN_SERVICES: frozenset[str] = frozenset(
    {
        "open_cover",
        "toggle",
        "set_cover_position",
        "set_cover_tilt_position",
        "open_cover_tilt",
    }
)

# Affirmative / negative reply detection — a small, case-insensitive keyword set
# matched on whole words. Deliberately simple and robust: the chips offer
# ja/nein, and these cover the common spoken variants without a parser.
_AFFIRMATIVE: frozenset[str] = frozenset(
    {
        "ja",
        "jawohl",
        "jo",
        "jepp",
        "joa",
        "ok",
        "okay",
        "oki",
        "yes",
        "yep",
        "yeah",
        "genau",
        "bestätige",
        "bestätigt",
    }
)
_NEGATIVE: frozenset[str] = frozenset(
    {
        "nein",
        "ne",
        "nee",
        "nö",
        "stop",
        "stopp",
        "halt",
        "abbrechen",
        "abbruch",
        "lass",
        "doch",
        "nicht",
        "no",
        "nope",
        "cancel",
    }
)
_WORD_RE = re.compile(r"[a-zäöüß]+")

# German action verbs for the confirmation question, by service. Falls back to
# the bare service name so an extension to SENSITIVE_SERVICES still asks.
_ACTION_VERBS: dict[str, str] = {
    "open_cover": "öffnen",
    "open_cover_tilt": "öffnen",
    "toggle": "umschalten",
    "set_cover_position": "verstellen",
    "set_cover_tilt_position": "verstellen",
    "open": "öffnen",
    "lock": "abschließen",
    "unlock": "aufschließen",
    "alarm_disarm": "entschärfen",
}


def confirm_prompt(domain: str, service: str, entity_id: str) -> str:
    """The "Soll ich … wirklich …?" question for a held sensitive action.

    Uses the entity_id's readable slug (no HA round-trip — the gate must be
    synchronous and deterministic); the model relays it verbatim."""
    name = (
        entity_id.split(".", 1)[1].replace("_", " ") if "." in entity_id else entity_id
    )
    verb = _ACTION_VERBS.get(service, service)
    return f"Soll ich {name} wirklich {verb}?"


def is_sensitive(domain: str, service: str, device_class: str | None = None) -> bool:
    """True when a ha_call_service domain+service can open/unsecure the house.

    For a `cover`, the danger is class-specific: a garage/door/gate opening or
    moving is gated, an ordinary blind/shade/curtain is not. `device_class` is
    the target entity's HA device_class (None when it could not be resolved); an
    open-direction cover service with an unresolved class fails SAFE (gated)."""
    if domain in SENSITIVE_DOMAINS or service in SENSITIVE_SERVICES:
        return True
    if domain == "cover" and service in COVER_OPEN_SERVICES:
        dc = (device_class or "").lower()
        if not dc:
            return True  # fail safe — can't prove it's a harmless blind
        return dc in SENSITIVE_COVER_CLASSES
    return False


def is_affirmative(text: str) -> bool:
    words = set(_WORD_RE.findall((text or "").lower()))
    # Negative wins on a tie ("nein doch nicht öffnen") — never auto-execute on
    # an ambiguous reply; only a clean yes proceeds.
    if words & _NEGATIVE:
        return False
    return bool(words & _AFFIRMATIVE)


def is_negative(text: str) -> bool:
    words = set(_WORD_RE.findall((text or "").lower()))
    return bool(words & _NEGATIVE)


@dataclass
class PendingAction:
    """A sensitive ha_call_service held for confirmation."""

    domain: str
    service: str
    entity_id: str
    data: dict[str, Any] | None
    prompt: str

    def args(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "domain": self.domain,
            "service": self.service,
            "entity_id": self.entity_id,
        }
        if self.data:
            out["data"] = self.data
        return out


class PendingStore:
    """Per-conversation stash of one pending sensitive action.

    In-memory and keyed by the conversation the loop runs under: the durable
    household session id (voice + browser share one row — by design), or, on the
    stateless facade path, HA's per-conversation id. A per-profile constant must
    NOT be used as the key — that would let one caller confirm another caller's
    held action (#570 fail-open F3). When the ephemeral path has no
    per-conversation key, the loop simply does not stash (re-gate every turn).
    One slot per key: a fresh sensitive request replaces an unanswered one, and
    an unanswered one goes stale after `PENDING_TTL_S` (#1183).
    """

    def __init__(self) -> None:
        self._pending: dict[str, tuple[PendingAction, float]] = {}

    def stash(self, session_id: str, action: PendingAction) -> None:
        self._pending[session_id] = (action, time.monotonic() + PENDING_TTL_S)

    def take(self, session_id: str) -> PendingAction | None:
        entry = self._pending.pop(session_id, None)
        return entry[0] if entry and entry[1] > time.monotonic() else None

    def peek(self, session_id: str) -> PendingAction | None:
        entry = self._pending.get(session_id)
        return entry[0] if entry and entry[1] > time.monotonic() else None

    def take_lapsed(self, session_id: str) -> PendingAction | None:
        """Pop and return an action whose confirmation window has closed.

        Separate from `take` so the loop can SAY it lapsed: a resident whose
        "ja" reaches no held action must not be left guessing whether the house
        just opened."""
        entry = self._pending.get(session_id)
        if entry is None or entry[1] > time.monotonic():
            return None
        del self._pending[session_id]
        return entry[0]
