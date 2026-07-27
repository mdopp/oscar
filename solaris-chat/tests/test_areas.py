"""Area-registry tests (#535) — rooms as first-class data.

The HA WebSocket is stubbed so the auth handshake + the three registry-list
commands are exercised without a real HA, asserting the resolved room list and
entity→area map; the snapshot builder is unit-tested directly, and the
EntityRegistry + ha_list_rooms tool are checked to surface real room names with
no raw entity_id in user-facing output.
"""

from __future__ import annotations

import json
import time

from solaris_chat.engine import areas as areas_mod
from solaris_chat.engine.areas import AreaRegistry, _build_snapshot
from solaris_chat.engine.registry import EntityRegistry, rank_fallback_players
from solaris_chat.engine.tools.ha import build_ha_tools

_AREAS = [
    {"area_id": "wohnzimmer", "name": "Wohnzimmer"},
    {"area_id": "kueche", "name": "Küche"},
]
_DEVICES = [{"id": "dev1", "area_id": "wohnzimmer"}]
_ENTITIES = [
    # entity with its own area
    {"entity_id": "light.wohnzimmer_jackie", "area_id": "wohnzimmer"},
    # entity inheriting its device's area
    {"entity_id": "light.wohnzimmerwandlicht", "device_id": "dev1"},
    # entity with no area at all
    {"entity_id": "sensor.unassigned", "area_id": None},
]


def test_build_snapshot_resolves_rooms_and_entity_areas():
    snap = _build_snapshot(_AREAS, _DEVICES, _ENTITIES)
    assert snap.rooms == ["Küche", "Wohnzimmer"]
    assert snap.area_of("light.wohnzimmer_jackie") == "Wohnzimmer"
    # device-inherited area
    assert snap.area_of("light.wohnzimmerwandlicht") == "Wohnzimmer"
    # no area -> empty, never raised
    assert snap.area_of("sensor.unassigned") == ""
    assert snap.area_of("light.unknown") == ""


