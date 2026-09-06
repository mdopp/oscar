"""HA tool tests (#369 state-history, #370 list/run scenes-scripts).

aiohttp is stubbed so each handler is exercised without a real HA, asserting
the request it builds and the shape it returns; guest scoping is checked
against profiles.build_engine_clients.
"""

from __future__ import annotations

import json

import pytest

from solaris_chat.engine import areas as areas_mod
from solaris_chat.engine import registry as registry_mod
from solaris_chat.engine.tools import ha as ha_mod
from solaris_chat.engine.tools.ha import build_ha_tools


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return ""

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(self.status)


def _stub(monkeypatch, *, states=None, history=None, calls=None, post_body=None):
    """Stub aiohttp.ClientSession; record GET urls/params and POST bodies.

    `post_body` is what HA answers a service call with — the real one is `[]`
    with status 200, for a live entity and a non-existent one alike (#1241).
    """
    gets: list[tuple[str, dict]] = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, geturl, *, params=None, **k):
            gets.append((geturl, params or {}))
            if "/api/history/period/" in geturl:
                return _Resp(history)
            return _Resp(states)

        def post(self, posturl, *, json, **k):
            if calls is not None:
                calls.append((posturl, json))
            return _Resp({"ok": True} if post_body is None else post_body)

        def ws_connect(self, _url):
            # No HA WS in these REST tests — the area registry fails open to an
            # empty snapshot (room data must never break a state read, #535).
            raise OSError("no ws")

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(areas_mod.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(registry_mod.aiohttp, "ClientSession", _Session)
    return gets


def _tool(name):
    tools = build_ha_tools("http://ha", "tok")
    return next(t for t in tools if t.name == name)


def _checked_tool(name):
    """The tool wired to a real registry, as `build_engine_clients` wires it —
    so an entity_id is checked against HA's actual entity set before use."""
    check = registry_mod.EntityRegistry("http://ha", "tok").check_entity
    tools = build_ha_tools("http://ha", "tok", check_entity=check)
    return next(t for t in tools if t.name == name)


async def test_history_resolves_name_and_summarizes_transitions(monkeypatch):
    states = [
        {"entity_id": "light.kitchen", "attributes": {"friendly_name": "Küche"}},
    ]
    history = [
        [
            {"state": "off", "last_changed": "2026-06-01T08:00:00+00:00"},
            {"state": "on", "last_changed": "2026-06-01T09:00:00+00:00"},
            {"state": "on", "last_changed": "2026-06-01T09:30:00+00:00"},  # dup
            {"state": "off", "last_changed": "2026-06-01T10:00:00+00:00"},
        ]
    ]
    gets = _stub(monkeypatch, states=states, history=history)

    out = json.loads(await _tool("ha_state_history").handler({"entity": "Küche"}))

    assert out["entity_id"] == "light.kitchen"
    # name resolution hit /api/states, then the history period url
    assert any("/api/states" in u for u, _ in gets)
    hist = next((u, p) for u, p in gets if "/api/history/period/" in u)
    assert hist[1]["filter_entity_id"] == "light.kitchen"
    assert "end_time" in hist[1]
    # the duplicate "on" is collapsed; the "on" lasted one hour
    states_seq = [t["state"] for t in out["transitions"]]
    assert states_seq == ["off", "on", "off"]
    on = next(t for t in out["transitions"] if t["state"] == "on")
    assert on["duration_s"] == 3600


async def test_history_accepts_existing_entity_id(monkeypatch):
    states = [{"entity_id": "light.kitchen", "attributes": {"friendly_name": "Küche"}}]
    _stub(monkeypatch, states=states, history=[[]])
    out = json.loads(
        await _tool("ha_state_history").handler({"entity": "light.kitchen"})
    )
    assert out["entity_id"] == "light.kitchen"


async def test_history_guessed_missing_id_falls_back_to_name(monkeypatch):
    # The model often guesses an id from the name (light.sofalicht) that doesn't
    # exist; the real one is light.dimmer_2_5. Resolve by slug + domain instead
    # of querying a phantom id (which would return an empty "never happened").
    states = [
        {"entity_id": "light.dimmer_2_5", "attributes": {"friendly_name": "Sofalicht"}},
        {
            "entity_id": "sensor.sofalicht_power",
            "attributes": {"friendly_name": "Sofalicht"},
        },
    ]
    history = [[{"state": "on", "last_changed": "2026-06-14T19:00:00+00:00"}]]
    _stub(monkeypatch, states=states, history=history)
    out = json.loads(
        await _tool("ha_state_history").handler({"entity": "light.sofalicht"})
    )
    # domain bias picks the light, not the same-named sensor
    assert out["entity_id"] == "light.dimmer_2_5"


async def test_history_no_match(monkeypatch):
    _stub(monkeypatch, states=[], history=[[]])
    out = json.loads(await _tool("ha_state_history").handler({"entity": "Nope"}))
    assert "error" in out


async def test_list_entities_filters_by_device_class_and_name(monkeypatch):
    states = [
        {
            "entity_id": "sensor.kuche_temp",
            "state": "21.4",
            "attributes": {
                "friendly_name": "Küchensensor Air temperature",
                "device_class": "temperature",
            },
        },
        {
            "entity_id": "sensor.bad_temp",
            "state": "19.0",
            "attributes": {
                "friendly_name": "Bad Temperatur",
                "device_class": "temperature",
            },
        },
        {
            "entity_id": "sensor.wm_power",
            "state": "5",
            "attributes": {"friendly_name": "Waschmaschine", "device_class": "power"},
        },
        {
            "entity_id": "light.kuche",
            "state": "on",
            "attributes": {"friendly_name": "Küchenlicht"},
        },
    ]
    _stub(monkeypatch, states=states)
    # device_class narrows to the temperature sensors only
    out = json.loads(
        await _tool("ha_list_entities").handler({"device_class": "temperature"})
    )
    assert [e["entity_id"] for e in out] == ["sensor.kuche_temp", "sensor.bad_temp"]
    # device_class + name substring narrows to the kitchen one
    out = json.loads(
        await _tool("ha_list_entities").handler(
            {"device_class": "temperature", "name": "küche"}
        )
    )
    assert [e["entity_id"] for e in out] == ["sensor.kuche_temp"]
    assert out[0]["state"] == "21.4"


async def test_list_runnable_filters_to_domains(monkeypatch):
    states = [
        {"entity_id": "scene.movie", "attributes": {"friendly_name": "Kino"}},
        {"entity_id": "script.bedtime", "attributes": {}},
        {"entity_id": "automation.morning", "attributes": {}},
        {"entity_id": "light.kitchen", "attributes": {}},
    ]
    _stub(monkeypatch, states=states)
    out = json.loads(await _tool("ha_list_scenes_scripts").handler({}))
    ids = {e["entity_id"] for e in out}
    assert ids == {"scene.movie", "script.bedtime", "automation.morning"}


async def test_fetch_addable_runnables_keeps_runnable_domains(monkeypatch):
    # #702: the picker's Automationen source keeps scenes/scripts/automations
    # and returns {entity_id, name, domain}, sorted by name; other domains drop.
    states = [
        {"entity_id": "scene.movie", "attributes": {"friendly_name": "Kino"}},
        {"entity_id": "script.bedtime", "attributes": {"friendly_name": "Ab ins Bett"}},
        {"entity_id": "automation.morning", "attributes": {}},
        {"entity_id": "light.kitchen", "attributes": {"friendly_name": "Küche"}},
    ]
    _stub(monkeypatch, states=states)
    out = await ha_mod.fetch_addable_runnables("http://ha", "tok")
    assert [r["entity_id"] for r in out] == [
        "script.bedtime",
        "automation.morning",
        "scene.movie",
    ]
    assert out[-1] == {"entity_id": "scene.movie", "name": "Kino", "domain": "scene"}
    # automation with no friendly_name falls back to its entity_id.
    assert out[1]["name"] == "automation.morning"


async def test_fetch_addable_cards_offers_lock_but_not_button(monkeypatch):
    # #1212: a lock is selectable in the start-page picker (it was filtered out
    # before, so the widget config could not list it at all). `button` stays out
    # by explicit decision — a door-opener press has no confirm rule.
    states = [
        {
            "entity_id": "lock.zuhause",
            "state": "locked",
            "attributes": {"friendly_name": "Haustür"},
        },
        {
            "entity_id": "button.zuhause_unlatch",
            "state": "unknown",
            "attributes": {"friendly_name": "Falle öffnen"},
        },
        {
            "entity_id": "light.kitchen",
            "state": "on",
            "attributes": {"friendly_name": "Küche"},
        },
    ]
    _stub(monkeypatch, states=states)
    cards = await ha_mod.fetch_addable_cards(
        "http://ha", "tok", {"lock.zuhause": "Flur"}
    )
    by_id = {c["entity_id"]: c for c in cards}
    assert "lock.zuhause" in by_id
    assert "button.zuhause_unlatch" not in by_id
    lock = by_id["lock.zuhause"]
    assert lock["domain"] == "lock"
    assert lock["room"] == "Flur"
    # The bolt state verbatim — never an open/closed door claim (there is no
    # door sensor), so a consumer cannot render one.
    assert lock["state"] == "locked"


async def test_fetch_addable_cards_keeps_lock_unknown_distinct(monkeypatch):
    # #1212: MQTT delivers state via retained messages — after a broker restart
    # there may be none. `unknown` must survive as its own state and must never
    # be flattened into "locked".
    states = [
        {
            "entity_id": "lock.zuhause",
            "state": "unknown",
            "attributes": {"friendly_name": "Haustür"},
        }
    ]
    _stub(monkeypatch, states=states)
    cards = await ha_mod.fetch_addable_cards("http://ha", "tok", {})
    assert cards[0]["state"] == "unknown"


@pytest.mark.parametrize(
    "entity_id,service",
    [
        ("scene.movie", "turn_on"),
        ("script.bedtime", "turn_on"),
        ("automation.morning", "trigger"),
    ],
)
async def test_run_runnable_builds_service_call(monkeypatch, entity_id, service):
    calls: list[tuple[str, dict]] = []
    runnables = [
        {"entity_id": "scene.movie", "attributes": {}},
        {"entity_id": "script.bedtime", "attributes": {}},
        {"entity_id": "automation.morning", "attributes": {}},
    ]
    _stub(monkeypatch, states=runnables, calls=calls)
    domain = entity_id.split(".")[0]
    out = json.loads(await _tool("ha_run_scene_script").handler({"entity": entity_id}))
    assert out["success"] is True
    posturl, body = calls[0]
    assert posturl == f"http://ha/api/services/{domain}/{service}"
    assert body["entity_id"] == entity_id


async def test_run_runnable_rejects_non_runnable(monkeypatch):
    calls: list[tuple[str, dict]] = []
    states = [{"entity_id": "light.kitchen", "attributes": {"friendly_name": "Küche"}}]
    _stub(monkeypatch, states=states, calls=calls)
    out = json.loads(
        await _tool("ha_run_scene_script").handler({"entity": "light.kitchen"})
    )
    assert "error" in out
    assert calls == []


@pytest.mark.parametrize(
    "service,expected",
    [("open", "open_cover"), ("close", "close_cover"), ("stop", "stop_cover")],
)
async def test_call_service_normalizes_cover_aliases(monkeypatch, service, expected):
    calls: list[tuple[str, dict]] = []
    _stub(monkeypatch, calls=calls)
    out = json.loads(
        await _tool("ha_call_service").handler(
            {"domain": "cover", "service": service, "entity_id": "cover.garage"}
        )
    )
    assert out["success"] is True
    assert out["service"] == f"cover.{expected}"
    posturl, _ = calls[0]
    assert posturl == f"http://ha/api/services/cover/{expected}"


@pytest.mark.parametrize(
    "domain,service",
    [("cover", "open_cover"), ("light", "turn_on"), ("climate", "set_temperature")],
)
async def test_call_service_passes_unmapped_through(monkeypatch, domain, service):
    calls: list[tuple[str, dict]] = []
    _stub(monkeypatch, calls=calls)
    out = json.loads(
        await _tool("ha_call_service").handler(
            {"domain": domain, "service": service, "entity_id": f"{domain}.x"}
        )
    )
    assert out["service"] == f"{domain}.{service}"
    posturl, _ = calls[0]
    assert posturl == f"http://ha/api/services/{domain}/{service}"


async def test_call_service_unknown_service_still_errors(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, posturl, *, json, **k):
            calls.append((posturl, json))
            return _Resp({"message": "service not found"}, status=400)

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)
    out = json.loads(
        await _tool("ha_call_service").handler(
            {"domain": "cover", "service": "levitate", "entity_id": "cover.garage"}
        )
    )
    assert "error" in out
    assert "400" in out["error"]
    # an unmapped service is forwarded as-is, not rewritten or dropped
    assert calls[0][0] == "http://ha/api/services/cover/levitate"


