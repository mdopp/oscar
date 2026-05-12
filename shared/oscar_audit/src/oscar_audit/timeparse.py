"""Tiny relative-time parser. Accepts ISO 8601 or these shorthand forms:

    1h, 24h, 7d, 30d, 1w
    today      → 00:00:00 of today (UTC)
    yesterday  → 00:00:00 of yesterday (UTC)
    now        → exact moment of call

Returns timezone-aware UTC datetimes.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_RELATIVE = re.compile(r"^(\d+)([mhdw])$")


def parse_since(text: str, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if text == "now":
        return now
    if text == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "yesterday":
        midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight_today - timedelta(days=1)

    match = _RELATIVE.match(text)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        delta = {
            "m": timedelta(minutes=value),
            "h": timedelta(hours=value),
            "d": timedelta(days=value),
            "w": timedelta(weeks=value),
        }[unit]
        return now - delta

    # Fallback: try ISO 8601.
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unrecognised time: {text!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
