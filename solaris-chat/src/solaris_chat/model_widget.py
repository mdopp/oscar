"""The Modell tile — the GPU lease as a `kind: tool` widget (#1374).

`GET /api/model-lease` answers with ONE state; a `.tool` needs **rows**. This
module is that translation plus the two things a resident-operated window needs
that a service-operated one never did:

* **A window said in human terms.** A service asks for `ttl_s`; a resident taps
  "4 Stunden" or "bis morgen 07:00". `parse_until` turns either into the
  seconds the contract wants, and `rows` says the result back as "bis 16:30"
  rather than as an epoch.
* **A holder that is not the caller.** foundry and pi-web hold their own
  windows and renew them from their own process. Nobody renews on behalf of a
  phone that has been put back in a pocket, so the **engine** takes the window
  (`holder: "widget"`) and `hold` renews it until the chosen end, then releases.
  There is no auto-extension: the loop never asks for more than the window the
  resident chose, and the box's own timer is the net under it.

The operator's decision of 2026-09-08: fixed durations (1/2/4 h) **and** target
times, scope household, countdown/end time on the card, the card comes back by
itself.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta

from solaris_chat import model_lease

# The engine's permanent name on a window it holds for the tile. A holder is
# the identity of the *service*, never of a session or a person (#1347), so
# every resident's tap lands on this one name.
HOLDER = "widget"

# The household profile is not a lease: it is the absence of one. Asking for it
# means "give the card back", which is what makes one row per profile a
# complete control surface instead of a list plus a stray release button.
HOUSEHOLD = "household"

# 24 hours — what "bis morgen 07:00" needs and the new ceiling on both sides of
# the contract (mdopp/foundry-chronicle#321).
UNTIL_MAX_SECONDS = 86400

# What a `model.lease` names when it names no window at all. Two hours is the
# middle of the operator's 1/2/4 h, so a mis-wired caller costs an evening at
# worst and never a day.
DEFAULT_UNTIL = "2h"

# How often to look for the card coming back before taking it for the next
# profile. Giving up is harmless: the release runs on the host either way.
SWITCH_TRIES = 20

# The tile's rows, in the order they are shown: the house first, then the two
# windows that take the card away from it.
PROFILES = (
    (HOUSEHOLD, "Haushalt (Gemma)"),
    ("coding", "Programmieren (Qwen)"),
    ("foundry", "Foundry (Gemma 12B)"),
)

# The enumerated windows the tile offers. A `tool-action-params` value is a
# flat literal or a `$field` reference (ADR 0014) — there is no chooser a
# RemoteViews row could render — so each duration is its own action id, and
# these are the literals behind them.
UNTIL_CHOICES = {
    "model.lease.1h": "1h",
    "model.lease.2h": "2h",
    "model.lease.4h": "4h",
    "model.lease.until_morning": "morgen 07:00",
}

_STATUS_TEXT = {
    "active": "aktiv",
    "available": "frei",
    "preparing": "wird geladen",
    "releasing": "wird freigegeben",
}

_HOURS = re.compile(r"\A(\d{1,2})\s*(?:h|std|stunde|stunden)\Z")
_MINUTES = re.compile(r"\A(\d{1,4})\s*(?:m|min|minute|minuten)\Z")
_CLOCK = re.compile(r"\A(\d{1,2})[:.](\d{2})\Z")
_DATED = re.compile(r"\A(\d{4})-(\d{2})-(\d{2})[t ](\d{1,2})[:.](\d{2})")


def _clamp(seconds: float) -> int:
    return int(min(max(seconds, model_lease.TTL_MIN_SECONDS), UNTIL_MAX_SECONDS))


def parse_until(value: object, *, now: float | None = None) -> int:
    """Seconds from now to the end of the window `value` names.

    Accepts a duration (`1h`, `2h`, `4h`, `90m`) or a target time — a clock
    (`07:00`, optionally prefixed `bis`/`um`/`morgen`/`heute`) or a dated one
    (`2026-09-09T07:00`). A bare clock that has already gone by today means
    tomorrow, which is what "bis 07:00" said at midnight has always meant.
    Anything else raises `ValueError("invalid_until")`.
    """
    now = time.time() if now is None else now
    text = str(value or "").strip().lower()
    for filler in ("bis ", "um "):
        while text.startswith(filler):
            text = text[len(filler) :].strip()
    tomorrow = False
    for word, shift in (("morgen", True), ("heute", False)):
        if text.startswith(word):
            tomorrow, text = shift, text[len(word) :].strip()
            break
    if not text:
        raise ValueError("invalid_until")

    if not tomorrow:
        hours = _HOURS.match(text)
        if hours:
            return _clamp(int(hours.group(1)) * 3600)
        minutes = _MINUTES.match(text)
        if minutes:
            return _clamp(int(minutes.group(1)) * 60)

    target = _target_time(text, tomorrow=tomorrow, now=now)
    remaining = target - now
    if remaining <= 0:
        raise ValueError("until_in_the_past")
    return _clamp(remaining)


def _target_time(text: str, *, tomorrow: bool, now: float) -> float:
    dated = _DATED.match(text)
    if dated:
        year, month, day, hour, minute = (int(g) for g in dated.groups())
        try:
            return datetime(year, month, day, hour, minute).timestamp()
        except ValueError as exc:
            raise ValueError("invalid_until") from exc
    clock = _CLOCK.match(text)
    if not clock:
        raise ValueError("invalid_until")
    hour, minute = int(clock.group(1)), int(clock.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("invalid_until")
    base = datetime.fromtimestamp(now).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if tomorrow or base.timestamp() <= now:
        base += timedelta(days=1)
    return base.timestamp()


def lease_model(model: object) -> str:
    """The profile a `model.lease` names, or `ValueError("invalid_model")`.

    `household` is accepted here and nowhere else in the contract: it is the
    row that releases.
    """
    name = str(model or "").strip().lower()
    if name != HOUSEHOLD and name not in model_lease.MODELS:
        raise ValueError("invalid_model")
    return name


def when_text(expires_at: float | None, *, now: float | None = None) -> str:
    """ "bis 16:30" / "bis morgen 07:00" — the end of the window in the words a
    resident would use for it, or "" when there is no deadline yet."""
    if not isinstance(expires_at, (int, float)) or not expires_at:
        return ""
    now = time.time() if now is None else now
    end = datetime.fromtimestamp(expires_at)
    today = datetime.fromtimestamp(now).date()
    days = (end.date() - today).days
    if days <= 0:
        return f"bis {end:%H:%M}"
    if days == 1:
        return f"bis morgen {end:%H:%M}"
    return f"bis {end:%d.%m}. {end:%H:%M}"


def _meta(state: str, *, expires_at: float | None, holder: str, now: float) -> str:
    if state == "preparing":
        return "wird geladen — das dauert etwa eine Minute"
    if state == "releasing":
        return "wird freigegeben — gleich wieder da"
    if state == "available":
        return "nicht geladen"
    parts = ["gerade geladen"]
    when = when_text(expires_at, now=now)
    if when:
        parts.append(when)
    if holder and holder != HOLDER:
        parts.append(f"von {holder}")
    return " · ".join(parts)


def rows(lease: dict, *, household_alias: str, now: float | None = None) -> list[dict]:
    """One row per profile for the tile — the shape `tool-api-path` serves.

    `state` is the machine word (`active` = loaded right now, `available`,
    `preparing`, `releasing`); `status_text` and `meta` are the same thing in
    German, because a RemoteViews row shows text and nothing else.
    """
    now = time.time() if now is None else now
    lease_state = str(lease.get("state") or "none")
    leased = str(lease.get("model") or "")
    holder = str(lease.get("holder") or "")
    expires_at = lease.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        expires_at = None

    out: list[dict] = []
    for profile_id, title in PROFILES:
        if profile_id == HOUSEHOLD:
            # The house keeps the card until the swap actually lands, and gets
            # it back a few seconds after the release request — so `preparing`
            # on another row still reads as `active` here.
            state = {
                "none": "active",
                "preparing": "active",
                "releasing": "preparing",
            }.get(lease_state, "available")
            row_holder, row_expires = "", None
            alias = household_alias
        else:
            state = "available" if leased != profile_id else lease_state
            if state == "ready":
                state = "active"
            row_holder = holder if leased == profile_id else ""
            row_expires = expires_at if leased == profile_id else None
            alias = model_lease.ALIASES[profile_id]
        out.append(
            {
                "id": profile_id,
                "title": title,
                "alias": alias,
                "state": state,
                "status_text": _STATUS_TEXT[state],
                "holder": row_holder,
                "expires_at": row_expires,
                "remaining_s": (
                    max(int(row_expires - now), 0) if row_expires else None
                ),
                "meta": _meta(
                    state, expires_at=row_expires, holder=row_holder, now=now
                ),
            }
        )
    return out


async def hold(
    *,
    until: float,
    acquire,
    release,
    wait_free=None,
    sleep=asyncio.sleep,
    now=time.time,
) -> None:
    """Take the window, keep it alive until `until`, then give the card back.

    The box arms its release for two missed renewals rather than for the TTL
    (#1361), so a window nobody renews ends long before its deadline — which
    for a resident's four-hour window would be a card that vanished at 40
    minutes. `acquire(ttl)` is the one POST that both takes and renews, always
    with what is left of the window; `release()` is the DELETE at the end, and
    it runs on cancellation too — switching profiles is one tap, not a tap and
    a cleanup. `wait_free` is awaited first when the card still has to come
    back from the profile being switched away from. No auto-extension: the end
    the resident chose is the only end.
    """
    cancelled = False
    try:
        if wait_free is not None:
            await wait_free()
        while True:
            remaining = until - now()
            if remaining <= 0:
                break
            if remaining < model_lease.TTL_MIN_SECONDS:
                # Less left than the shortest window the contract accepts, so
                # asking again would push the deadline PAST the end the resident
                # chose. Sit out the rest instead — that is the no-extension
                # rule, and the box's own timer is under it either way.
                await sleep(remaining)
                break
            ttl = int(min(remaining, UNTIL_MAX_SECONDS))
            await acquire(ttl)
            await sleep(min(model_lease.renew_after(ttl), remaining))
    except asyncio.CancelledError:
        # The caller cancelled us to change or end the window ITSELF — handing
        # the card back here would fight it (and would release the window it is
        # about to extend).
        cancelled = True
        raise
    finally:
        if not cancelled:
            await release()