async def test_guest_toolset_excludes_run_tool():
    from solaris_chat.engine.profiles import build_engine_clients

    household, _, guest, _, _, _, _ = build_engine_clients(
        db_path=":memory:",
        llama_server_url="http://o",
        fast_model="m",
        thorough_model="m",
        soul_path="/nonexistent/SOUL.md",
        hass_url="http://ha",
        hass_token="tok",
    )
    guest_names = set((await guest.list_toolsets())[0]["tools"])
    household_names = set((await household.list_toolsets())[0]["tools"])
    # the run-tool is device control beyond a guest's remit (#370)
    assert "ha_run_scene_script" not in guest_names
    assert "ha_run_scene_script" in household_names
    # read-only history is allowed for guests
    assert "ha_state_history" in guest_names


async def test_household_has_self_enrollment_tools():
    # First-run/owner self-enrolment (#396): an unknown speaker with zero
    # enrolments resolves to `household`, not `guest`, so the enrol tools must be
    # in the household toolbox too — not only the guest set — or "Setup starten"
    # can never bootstrap the first voice profile.
    from solaris_chat.engine.profiles import build_engine_clients

    household, _, guest, _, _, _, _ = build_engine_clients(
        db_path=":memory:",
        llama_server_url="http://o",
        fast_model="m",
        thorough_model="m",
        soul_path="/nonexistent/SOUL.md",
        hass_url="http://ha",
        hass_token="tok",
        gatekeeper_url="http://gk",
        gatekeeper_token="t",
    )
    household_names = set((await household.list_toolsets())[0]["tools"])
    guest_names = set((await guest.list_toolsets())[0]["tools"])
    for name in ("start_voice_enrollment", "register_pending_resident"):
        assert name in household_names
        # the guest path keeps them too (the heard-but-below-threshold flow)
        assert name in guest_names


