"""RRULE handling — thin wrapper around `python-dateutil`.

Our use case is tiny: parse an RFC-5545 rrule string and ask
"what's the next firing time at or after `now`?".
"""

from __future__ import annotations

from datetime import datetime, timezone

from dateutil import rrule


def parse_rrule(text: str) -> rrule.rrule:
    """Accept either bare 'FREQ=…' or 'RRULE:FREQ=…' forms."""
    body = text.strip()
    if body.upper().startswith("RRULE:"):
        body = body[6:]
    # rrulestr requires an inception time; use a far-past constant so it
    # doesn't matter — `next_after` always anchors to the query time.
    dtstart = datetime(2000, 1, 1, tzinfo=timezone.utc)
    return rrule.rrulestr(body, dtstart=dtstart, forceset=False)


def next_after(rule: rrule.rrule, now: datetime) -> datetime:
    """First occurrence strictly after `now` (or at `now` if it's a match)."""
    occurrence = rule.after(now, inc=True)
    if occurrence is None:
        raise ValueError("rrule has no future occurrences")
    if occurrence.tzinfo is None:
        occurrence = occurrence.replace(tzinfo=timezone.utc)
    return occurrence
