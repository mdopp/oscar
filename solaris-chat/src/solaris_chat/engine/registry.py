"""HA entity registry for prompt injection — the second-roundtrip killer.

Injecting the controllable-entity registry (id | name | area, NO live state)
into the system prompt lets the model call `ha_call_service` with the right
entity_id directly instead of spending an LLM pass on `ha_list_entities`
first — the same approach HA's own Assist uses. Live state is deliberately
absent: it goes stale in a cached prompt, and the soul rule "read live state
before answering" stays for state questions.

The block is sorted and stable so the KV prefix cache keeps hitting; a TTL
refresh picks up registry changes (new/renamed devices) within minutes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

import aiohttp

from solaris_chat.engine.areas import AreaRegistry, AreaSnapshot
from solaris_chat.logging import log

# Read-only domains we advertise for ON-DEMAND discovery instead of packing
# their (often hundreds of) entities into every prompt: the prompt carries the
# actionable devices in full, plus a legend of which sensor device_classes /
# read-only domains exist, and the model fetches the specific ones it needs with
# ha_list_entities(device_class=… / domain=…). Box-observed without this the
# model guesses a non-existent climate.* and gives up rather than querying.
QUERYABLE_READONLY_DOMAINS = ("sensor", "binary_sensor")

# Domains a household voice command can act on — packed into the prompt in full
# so actions resolve in one pass. Everything else is discovered via the legend.
CONTROLLABLE_DOMAINS = (
    "light",
    "switch",
    "climate",
    "cover",
    "media_player",
    "scene",
    "script",
    "fan",
    "lock",
    "vacuum",
    "humidifier",
)

_TTL_S = 300.0

# How long a cached block may still be served after HA stopped answering. The
# block is an inventory (id | name | area, no live state), so a stale one is
# survivable — but not unboundedly: it long outlives renamed, removed or newly
# added devices, and the model then confidently addresses entity_ids that are
# gone. A grace window rides out a restart or a blip; past it the prompt omits
# the list and the model discovers entities with ha_list_entities instead.
_STALE_GRACE_S = 900.0

# Real HA service names per domain, so the model emits e.g. cover.open_cover
# (not the guessed cover.open that 400'd, #379) without a separate roundtrip.
# Kept as a compact per-domain legend appended once, not repeated per entity.
_DOMAIN_SERVICES = {
    "light": "turn_on/turn_off",
    "switch": "turn_on/turn_off",
    "climate": "set_temperature/set_hvac_mode",
    "cover": "open_cover/close_cover/stop_cover",
    "media_player": "play_media/media_play/media_pause/media_stop/media_next_track/media_previous_track/volume_set",
    "scene": "turn_on",
    "script": "turn_on",
    "fan": "turn_on/turn_off/set_percentage",
    "lock": "lock/unlock",
    "vacuum": "start/pause/return_to_base",
    "humidifier": "turn_on/turn_off/set_humidity",
}
# cover.set_cover_position is only valid when SUPPORT_SET_POSITION (bit 2 = 4)
# is in supported_features; appended to the cover legend when any cover has it.
_COVER_SET_POSITION = 4


def rank_fallback_players(players: list[str]) -> list[str]:
    """Order media_player entity_ids for the group-cast fallback (#638), best-first.

    A Voice PE / esphome single speaker (`home_assistant_voice` in the id) wins —
    it's the device the resident is speaking to and plays a URL stream reliably.
    An entity id with `group` in it is an obvious whole-house Cast group and goes
    last (those 500 on URL play_media); everything else is a plausible single
    device in the middle. Ties stay in sorted-id order for determinism."""

    def rank(eid: str) -> int:
        if "home_assistant_voice" in eid:
            return 0
        if "group" in eid:
            return 2
        return 1

    return sorted(sorted(players), key=rank)


def _match_score(term: str, entity_id: str, name: str) -> float:
    slug = entity_id.split(".", 1)[-1].replace("_", " ").casefold()
    best = SequenceMatcher(None, term, slug).ratio()
    if name:
        best = max(best, SequenceMatcher(None, term, name.casefold()).ratio())
    return best


def rank_entities(
    guess: str, index: dict[str, str], limit: int = 3
) -> list[tuple[str, str, float]]:
    """`(entity_id, friendly_name, score)` for the entities closest to a guess.

    The model guesses a friendly-name-shaped id — `light.sofalicht` for the
    Sofalicht whose real id is `light.dimmer_2_5` — so the readable part of the
    guess is matched against both the real slug and the friendly name. The
    guessed domain is almost always right, so when it exists the candidates stay
    inside it; otherwise the whole house is searched and only close matches
    survive.
    """
    domain, _, slug = guess.partition(".")
    term = (slug or guess).replace("_", " ").casefold()
    if not term or not index:
        return []
    same_domain = {
        e: n for e, n in index.items() if slug and e.startswith(f"{domain}.")
    }
    pool = same_domain or index
    ranked = sorted(
        (-_match_score(term, eid, name), eid, name) for eid, name in pool.items()
    )
    floor = 0.0 if same_domain else 0.5
    return [
        (eid, name, -score) for score, eid, name in ranked[:limit] if -score >= floor
    ]


def suggest_entities(guess: str, index: dict[str, str], limit: int = 3) -> list[str]:
    """Real `entity_id | Name` entries closest to a guessed id, best first."""
    return [
        f"{eid} | {name}".rstrip(" |")
        for eid, name, _ in rank_entities(guess, index, limit)
    ]


# How sure the engine must be before it swaps a guessed entity_id for a real one
# without asking (#1263). `light.sofalicht` matches `light.dimmer_2_5 | Sofalicht`
# at 1.0, so the bar can sit high: below it the guess is a different word than any
# real device (`light.esszimmerjalousie` reaches ~0.65 against the room's lights)
# and swapping would be a coin flip. The margin is the second half of the rule —
# two devices that match nearly as well are an ambiguity, and ambiguity is exactly
# where the resident gets asked instead of guessed at.
_RESOLVE_MIN = 0.75
_RESOLVE_MARGIN = 0.15


@dataclass(frozen=True)
class Resolution:
    """The real entity a guessed id means (`entity_id` set), or the device names
    to ask the resident about (`choices`) when no single one is clear."""

    entity_id: str = ""
    name: str = ""
    choices: tuple[str, ...] = ()


def _readable(entity_id: str, name: str) -> str:
    return name or entity_id.partition(".")[2].replace("_", " ") or entity_id


def resolve_entity(guess: str, index: dict[str, str]) -> Resolution:
    """Resolve a guessed entity_id in code, or come back with names to ask about.

    Only a clear winner INSIDE the guessed domain resolves: the service the
    caller is about to run (`light.turn_on`) belongs to that domain, so a
    `cover` that happens to carry the guessed word is not a substitute — it is a
    different device with different services.
    """
    ranked = rank_entities(guess, index)
    if not ranked:
        return Resolution()
    choices = tuple(_readable(eid, name) for eid, name, _ in ranked)
    top_id, top_name, top = ranked[0]
    runner_up = ranked[1][2] if len(ranked) > 1 else 0.0
    domain = guess.partition(".")[0]
    if (
        "." in guess
        and top_id.startswith(f"{domain}.")
        and top >= _RESOLVE_MIN
        and top - runner_up >= _RESOLVE_MARGIN
    ):
        return Resolution(entity_id=top_id, name=_readable(top_id, top_name))
    return Resolution(choices=choices)


class EntityRegistry:
    def __init__(self, hass_url: str, hass_token: str):
        self._url = hass_url.rstrip("/")
        self._token = hass_token
        self._areas = AreaRegistry(hass_url, hass_token)
        self._block = ""
        self._fetched_at = 0.0
        # entity_id -> device_class ("" when the entity has none), filled from
        # the same /api/states read prompt_block already does. The gate reads it
        # to tell a garage cover from a blind (#570 F1).
        self._device_classes: dict[str, str] = {}
        # entity_id -> friendly_name for EVERY entity HA exposes, from the same
        # /api/states read; the existence check below is served from it.
        self._entities: dict[str, str] = {}
        self._entities_at = 0.0

    async def entity_index(self, *, force: bool = False) -> dict[str, str]:
        """`entity_id -> friendly_name` for everything HA exposes, TTL-cached.

        The same inventory the prompt block is built from, kept as a lookup so a
        tool call can check an entity_id exists BEFORE issuing it. Empty when HA
        is absent or unreachable. `force` re-reads HA now, for the one caller
        that must not act on a stale answer.
        """
        if not self._url or not self._token:
            return {}
        fresh = (time.time() - self._entities_at) < _TTL_S
        if self._entities and fresh and not force:
            return self._entities
        try:
            states = await self._fetch_states()
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.warn("engine.registry.index_unreachable", error=str(e))
            return self._entities
        self._entities = {
            str(s.get("entity_id")): str(
                (s.get("attributes") or {}).get("friendly_name") or ""
            )
            for s in states
            if s.get("entity_id")
        }
        self._entities_at = time.time()
        return self._entities

    async def check_entity(self, entity_id: str) -> tuple[bool, list[str]]:
        """`(exists, closest real candidates)` for a model-supplied entity_id.

        HA answers 200 with an empty body for a service call on an entity that
        never existed — byte-identical to a real entity already in the target
        state — so an unchecked guess reads back as a completed action and the
        model tells the resident it acted (#1241). Unknown ids come back False
        with real candidates so the model can retry with the right id.

        Fails OPEN (`True, []`) when the index is empty: HA is then unreachable
        or absent, the call itself will surface that, and a registry blip must
        not take device control down.
        """
        index = await self.entity_index()
        if not index or entity_id in index:
            return True, []
        # A miss can just be a stale index — a device added or renamed since the
        # last refresh — and "unknown" is a hard refusal, so re-read HA once
        # before saying it. Only a genuinely absent id pays for this.
        index = await self.entity_index(force=True)
        if not index or entity_id in index:
            return True, []
        return False, suggest_entities(entity_id, index)

    async def resolve(self, entity_id: str) -> Resolution:
        """The real entity a model-supplied id means — itself when it exists.

        Handing the model a candidate list and hoping it retries does not work:
        gemma4:e4b retried 0 of 6 measured rejections and told the resident the
        lamp was on instead (#1263). So the guess is resolved HERE, against the
        same index `check_entity` refuses from.
        """
        known, _ = await self.check_entity(entity_id)
        if known:
            return Resolution(
                entity_id=entity_id,
                name=_readable(entity_id, self._entities.get(entity_id, "")),
            )
        return resolve_entity(entity_id, self._entities)

    async def device_class(self, entity_id: str) -> str | None:
        """The entity's HA device_class, or None when it can't be resolved.

        Synchronous policy needs this for the confirmation gate (a garage cover
        vs. a blind). Served from the cache the registry already populates; on a
        miss it does one targeted state read. None on any HA error so the caller
        can fail SAFE — an empty string means the entity exists but has no class.
        """
        if entity_id in self._device_classes:
            return self._device_classes[entity_id]
        if not self._url or not self._token:
            return None
        try:
            states = await self._fetch_states()
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.warn("engine.registry.device_class.unreachable", error=str(e))
            return None
        for s in states:
            eid = str(s.get("entity_id") or "")
            attrs = s.get("attributes") or {}
            self._device_classes[eid] = str(attrs.get("device_class") or "")
        return self._device_classes.get(entity_id)

    async def area_snapshot(self) -> AreaSnapshot:
        """The current area snapshot (entity→room map), for card room-grouping
        (#537). Shares the registry's cached `AreaRegistry`."""
        return await self._areas.snapshot()

    async def media_player_for_room(self, room: str) -> str | None:
        """The room's `media_player.*` entity, or None when none/unresolvable.

        Resolves the originating room of a device-less "spiele Musik" (u99) to a
        cast target: match the area name case-insensitively in the area
        snapshot, then return its first media_player. A non-group player wins
        over a group (the area's primary speaker, not a whole-house cast);
        otherwise the first by entity_id."""
        room = room.strip()
        if not room:
            return None
        snap = await self._areas.snapshot()
        players = sorted(
            eid
            for eid, area in snap.entity_area.items()
            if eid.startswith("media_player.") and area.casefold() == room.casefold()
        )
        if not players:
            return None
        non_group = [eid for eid in players if "group" not in eid]
        return non_group[0] if non_group else players[0]

    async def media_player_fallbacks(self, entity_id: str) -> list[str]:
        """Other media_players in the same area as `entity_id`, best-first.

        A Cast GROUP rejects URL play_media (HA 500, #638), so when a play on
        such a target fails the caller retries on a single device in the SAME
        area. The room's Voice PE / esphome single speaker (the device the
        person is speaking to) is preferred, then any other non-group single
        device; the failed entity itself is excluded. Group membership isn't
        exposed, so the practical ranking keys on the id (esphome first, obvious
        group names last) — see `rank_fallback_players`."""
        entity_id = entity_id.strip()
        if not entity_id:
            return []
        snap = await self._areas.snapshot()
        area = snap.area_of(entity_id)
        if not area:
            return []
        peers = [
            eid
            for eid, eid_area in snap.entity_area.items()
            if eid.startswith("media_player.")
            and eid != entity_id
            and eid_area.casefold() == area.casefold()
        ]
        return rank_fallback_players(peers)

    async def prompt_block(self) -> str:
        """The registry block for the system prompt; "" when HA is absent or
        unreachable (the prompt simply omits the device list — fail-open)."""
        if not self._url or not self._token:
            return ""
        if self._block and (time.time() - self._fetched_at) < _TTL_S:
            return self._block
        try:
            states = await self._fetch_states()
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            age_s = time.time() - self._fetched_at
            if self._block and age_s <= _STALE_GRACE_S:
                log.warn("engine.registry.unreachable", error=str(e), age_s=int(age_s))
                return self._block  # a blip: yesterday's list still beats none
            # Past the grace window the cached inventory is too likely to name
            # entities that no longer exist. Drop it; discovery still works.
            log.warn("engine.registry.stale_dropped", error=str(e), age_s=int(age_s))
            self._block = ""
            return ""
        areas = await self._areas.snapshot()
        lines = []
        domains: set[str] = set()
        cover_set_position = False
        # Discovery legend inputs: which read-only domains exist and, for sensors,
        # which device_classes — so the model knows what it can fetch on demand.
        readonly_domains: set[str] = set()
        sensor_classes: set[str] = set()
        self._device_classes = {}
        for s in states:
            entity_id = str(s.get("entity_id") or "")
            domain = entity_id.split(".", 1)[0]
            attrs = s.get("attributes") or {}
            device_class = str(attrs.get("device_class") or "")
            self._device_classes[entity_id] = device_class
            if domain not in CONTROLLABLE_DOMAINS:
                # Not actionable — keep it out of the prompt, just record that it
                # exists so the legend can point the model at a targeted query.
                if domain in QUERYABLE_READONLY_DOMAINS:
                    readonly_domains.add(domain)
                    if device_class:
                        sensor_classes.add(device_class)
                continue
            name = str(attrs.get("friendly_name") or entity_id)
            area = areas.area_of(entity_id) or str(attrs.get("area") or "")
            line = f"{entity_id} | {name} | {area}".rstrip(" |")
            domains.add(domain)
            if domain == "cover":
                # device_class distinguishes a garage cover (confirm-first) from
                # an ordinary blind/shade (act) — both are domain=cover, so the
                # safety rule can only key on it if it's surfaced per entity.
                if device_class:
                    line += f" | {device_class}"
                features = attrs.get("supported_features") or 0
                if isinstance(features, int) and features & _COVER_SET_POSITION:
                    cover_set_position = True
            lines.append(line)
        lines.sort()
        parts = [
            self._rooms_block(areas.rooms),
            "Steuerbare Geräte (entity_id | Name | Raum[ | Geräteklasse bei cover]):\n"
            + "\n".join(lines),
            self._actions_legend(domains, cover_set_position),
            self._discovery_legend(sorted(readonly_domains), sorted(sensor_classes)),
        ]
        self._block = "\n".join(p for p in parts if p) if (lines or areas.rooms) else ""
        self._fetched_at = time.time()
        log.info(
            "engine.registry.refreshed",
            entities=len(lines),
            sensor_classes=len(sensor_classes),
        )
        return self._block

    @staticmethod
    def _rooms_block(rooms: list[str]) -> str:
        """The house's rooms, so "welche Räume hat das Haus" is answerable from
        the prompt directly (HA states carry no area, #535)."""
        if not rooms:
            return ""
        return "Räume im Haus: " + ", ".join(rooms)

    @staticmethod
    def _discovery_legend(domains: list[str], classes: list[str]) -> str:
        """Tell the model what read-only devices exist beyond the actionable list
        and how to pull the specific ones it needs — so we don't pack hundreds of
        sensors into every prompt and the model still answers e.g. room
        temperature, energy or battery questions in one targeted query."""
        if not domains and not classes:
            return ""
        legend = [
            "Weitere Geräte sind nur lesbar und NICHT oben gelistet — bei Bedarf",
            'gezielt abrufen mit ha_list_entities (z.B. device_class="temperature"',
            "für die Raumtemperatur, oder domain=… / name=…):",
        ]
        if classes:
            legend.append("  Sensor-device_class: " + ", ".join(classes))
        if domains:
            legend.append("  read-only domains: " + ", ".join(domains))
        return "\n".join(legend)

    @staticmethod
    def _actions_legend(domains: set[str], cover_set_position: bool) -> str:
        legend = ["Aktionen (ha_call_service domain.service):"]
        for domain in CONTROLLABLE_DOMAINS:
            if domain not in domains:
                continue
            services = _DOMAIN_SERVICES[domain]
            if domain == "cover" and cover_set_position:
                services += "/set_cover_position"
            legend.append(f"{domain}: {services}")
        return "\n".join(legend)

    async def _fetch_states(self) -> list[dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"Authorization": f"Bearer {self._token}"}
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(f"{self._url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                body = await resp.json()
        return body if isinstance(body, list) else []
