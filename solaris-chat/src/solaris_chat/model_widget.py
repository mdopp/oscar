"""The Modell tile — the GPU lease as a `kind: tool` widget (#1374).

`GET /api/model-lease` answers with ONE state; a `.tool` needs **rows**. This
module is that translation plus the two things a resident-operated window needs
that a service-operated one never did:

* **A window said in human terms.** A service asks for `ttl_s`; a resident taps
  "4 Stunden" or "bis morgen 07:00". `parse_until` turns either into the
  seconds the contract wants, and `rows` says the result back as "bis 16:30"
  rather than as an epoch.
* **The row IS the choice.** The app resolves exactly ONE action per tool
  (ADR 0014 — the first declared id the row can fill wins, a second is
  unreachable), so a duration cannot be a second button next to the profile.
  Every profile therefore gets one row per window it offers, each carrying its
  own `profile` + `hours` for the tile's single `model.set` (#1381).
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
import math
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

# The profiles, in the order their rows are shown: the house first, so the top
# line of a truncated tile always answers "what is running right now".
PROFILES = (
    (HOUSEHOLD, "Haushalt", "Gemma 4 e4b"),
    ("coding", "Programmieren", "Qwen 27B"),
    ("foundry", "Foundry", "Gemma 4 12B"),
)

# The short word the tile's badge chip shows, per state. The chip is bold and
# at the right edge, so it is the first thing read: it carries the state and
# nothing else. A raw `active`/`preparing` must never reach a resident, and an
# unlisted state renders "" — which hides the chip rather than leaving a word
# on a row that has nothing to say (#1385).
BADGES = {
    "active": "läuft",
    "preparing": "wird geladen",
    "releasing": "wird freigegeben",
}

PROFILE_TITLES = {profile: title for profile, title, _ in PROFILES}

# The windows every real profile offers, as the row says them and as the hours
# the row carries. `None` is "bis morgen 07:00" — the only one that shrinks as
# the morning gets closer, so it is recomputed on every fetch.
WINDOWS = (("1h", "1 h", 1.0), ("4h", "4 h", 4.0), ("morgen", "bis morgen 07:00", None))

MORNING = "morgen 07:00"

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


def lease_profile(profile: object) -> str:
    """The profile a row or a `model.lease` names, or `ValueError`.

    `household` is accepted here and nowhere else in the contract: it is the
    row that releases.
    """
    name = str(profile or "").strip().lower()
    if name != HOUSEHOLD and name not in model_lease.MODELS:
        raise ValueError("invalid_model")
    return name


def lease_seconds(hours: object) -> int:
    """Whole seconds for a row's `hours`, rounded up and capped at a day.

    The tile passes numbers through raw (ADR 0014), fractions included — "bis
    morgen 07:00" is 12.75 h at a quarter past six in the evening. Rounding UP
    is what keeps such a window from ending a second before the time it names.
    `0` is a value, not a missing one: it is the row that gives the card back.
    """
    try:
        value = float(hours)
    except (TypeError, ValueError):
        raise ValueError("invalid_hours") from None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        raise ValueError("invalid_hours")
    if value == 0:
        return 0
    return _clamp(math.ceil(value * 3600))


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


def _status_text(
    state: str,
    *,
    profile: str,
    model_name: str,
    expires_at: float | None,
    holder: str,
    now: float,
) -> str:
    """The line under the title: the model FIRST, then when its window ends.

    Always a clock ("bis 19:42", "bis morgen 07:00"), never a remaining
    duration — "noch 42 Min" is a number a resident has to add to the current
    time to know when the card is free again (#1385). An inactive row says
    nothing here: its `detail` already names the model, and the tile joins the
    two into one line.
    """
    if state == "available":
        return ""
    if state == "preparing":
        return f"{model_name} · das dauert etwa eine Minute"
    if state == "releasing":
        return f"{model_name} · gleich wieder da"
    parts = [model_name]
    when = when_text(expires_at, now=now)
    if not when and profile == HOUSEHOLD:
        # The house holds no window, so there is no end to name — say whose
        # model it is instead of leaving the active row on the bare name.
        when = "Haushalt"
    if when:
        parts.append(when)
    if holder and holder != HOLDER:
        parts.append(f"von {holder}")
    return " · ".join(parts)


def _windows(profile: str, *, now: float) -> list[tuple[str, str, float]]:
    """The rows one profile offers: `(id, title, hours)`.

    The house offers exactly one — giving the card back — and its `0 h` is what
    tells the action to release.
    """
    if profile == HOUSEHOLD:
        return [(HOUSEHOLD, "Haushalt (freigeben)", 0.0)]
    label = PROFILE_TITLES[profile]
    out = []
    for key, window, hours in WINDOWS:
        if hours is None:
            # Recomputed on every fetch: the distance to 07:00 shrinks all
            # evening, so a value cached from the last fetch would take the
            # card past the morning it names.
            hours = round(parse_until(MORNING, now=now) / 3600, 4)
        out.append((f"{profile}:{key}", f"{label} · {window}", hours))
    return out


def rows(lease: dict, *, household_alias: str, now: float | None = None) -> list[dict]:
    """One row per profile AND window — the shape `tool-api-path` serves.

    Each row is a complete choice, because the app resolves one action per tool
    (#1381): `profile` + `hours` are what the single `model.set` reads off it.
    `state` is the machine word (`active` = loaded right now, `available`,
    `preparing`, `releasing`); `badge` is that state as the one short word the
    tile shows in bold; `status_text` names the model and the end of its window
    and is the only place a time appears, because a tile renders a field raw.
    `status_text` and `detail` are never both filled — the tile joins them into
    one line, so the model name is in exactly one of them.
    """
    now = time.time() if now is None else now
    lease_state = str(lease.get("state") or "none")
    leased = str(lease.get("model") or "")
    holder = str(lease.get("holder") or "")
    expires_at = lease.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        expires_at = None

    out: list[dict] = []
    for profile, _title, model_name in PROFILES:
        if profile == HOUSEHOLD:
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
            state = "available" if leased != profile else lease_state
            if state == "ready":
                state = "active"
            row_holder = holder if leased == profile else ""
            row_expires = expires_at if leased == profile else None
            alias = model_lease.ALIASES[profile]
        status = _status_text(
            state,
            profile=profile,
            model_name=model_name,
            expires_at=row_expires,
            holder=row_holder,
            now=now,
        )
        for row_id, title, hours in _windows(profile, now=now):
            out.append(
                {
                    "id": row_id,
                    "title": title,
                    "profile": profile,
                    "hours": hours,
                    "alias": alias,
                    "state": state,
                    "badge": BADGES.get(state, ""),
                    "status_text": status,
                    "detail": "" if status else model_name,
                    "holder": row_holder,
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