async def test_get_state_emits_read_only_card(monkeypatch):
    states = {
        "state": "21.4",
        "attributes": {
            "friendly_name": "Küche Temperatur",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
        },
    }
    _stub(monkeypatch, states=states)
    sink: list = []
    ha_mod.card_sink.set(sink)

    await _tool("ha_get_state").handler({"entity_id": "sensor.kueche_temp"})

    assert sink == [
        {
            "entity_id": "sensor.kueche_temp",
            "name": "Küche Temperatur",
            "domain": "sensor",
            "device_class": "temperature",
            "state": "21.4",
            "unit": "°C",
        }
    ]


async def test_get_state_card_surfaces_control_attrs(monkeypatch):
    # Phase 3 (#477): a dimmable colour light's card carries the attrs the
    # frontend feature-gates the brightness slider + colour picker on.
    states = {
        "state": "on",
        "attributes": {
            "friendly_name": "Sofalicht",
            "brightness": 128,
            "rgb_color": [255, 0, 0],
            "supported_color_modes": ["rgb"],
            "supported_features": 0,
        },
    }
    _stub(monkeypatch, states=states)
    sink: list = []
    ha_mod.card_sink.set(sink)

    await _tool("ha_get_state").handler({"entity_id": "light.sofa"})

    assert sink[0]["brightness"] == 128
    assert sink[0]["rgb_color"] == [255, 0, 0]
    assert sink[0]["supported_color_modes"] == ["rgb"]


