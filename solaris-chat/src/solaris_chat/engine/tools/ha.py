"""Home Assistant tools — control, state, discovery.

The injected entity registry (registry.py) makes `ha_call_service` a one-pass
action; `ha_get_state`/`ha_list_entities` stay for state questions (the soul
rule: read live state, never answer device questions from memory).

The domain/service validation is ported from the Hermes tool it replaces:
the names are interpolated into `/api/services/{domain}/{service}`, so the
regex blocks path traversal and the blocklist keeps arbitrary-code domains
(shell_command & friends) unreachable no matter what the model asks for.
"""

from __future__ import annotations

import contextvars
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from solaris_chat.engine.areas import AreaRegistry
from solaris_chat.engine.tools import Tool

# Per-turn sink for the read-only state cards a turn surfaces (#475). Each HA
# state read appends a card-spec here; the engine loop drains it at turn end and
# emits a `ha_cards` event. A contextvar so the tools (built once per profile)
# attribute cards to the turn that is actually running.
card_sink: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "ha_card_sink", default=None
)

# Domains that get a read-only state card in phase 1. Sensors render a value
# card (state + unit); binary_sensor/cover.garage an open/closed status; the
# actionable light/switch a current-state badge (controls are later phases).
# media_player (#541) cards the playing/paused state + transport/volume controls.
_CARD_DOMAINS = frozenset(
    {"sensor", "binary_sensor", "cover", "light", "switch", "climate", "media_player"}
)
# A room query ("zeig mir das Wohnzimmer") cards the room's ACTUATORS — the
# controllable card domains, not the read-only sensors (#540). Sensor data
# stays on-demand via ha_list_entities.
_ROOM_ACTUATOR_DOMAINS = frozenset(
    {"light", "switch", "cover", "climate", "media_player"}
)
# Attributes the phase-3 controls (sliders/colour/climate) read off the card-spec
# so the frontend can feature-gate them without a second HA round-trip (#477).
_CONTROL_ATTRS = (
    "supported_features",
    "brightness",
    "color_temp",
    "rgb_color",
    "color_mode",
    "supported_color_modes",
    "current_position",
    "temperature",
    "current_temperature",
    "hvac_action",
    "target_temp_step",
    "min_temp",
    "max_temp",
    "hvac_modes",
    # media_player (#541): volume + what's playing for the transport card.
    "volume_level",
    "media_title",
    "media_artist",
)


def _emit_card(entity_id: str, name: str, state: Any, attrs: dict[str, Any]) -> None:
    """Append a card-spec for one entity to the turn's sink (#475, #477)."""
    sink = card_sink.get()
    if sink is None:
        return
    domain = entity_id.split(".", 1)[0]
    if domain not in _CARD_DOMAINS:
        return
    if any(c["entity_id"] == entity_id for c in sink):
        return
    spec = {
        "entity_id": entity_id,
        "name": name,
        "domain": domain,
        "device_class": attrs.get("device_class"),
        "state": None if state is None else str(state),
        "unit": attrs.get("unit_of_measurement"),
    }
    for key in _CONTROL_ATTRS:
        if attrs.get(key) is not None:
            spec[key] = attrs[key]
    sink.append(spec)


# State-scope detection (#536): when a query asks which entities are in a given
# state ("welche lichter sind AN"), the cards should cover only the matching
# entities, not every entity the model state-read. Each entry maps query cues to
# the on-/off-style states a card's `state` may carry. A query that names no
# state (e.g. "welche lichter gibt es") matches nothing here ⇒ no filtering.
_STATE_SCOPES: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (frozenset({"an", "eingeschaltet", "on"}), frozenset({"on"})),
    (frozenset({"aus", "ausgeschaltet", "off"}), frozenset({"off"})),
    (frozenset({"offen", "geöffnet", "open"}), frozenset({"open"})),
    (
        frozenset({"geschlossen", "zu", "closed"}),
        frozenset({"closed"}),
    ),
)
_WORD_RE = re.compile(r"[a-zäöüß]+")


def filter_cards_by_query_state(
    cards: list[dict[str, Any]], query: str
) -> list[dict[str, Any]]:
    """Narrow a turn's cards to the state the query asked about (#536).

    "welche lichter sind an" ⇒ keep only cards whose `state` is on; a query that
    names no state ("welche lichter gibt es") is returned unchanged so existence
    questions still show the full set."""
    words = set(_WORD_RE.findall(query.lower()))
    wanted: set[str] = set()
    for cues, states in _STATE_SCOPES:
        if words & cues:
            wanted |= states
    if not wanted:
        return cards
    return [c for c in cards if str(c.get("state") or "").lower() in wanted]


# Above this many cards a flat list is hard to scan, so we group by room (#537).
_GROUP_THRESHOLD = 4