class _WS:
    """A scripted HA websocket: greeting + auth_ok, then a result per command."""

    def __init__(self):
        self._sent: list[dict] = []
        # queued inbound frames the client will receive
        self._inbox = [{"type": "auth_required"}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send_json(self, msg):
        self._sent.append(msg)
        if msg.get("type") == "auth":
            self._inbox.append({"type": "auth_ok"})
            return
        result = {
            "config/area_registry/list": _AREAS,
            "config/device_registry/list": _DEVICES,
            "config/entity_registry/list": _ENTITIES,
        }[msg["type"]]
        self._inbox.append(
            {"id": msg["id"], "type": "result", "success": True, "result": result}
        )

    async def receive_json(self):
        return self._inbox.pop(0)


def _stub_ws(monkeypatch, ws_factory):
    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def ws_connect(self, _url):
            return ws_factory()

    monkeypatch.setattr(areas_mod.aiohttp, "ClientSession", _Session)


async def test_area_registry_fetches_over_ws(monkeypatch):
    _stub_ws(monkeypatch, _WS)
    reg = AreaRegistry("http://ha:8123", "tok")
    snap = await reg.snapshot()
    assert snap.rooms == ["Küche", "Wohnzimmer"]
    assert snap.area_of("light.wohnzimmer_jackie") == "Wohnzimmer"


async def test_area_registry_fail_open_on_ws_error(monkeypatch):
    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def receive_json(self):
            raise OSError("conn refused")

        async def send_json(self, msg):
            pass

    _stub_ws(monkeypatch, _Boom)
    reg = AreaRegistry("http://ha:8123", "tok")
    snap = await reg.snapshot()
    # empty snapshot, no exception — room data must never break the prompt
    assert snap.rooms == []
    assert snap.entity_area == {}


async def test_area_registry_caches_empty_result(monkeypatch):
    # #546: an HA with 0 configured areas yields an empty-but-valid snapshot;
    # the TTL must still apply (cache on "have we fetched", not "is it
    # non-empty") so a second turn reuses the cache instead of re-opening a WS.
    connects = {"n": 0}

    class _EmptyWS(_WS):
        def __init__(self):
            super().__init__()
            connects["n"] += 1

        async def send_json(self, msg):
            self._sent.append(msg)
            if msg.get("type") == "auth":
                self._inbox.append({"type": "auth_ok"})
                return
            self._inbox.append(
                {"id": msg["id"], "type": "result", "success": True, "result": []}
            )

    _stub_ws(monkeypatch, _EmptyWS)
    reg = AreaRegistry("http://ha:8123", "tok")
    first = await reg.snapshot()
    second = await reg.snapshot()
    assert first.rooms == [] and second.rooms == []
    # only ONE WS connection across the two calls — the empty result is cached
    assert connects["n"] == 1


def test_ws_url_derivation():
    assert areas_mod._ws_url("http://ha:8123") == "ws://ha:8123/api/websocket"
    assert areas_mod._ws_url("https://ha/") == "wss://ha/api/websocket"


# -- EntityRegistry: real room names land in the prompt block (the leak fix) --


def _stub_states(monkeypatch, states):
    class _Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return states

        def raise_for_status(self):
            pass

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    import solaris_chat.engine.registry as reg_mod

    monkeypatch.setattr(reg_mod.aiohttp, "ClientSession", _Session)


async def test_prompt_block_lists_rooms_and_uses_friendly_names(monkeypatch):
    states = [
        {
            "entity_id": "light.wohnzimmer_jackie",
            "attributes": {"friendly_name": "Jackie"},
        },
    ]
    snapshot = _build_snapshot(_AREAS, _DEVICES, _ENTITIES)
    _stub_states(monkeypatch, states)
    reg = EntityRegistry("http://ha:8123", "tok")

    async def _snap():
        return snapshot

    monkeypatch.setattr(reg._areas, "snapshot", _snap)
    block = await reg.prompt_block()
    # "welche Räume hat das Haus" is answerable from the prompt itself
    assert "Räume im Haus" in block
    assert "Wohnzimmer" in block and "Küche" in block
    # the device shows its friendly name + resolved room, not just the raw id
    assert "Jackie" in block
    assert "light.wohnzimmer_jackie | Jackie | Wohnzimmer" in block


# -- ha_list_rooms tool + list_entities room enrichment (the two repros) ------


def _ha_tool(name):
    return next(t for t in build_ha_tools("http://ha:8123", "tok") if t.name == name)


async def test_ha_list_rooms_tool_returns_real_rooms(monkeypatch):
    _stub_ws(monkeypatch, _WS)
    tool = _ha_tool("ha_list_rooms")
    out = json.loads(await tool.handler({}))
    assert out["rooms"] == ["Küche", "Wohnzimmer"]


async def test_list_entities_attaches_room_and_friendly_name(monkeypatch):
    states = [
        {
            "entity_id": "light.wohnzimmer_jackie",
            "state": "on",
            "attributes": {"friendly_name": "Jackie"},
        },
    ]
    snapshot = _build_snapshot(_AREAS, _DEVICES, _ENTITIES)

    # stub aiohttp for the /api/states REST read inside the tool
    import solaris_chat.engine.tools.ha as ha_mod

    class _Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return states

        def raise_for_status(self):
            pass

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)

    tool = _ha_tool("ha_list_entities")
    # force the area snapshot the tool's AreaRegistry returns
    import solaris_chat.engine.areas as a_mod

    async def _snap(self):
        return snapshot

    monkeypatch.setattr(a_mod.AreaRegistry, "snapshot", _snap)

    out = json.loads(await tool.handler({"room": "Wohnzimmer"}))
    assert len(out) == 1
    row = out[0]
    assert row["name"] == "Jackie"
    assert row["room"] == "Wohnzimmer"
    # the entity_id is metadata for the model, but the user-facing name is there
    # and the room resolves — no bare-id-only answer.
    assert row["entity_id"] == "light.wohnzimmer_jackie"


async def test_ha_room_cards_cards_every_actuator_of_the_room(monkeypatch):
    # #540: a room query cards every ACTUATOR of the room (not the sensor),
    # each emitted to the turn's sink so they render under the one room header.
    states = [
        {
            "entity_id": "light.wz_a",
            "state": "on",
            "attributes": {"friendly_name": "WZ A"},
        },
        {
            "entity_id": "switch.wz_b",
            "state": "off",
            "attributes": {"friendly_name": "WZ B"},
        },
        {
            "entity_id": "sensor.wz_temp",
            "state": "21",
            "attributes": {"friendly_name": "WZ Temp"},
        },
        {
            "entity_id": "light.kueche_c",
            "state": "on",
            "attributes": {"friendly_name": "K C"},
        },
    ]
    area = {
        "light.wz_a": "Wohnzimmer",
        "switch.wz_b": "Wohnzimmer",
        "sensor.wz_temp": "Wohnzimmer",
        "light.kueche_c": "Küche",
    }
    import solaris_chat.engine.areas as a_mod
    import solaris_chat.engine.tools.ha as ha_mod
    from solaris_chat.engine.areas import AreaSnapshot

    class _Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return states

        def raise_for_status(self):
            pass

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)

    async def _snap(self):
        return AreaSnapshot(rooms=["Küche", "Wohnzimmer"], entity_area=area)

    monkeypatch.setattr(a_mod.AreaRegistry, "snapshot", _snap)

    sink: list = []
    ha_mod.card_sink.set(sink)
    tool = _ha_tool("ha_room_cards")
    out = json.loads(await tool.handler({"room": "Wohnzimmer"}))

    # the two Wohnzimmer actuators are cards; the sensor + the Küche light are not
    assert out["room"] == "Wohnzimmer"
    assert {a["entity_id"] for a in out["actuators"]} == {"light.wz_a", "switch.wz_b"}
    assert {c["entity_id"] for c in sink} == {"light.wz_a", "switch.wz_b"}


