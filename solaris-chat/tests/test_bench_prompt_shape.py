"""bench_models.py must bench the household that exists (#1149).

Its prompt used to be hand-written: three tool schemas plus a padded 60-entity
list, 1355 tokens, against a real turn-1 prefill of 7749-7817 measured on the
box (2026-08-03). Every latency number it produced described a household that
does not exist. The prompt is now *derived* from `build_engine_clients()` — the
production toolbox, the shipped SOUL.md, the real registry renderer — so a
second, drifting copy can't grow back. These pin that derivation.

Must NOT import alembic (it lives only in database/; #378).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_models.py"


def _bench():
    spec = importlib.util.spec_from_file_location("bench_models", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bench():
    return _bench()


@pytest.fixture(scope="module")
def household(bench):
    return bench.build_household("http://127.0.0.1:11434")


def test_tools_are_the_production_household_toolbox(bench, household):
    """Not a hand-written subset: the schemas the engine boots with."""
    names = set(household._profile.toolbox.names())
    assert {"ha_call_service", "ha_get_state", "timer_set"} <= names
    # the blocks the 1355-token prompt left out entirely
    assert {"notes_search", "music_query", "task_add", "pin_favorite"} <= names
    assert len(names) >= 30, f"only {len(names)} tools — profile assembly changed?"
    definitions = household._profile.toolbox.definitions()
    assert {d["function"]["name"] for d in definitions} == names


def test_system_prompt_is_the_real_assembly(bench, household):
    system = bench.build_system(household)
    soul = Path(bench.soul_path()).read_text(encoding="utf-8")
    assert soul.strip() in system
    # the registry block, rendered by EntityRegistry rather than restated here
    assert "Räume im Haus: Wohnzimmer, Esszimmer" in system
    assert "light.buero | Bürolicht | Büro" in system
    assert "cover.garagentor | Garagentor | Garage | garage" in system
    assert "cover: open_cover/close_cover/stop_cover/set_cover_position" in system
    # read-only entities stay out of the prompt but seed the discovery legend
    assert "sensor.wohnzimmer_temperatur" not in system
    assert "Sensor-device_class: battery, door, energy" in system
    assert "read-only domains: binary_sensor, sensor" in system
    # the tool-discipline rule respond() pins last
    assert system.endswith(bench._TOOL_DISCIPLINE)


def test_task_entities_exist_in_the_registry(bench):
    """The control turns are only scoreable if their targets are in the house."""
    ids = {e for e, _n, _a in bench.ENTITIES}
    for _user, want_tool, want_entity in bench.TASKS:
        if want_tool and want_entity:
            assert want_entity in ids


def test_prefill_matches_the_box_measurement(bench, household):
    """The 51-entity count and the resulting total both track 2026-08-03."""
    assert len(bench.ENTITIES) == 51
    system = bench.build_system(household)
    est = bench.check_shape(household, system)  # raises SystemExit on drift
    lo, hi = bench.MEASURED_TURN1_TOKENS
    assert est > 4 * 1355, f"{est} est-tok is still near the old synthetic prompt"
    assert lo * 0.9 <= est <= hi * 1.1


def test_check_shape_rejects_a_drifted_prompt(bench, household):
    with pytest.raises(SystemExit, match="prefill shape drifted"):
        bench.check_shape(household, "Du bist Solaris.")