def group_cards_by_room(
    cards: list[dict[str, Any]], entity_area: dict[str, str]
) -> bool:
    """Annotate cards with their room and decide whether to group by room (#537).

    Mutates each card with a `room` field (the entity's area, "" when unknown).
    A room query (#540) yields a set that all resolves to one non-empty room —
    that always groups under the single header regardless of count. Otherwise,
    with more than `_GROUP_THRESHOLD` cards, grouping applies only when **every**
    room would hold ≥2 cards — then returns True (the frontend renders one group
    per room with a header). If grouping would leave a singleton room, returns
    False: the cards still carry their `room` so the frontend labels each card,
    but renders them ungrouped. ≤4 mixed-room cards also carry their `room` (so
    each is labelled) but render ungrouped."""
    rooms = [entity_area.get(str(c.get("entity_id") or ""), "") for c in cards]
    single_room = len(cards) > 1 and len(set(rooms)) == 1 and rooms[0] != ""
    counts: dict[str, int] = {}
    for c, room in zip(cards, rooms):
        c["room"] = room
        counts[room] = counts.get(room, 0) + 1
    if len(cards) <= _GROUP_THRESHOLD and not single_room:
        return False
    if single_room:
        return True
    return all(n >= 2 for n in counts.values())


def _last_updated_ms(iso: str | None) -> int | None:
    """HA `last_updated` (ISO-8601) → epoch **milliseconds**, or None when
    absent/unparseable.

    This is the source-authoritative ordering stamp the companion app needs to
    drop stale/out-of-order card updates (the multi-device toggle "spring",
    solaris-android #66). HA is the single writer, so `last_updated` is monotonic
    per entity — unlike a client wall-clock or SSE receive-time, which drift and
    jitter across devices. A naive timestamp is treated as UTC.
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def card_spec(
    entity_id: str,
    state: Any,
    attrs: dict[str, Any],
    last_updated: str | None = None,
) -> dict[str, Any] | None:
    """Build one renderable card-spec from a live HA state, or None if the
    entity's domain has no card (#502 concept page reuses this; same shape as
    `_emit_card`).

    `last_updated` is the HA state's ISO timestamp; when given it is forwarded as
    `updated_at_ms` (epoch-ms) so the companion app can order concurrent updates
    and reject stale ones (#850 / solaris-android #66)."""
    domain = entity_id.split(".", 1)[0]
    if domain not in _CARD_DOMAINS:
        return None
    spec: dict[str, Any] = {
        "entity_id": entity_id,
        "name": attrs.get("friendly_name") or entity_id,
        "domain": domain,
        "device_class": attrs.get("device_class"),
        "state": None if state is None else str(state),
        "unit": attrs.get("unit_of_measurement"),
    }
    ms = _last_updated_ms(last_updated)
    if ms is not None:
        spec["updated_at_ms"] = ms
    for key in _CONTROL_ATTRS:
        if attrs.get(key) is not None:
            spec[key] = attrs[key]
    return spec


async def fetch_card(
    hass_url: str, hass_token: str, entity_id: str
) -> dict[str, Any] | None:
    """Fetch one entity's live state and return its card-spec (read-only).

    The concept page (#502) calls this when the page id is an HA entity; returns
    None for an unknown entity, a non-card domain, or any HA error so the page
    just omits the live card.
    """
    if not _ENTITY_RE.match(entity_id):
        return None
    headers = {"Authorization": f"Bearer {hass_token}"}
    url = hass_url.rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(
                f"{url}/api/states/{entity_id}", headers=headers
            ) as resp:
                if resp.status >= 400:
                    return None
                body = await resp.json()
    except aiohttp.ClientError:
        return None
    return card_spec(
        entity_id,
        body.get("state"),
        body.get("attributes") or {},
        body.get("last_updated"),
    )


async def fetch_entity_names(
    hass_url: str, hass_token: str
) -> list[dict[str, str]] | None:
    """`[{entity_id, name}]` for every HA entity, for the auto-linkify index
    (#694). One read-only `/api/states`; `name` is the friendly_name (falling
    back to the entity_id). Returns None on any HA error."""
    headers = {"Authorization": f"Bearer {hass_token}"}
    url = hass_url.rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                if resp.status >= 400:
                    return None
                states = await resp.json()
    except aiohttp.ClientError:
        return None
    names: list[dict[str, str]] = []
    for s in states:
        eid = str(s.get("entity_id") or "")
        if not eid:
            continue
        attrs = s.get("attributes") or {}
        names.append({"entity_id": eid, "name": str(attrs.get("friendly_name") or eid)})
    return names


async def fetch_addable_cards(
    hass_url: str, hass_token: str, entity_area: dict[str, str]
) -> list[dict[str, Any]] | None:
    """Card-specs for the house's controllable actuators, for the start-page
    picker (#669). One read-only `/api/states`; keeps only the actuator domains
    (light/switch/cover/climate/media_player — the same set a room query cards),
    builds each entity's card-spec and annotates it with its room. Returns None
    on any HA error; entities without an area carry room "".
    """
    headers = {"Authorization": f"Bearer {hass_token}"}
    url = hass_url.rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                if resp.status >= 400:
                    return None
                states = await resp.json()
    except aiohttp.ClientError:
        return None
    cards: list[dict[str, Any]] = []
    for s in states:
        eid = str(s.get("entity_id") or "")
        if eid.split(".", 1)[0] not in _ROOM_ACTUATOR_DOMAINS:
            continue
        spec = card_spec(
            eid, s.get("state"), s.get("attributes") or {}, s.get("last_updated")
        )
        if spec is None:
            continue
        spec["room"] = entity_area.get(eid, "")
        cards.append(spec)
    cards.sort(key=lambda c: (c["room"].lower(), c["name"].lower()))
    return cards


async def fetch_addable_runnables(
    hass_url: str, hass_token: str
) -> list[dict[str, Any]] | None:
    """Scenes/scripts/automations offered as addable ACTION cards (#702).

    One read-only `/api/states`; keeps the runnable domains (scene/script/
    automation) and returns `{entity_id, name, domain}` for each so the picker
    can pin them as `{tool: "ha_run_scene_script", args: {entity: <id>}}`. These
    are non-sensitive one-shot routines — they run on tap like other action
    cards. Returns None on any HA error.
    """
    headers = {"Authorization": f"Bearer {hass_token}"}
    url = hass_url.rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                if resp.status >= 400:
                    return None
                states = await resp.json()
    except aiohttp.ClientError:
        return None
    out: list[dict[str, Any]] = []
    for s in states:
        eid = str(s.get("entity_id") or "")
        domain = eid.split(".", 1)[0]
        if domain not in _RUNNABLE_DOMAINS:
            continue
        name = str((s.get("attributes") or {}).get("friendly_name") or eid)
        out.append({"entity_id": eid, "name": name, "domain": domain})
    out.sort(key=lambda r: r["name"].lower())
    return out


async def fetch_cameras(
    hass_url: str, hass_token: str, entity_area: dict[str, str]
) -> list[dict[str, Any]] | None:
    """Camera entities for the Android camera-widget picker (#779). The addable
    actuator list omits cameras, so this does the ONE read-only `/api/states`,
    keeps only `camera.*`, and returns `{entity_id, name, room}` for each — `room`
    is the area friendly name from the same snapshot the actuator picker uses.
    Returns None on any HA error; entities without an area carry room "".
    """
    headers = {"Authorization": f"Bearer {hass_token}"}
    url = hass_url.rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                if resp.status >= 400:
                    return None
                states = await resp.json()
    except aiohttp.ClientError:
        return None
    out: list[dict[str, Any]] = []
    for s in states:
        eid = str(s.get("entity_id") or "")
        if eid.split(".", 1)[0] != "camera":
            continue
        name = str((s.get("attributes") or {}).get("friendly_name") or eid)
        out.append({"entity_id": eid, "name": name, "room": entity_area.get(eid, "")})
    out.sort(key=lambda c: (c["room"].lower(), c["name"].lower()))
    return out


# Current-power (W) flow sensors for the "Jetzt" picture + the trend chart
# (#691). Matched by EXACT friendly_name and require device_class=power / unit W
# so the kWh lifetime counters (PV Erzeugung, Netzbezug, …) never leak into the
# flow. `sense` fixes the sign convention:
#   supply  → ≥0 means "liefert/erzeugt" (green)
#   draw    → ≥0 means "Verbrauch" (red)
#   grid    → +W = Bezug (draw/red), −W = Einspeisung (supply/green)
#   battery → −W = entlädt (supply/green), +W = lädt (draw/red)
_ENERGY_FLOW = (
    ("pv", "PV", "Aktuell erzeugter PV-Strom", "supply"),
    ("house", "Haus", "Aktueller Hausverbrauch", "draw"),
    ("grid", "Netz", "Aktuelle Netz Leistung", "grid"),
    ("battery", "Akku", "Aktuelle Akku Leistung", "battery"),
)
# Lifetime energy (kWh) counters — the "Energie gesamt" totals, NEVER the flow.
_ENERGY_TOTALS = (
    ("PV Erzeugung", "PV-Erzeugung"),
    ("Netzeinspeisung", "Einspeisung"),
    ("Netzbezug", "Netzbezug"),
    ("Batterie laden", "Batterie geladen"),
    ("Batterie entladen", "Batterie entladen"),
)


def _bucket_energy_states(
    states: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Sort HA states into flow (W) / totals (kWh) / per-circuit power (#691).

    Shared by `fetch_energy` (live picture) and `fetch_energy_history` (which
    reuses the flow entity_ids to query their history). Flow buckets are keyed
    by exact friendly_name and require device_class=power; totals by exact
    friendly_name and device_class=energy; leftover power sensors fall through
    to the circuit list.
    """
    flow_by_name = {name: key for key, _, name, _ in _ENERGY_FLOW}
    totals_by_name = {name: label for name, label in _ENERGY_TOTALS}
    flow: dict[str, dict[str, Any]] = {}
    totals: dict[str, dict[str, Any]] = {}
    circuits: list[dict[str, Any]] = []
    for s in states:
        eid = str(s.get("entity_id") or "")
        if not eid.startswith("sensor."):
            continue
        attrs = s.get("attributes") or {}
        dclass = str(attrs.get("device_class") or "").lower()
        if dclass not in ("power", "energy"):
            continue
        name = str(attrs.get("friendly_name") or eid)
        spec = {
            "entity_id": eid,
            "name": name,
            "state": None if s.get("state") is None else str(s.get("state")),
            "unit": attrs.get("unit_of_measurement"),
            "device_class": dclass,
        }
        if (
            dclass == "power"
            and name in flow_by_name
            and flow_by_name[name] not in flow
        ):
            flow[flow_by_name[name]] = spec
        elif dclass == "energy" and name in totals_by_name:
            totals.setdefault(name, {**spec, "label": totals_by_name[name]})
        elif dclass == "power":
            circuits.append(spec)
    return flow, totals, circuits


async def fetch_energy(hass_url: str, hass_token: str) -> dict[str, Any] | None:
    """Compose the home-energy picture from live HA state (read-only, #503/#691).

    One `/api/states` read. The "Jetzt" flow (PV/Haus/Netz/Akku) comes from the
    current-power (W) sensors with a sign-corrected direction; the lifetime kWh
    counters are returned separately as `totals` ("Energie gesamt"), never mixed
    into the flow. Leftover power sensors become the per-circuit list. Returns
    None on any HA error.
    """
    headers = {"Authorization": f"Bearer {hass_token}"}
    url = hass_url.rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                if resp.status >= 400:
                    return None
                states = await resp.json()
    except aiohttp.ClientError:
        return None
    flow, totals, circuits = _bucket_energy_states(states)
    circuits.sort(key=lambda c: c["name"].lower())
    return {
        "flow": [
            {**flow[key], "label": label, "sense": sense}
            for key, label, _, sense in _ENERGY_FLOW
            if key in flow
        ],
        "totals": [totals[name] for name, _ in _ENERGY_TOTALS if name in totals],
        "circuits": circuits,
    }


def _downsample(points: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    """Thin a time-series to ~target points by even stride, keeping the last one
    so the trend ends at "now"."""
    if len(points) <= target:
        return points
    step = len(points) / target
    kept = [points[int(i * step)] for i in range(target)]
    if kept[-1] is not points[-1]:
        kept.append(points[-1])
    return kept


async def fetch_energy_history(
    hass_url: str, hass_token: str, hours: int
) -> dict[str, Any] | None:
    """Per-series current-power (W) history for the flow sensors (#689/#691).

    Resolves the same current-power (W) flow sensors `fetch_energy` finds (one
    `/api/states` read, reusing its bucketing) — the kWh lifetime counters are
    deliberately excluded so the chart is single-axis (W). Then one batched
    `/api/history/period` query for their entity_ids. Each series is downsampled
    to a small point count so the payload stays light. Returns None on any HA
    error; empty `series` when no flow sensors resolve (the frontend degrades
    gracefully).
    """
    headers = {"Authorization": f"Bearer {hass_token}"}
    url = hass_url.rstrip("/")
    target = 48 if hours <= 24 else 84
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                if resp.status >= 400:
                    return None
                states = await resp.json()
            flow, _, _ = _bucket_energy_states(states)
            ordered = [
                {**flow[key], "label": label}
                for key, label, _, _ in _ENERGY_FLOW
                if key in flow
            ]
            if not ordered:
                return {"hours": hours, "series": []}
            end = datetime.now(UTC)
            start = end - timedelta(hours=hours)
            params = {
                "filter_entity_id": ",".join(h["entity_id"] for h in ordered),
                "end_time": end.isoformat(),
                "minimal_response": "true",
                "no_attributes": "true",
            }
            async with client.get(
                f"{url}/api/history/period/{start.isoformat()}",
                params=params,
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    return None
                body = await resp.json()
    except aiohttp.ClientError:
        return None
    # HA returns one list per entity, but not necessarily in the requested
    # order — key each returned list by its first point's entity_id.
    by_eid: dict[str, list[dict[str, Any]]] = {}
    for run in body if isinstance(body, list) else []:
        if run and isinstance(run, list):
            eid = str(run[0].get("entity_id") or "")
            if eid:
                by_eid[eid] = run
    series = []
    for h in ordered:
        raw = by_eid.get(h["entity_id"], [])
        points = []
        for p in raw:
            when = p.get("last_changed") or p.get("last_updated")
            try:
                val = float(p.get("state"))
            except (TypeError, ValueError):
                continue
            if when:
                points.append({"t": when, "v": val})
        series.append(
            {
                "entity_id": h["entity_id"],
                "label": h["label"],
                "unit": h.get("unit"),
                "points": _downsample(points, target),
            }
        )
    return {"hours": hours, "series": series}


# Range presets for the per-entity history endpoint (#755): a native widget's
# sparkline picks one of these; unknown values fall back to the default 24h.
_ENTITY_HISTORY_RANGES = {"24h": 24, "48h": 48, "7d": 168}
_ENTITY_HISTORY_DEFAULT = "24h"


async def fetch_entity_history(
    hass_url: str, hass_token: str, entity_id: str, range: str
) -> list[dict[str, Any]] | None:
    """Raw state history for one entity, for the native widget sparkline (#755).

    Mirrors `fetch_energy_history`'s HA `/api/history/period` call and time
    window, but for a single caller-supplied entity and returning the raw
    `[{t, state}, …]` points (no bucketing/flow logic). `range` is a preset key
    (24h/48h/7d); an unknown value falls back to 24h. Returns None on any HA
    error; an empty list when the entity has no history in the window.
    """
    if not _ENTITY_RE.match(entity_id):
        return None
    hours = _ENTITY_HISTORY_RANGES.get(
        range, _ENTITY_HISTORY_RANGES[_ENTITY_HISTORY_DEFAULT]
    )
    headers = {"Authorization": f"Bearer {hass_token}"}
    url = hass_url.rstrip("/")
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    params = {
        "filter_entity_id": entity_id,
        "end_time": end.isoformat(),
        "minimal_response": "true",
        "no_attributes": "true",
    }
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(
                f"{url}/api/history/period/{start.isoformat()}",
                params=params,
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    return None
                body = await resp.json()
    except aiohttp.ClientError:
        return None
    # HA returns [[ {state, last_changed}, … ]] — one list per entity.
    series = body[0] if body and isinstance(body[0], list) else []
    points: list[dict[str, Any]] = []
    for p in series:
        when = p.get("last_changed") or p.get("last_updated")
        if when:
            points.append({"t": when, "state": p.get("state")})
    return points


async def fetch_camera_snapshot(
    hass_url: str, hass_token: str, entity_id: str
) -> tuple[bytes, str] | None:
    """Current still image for a `camera.*` entity, for the Android camera widget
    (#770). Reads HA's `/api/camera_proxy/{entity_id}` (read-only) with the
    `HASS_TOKEN` bearer and returns `(image_bytes, content_type)`.

    Enforces the `camera` domain (privacy-sensitive live image): a non-camera or
    malformed `entity_id` returns None so the caller answers 400. Returns None on
    any HA error too.
    """
    if not _ENTITY_RE.match(entity_id) or not entity_id.startswith("camera."):
        return None
    headers = {"Authorization": f"Bearer {hass_token}"}
    url = hass_url.rstrip("/")
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(
                f"{url}/api/camera_proxy/{entity_id}", headers=headers
            ) as resp:
                if resp.status >= 400:
                    return None
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                image = await resp.read()
    except aiohttp.ClientError:
        return None
    return image, content_type


_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_BLOCKED_DOMAINS = frozenset(
    {"shell_command", "python_script", "pyscript", "hassio", "homeassistant"}
)
_TIMEOUT = aiohttp.ClientTimeout(total=15)
# Domains that "list/run scripts, automations, scenes" operates on (#370).
_RUNNABLE_DOMAINS = ("scene", "script", "automation")
# Run service per runnable domain: scenes/scripts turn_on, automations trigger.
_RUN_SERVICE = {"scene": "turn_on", "script": "turn_on", "automation": "trigger"}
# Some domains name their actions verb_<domain> rather than the bare verb the
# model tends to guess (cover has no `open`, only `open_cover`) — map the
# known-safe aliases so a natural "open" reaches the right HA service (#379).
_SERVICE_ALIASES = {
    "cover": {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"},
}
_HISTORY_DEFAULT_DAYS = 7
_HISTORY_MAX_TRANSITIONS = 20


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


async def resolve_entity_ref(hass_url: str, hass_token: str, ref: str) -> str:
    """Resolve a model-supplied reference to a real entity_id, "" on no match.

    A literal id is honoured ONLY if it actually exists — the model often
    guesses one from the name (e.g. `light.sofalicht` for "Sofalicht", whose
    real id is `light.dimmer_2_5`); a phantom id would otherwise sail through
    and return an empty result, reading as "never happened". So: an existing id
    wins; otherwise match the readable part against friendly_name (exact, then
    substring), preferring the guessed domain. Shared by `build_ha_tools` and the
    pin handler (#645) — no behaviour change from the old ha.py closure."""
    ref = ref.strip()
    if not ref:
        return ""
    url = hass_url.rstrip("/")
    headers = {"Authorization": f"Bearer {hass_token}"}
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
        async with client.get(f"{url}/api/states", headers=headers) as resp:
            resp.raise_for_status()
            states = await resp.json()
    ids = {str(s.get("entity_id") or "") for s in states}
    if ref in ids:
        return ref
    # A guessed-but-missing id: search by its slug, biased to its domain.
    domain = ""
    term = ref
    if _ENTITY_RE.match(ref):
        domain, slug = ref.split(".", 1)
        term = slug.replace("_", " ")
    wanted = term.lower()
    best: tuple[int, str] | None = None
    for s in states:
        eid = str(s.get("entity_id") or "")
        name = str((s.get("attributes") or {}).get("friendly_name") or "").lower()
        in_dom = bool(domain) and eid.startswith(domain + ".")
        if name == wanted:
            pri = 0 if (not domain or in_dom) else 2
        elif wanted and wanted in name:
            pri = 1 if (not domain or in_dom) else 3
        else:
            continue
        if best is None or pri < best[0]:
            best = (pri, eid)
            if pri == 0:
                break
    return best[1] if best else ""


async def call_service_scoped(
    hass_url: str,
    hass_token: str,
    entity_id: str,
    service: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a single `domain.service` on one entity, returning only ok/error.

    The card-action path (#476): same domain/service allowlist as the
    `ha_call_service` tool — the names go into `/api/services/{domain}/{service}`
    so the regex blocks path traversal and `_BLOCKED_DOMAINS` keeps
    arbitrary-code domains unreachable. `service` is dotted (`light.toggle`); its
    domain must match the entity's so a card can only act on its own entity.
    """
    if not _ENTITY_RE.match(entity_id):
        return {"ok": False, "error": "invalid entity_id"}
    domain, _, action = service.partition(".")
    if not action or not _NAME_RE.match(domain) or not _NAME_RE.match(action):
        return {"ok": False, "error": "invalid service"}
    if domain in _BLOCKED_DOMAINS:
        return {"ok": False, "error": f"domain {domain} is not allowed"}
    if domain != entity_id.split(".", 1)[0]:
        return {"ok": False, "error": "service domain does not match entity"}
    headers = {
        "Authorization": f"Bearer {hass_token}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {"entity_id": entity_id}
    if isinstance(data, dict):
        payload.update(data)
    url = hass_url.rstrip("/")
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
        async with client.post(
            f"{url}/api/services/{domain}/{action}", json=payload, headers=headers
        ) as resp:
            if resp.status >= 400:
                detail = (await resp.text())[:200]
                return {"ok": False, "error": f"HA {resp.status}: {detail}"}
    # No immediate state read-back: a GET right after the POST races HA's own
    # state settle and often returns the STALE pre-action state (#732), which the
    # card client would apply over its optimistic target and bounce the toggle.
    # Success is enough — the authoritative new state arrives via the SSE
    # card_state bus / poll. No caller reads the returned state.
    return {"ok": True}


def build_ha_tools(hass_url: str, hass_token: str) -> list[Tool]:
    url = hass_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {hass_token}",
        "Content-Type": "application/json",
    }
    areas = AreaRegistry(hass_url, hass_token)

    async def list_rooms(args: dict[str, Any]) -> str:
        snap = await areas.snapshot()
        return json.dumps({"rooms": snap.rooms}, ensure_ascii=False)

    async def call_service(args: dict[str, Any]) -> str:
        domain = str(args.get("domain") or "")
        service = str(args.get("service") or "")
        entity_id = str(args.get("entity_id") or "")
        if not _NAME_RE.match(domain) or not _NAME_RE.match(service):
            return '{"error": "invalid domain or service name"}'
        if domain in _BLOCKED_DOMAINS:
            return f'{{"error": "domain {domain} is not allowed"}}'
        service = _SERVICE_ALIASES.get(domain, {}).get(service, service)
        payload: dict[str, Any] = {"entity_id": entity_id} if entity_id else {}
        data = args.get("data")
        if isinstance(data, dict):
            payload.update(data)
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.post(
                f"{url}/api/services/{domain}/{service}",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:200]
                    return json.dumps({"error": f"HA {resp.status}: {detail}"})
        return json.dumps({"success": True, "service": f"{domain}.{service}"})

    async def get_state(args: dict[str, Any]) -> str:
        entity_id = str(args.get("entity_id") or "")
        if not re.match(r"^[a-z_]+\.[a-z0-9_]+$", entity_id):
            return '{"error": "invalid entity_id"}'
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(
                f"{url}/api/states/{entity_id}", headers=headers
            ) as resp:
                if resp.status == 404:
                    return json.dumps({"error": f"unknown entity: {entity_id}"})
                resp.raise_for_status()
                body = await resp.json()
        raw_attrs = body.get("attributes") or {}
        _emit_card(
            entity_id,
            raw_attrs.get("friendly_name") or entity_id,
            body.get("state"),
            raw_attrs,
        )
        return json.dumps(
            {
                "entity_id": entity_id,
                "state": body.get("state"),
                "attributes": {
                    k: v
                    for k, v in (body.get("attributes") or {}).items()
                    if k
                    in (
                        "friendly_name",
                        "unit_of_measurement",
                        "temperature",
                        "current_temperature",
                        "brightness",
                        "media_title",
                    )
                },
            },
            ensure_ascii=False,
        )

    async def list_entities(args: dict[str, Any]) -> str:
        domain = str(args.get("domain") or "")
        want_class = str(args.get("device_class") or "").lower()
        name_q = str(args.get("name") or "").lower()
        room_q = str(args.get("room") or "").lower()
        snap = await areas.snapshot()
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                states = await resp.json()
        out = []
        truncated = False
        for s in states:
            eid = str(s.get("entity_id") or "")
            if domain and not eid.startswith(f"{domain}."):
                continue
            attrs = s.get("attributes") or {}
            if (
                want_class
                and str(attrs.get("device_class") or "").lower() != want_class
            ):
                continue
            name = attrs.get("friendly_name") or eid
            if name_q and name_q not in str(name).lower():
                continue
            room = snap.area_of(eid)
            if room_q and room_q not in room.lower():
                continue
            # No card here: a bulk scan would card every match (#499). The model
            # cards the subset it actually reports by ha_get_state-ing those.
            item: dict[str, Any] = {
                "entity_id": eid,
                "state": s.get("state"),
                "name": name,
            }
            if room:
                item["room"] = room
            out.append(item)
            # Cap to bound the prompt; the filters keep targeted queries well under it.
            if len(out) >= 200:
                truncated = True
                break
        if truncated:
            out.append(
                {"_note": "gekürzt — mit device_class, domain oder name eingrenzen"}
            )
        return json.dumps(out, ensure_ascii=False)

    async def room_cards(args: dict[str, Any]) -> str:
        """Card every actuator of a room (#540) — a room query shows them all."""
        room_q = " ".join(str(args.get("room") or "").split()).lower()
        if not room_q:
            return json.dumps({"error": "room required"})
        snap = await areas.snapshot()
        # Resolve the query to ONE room before emitting, so a substring like
        # "zimmer" can't mix Wohnzimmer + Schlafzimmer (#547): prefer a
        # case/whitespace-insensitive exact match, else the first substring
        # match in sorted order (deterministic, single room).
        room_name = ""
        for r in snap.rooms:
            if " ".join(r.split()).lower() == room_q:
                room_name = r
                break
        if not room_name:
            for r in sorted(snap.rooms):
                if room_q in " ".join(r.split()).lower():
                    room_name = r
                    break
        if not room_name:
            return json.dumps({"room": room_q, "actuators": []}, ensure_ascii=False)
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                states = await resp.json()
        out = []
        for s in states:
            eid = str(s.get("entity_id") or "")
            if eid.split(".", 1)[0] not in _ROOM_ACTUATOR_DOMAINS:
                continue
            if snap.area_of(eid) != room_name:
                continue
            attrs = s.get("attributes") or {}
            name = attrs.get("friendly_name") or eid
            _emit_card(eid, name, s.get("state"), attrs)
            out.append({"entity_id": eid, "name": name, "state": s.get("state")})
        return json.dumps({"room": room_name, "actuators": out}, ensure_ascii=False)

    async def _resolve_entity_id(ref: str) -> str:
        return await resolve_entity_ref(hass_url, hass_token, ref)

    async def get_state_history(args: dict[str, Any]) -> str:
        ref = str(args.get("entity") or args.get("entity_id") or "")
        entity_id = await _resolve_entity_id(ref)
        if not entity_id:
            return json.dumps({"error": f"no entity matched: {ref}"})
        try:
            days = int(args.get("days") or _HISTORY_DEFAULT_DAYS)
        except (TypeError, ValueError):
            days = _HISTORY_DEFAULT_DAYS
        days = max(1, min(days, 30))
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        params = {
            "filter_entity_id": entity_id,
            "end_time": end.isoformat(),
            "minimal_response": "true",
        }
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(
                f"{url}/api/history/period/{start.isoformat()}",
                params=params,
                headers=headers,
            ) as resp:
                if resp.status == 404:
                    return json.dumps({"error": f"unknown entity: {entity_id}"})
                resp.raise_for_status()
                body = await resp.json()
        # HA returns [[ {state, last_changed}, ... ]] — one list per entity.
        series = body[0] if body and isinstance(body[0], list) else []
        transitions = []
        prev = None
        for point in series:
            state = point.get("state")
            when = point.get("last_changed") or point.get("last_updated")
            if state == prev or not when:
                continue
            transitions.append({"state": state, "since": when})
            prev = state
        # Durations: each transition lasts until the next (the last is "now").
        bounds = [t["since"] for t in transitions] + [end.isoformat()]
        for i, t in enumerate(transitions):
            t["duration_s"] = round(
                (_parse(bounds[i + 1]) - _parse(bounds[i])).total_seconds()
            )
        recent = transitions[-_HISTORY_MAX_TRANSITIONS:]
        return json.dumps(
            {"entity_id": entity_id, "days": days, "transitions": recent},
            ensure_ascii=False,
        )

    async def list_runnable(args: dict[str, Any]) -> str:
        domain = str(args.get("domain") or "")
        domains = (domain,) if domain in _RUNNABLE_DOMAINS else _RUNNABLE_DOMAINS
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                states = await resp.json()
        out = []
        for s in states:
            eid = str(s.get("entity_id") or "")
            if eid.split(".", 1)[0] not in domains:
                continue
            name = (s.get("attributes") or {}).get("friendly_name") or eid
            out.append({"entity_id": eid, "name": name})
        return json.dumps(out, ensure_ascii=False)

    async def run_runnable(args: dict[str, Any]) -> str:
        ref = str(args.get("entity") or args.get("entity_id") or "")
        entity_id = await _resolve_entity_id(ref)
        domain = entity_id.split(".", 1)[0] if entity_id else ""
        if domain not in _RUNNABLE_DOMAINS:
            return json.dumps({"error": f"not a script/automation/scene: {ref}"})
        return await call_service(
            {"domain": domain, "service": _RUN_SERVICE[domain], "entity_id": entity_id}
        )

    return [
        Tool(
            name="ha_call_service",
            description=(
                "Steuert ein Home-Assistant-Gerät. Nutze die entity_id aus der"
                " Geräteliste im Systemprompt. Service-Namen sind HA-spezifisch:"
                " light/switch/climate -> turn_on/turn_off (climate auch"
                " set_temperature); cover (Garage/Rollladen/Tor) ->"
                " open_cover/close_cover/stop_cover; lock -> lock/unlock."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "z.B. light, climate"},
                    "service": {
                        "type": "string",
                        "description": (
                            "HA-Service, z.B. turn_on, turn_off, set_temperature;"
                            " cover: open_cover/close_cover/stop_cover"
                        ),
                    },
                    "entity_id": {"type": "string"},
                    "data": {
                        "type": "object",
                        "description": 'optional, z.B. {"temperature": 21}',
                    },
                },
                "required": ["domain", "service", "entity_id"],
            },
            handler=call_service,
        ),
        Tool(
            name="ha_get_state",
            description="Liest den Live-Zustand einer Entity.",
            parameters={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
            handler=get_state,
        ),
        Tool(
            name="ha_list_entities",
            description=(
                "Listet Entities mit Live-Zustand und Raum — für read-only Geräte,"
                " die NICHT in der Geräteliste des Prompts stehen (Sensoren etc.)."
                " Filter kombinierbar: device_class (z.B. 'temperature',"
                " 'humidity', 'power', 'energy', 'battery'), domain (z.B. 'sensor',"
                " 'binary_sensor'), name (Teilstring, z.B. 'Küche') und room"
                " (Raumname, z.B. 'Wohnzimmer'). Beispiel: device_class='temperature'"
                " + name='Küche' für die Küchentemperatur. Nenne dem Nutzer immer den"
                " Klartext-Namen (name) und ggf. den Raum, NIE die entity_id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "device_class": {"type": "string"},
                    "name": {"type": "string"},
                    "room": {"type": "string"},
                },
            },
            handler=list_entities,
        ),
        Tool(
            name="ha_list_rooms",
            description=(
                "Listet alle Räume des Hauses (z.B. 'welche Räume hat das Haus')."
                " Liefert die echten Raumnamen aus dem Home-Assistant-Bereichs-"
                "register."
            ),
            parameters={"type": "object", "properties": {}},
            handler=list_rooms,
        ),
        Tool(
            name="ha_room_cards",
            description=(
                "Zeigt ALLE steuerbaren Geräte (Lichter, Schalter, Rollos,"
                " Heizung) eines Raums als Karten — für Raum-Fragen wie 'zeig mir"
                " das Wohnzimmer' oder 'was ist im Wohnzimmer'. Der Raumname wird"
                " einmal als Überschrift gezeigt. Parameter: room (Raumname)."
            ),
            parameters={
                "type": "object",
                "properties": {"room": {"type": "string"}},
                "required": ["room"],
            },
            handler=room_cards,
        ),
        Tool(
            name="ha_state_history",
            description=(
                "Wann war eine Entity zuletzt an/aus? Liefert die letzten"
                " Zustandswechsel mit Zeit und Dauer. Akzeptiert entity_id oder"
                " Gerätenamen; Fenster standardmäßig 7 Tage."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "entity_id oder Gerätename",
                    },
                    "days": {"type": "integer", "description": "Fenster, 1-30"},
                },
                "required": ["entity"],
            },
            handler=get_state_history,
        ),
        Tool(
            name="ha_list_scenes_scripts",
            description=(
                "Listet verfügbare Szenen, Skripte und Automationen, optional"
                " nach Domain (scene/script/automation) gefiltert."
            ),
            parameters={
                "type": "object",
                "properties": {"domain": {"type": "string"}},
            },
            handler=list_runnable,
        ),
        Tool(
            name="ha_run_scene_script",
            description=(
                "Startet eine Szene, ein Skript oder eine Automation."
                " Akzeptiert entity_id oder Namen (z.B. 'Schlafenszeit-Routine')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "entity_id oder Name",
                    },
                },
                "required": ["entity"],
            },
            handler=run_runnable,
        ),
    ]
