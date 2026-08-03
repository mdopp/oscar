"""Deterministic resolution of German date/time expressions (#1126, guideline G-1).

The model never produces a date. It passes the words the resident actually said
("morgen", "nächsten Dienstag", "Donnerstag 15 Uhr") and this module resolves them
against the current time in the household timezone. A hallucinated appointment date
is worse than a hallucinated number, so anything genuinely ambiguous comes back as a
`question` to ask instead of a guess.

Two ambiguities are answered rather than resolved:
  * an hour of 1–6 with no daypart word — "4 Uhr" is 04:00 by the letter and 16:00
    by everyday use, and a dentist at 04:00 is a real failure;
  * a bare weekday that IS today — "Donnerstag" on a Thursday means either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# The household timezone, as in engine.client / engine.crons.
LOCAL_TZ = ZoneInfo("Europe/Berlin")

WEEKDAYS_DE = (
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
)

_WEEKDAYS = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonnabend": 5,
    "sonntag": 6,
}
_MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}
_NUMBER_WORDS = {
    "einem": 1,
    "einer": 1,
    "eine": 1,
    "ein": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
    "elf": 11,
    "zwölf": 12,
}
# The hour a daypart word stands for on its own ("heute Abend" → 19:00).
_DAYPARTS = {
    "früh": 8,
    "morgens": 8,
    "vormittag": 10,
    "vormittags": 10,
    "mittag": 12,
    "mittags": 12,
    "nachmittag": 15,
    "nachmittags": 15,
    "abend": 19,
    "abends": 19,
    "nacht": 22,
    "nachts": 22,
}
# Dayparts that push a spoken 1–11 into the afternoon/evening.
_PM_DAYPARTS = {"nachmittag", "nachmittags", "abend", "abends"}

# Longest alternative first so "nachmittags" never reads as "mittags".
_DAYPART_RE = re.compile(
    r"\b(nachmittags?|vormittags?|mittags?|abends?|nachts?|morgens|früh)\b"
)
# A dotted HH.MM only counts as a time when "Uhr" follows — otherwise "5.9." (a
# date) would read as 5 past 9.
_TIME_RES = (
    re.compile(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\b"),
    re.compile(r"\b(?P<h>\d{1,2})\.(?P<m>\d{2})\s*uhr"),
    re.compile(r"\b(?P<h>\d{1,2})\s*uhr(\s+(?P<m>\d{1,2})\b)?"),
)
_ISO_RE = re.compile(r"\b(?P<y>\d{4})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})\b")
_DOTTED_RE = re.compile(r"\b(?P<d>\d{1,2})\.\s*(?P<mo>\d{1,2})\.(\s*(?P<y>\d{4}))?")
_MONTHNAME_RE = re.compile(
    r"\b(?P<d>\d{1,2})\.?\s*(?P<mo>" + "|".join(_MONTHS) + r")\b(\s*(?P<y>\d{4}))?"
)
_IN_N_RE = re.compile(
    r"\bin\s+(?P<n>\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")\s+"
    r"(?P<unit>tag(?:en)?|woche(?:n)?|monat(?:en)?)\b"
)
_NEXT_WEEKDAY_RE = re.compile(
    r"\b(nächste|kommende)[nrms]?\s+(?P<wd>" + "|".join(_WEEKDAYS) + r")\b"
)
_WEEKDAY_RE = re.compile(r"\b(?P<wd>" + "|".join(_WEEKDAYS) + r")\b")
_VAGUE_WEEK_RE = re.compile(r"\b(nächste|kommende)[nrms]?\s+wochen?\b")


@dataclass(frozen=True)
class Resolution:
    """A resolved calendar moment, or a question to ask back.

    `day` set → resolved; `at` None means the resident named no time (an all-day
    entry). `question` non-empty → ambiguous, ask it instead of writing anything.
    """

    day: date | None = None
    at: time | None = None
    question: str = ""

    def start(self) -> datetime | None:
        """The tz-aware start, or None for an all-day / unresolved expression."""
        if self.day is None or self.at is None:
            return None
        return datetime.combine(self.day, self.at, tzinfo=LOCAL_TZ)


def _add_months(day: date, months: int) -> date:
    total = day.month - 1 + months
    year = day.year + total // 12
    month = total % 12 + 1
    # Clamp the day into the target month (31.01. + 1 Monat → 28./29.02.).
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def _resolve_time(text: str) -> tuple[time | None, str, str]:
    """Pull the time out of `text` → (time, question, remaining text)."""
    daypart_m = _DAYPART_RE.search(text)
    daypart = daypart_m.group(1) if daypart_m else ""
    rest = text
    if daypart_m:
        rest = rest[: daypart_m.start()] + " " + rest[daypart_m.end() :]
    for pattern in _TIME_RES:
        m = pattern.search(rest)
        if not m:
            continue
        hour = int(m.group("h"))
        minute = int(m.group("m") or 0)
        rest = rest[: m.start()] + " " + rest[m.end() :]
        if hour > 23 or minute > 59:
            return (
                None,
                f"„{m.group(0).strip()}“ ist keine gültige Uhrzeit — wann genau?",
                rest,
            )
        if daypart in _PM_DAYPARTS and 1 <= hour <= 11:
            hour += 12
        elif not daypart and 1 <= hour <= 6:
            return (
                None,
                f"Meinst du {hour} Uhr morgens oder {hour + 12} Uhr nachmittags?",
                rest,
            )
        return time(hour, minute), "", rest
    if daypart:
        return time(_DAYPARTS[daypart], 0), "", rest
    return None, "", rest


def _year_completed(day: int, month: int, today: date) -> date | None:
    """A day/month with no year → this year, or next year once it has passed."""
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None


def _resolve_day(text: str, today: date) -> tuple[date | None, str]:
    """Resolve the date half of `text` → (day, question)."""
    if "übermorgen" in text:
        return today + timedelta(days=2), ""
    if re.search(r"\bmorgen\b", text):
        return today + timedelta(days=1), ""
    if re.search(r"\bheute\b", text):
        return today, ""
    m = _ISO_RE.search(text)
    if m:
        try:
            return date(int(m.group("y")), int(m.group("mo")), int(m.group("d"))), ""
        except ValueError:
            return None, "Das Datum gibt es nicht — welchen Tag meinst du?"
    for pattern in (_DOTTED_RE, _MONTHNAME_RE):
        m = pattern.search(text)
        if not m:
            continue
        raw_month = m.group("mo")
        month = _MONTHS.get(raw_month, 0) or int(raw_month)
        day = int(m.group("d"))
        if m.group("y"):
            try:
                return date(int(m.group("y")), month, day), ""
            except ValueError:
                return None, "Das Datum gibt es nicht — welchen Tag meinst du?"
        resolved = _year_completed(day, month, today)
        if resolved is None:
            return None, "Das Datum gibt es nicht — welchen Tag meinst du?"
        return resolved, ""
    m = _IN_N_RE.search(text)
    if m:
        raw = m.group("n")
        n = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
        unit = m.group("unit")
        if unit.startswith("tag"):
            return today + timedelta(days=n), ""
        if unit.startswith("woche"):
            return today + timedelta(weeks=n), ""
        return _add_months(today, n), ""
    m = _NEXT_WEEKDAY_RE.search(text)
    if m:
        # "nächsten Dienstag" = that weekday in the following calendar week, so
        # it never collides with the bare weekday's "next one coming up".
        next_monday = today + timedelta(days=7 - today.weekday())
        return next_monday + timedelta(days=_WEEKDAYS[m.group("wd")]), ""
    if _VAGUE_WEEK_RE.search(text):
        return None, "Nächste Woche — an welchem Tag?"
    m = _WEEKDAY_RE.search(text)
    if m:
        target = _WEEKDAYS[m.group("wd")]
        delta = (target - today.weekday()) % 7
        if delta == 0:
            return None, f"Meinst du heute oder {WEEKDAYS_DE[target]} in einer Woche?"
        return today + timedelta(days=delta), ""
    return None, ""


def resolve(expression: str, *, now: datetime | None = None) -> Resolution:
    """Resolve a German date/time expression against `now` (default: household time).

    Returns a `Resolution` carrying either a concrete day (plus an optional time)
    or a `question` to ask the resident.
    """
    now = now or datetime.now(LOCAL_TZ)
    text = " ".join(expression.lower().split())
    if not text:
        return Resolution(question="Für wann soll ich den Termin eintragen?")
    at, question, rest = _resolve_time(text)
    if question:
        return Resolution(question=question)
    day, question = _resolve_day(rest, now.date())
    if question:
        return Resolution(question=question)
    if day is None:
        if at is None:
            return Resolution(
                question=f"Für wann soll ich „{expression.strip()}“ eintragen?"
            )
        # A time with no day is today while today can still hold it, else open.
        if datetime.combine(now.date(), at, tzinfo=LOCAL_TZ) > now:
            return Resolution(day=now.date(), at=at)
        return Resolution(
            question="Heute ist die Zeit schon vorbei — meinst du morgen?"
        )
    return Resolution(day=day, at=at)


def format_de(day: date, at: time | None) -> str:
    """The German label the confirmation reads back ("Donnerstag, 06.08. um 15:00")."""
    label = f"{WEEKDAYS_DE[day.weekday()]}, {day.strftime('%d.%m.')}"
    return f"{label} um {at.strftime('%H:%M')}" if at else label
