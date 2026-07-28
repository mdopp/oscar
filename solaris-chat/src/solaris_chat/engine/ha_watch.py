"""HA WebSocket state watcher — live card_state onto the event bus (#714).

A persistent async task (started in __main__ like `TimerScheduler`) that holds a
`/api/websocket` connection to Home Assistant, authenticates with the existing
`hass_token`, and subscribes to `state_changed` events. It is bounded to the
*pinned* entities: the union of every resident's pinned HA entities (from
`favorites_store`). On a change to a pinned entity it builds the fresh card-spec
(reusing `card_spec`) and publishes a `card_state` event to the uids that pinned
it — owner-scoped pins to the owner, the shared `household` pin to the
`HOUSEHOLD` sentinel (the SSE endpoint subscribes each client to both).

Resilient: any drop reconnects with capped backoff; the pinned set is re-derived
on connect and refreshed periodically so a new pin starts flowing without a
restart. HA stays the device tool — this only *reads* state changes.

Two filters sit in front of the bus so a fast-changing pin doesn't become a
per-second full card-spec down an SSE connection the phone holds open all day
(#1094): a card identical to the last one sent is dropped, and `sensor` cards —
read-only numbers, never a push source — are rate-limited with a guaranteed
trailing flush so the latest value always arrives.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from solaris_chat import favorites_store
from solaris_chat.engine.notify import EventBus
from solaris_chat.engine.tools.ha import card_spec
from solaris_chat.favorites_store import HOUSEHOLD
from solaris_chat.logging import log

_BACKOFF_START_S = 2.0
_BACKOFF_MAX_S = 60.0
# Re-derive the pinned set this often so a new pin/unpin starts/stops flowing
# without a reconnect (a favorites change also re-derives on the next connect).
_PIN_REFRESH_S = 60.0

# Selective Web Push (#714): only these transitions push a notification while
# the app is closed (no open SSE client) — a cover/door opening or closing, a
# security/lock state flip. Lights/dimmers propagate over SSE only; they must
# NOT ring a phone. A small explicit allowlist by domain (device_class for
# cover, to keep e.g. an awning quiet).
_NOTEWORTHY_DOMAINS = frozenset({"lock", "binary_sensor"})
_NOTEWORTHY_COVER_CLASSES = frozenset({"garage", "door", "gate"})
_NOTEWORTHY_BINARY_CLASSES = frozenset({"door", "garage_door", "opening", "window"})

# Rate limit for `sensor` cards only (#1094): a pinned power/energy sensor can
# report every second, and its card is a number nobody touches. Actuators
# (light/switch/cover/climate/media_player) stay immediate — the whole point of
# #714 is that a switch reacts in ~1s — and no throttled domain is noteworthy
# (`_is_noteworthy` never matches `sensor`), so this can't delay a push.
# Leading edge fires at once, then at most one card per interval, and the last
# value is always flushed: a debounce would starve a sensor that never goes
# quiet and freeze its card on a stale reading.
_THROTTLED_DOMAINS = frozenset({"sensor"})
_THROTTLE_INTERVAL_S = 10.0


def _is_noteworthy(card: dict[str, Any]) -> bool:
    domain = card.get("domain")
    if domain == "cover":
        return card.get("device_class") in _NOTEWORTHY_COVER_CLASSES
    if domain == "binary_sensor":
        return card.get("device_class") in _NOTEWORTHY_BINARY_CLASSES
    return domain in _NOTEWORTHY_DOMAINS


class HaStateWatcher:
    def __init__(
        self,
        hass_url: str,
        hass_token: str,
        bus: EventBus,
        db_path: str,
        notifier: Any = None,
        native_watch: Any = None,
    ) -> None:
        self._hass_url = hass_url.rstrip("/")
        self._hass_token = hass_token
        self._bus = bus
        self._db_path = db_path
        self._notifier = notifier
        self._native_watch = native_watch
        self._task: asyncio.Task | None = None
        # entity_id -> owner uids that pinned it (HOUSEHOLD for the shared pin).
        self._owners: dict[str, set[str]] = {}
        self._refresh_at = 0.0
        # Live WS reachability, flipped on the loop thread; a plain bool read is
        # atomic in CPython so `status` needs no lock.
        self._connected = False
        # Per-entity send filters (#1094), all keyed by entity_id and bounded by
        # the watched set (pruned in `_refresh_pins`): last card sent, earliest
        # next send for a throttled domain, the pending latest card, and its
        # trailing-flush task.
        self._last_card: dict[str, dict[str, Any]] = {}
        self._ready_at: dict[str, float] = {}
        self._pending: dict[str, tuple[set[str], dict[str, Any]]] = {}
        self._flush_tasks: dict[str, asyncio.Task] = {}

    @property
    def status(self) -> str:
        """Authoritative HA health for the start page (#729): 'disabled' when no
        url/token is configured, 'connected' while the WS is authenticated and
        live, 'disconnected' when configured but the WS is down / auth failing."""
        if not self._hass_url or not self._hass_token:
            return "disabled"
        return "connected" if self._connected else "disconnected"

    def start(self) -> None:
        if not self._hass_url or not self._hass_token:
            log.info("engine.ha_watch.disabled")
            return
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        self._forget_all()
        if self._task:
            self._task.cancel()

    def _forget(self, entity_id: str) -> None:
        self._last_card.pop(entity_id, None)
        self._ready_at.pop(entity_id, None)
        self._pending.pop(entity_id, None)
        task = self._flush_tasks.pop(entity_id, None)
        if task is not None:
            task.cancel()

    def _forget_all(self) -> None:
        for entity_id in self._last_card.keys() | self._flush_tasks.keys():
            self._forget(entity_id)

    def _refresh_pins(self) -> None:
        # Watched entities = web-pinned favorites ∪ per-device native watch-sets
        # (#810), so a native widget can watch an entity nobody favorited.
        owners = favorites_store.pinned_entity_owners(self._db_path)
        if self._native_watch is not None:
            for entity_id, uids in self._native_watch.native_watch_owners().items():
                owners.setdefault(entity_id, set()).update(uids)
        self._owners = owners
        for entity_id in [e for e in self._last_card if e not in owners]:
            self._forget(entity_id)

    async def _run(self) -> None:
        backoff = _BACKOFF_START_S
        while True:
            try:
                await self._connect_and_watch()
                backoff = _BACKOFF_START_S  # a clean return resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the watcher must outlive any drop
                log.error("engine.ha_watch.error", error=str(e))
            finally:
                self._connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _connect_and_watch(self) -> None:
        ws_url = self._hass_url.replace("http", "ws", 1) + "/api/websocket"
        # A pending flush belongs to the connection it was queued on; after a
        # drop the first event per entity goes out unfiltered again.
        self._forget_all()
        self._refresh_pins()
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, heartbeat=30) as ws:
                await self._authenticate(ws)
                await ws.send_json(
                    {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
                )
                self._connected = True
                log.info("engine.ha_watch.connected", pinned=len(self._owners))
                loop = asyncio.get_event_loop()
                self._refresh_at = loop.time() + _PIN_REFRESH_S
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    if loop.time() >= self._refresh_at:
                        self._refresh_pins()
                        self._refresh_at = loop.time() + _PIN_REFRESH_S
                    self._on_message(json.loads(msg.data))

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # HA sends `auth_required`, we reply with the token, it answers
        # `auth_ok`/`auth_invalid`. An invalid token raises so the backoff loop
        # keeps retrying rather than silently watching nothing.
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": self._hass_token})
        reply = await ws.receive_json()
        if reply.get("type") != "auth_ok":
            raise RuntimeError(f"ha auth failed: {reply.get('type')}")

    def _on_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") != "event":
            return
        data = (msg.get("event") or {}).get("data") or {}
        entity_id = str(data.get("entity_id") or "")
        owners = self._owners.get(entity_id)
        if not owners:
            return
        new_state = data.get("new_state") or {}
        card = card_spec(
            entity_id,
            new_state.get("state"),
            new_state.get("attributes") or {},
            new_state.get("last_updated"),
        )
        if card is None:
            return
        noteworthy = self._notifier is not None and _is_noteworthy(card)
        if self._is_new_card(entity_id, card):
            self._send(entity_id, owners, card)
        for uid in owners:
            # Push only when nobody is watching this uid live and the transition
            # is noteworthy; an SSE client already saw it (HOUSEHOLD reaches no
            # single phone, so it never pushes).
            if noteworthy and uid != HOUSEHOLD and not self._bus.has_subscriber(uid):
                asyncio.ensure_future(self._push(uid, card))

    def _is_new_card(self, entity_id: str, card: dict[str, Any]) -> bool:
        """False when the card renders exactly what was last sent (#1094).

        HA emits `state_changed` for attribute churn the card never shows, so an
        unchanged card is nothing to send. `updated_at_ms` is excluded from the
        comparison: it moves on every event by construction and is an ordering
        stamp, not content."""
        sig = {k: v for k, v in card.items() if k != "updated_at_ms"}
        if self._last_card.get(entity_id) == sig:
            return False
        self._last_card[entity_id] = sig
        return True

    def _send(self, entity_id: str, owners: set[str], card: dict[str, Any]) -> None:
        """Publish now, or hold the latest card for the trailing flush (#1094)."""
        if card.get("domain") not in _THROTTLED_DOMAINS:
            self._fan_out(entity_id, owners, card)
            return
        now = asyncio.get_event_loop().time()
        ready_at = self._ready_at.get(entity_id, 0.0)
        if now >= ready_at:
            self._ready_at[entity_id] = now + _THROTTLE_INTERVAL_S
            self._fan_out(entity_id, owners, card)
            return
        self._pending[entity_id] = (set(owners), card)
        if entity_id not in self._flush_tasks:
            self._flush_tasks[entity_id] = asyncio.ensure_future(
                self._flush_later(entity_id, ready_at - now)
            )

    async def _flush_later(self, entity_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            pending = self._pending.pop(entity_id, None)
            if pending is None:
                return
            owners, card = pending
            self._ready_at[entity_id] = (
                asyncio.get_event_loop().time() + _THROTTLE_INTERVAL_S
            )
            self._fan_out(entity_id, owners, card)
        finally:
            # Only de-register self: a cancelled task resumes late, by which
            # time a fresh flush may already be registered for this entity.
            if self._flush_tasks.get(entity_id) is asyncio.current_task():
                del self._flush_tasks[entity_id]

    def _fan_out(self, entity_id: str, owners: set[str], card: dict[str, Any]) -> None:
        for uid in owners:
            self._bus.publish(uid, "card_state", {"entity_id": entity_id, "card": card})

    async def _push(self, uid: str, card: dict[str, Any]) -> None:
        name = card.get("name") or card.get("entity_id")
        state = card.get("state") or ""
        await self._notifier.push(
            uid,
            str(name),
            str(state),
            {"kind": "card_state", "entity_id": card.get("entity_id")},
        )