async def test_climate_card_is_emitted_with_setpoint_attrs(monkeypatch):
    states = {
        "state": "heat",
        "attributes": {
            "friendly_name": "Wohnzimmer",
            "current_temperature": 20.5,
            "temperature": 22,
            "supported_features": 1,
            "hvac_modes": ["off", "heat"],
        },
    }
    _stub(monkeypatch, states=states)
    sink: list = []
    ha_mod.card_sink.set(sink)

    await _tool("ha_get_state").handler({"entity_id": "climate.living"})

    assert sink[0]["domain"] == "climate"
    assert sink[0]["current_temperature"] == 20.5
    assert sink[0]["temperature"] == 22
    assert sink[0]["hvac_modes"] == ["off", "heat"]


def test_card_spec_light_includes_live_attributes():
    # #754: a light card surfaces the live attributes a native widget needs —
    # raw brightness (client derives %), color_temp/rgb_color when present.
    spec = ha_mod.card_spec(
        "light.sofa",
        "on",
        {
            "friendly_name": "Sofalicht",
            "brightness": 200,
            "color_temp": 350,
            "rgb_color": [255, 0, 0],
        },
    )
    assert spec["domain"] == "light"
    assert spec["state"] == "on"
    assert spec["brightness"] == 200
    assert spec["color_temp"] == 350
    assert spec["rgb_color"] == [255, 0, 0]


def test_card_spec_cover_includes_current_position():
    # #754: a cover card carries its position % so a widget can show it.
    spec = ha_mod.card_spec(
        "cover.rollo", "open", {"friendly_name": "Rollo", "current_position": 60}
    )
    assert spec["current_position"] == 60


def test_card_spec_climate_includes_temps_and_action():
    # #754: a climate card carries current temp, setpoint and hvac_action.
    spec = ha_mod.card_spec(
        "climate.living",
        "heat",
        {
            "friendly_name": "Wohnzimmer",
            "current_temperature": 20.5,
            "temperature": 22,
            "hvac_action": "heating",
        },
    )
    assert spec["current_temperature"] == 20.5
    assert spec["temperature"] == 22
    assert spec["hvac_action"] == "heating"


def test_card_spec_omits_absent_live_attributes():
    # #754: additive only — an off light with no brightness must not emit the
    # keys (so existing web-card consumers never see null-poisoned fields).
    spec = ha_mod.card_spec("light.sofa", "off", {"friendly_name": "Sofalicht"})
    assert set(spec) == {"entity_id", "name", "domain", "device_class", "state", "unit"}
    assert "brightness" not in spec
    assert "color_temp" not in spec
    assert "hvac_action" not in spec


async def test_media_player_card_carries_state_and_controls(monkeypatch):
    # #541: media_player gets a card with its transport state + control attrs
    # (volume + what's playing) so the SPA can render the player variant.
    states = {
        "state": "playing",
        "attributes": {
            "friendly_name": "Wohnzimmer TV",
            "volume_level": 0.4,
            "media_title": "Tagesschau",
            "media_artist": "ARD",
        },
    }
    _stub(monkeypatch, states=states)
    sink: list = []
    ha_mod.card_sink.set(sink)

    await _tool("ha_get_state").handler({"entity_id": "media_player.living_tv"})

    assert sink[0]["domain"] == "media_player"
    assert sink[0]["state"] == "playing"
    assert sink[0]["volume_level"] == 0.4
    assert sink[0]["media_title"] == "Tagesschau"
    assert sink[0]["media_artist"] == "ARD"


def test_media_player_participates_in_room_grouping():
    # #541: a media_player card groups by room alongside lights.
    cards = [
        {"entity_id": "light.l0", "state": "on", "domain": "light"},
        {"entity_id": "light.l1", "state": "on", "domain": "light"},
        {"entity_id": "light.l2", "state": "on", "domain": "light"},
        {"entity_id": "media_player.tv", "state": "playing", "domain": "media_player"},
        {"entity_id": "light.l3", "state": "on", "domain": "light"},
    ]
    area = {
        "light.l0": "Küche",
        "light.l1": "Küche",
        "light.l2": "Wohnzimmer",
        "media_player.tv": "Wohnzimmer",
        "light.l3": "Wohnzimmer",
    }
    assert ha_mod.group_cards_by_room(cards, area) is True
    assert cards[3]["room"] == "Wohnzimmer"


async def test_card_omits_absent_control_attrs(monkeypatch):
    # A plain temperature sensor must not gain phase-3 control keys.
    states = {
        "state": "21.4",
        "attributes": {"friendly_name": "Küche", "unit_of_measurement": "°C"},
    }
    _stub(monkeypatch, states=states)
    sink: list = []
    ha_mod.card_sink.set(sink)

    await _tool("ha_get_state").handler({"entity_id": "sensor.kueche"})

    assert "supported_features" not in sink[0]
    assert "brightness" not in sink[0]


async def test_list_entities_emits_no_cards(monkeypatch):
    # A bulk scan must NOT card every match (#499): "welche Lichter sind an"
    # would otherwise dump a card for every light, not just the on one. The
    # model cards the subset it reports via ha_get_state on those entities.
    states = [
        {
            "entity_id": "light.sofa",
            "state": "on",
            "attributes": {"friendly_name": "Sofalicht"},
        },
        {
            "entity_id": "binary_sensor.garage",
            "state": "off",
            "attributes": {"friendly_name": "Garage", "device_class": "garage"},
        },
    ]
    _stub(monkeypatch, states=states)
    sink: list = []
    ha_mod.card_sink.set(sink)

    out = await _tool("ha_list_entities").handler({})

    assert sink == []
    # the scan still returns the entities for the model to read.
    assert "light.sofa" in out and "binary_sensor.garage" in out