async def test_ha_room_cards_substring_does_not_mix_rooms(monkeypatch):
    # #547: "zimmer" is a substring of both Wohnzimmer and Schlafzimmer; the
    # room query must resolve to a single room (here the deterministic first
    # substring match in sorted order = Schlafzimmer) and never emit entities
    # from the other room, so the reported room matches the cards.
    states = [
        {
            "entity_id": "light.wohn_a",
            "state": "on",
            "attributes": {"friendly_name": "Wohn A"},
        },
        {
            "entity_id": "light.schlaf_b",
            "state": "off",
            "attributes": {"friendly_name": "Schlaf B"},
        },
    ]
    area = {"light.wohn_a": "Wohnzimmer", "light.schlaf_b": "Schlafzimmer"}
    import solaris_chat.engine.areas as a_mod
    import solaris_chat.engine.tools.ha as ha_mod
    from solaris_chat.engine.areas import AreaSnapshot

    class _Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return states

        def raise_for_status(self):
            pass

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)

    async def _snap(self):
        return AreaSnapshot(rooms=["Schlafzimmer", "Wohnzimmer"], entity_area=area)

    monkeypatch.setattr(a_mod.AreaRegistry, "snapshot", _snap)

    sink: list = []
    ha_mod.card_sink.set(sink)
    tool = _ha_tool("ha_room_cards")
    out = json.loads(await tool.handler({"room": "zimmer"}))

    # exactly one room, and the cards all belong to it — no mix
    assert out["room"] == "Schlafzimmer"
    assert {a["entity_id"] for a in out["actuators"]} == {"light.schlaf_b"}
    assert {c["entity_id"] for c in sink} == {"light.schlaf_b"}


async def test_ha_room_cards_exact_match_preferred(monkeypatch):
    # An exact (case/whitespace-insensitive) name wins over a substring match.
    states = [
        {
            "entity_id": "light.bad_a",
            "state": "on",
            "attributes": {"friendly_name": "Bad A"},
        },
        {
            "entity_id": "light.badezimmer_b",
            "state": "on",
            "attributes": {"friendly_name": "Bade B"},
        },
    ]
    area = {"light.bad_a": "Bad", "light.badezimmer_b": "Badezimmer"}
    import solaris_chat.engine.areas as a_mod
    import solaris_chat.engine.tools.ha as ha_mod
    from solaris_chat.engine.areas import AreaSnapshot

    class _Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return states

        def raise_for_status(self):
            pass

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)

    async def _snap(self):
        return AreaSnapshot(rooms=["Bad", "Badezimmer"], entity_area=area)

    monkeypatch.setattr(a_mod.AreaRegistry, "snapshot", _snap)

    sink: list = []
    ha_mod.card_sink.set(sink)
    tool = _ha_tool("ha_room_cards")
    out = json.loads(await tool.handler({"room": " bad "}))

    assert out["room"] == "Bad"
    assert {a["entity_id"] for a in out["actuators"]} == {"light.bad_a"}


# -- media_player_for_room (u99): a room → its cast target ---------------------


def _registry_with_areas(monkeypatch, entity_area):
    from solaris_chat.engine.areas import AreaSnapshot

    reg = EntityRegistry("http://ha:8123", "tok")

    async def _snap():
        return AreaSnapshot(
            rooms=sorted(set(entity_area.values())), entity_area=entity_area
        )

    monkeypatch.setattr(reg._areas, "snapshot", _snap)
    return reg


async def test_media_player_for_room_resolves_case_insensitive(monkeypatch):
    reg = _registry_with_areas(
        monkeypatch,
        {
            "media_player.kuche": "Küche",
            "light.kuche": "Küche",
            "media_player.wohnzimmer": "Wohnzimmer",
        },
    )
    # case-insensitive room match → the room's media_player (not the light)
    assert await reg.media_player_for_room("küche") == "media_player.kuche"
    assert await reg.media_player_for_room("Wohnzimmer") == "media_player.wohnzimmer"


