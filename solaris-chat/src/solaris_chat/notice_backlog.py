"""Short backlog behind the `ha` notification kind, so a gap is survivable (#1284).

The event bus is in-process pub/sub with no backlog: an event exists only for
the clients subscribed at the instant it is published. On Android the app's
foreground service holds the SSE open, but a screen-off wake only listens for a
few seconds — so the case this channel was built for (phone in a pocket,
screen off) was its worst case: a notice published outside that window was
**lost**, not delayed.

This module is the missing half. Every `ha` event both producers publish — the
HA endpoint and the timer scheduler — is also appended here, and
`GET /napi/notifications?since=…` replays what a client missed. Same mechanic
the app already uses for approvals and updates: one plain read on wake.

## Retention is SHORT and that is deliberate

`RETENTION_HOURS` hours, and no longer. The payloads name residents by name and
describe what is happening in their home; this is a convenience channel, not a
message archive, and a row that outlives the gap it exists to bridge is only a
privacy liability. Two independent bounds keep the table small, both applied on
every write, so nothing depends on a cron:

- **age** — rows older than the retention window are deleted.
- **count** — at most `MAX_ROWS_PER_TARGET` rows survive per stream, so a
  misfiring automation cannot grow the table without bound *inside* the window
  either.

**This does not make the channel an alarm channel and does not promise
delivery.** A notice can still be missed: after the window, past the cap, or
because nothing ever fetches. What changed is only that a client which was not
listening now has a *chance* to catch up — see `solaris_chat.ha_notify`.

The stored payload is the event **as emitted**, verbatim JSON. The catch-up
replays it unchanged, so the backlog carries no second copy of the event's
shape that could drift from the one on the stream.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solaris_chat.logging import log

# Long enough to bridge a screen-off stretch, short enough that the row is gone
# well before it becomes a record of the household's day.
RETENTION_HOURS = 6
MAX_ROWS_PER_TARGET = 200
MAX_FETCH = 100

# Millisecond resolution: a cursor at second resolution would drop a second
# notice published in the same second as the one the client last saw. Order is
# the autoincrement `id`, not the stamp — two notices CAN share a millisecond,
# and insertion order is the one thing that never ties.
_NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"
_FLOOR = "strftime('%Y-%m-%d %H:%M:%f', 'now', ?)"
_RETENTION = f"-{RETENTION_HOURS} hours"

_SINCE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z?$")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _wire(stamp: str) -> str:
    """The stored stamp as the ISO-8601 UTC string the client sees."""
    return stamp.replace(" ", "T", 1) + "Z"


def now() -> str:
    """The server's clock in the same wire form as a notice's `ts`.

    A client that fetches and gets nothing still needs a cursor to advance, or
    it re-asks for the whole window forever.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def normalize_since(raw: str) -> str:
    """A client cursor as the comparable stored form, or `ValueError`.

    Accepts what this module hands out (`2026-08-30T01:02:03.123Z`) and the
    plain SQLite form, so a client can pass back the last `ts` it saw.
    """
    value = (raw or "").strip()
    if not _SINCE.match(value):
        raise ValueError("invalid_since")
    return value.rstrip("Z").replace("T", " ", 1)


def record(db_path: str, target_uid: str, data: dict[str, Any]) -> None:
    """Append one emitted `ha` event to the backlog and prune on the way out.

    Never raises into the producer: a box whose schema-init sidecar has not run
    the migration yet simply has no backlog, which is the behaviour that
    shipped before this existed.
    """
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO ha_notice_backlog (target_uid, created_at, payload)"
                f" VALUES (?, {_NOW}, ?)",
                (target_uid, json.dumps(data, ensure_ascii=False)),
            )
            conn.execute(
                f"DELETE FROM ha_notice_backlog WHERE created_at < {_FLOOR}",
                (_RETENTION,),
            )
            conn.execute(
                "DELETE FROM ha_notice_backlog WHERE target_uid = ? AND id NOT IN ("
                " SELECT id FROM ha_notice_backlog WHERE target_uid = ?"
                " ORDER BY id DESC LIMIT ?)",
                (target_uid, target_uid, MAX_ROWS_PER_TARGET),
            )
            conn.commit()
    except sqlite3.Error as e:
        log.error("chat.notice_backlog.write_failed", target=target_uid, error=str(e))


def fetch(
    db_path: str,
    uids: list[str],
    *,
    since: str | None = None,
    limit: int = MAX_FETCH,
) -> list[dict[str, Any]]:
    """The notices published on `uids` after `since`, oldest first.

    `uids` are the bus streams the caller's live SSE subscribes to (their own
    and the shared household one) — per-resident privacy holds here exactly as
    it does on the stream. Rows past the retention window are never returned
    even if a prune has not run yet, and the newest `limit` bound the response.
    Degrades to empty on a missing DB/table, like every other operational read.
    """
    if not uids or not Path(db_path).exists():
        return []
    holes = ",".join("?" * len(uids))
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, created_at, payload FROM ha_notice_backlog"
                f" WHERE target_uid IN ({holes})"
                f" AND created_at >= {_FLOOR} AND created_at > ?"
                " ORDER BY id DESC LIMIT ?",
                (*uids, _RETENTION, since or "", limit),
            ).fetchall()
    except sqlite3.Error:
        return []
    notices: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        # The payload's own `id` is the one the live SSE frame carried
        # (#1346) — the row id only stands in for a notice stored before that
        # id existed, which the retention window ages out within hours.
        notices.append({"id": row["id"], **payload, "ts": _wire(row["created_at"])})
    return notices