async def test_card_sink_dedupes_same_entity(monkeypatch):
    states = {"state": "on", "attributes": {"friendly_name": "Sofalicht"}}
    _stub(monkeypatch, states=states)
    sink: list = []
    ha_mod.card_sink.set(sink)

    await _tool("ha_get_state").handler({"entity_id": "light.sofa"})
    await _tool("ha_get_state").handler({"entity_id": "light.sofa"})

    assert len(sink) == 1


async def test_no_sink_is_noop(monkeypatch):
    # A turn without a sink set (the facade path may not collect cards) must not
    # raise when a state tool runs.
    states = {"state": "21", "attributes": {}}
    _stub(monkeypatch, states=states)
    ha_mod.card_sink.set(None)

    out = await _tool("ha_get_state").handler({"entity_id": "sensor.x"})
    assert '"state": "21"' in out


def _light_cards():
    return [
        {"entity_id": "light.a", "state": "on", "domain": "light"},
        {"entity_id": "light.b", "state": "off", "domain": "light"},
        {"entity_id": "light.c", "state": "on", "domain": "light"},
    ]


def test_state_scoped_on_query_keeps_only_on_cards():
    # "welche lichter sind an" -> only the ON lights get a card (#536).
    kept = ha_mod.filter_cards_by_query_state(_light_cards(), "welche lichter sind an")
    assert [c["entity_id"] for c in kept] == ["light.a", "light.c"]


def test_state_scoped_off_query_keeps_only_off_cards():
    kept = ha_mod.filter_cards_by_query_state(
        _light_cards(), "welche lichter sind ausgeschaltet"
    )
    assert [c["entity_id"] for c in kept] == ["light.b"]


def test_existence_query_keeps_the_full_set():
    # "welche lichter gibt es" names no state -> all cards survive (#536).
    cards = _light_cards()
    kept = ha_mod.filter_cards_by_query_state(cards, "welche lichter gibt es")
    assert kept == cards


def test_open_state_scope_filters_covers():
    cards = [
        {"entity_id": "cover.a", "state": "open"},
        {"entity_id": "cover.b", "state": "closed"},
    ]
    kept = ha_mod.filter_cards_by_query_state(cards, "welche rollos sind offen")
    assert [c["entity_id"] for c in kept] == ["cover.a"]


def _room_cards(n):
    return [
        {"entity_id": f"light.l{i}", "state": "on", "domain": "light"} for i in range(n)
    ]


def test_group_under_threshold_ungrouped_but_labelled():
    # ≤4 multi-room cards: not grouped, but each still carries its room so the
    # frontend labels every card (#545 — the per-card label path must run).
    cards = _room_cards(4)
    area = {
        "light.l0": "Wohnzimmer",
        "light.l1": "Küche",
        "light.l2": "Bad",
        "light.l3": "Flur",
    }
    assert ha_mod.group_cards_by_room(cards, area) is False
    assert [c["room"] for c in cards] == ["Wohnzimmer", "Küche", "Bad", "Flur"]


def test_group_when_every_room_has_two_plus():
    # >4 cards across 2 rooms, ≥2 each -> grouped, each card carries its room.
    cards = _room_cards(6)
    area = {
        "light.l0": "Küche",
        "light.l1": "Küche",
        "light.l2": "Küche",
        "light.l3": "Bad",
        "light.l4": "Bad",
        "light.l5": "Bad",
    }
    assert ha_mod.group_cards_by_room(cards, area) is True
    assert [c["room"] for c in cards] == ["Küche"] * 3 + ["Bad"] * 3


def test_no_group_when_a_room_is_a_singleton():
    # >4 cards but one room holds a single card -> no grouping; rooms annotated
    # so the frontend labels each card instead (#537).
    cards = _room_cards(5)
    area = {
        "light.l0": "Küche",
        "light.l1": "Küche",
        "light.l2": "Bad",
        "light.l3": "Bad",
        "light.l4": "Flur",
    }
    assert ha_mod.group_cards_by_room(cards, area) is False
    assert cards[4]["room"] == "Flur"


def test_single_room_groups_under_threshold():
    # A room query (#540) cards one room's actuators -> one header even at ≤4.
    cards = _room_cards(3)
    area = {
        "light.l0": "Wohnzimmer",
        "light.l1": "Wohnzimmer",
        "light.l2": "Wohnzimmer",
    }
    assert ha_mod.group_cards_by_room(cards, area) is True
    assert [c["room"] for c in cards] == ["Wohnzimmer"] * 3


def _stub_call(monkeypatch, *, new_state="on", post_status=200, gets=None):
    """Stub aiohttp for call_service_scoped: record POSTs (and any GETs)."""
    posts: list[tuple[str, dict]] = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, posturl, *, json, **k):
            posts.append((posturl, json))
            return _Resp({}, status=post_status)

        def get(self, geturl, **k):
            if gets is not None:
                gets.append(geturl)
            return _Resp({"state": new_state})

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)
    return posts


async def test_call_service_scoped_toggles_without_racy_readback(monkeypatch):
    # #732: no immediate state read-back — a GET right after the POST races HA's
    # settle and would return the stale pre-toggle state. Success is ok-only; the
    # authoritative new state arrives via the SSE card_state bus / poll.
    gets: list[str] = []
    posts = _stub_call(monkeypatch, new_state="on", gets=gets)
    res = await ha_mod.call_service_scoped(
        "http://ha", "tok", "light.kitchen", "light.toggle"
    )
    assert res == {"ok": True}
    assert "state" not in res
    assert not gets  # never reads back the state
    assert posts[0][0] == "http://ha/api/services/light/toggle"
    assert posts[0][1] == {"entity_id": "light.kitchen"}


