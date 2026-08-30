"""Timer/alarm/reminder scheduler — fires speaker announcements.

Timers live in solaris.db (`engine_timers`) so they survive a restart; one
asyncio loop polls the next pending row and fires it. Delivery is an HA
`assist_satellite.announce` to the Voice PE speaker the timer was set on (TTS
rides HA's pipeline) — HA stays the device tool, the schedule itself lives
here, not in HA.
Fail-open: an unreachable HA marks the timer `failed` and logs; it never
kills the loop.

The announcement is and stays the **primary** delivery. A fired timer also goes
out to the owner's phones twice over, both best effort and both secondary: Web
Push to an installed PWA (#713) and an `ha`-kind bus event to a paired native
device (#1280). Neither is an alarm channel — see `solaris_chat.ha_notify`.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from solaris_chat import ha_notify
from solaris_chat.engine.areas import AreaRegistry
from solaris_chat.logging import log

_POLL_S = 5.0

# The notification headline per timer kind, shared by the Web Push and the
# native `ha` event so a resident on both channels reads the same words.
_TITLES = {
    "timer": "Timer abgelaufen",
    "alarm": "Wecker",
    "reminder": "Erinnerung",
}


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout so concurrent writers wait instead of raising
    # "database is locked" (#600).
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def add_timer(
    db_path: str,
    uid: str,
    *,
    duration_s: int | None = None,
    fire_at: str | None = None,
    kind: str = "timer",
    label: str = "",
    session_id: str = "",
    room: str = "",
) -> dict[str, Any]:
    """Insert a pending timer; returns its row as a dict.

    `room` is the area the request came in on (`current_room`, #1187) — it
    decides which satellite rings; "" for an app/browser-set timer."""
    if duration_s is not None:
        when = datetime.now(UTC) + timedelta(seconds=max(int(duration_s), 1))
    elif fire_at:
        when = datetime.fromisoformat(fire_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
    else:
        raise ValueError("duration_s or fire_at required")
    timer_id = uuid.uuid4().hex[:12]
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO engine_timers (id, owner_uid, kind, label, fire_at,"
            " session_id, room) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (timer_id, uid, kind, label, when.isoformat(), session_id, room),
        )
    return {"id": timer_id, "kind": kind, "label": label, "fire_at": when.isoformat()}


def list_timers(db_path: str, uid: str) -> list[dict[str, Any]]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, kind, label, fire_at FROM engine_timers"
            " WHERE owner_uid = ? AND status = 'pending' ORDER BY fire_at",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def cancel_timer(db_path: str, uid: str, timer_id: str) -> bool:
    with _conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE engine_timers SET status = 'cancelled'"
            " WHERE id = ? AND owner_uid = ? AND status = 'pending'",
            (timer_id, uid),
        )
    return cur.rowcount > 0


class TimerScheduler:
    def __init__(
        self,
        db_path: str,
        hass_url: str,
        hass_token: str,
        alarm_sound_media_id: str = "",
        alarm_sound_path: str = "",
        notifier: Any = None,
        event_bus: Any = None,
    ):
        self._db_path = db_path
        self._hass_url = hass_url.rstrip("/")
        self._hass_token = hass_token
        self._alarm_sound_media_id = alarm_sound_media_id
        self._alarm_sound_path = alarm_sound_path
        self._areas = AreaRegistry(hass_url, hass_token)
        # Best-effort Web Push (#713): a fired timer also fans a phone
        # notification out to the owner's PWA. The speaker announce stays
        # primary; a missing/disabled notifier just skips the push.
        self._notifier = notifier
        # Best-effort native notice (#1280): the same `ha` event kind an HA
        # household notice uses, so the app needs one receiver, not two. Added
        # ALONGSIDE the Web Push above, which is unchanged — the PWA channel
        # only goes away if somebody decides to remove it, not as a side effect
        # of this.
        self._event_bus = event_bus
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self._fire_due()
            except Exception as e:  # noqa: BLE001 — the loop must outlive any hiccup
                log.error("engine.scheduler.error", error=str(e))
            await asyncio.sleep(_POLL_S)

    async def _fire_due(self) -> None:
        now = datetime.now(UTC).isoformat()
        with _conn(self._db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM engine_timers WHERE status = 'pending' AND fire_at <= ?",
                (now,),
            ).fetchall()
        for row in rows:
            timer = dict(row)
            ok = await self._announce(timer)
            await self._push(timer)
            self._publish(timer)
            with _conn(self._db_path) as conn:
                conn.execute(
                    "UPDATE engine_timers SET status = ? WHERE id = ?",
                    ("fired" if ok else "failed", row["id"]),
                )
            log.info(
                "engine.timer.fired",
                timer_id=row["id"],
                kind=row["kind"],
                label=row["label"],
                delivered=ok,
            )

    def _payload(self, kind: str, label: str) -> dict[str, Any]:
        text = {
            "timer": f"Der Timer {label} ist abgelaufen."
            if label
            else "Der Timer ist abgelaufen.",
            "alarm": f"Wecker: {label}" if label else "Es ist Zeit aufzustehen.",
            "reminder": f"Erinnerung: {label}" if label else "Erinnerung.",
        }.get(kind, f"{kind}: {label}")
        # An alarm rings the configured sound; timers and reminders speak. The
        # sound only wins when its file is present — HA can't tell us up front
        # whether a media_id will play, so we fall back to the TTS text rather
        # than risk a silent alarm.
        if kind == "alarm" and self._alarm_sound_can_play():
            return {"media_id": self._alarm_sound_media_id}
        return {"message": text}

    async def _room_satellites(self, room: str, satellites: list[str]) -> list[str]:
        """`satellites` that sit in `room`; empty when the room can't be resolved."""
        room = room.strip()
        if not room:
            return []
        snap = await self._areas.snapshot()
        return [
            eid for eid in satellites if snap.area_of(eid).casefold() == room.casefold()
        ]

    async def _announce(self, timer: dict[str, Any]) -> bool:
        """Ring on the PE speaker via HA. True when HA accepted the call.

        Scoped to the satellite the timer was set on (#1187): reminder text is
        often private, so it is spoken only in the originating room. When that
        room is unknown (app/browser-set), has no satellite, or the satellite is
        offline, the announcement still rings on every satellite — but without
        the label, so nothing private is read out to the house. The owner's Web
        Push (`_push`) carries the full text either way.
        """
        if not self._hass_url or not self._hass_token:
            return False
        label = timer.get("label") or ""
        kind = timer.get("kind") or "timer"
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {"Authorization": f"Bearer {self._hass_token}"}
            async with aiohttp.ClientSession(timeout=timeout) as client:
                # The announce service requires a target (box-verified: a
                # target-less call is a 400).
                async with client.get(
                    f"{self._hass_url}/api/states", headers=headers
                ) as resp:
                    resp.raise_for_status()
                    states = await resp.json()
                satellites = [
                    s["entity_id"]
                    for s in states
                    if str(s.get("entity_id", "")).startswith("assist_satellite.")
                    and s.get("state") != "unavailable"
                ]
                if not satellites:
                    log.warn("engine.timer.no_satellites")
                    return False
                targets = await self._room_satellites(
                    str(timer.get("room") or ""), satellites
                )
                if not targets:
                    log.info(
                        "engine.timer.announce_house_wide", timer_id=timer.get("id")
                    )
                    targets, label = satellites, ""
                async with client.post(
                    f"{self._hass_url}/api/services/assist_satellite/announce",
                    json={"entity_id": targets, **self._payload(kind, label)},
                    headers=headers,
                ) as resp:
                    return resp.status < 400
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.error("engine.timer.announce_failed", error=str(e))
            return False

    async def _push(self, timer: dict[str, Any]) -> None:
        """Fan a fired timer out to the owner's phones (best-effort, #713).

        No-op without a notifier or when Web Push is unconfigured; any error is
        swallowed by the notifier so the timer loop is never affected."""
        if self._notifier is None:
            return
        label = timer.get("label") or ""
        kind = timer.get("kind") or "timer"
        title = _TITLES.get(kind, kind.capitalize())
        await self._notifier.push(
            timer.get("owner_uid") or "",
            title,
            label or title,
            {"kind": kind, "timer_id": timer.get("id")},
        )

    def _publish(self, timer: dict[str, Any]) -> None:
        """Publish a fired timer on the `ha` event kind (#1280).

        The same kind an HA household notice rides, carrying `category` so a
        resident can mute the house's notices without losing their timers. This
        is a third best-effort copy: the speaker announce above stays primary
        and `_push` (Web Push) is untouched.
        """
        if self._event_bus is None:
            return
        uid = timer.get("owner_uid") or ""
        if not uid:
            return
        kind = timer.get("kind") or "timer"
        title = _TITLES.get(kind, kind.capitalize())
        self._event_bus.publish(
            uid,
            ha_notify.EVENT_KIND,
            ha_notify.event_data(
                uid,
                title,
                timer.get("label") or title,
                category=ha_notify.TIMER_CATEGORIES.get(kind, "reminder"),
            ),
        )

    def _alarm_sound_can_play(self) -> bool:
        return bool(
            self._alarm_sound_media_id
            and self._alarm_sound_path
            and os.path.isfile(self._alarm_sound_path)
        )