async def test_media_player_for_room_prefers_non_group(monkeypatch):
    reg = _registry_with_areas(
        monkeypatch,
        {
            "media_player.wohnzimmer_group": "Wohnzimmer",
            "media_player.wohnzimmer_sonos": "Wohnzimmer",
        },
    )
    # a group player loses to the room's primary (non-group) speaker
    assert (
        await reg.media_player_for_room("Wohnzimmer") == "media_player.wohnzimmer_sonos"
    )


async def test_media_player_for_room_unknown_is_none(monkeypatch):
    reg = _registry_with_areas(monkeypatch, {"media_player.kuche": "Küche"})
    assert await reg.media_player_for_room("Keller") is None
    # an empty/blank room is None too (never a stray cast)
    assert await reg.media_player_for_room("") is None


# -- media_player_fallbacks (#638): same-area single-device fallback -----------


def test_rank_fallback_players_prefers_voice_pe_then_single_then_group():
    ranked = rank_fallback_players(
        [
            "media_player.wohnzimmer_group",
            "media_player.wohnzimmer_sonos",
            "media_player.home_assistant_voice_0907c9_media_player",
        ]
    )
    # Voice PE / esphome single first, a plain single device next, group last.
    assert ranked == [
        "media_player.home_assistant_voice_0907c9_media_player",
        "media_player.wohnzimmer_sonos",
        "media_player.wohnzimmer_group",
    ]


def test_rank_fallback_players_ties_keep_sorted_id_order():
    assert rank_fallback_players(["media_player.b_sonos", "media_player.a_sonos"]) == [
        "media_player.a_sonos",
        "media_player.b_sonos",
    ]


async def test_media_player_fallbacks_same_area_excludes_failed_prefers_voice(
    monkeypatch,
):
    reg = _registry_with_areas(
        monkeypatch,
        {
            "media_player.wohnzimmer": "Wohnzimmer",  # the failed Cast group
            "media_player.home_assistant_voice_0907c9_media_player": "Wohnzimmer",
            "media_player.wohnzimmer_sonos": "Wohnzimmer",
            "media_player.kuche": "Küche",  # different area, never offered
        },
    )
    out = await reg.media_player_fallbacks("media_player.wohnzimmer")
    assert out == [
        "media_player.home_assistant_voice_0907c9_media_player",
        "media_player.wohnzimmer_sonos",
    ]
    # the failed entity itself + other-area players are excluded
    assert "media_player.wohnzimmer" not in out
    assert "media_player.kuche" not in out


async def test_media_player_fallbacks_none_when_no_peers(monkeypatch):
    reg = _registry_with_areas(
        monkeypatch,
        {"media_player.wohnzimmer": "Wohnzimmer", "media_player.kuche": "Küche"},
    )
    # only the failed entity in its area → no candidate; unknown entity → none too
    assert await reg.media_player_fallbacks("media_player.wohnzimmer") == []
    assert await reg.media_player_fallbacks("media_player.unknown") == []
    assert await reg.media_player_fallbacks("") == []


# --- stale device block is bounded (#1067) --------------------------------


async def test_prompt_block_serves_a_recent_cache_when_ha_blips(monkeypatch):
    """A restart or a short outage must not blank the device list."""
    import aiohttp

    from solaris_chat.engine import registry as registry_mod

    reg = EntityRegistry("http://ha:8123", "tok")
    reg._block = "Geräte:\nlight.wohnzimmer | Jackie | an"
    reg._fetched_at = time.time() - 60  # cached a minute ago

    async def _boom():
        raise aiohttp.ClientError("HA down")

    monkeypatch.setattr(reg, "_fetch_states", _boom)
    assert "Jackie" in await reg.prompt_block()
    assert registry_mod._STALE_GRACE_S > registry_mod._TTL_S


async def test_prompt_block_drops_a_long_stale_cache(monkeypatch):
    """Past the grace window the cached inventory has outlived any rename or
    removal, so the prompt omits it rather than naming entities that may be
    gone — the model can still discover them with ha_list_entities."""
    import aiohttp

    from solaris_chat.engine import registry as registry_mod

    reg = EntityRegistry("http://ha:8123", "tok")
    reg._block = "Geräte:\nlight.wohnzimmer | Jackie | an"
    reg._fetched_at = time.time() - (registry_mod._STALE_GRACE_S + 60)

    async def _boom():
        raise aiohttp.ClientError("HA down")

    monkeypatch.setattr(reg, "_fetch_states", _boom)
    assert await reg.prompt_block() == ""
    # and it stays dropped rather than resurfacing on the next turn
    assert await reg.prompt_block() == ""