async def test_call_service_scoped_blocks_unsafe_domain(monkeypatch):
    posts = _stub_call(monkeypatch)
    res = await ha_mod.call_service_scoped(
        "http://ha", "tok", "shell_command.evil", "shell_command.run"
    )
    assert res["ok"] is False
    assert not posts  # never reaches HA


@pytest.mark.parametrize(
    "entity_id,service",
    [
        ("light.kitchen", "switch.toggle"),  # service domain != entity domain
        ("light.kitchen", "../etc/passwd"),  # no dot / path traversal
        ("not_an_entity", "light.toggle"),  # invalid entity_id
    ],
)
async def test_call_service_scoped_rejects_mismatch_and_garbage(
    monkeypatch, entity_id, service
):
    posts = _stub_call(monkeypatch)
    res = await ha_mod.call_service_scoped("http://ha", "tok", entity_id, service)
    assert res["ok"] is False
    assert not posts


async def test_call_service_scoped_surfaces_ha_error(monkeypatch):
    _stub_call(monkeypatch, post_status=500)
    res = await ha_mod.call_service_scoped(
        "http://ha", "tok", "switch.fan", "switch.toggle"
    )
    assert res["ok"] is False
    assert "HA 500" in res["error"]


def _power(eid, name, state):
    return {
        "entity_id": eid,
        "state": state,
        "attributes": {
            "friendly_name": name,
            "device_class": "power",
            "unit_of_measurement": "W",
        },
    }


def _energy(eid, name, state):
    return {
        "entity_id": eid,
        "state": state,
        "attributes": {
            "friendly_name": name,
            "device_class": "energy",
            "unit_of_measurement": "kWh",
        },
    }


async def test_fetch_energy_flow_uses_power_sensors_not_kwh(monkeypatch):
    # The real box exposes BOTH a kWh lifetime counter ("PV Erzeugung") and the
    # current-power (W) sensor ("Aktuell erzeugter PV-Strom") — the flow must
    # pick the W one (#691). Signs: Netz +50 = Bezug, Akku -465 = entlädt.
    states = [
        _power("sensor.pv_w", "Aktuell erzeugter PV-Strom", "0"),
        _power("sensor.haus_w", "Aktueller Hausverbrauch", "271.5"),
        _power("sensor.netz_w", "Aktuelle Netz Leistung", "50"),
        _power("sensor.akku_w", "Aktuelle Akku Leistung", "-465.5"),
        # kWh lifetime counters — totals only, must NOT enter the flow
        _energy("sensor.pv_kwh", "PV Erzeugung", "775.4"),
        _energy("sensor.netzbezug_kwh", "Netzbezug", "6.8"),
        _energy("sensor.einspeisung_kwh", "Netzeinspeisung", "503.8"),
        _energy("sensor.laden_kwh", "Batterie laden", "300.1"),
        _energy("sensor.entladen_kwh", "Batterie entladen", "250.2"),
        # leftover power sensors fall through to circuits, sorted by name
        _power("sensor.kueche", "Küche", "150"),
        _power("sensor.bad", "Bad", "40"),
        # non-energy sensor + a light are ignored
        {
            "entity_id": "sensor.temp",
            "state": "21",
            "attributes": {"device_class": "temperature"},
        },
        {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
    ]
    gets = _stub(monkeypatch, states=states)
    energy = await ha_mod.fetch_energy("http://ha", "tok")
    assert gets[0][0] == "http://ha/api/states"
    flow = {f["label"]: f for f in energy["flow"]}
    # flow order preserved: PV, Haus, Netz, Akku
    assert [f["label"] for f in energy["flow"]] == ["PV", "Haus", "Netz", "Akku"]
    assert flow["PV"]["entity_id"] == "sensor.pv_w"
    assert flow["PV"]["unit"] == "W"
    assert flow["PV"]["sense"] == "supply"
    assert flow["Haus"]["state"] == "271.5" and flow["Haus"]["sense"] == "draw"
    # Netz +50 → grid draw (Bezug); Akku -465.5 → battery supply (entlädt)
    assert flow["Netz"]["state"] == "50" and flow["Netz"]["sense"] == "grid"
    assert flow["Akku"]["state"] == "-465.5" and flow["Akku"]["sense"] == "battery"
    # kWh counters live only in totals, labeled — never in flow/circuits
    total_labels = [t["label"] for t in energy["totals"]]
    assert total_labels == [
        "PV-Erzeugung",
        "Einspeisung",
        "Netzbezug",
        "Batterie geladen",
        "Batterie entladen",
    ]
    assert all(t["unit"] == "kWh" for t in energy["totals"])
    circuit_names = [c["name"] for c in energy["circuits"]]
    assert circuit_names == ["Bad", "Küche"]


async def test_fetch_energy_returns_none_on_ha_error(monkeypatch):
    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp(None, status=500)

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)
    assert await ha_mod.fetch_energy("http://ha", "tok") is None


async def test_fetch_energy_history_only_power_series_ordered(monkeypatch):
    # History must return only the current-power (W) flow sensors — the kWh
    # lifetime counter is present but excluded so the chart is single-axis (#691).
    states = [
        _power("sensor.pv_w", "Aktuell erzeugter PV-Strom", "0"),
        _power("sensor.haus_w", "Aktueller Hausverbrauch", "271.5"),
        _energy("sensor.pv_kwh", "PV Erzeugung", "775.4"),
    ]
    # HA returns the runs out of requested order; each keyed by first point's id.
    pv_run = [
        {"entity_id": "sensor.pv_w", "state": str(i), "last_changed": f"t{i}"}
        for i in range(120)
    ]
    house_run = [
        {"entity_id": "sensor.haus_w", "state": "500", "last_changed": "t0"},
        {"entity_id": "sensor.haus_w", "state": "nope", "last_changed": "t1"},
        {"entity_id": "sensor.haus_w", "state": "700", "last_changed": "t2"},
    ]
    gets = _stub(monkeypatch, states=states, history=[pv_run, house_run])
    out = await ha_mod.fetch_energy_history("http://ha", "tok", 24)
    assert out["hours"] == 24
    assert gets[0][0] == "http://ha/api/states"
    hist_url, params = next((u, p) for u, p in gets if "/api/history/period/" in u)
    # only the W flow ids are queried — the kWh counter is excluded
    assert params["filter_entity_id"] == "sensor.pv_w,sensor.haus_w"
    assert params["no_attributes"] == "true"
    series = {s["entity_id"]: s for s in out["series"]}
    # flow order preserved (PV before Haus) despite HA run order
    assert [s["label"] for s in out["series"]] == ["PV", "Haus"]
    assert all(s["unit"] == "W" for s in out["series"])
    # 120 points downsampled to ~48 for 24h
    assert len(series["sensor.pv_w"]["points"]) <= 49
    # non-numeric state dropped, numeric points kept as {t, v}
    assert series["sensor.haus_w"]["points"] == [
        {"t": "t0", "v": 500.0},
        {"t": "t2", "v": 700.0},
    ]


async def test_fetch_energy_history_empty_when_no_flow(monkeypatch):
    states = [
        {
            "entity_id": "sensor.temp",
            "state": "21",
            "attributes": {"device_class": "temperature"},
        }
    ]
    _stub(monkeypatch, states=states, history=[])
    out = await ha_mod.fetch_energy_history("http://ha", "tok", 168)
    assert out == {"hours": 168, "series": []}


async def test_fetch_entity_history_returns_raw_points(monkeypatch):
    # #755: one entity's history as raw {t, state} points for a widget sparkline.
    run = [
        {"entity_id": "sensor.temp", "state": "21.2", "last_changed": "t0"},
        {"entity_id": "sensor.temp", "state": "21.5", "last_updated": "t1"},
    ]
    gets = _stub(monkeypatch, history=[run])
    out = await ha_mod.fetch_entity_history("http://ha", "tok", "sensor.temp", "48h")
    hist_url, params = next((u, p) for u, p in gets if "/api/history/period/" in u)
    assert params["filter_entity_id"] == "sensor.temp"
    assert out == [{"t": "t0", "state": "21.2"}, {"t": "t1", "state": "21.5"}]


async def test_fetch_entity_history_rejects_bad_entity_id(monkeypatch):
    _stub(monkeypatch, history=[])
    assert (
        await ha_mod.fetch_entity_history("http://ha", "tok", "not-an-id", "24h")
        is None
    )


# --- updated_at_ms ordering stamp (#850 / solaris-android #66) -----------------


def test_last_updated_ms_parses_iso_variants():
    from datetime import UTC, datetime

    from solaris_chat.engine.tools.ha import _last_updated_ms

    expected = int(
        datetime(2026, 7, 16, 10, 0, 0, 123000, tzinfo=UTC).timestamp() * 1000
    )
    # offset form (what HA sends), Z form, and a naive value (assumed UTC)
    assert _last_updated_ms("2026-07-16T10:00:00.123+00:00") == expected
    assert _last_updated_ms("2026-07-16T10:00:00.123Z") == expected
    assert _last_updated_ms("2026-07-16T10:00:00.123") == expected
    # strictly increasing across two updates → orders concurrent toggles
    assert _last_updated_ms("2026-07-16T10:00:01+00:00") > _last_updated_ms(
        "2026-07-16T10:00:00+00:00"
    )


def test_last_updated_ms_none_on_absent_or_garbage():
    from solaris_chat.engine.tools.ha import _last_updated_ms

    assert _last_updated_ms(None) is None
    assert _last_updated_ms("") is None
    assert _last_updated_ms("not-a-timestamp") is None


def test_card_spec_forwards_updated_at_ms_when_last_updated_given():
    from solaris_chat.engine.tools.ha import _last_updated_ms, card_spec

    card = card_spec(
        "light.kitchen",
        "on",
        {"friendly_name": "Küche"},
        "2026-07-16T10:00:00.123+00:00",
    )
    assert card is not None
    assert card["updated_at_ms"] == _last_updated_ms("2026-07-16T10:00:00.123+00:00")


def test_card_spec_omits_updated_at_ms_without_last_updated():
    from solaris_chat.engine.tools.ha import card_spec

    card = card_spec("light.kitchen", "on", {})
    assert card is not None
    assert "updated_at_ms" not in card  # legacy path → guard stays inert


# The house the resident actually has: the "Sofalicht" is light.dimmer_2_5, and
# there is no light.sofalicht for the model to hit (#1241).
_HOUSE = [
    {"entity_id": "light.dimmer_2_5", "attributes": {"friendly_name": "Sofalicht"}},
    {
        "entity_id": "light.dimmer_2_3",
        "attributes": {"friendly_name": "Esszimmertischlicht"},
    },
    {"entity_id": "switch.kaffee", "attributes": {"friendly_name": "Kaffeemaschine"}},
]


async def test_call_service_rejects_hallucinated_entity_id(monkeypatch):
    # HA answers 200 with an empty body for a service call on an entity that
    # never existed — byte-identical to a real one already in the target state.
    # Without the registry check the model got {"success": true} and told the
    # resident "Klar." while nothing had happened (#1241).
    calls: list = []
    _stub(monkeypatch, states=_HOUSE, calls=calls, post_body=[])

    out = json.loads(
        await _checked_tool("ha_call_service").handler(
            {"domain": "light", "service": "turn_on", "entity_id": "light.sofalicht"}
        )
    )

    assert "success" not in out
    assert "light.sofalicht" in out["error"]
    # and it names the real device, so the model retries instead of apologising
    assert out["candidates"][0].startswith("light.dimmer_2_5 | Sofalicht")
    assert all(c.startswith("light.") for c in out["candidates"])
    assert calls == []  # nothing was ever sent to HA


async def test_call_service_still_acts_on_a_real_entity_id(monkeypatch):
    calls: list = []
    _stub(monkeypatch, states=_HOUSE, calls=calls, post_body=[])

    out = json.loads(
        await _checked_tool("ha_call_service").handler(
            {"domain": "light", "service": "turn_on", "entity_id": "light.dimmer_2_5"}
        )
    )

    assert out == {"success": True, "service": "light.turn_on"}
    assert calls == [
        ("http://ha/api/services/light/turn_on", {"entity_id": "light.dimmer_2_5"})
    ]


async def test_call_service_without_entity_id_is_unaffected(monkeypatch):
    # A service that takes no target (e.g. a script run) has nothing to check.
    calls: list = []
    _stub(monkeypatch, states=_HOUSE, calls=calls, post_body=[])
    out = json.loads(
        await _checked_tool("ha_call_service").handler(
            {"domain": "script", "service": "turn_on", "data": {"x": 1}}
        )
    )
    assert out["success"] is True
    assert calls[0][1] == {"x": 1}


async def test_call_service_fails_open_when_the_registry_is_empty(monkeypatch):
    # HA unreachable/absent ⇒ no entity set to check against. Device control
    # must not go down over a registry blip; the call itself surfaces a failure.
    calls: list = []
    _stub(monkeypatch, states=[], calls=calls, post_body=[])
    out = json.loads(
        await _checked_tool("ha_call_service").handler(
            {"domain": "light", "service": "turn_on", "entity_id": "light.dimmer_2_5"}
        )
    )
    assert out["success"] is True
    assert len(calls) == 1


async def test_get_state_rejects_hallucinated_entity_id(monkeypatch):
    _stub(monkeypatch, states=_HOUSE)
    out = json.loads(
        await _checked_tool("ha_get_state").handler({"entity_id": "light.sofalicht"})
    )
    assert "state" not in out
    assert out["candidates"][0].startswith("light.dimmer_2_5 | Sofalicht")


def test_suggest_entities_prefers_the_guessed_domain_then_the_name():
    index = {
        "light.dimmer_2_5": "Sofalicht",
        "light.dimmer_2_3": "Esszimmertischlicht",
        "sensor.sofalicht_power": "Sofalicht Leistung",
    }
    got = registry_mod.suggest_entities("light.sofalicht", index)
    assert got[0] == "light.dimmer_2_5 | Sofalicht"
    assert "sensor.sofalicht_power | Sofalicht Leistung" not in got
    # an unknown domain falls back to the whole house, close matches only
    assert (
        registry_mod.suggest_entities("lamp.sofalicht", index)[0]
        == "light.dimmer_2_5 | Sofalicht"
    )
    assert registry_mod.suggest_entities("lamp.zzzz", index) == []


def test_resolve_entity_takes_the_one_clear_match():
    # The #1263 case: e4b guesses the friendly name as an id, 10/10 on the box.
    index = {
        "light.dimmer_2_5": "Sofalicht",
        "light.dimmer_2_3": "Esszimmertischlicht",
        "cover.esszimmerjalousie": "Esszimmerjalousie",
    }
    got = registry_mod.resolve_entity("light.sofalicht", index)
    assert (got.entity_id, got.name) == ("light.dimmer_2_5", "Sofalicht")
    # A cover slug welded onto the light domain is NOT a light: the service the
    # caller is about to run belongs to the guessed domain, so the resolution
    # stays inside it and the resident is asked instead.
    ask = registry_mod.resolve_entity("light.esszimmerjalousie", index)
    assert ask.entity_id == ""
    assert "Esszimmerjalousie" not in ask.choices
    # Nothing close enough at all → ask, never guess.
    assert registry_mod.resolve_entity("light.zzzzzzzz", index).entity_id == ""


def test_resolve_entity_asks_when_two_candidates_are_equally_good():
    index = {
        "light.dimmer_1": "Deckenlicht Küche",
        "light.dimmer_2": "Deckenlicht Bad",
    }
    got = registry_mod.resolve_entity("light.deckenlicht", index)
    assert got.entity_id == ""
    assert set(got.choices) == {"Deckenlicht Küche", "Deckenlicht Bad"}


async def test_call_service_rechecks_ha_before_refusing_a_new_device(monkeypatch):
    # The index is TTL-cached, so a device added since the last refresh would
    # otherwise be refused as "unknown" for minutes. A miss re-reads HA once.
    house = list(_HOUSE)
    calls: list = []
    _stub(monkeypatch, states=house, calls=calls, post_body=[])
    tool = _checked_tool("ha_call_service")
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.neu"}

    assert "error" in json.loads(await tool.handler(args))
    house.append({"entity_id": "light.neu", "attributes": {"friendly_name": "Neu"}})
    assert json.loads(await tool.handler(args))["success"] is True
    assert len(calls) == 1
