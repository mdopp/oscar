"""Compare gemma4 variants for the Solaris Engine model map (Phase 0b).

Benches each candidate model against raw Ollama `/api/chat` with the prefill
the engine actually sends: the **production** household prompt assembly —
`build_engine_clients()`'s own toolbox, the shipped/operator SOUL.md, and the
registry block the real `EntityRegistry` renders for a 51-entity house.
Measures TTFT, wall time, prefill/decode rates per turn and scores tool-call
accuracy (right tool, right entity) on control turns.

Until #1149 the prompt was hand-written here — three tool schemas and a padded
60-entity list, 1355 tokens against a real turn-1 prefill of 7749–7817. Every
latency number described a household that does not exist. The prompt is now
*derived* from the engine rather than restated, and `check_shape()` fails the
run if the derived total drifts out of the measured band, so a second copy
can't silently grow back.

Needs `solaris_chat` importable (the container, or `pip install -e solaris-chat`)
— this is a dev script and is not shipped in the runtime image.

Run on the box (host network) or anywhere that reaches Ollama:

    python3 bench_models.py --url http://127.0.0.1:11434 \
        --models gemma4:e2b,gemma4:e4b,gemma4:12b --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import urllib.request
from pathlib import Path
from typing import Any

from solaris_chat.config import settings
from solaris_chat.engine.areas import AreaSnapshot
from solaris_chat.engine.client import _TOOL_DISCIPLINE, EngineClient
from solaris_chat.engine.profiles import build_engine_clients
from solaris_chat.engine.tools import estimate_tokens

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The box measurement this bench is shaped against: a real household turn-1
# prefill, 34 tools and 51 injected HA entities, 2026-08-03 (README.md,
# solaris-architecture.md, and the #1120 comment that recorded it).
MEASURED_TURN1_TOKENS = (7749, 7817)
# How far the derived prefill may sit from that band before the bench refuses
# to report. A miss means the engine's prompt shape moved, not that the guard
# is wrong: re-measure `prompt_tokens` on the box and update the constant.
SHAPE_TOLERANCE = 0.10

# A 51-entity house — the count the 2026-08-03 measurement was taken on. These
# are the HA *states*; the prompt lines are rendered by the real registry.
ENTITIES = [
    ("light.wohnzimmer_decke", "Wohnzimmer Deckenlicht", "Wohnzimmer"),
    ("light.wohnzimmer_stehlampe", "Stehlampe", "Wohnzimmer"),
    ("light.wohnzimmer_tv_backlight", "TV-Hintergrundlicht", "Wohnzimmer"),
    ("light.esszimmer_pendelleuchte", "Esszimmer Pendelleuchte", "Esszimmer"),
    ("light.kueche", "Küchenlicht", "Küche"),
    ("light.kueche_arbeitsplatte", "Arbeitsplattenlicht", "Küche"),
    ("light.buero", "Bürolicht", "Büro"),
    ("light.buero_schreibtisch", "Schreibtischlampe", "Büro"),
    ("light.schlafzimmer", "Schlafzimmerlicht", "Schlafzimmer"),
    ("light.schlafzimmer_nachttisch", "Nachttischlampe", "Schlafzimmer"),
    ("light.kinderzimmer", "Kinderzimmerlicht", "Kinderzimmer"),
    ("light.kinderzimmer_sternenhimmel", "Sternenhimmel", "Kinderzimmer"),
    ("light.flur", "Flurlicht", "Flur"),
    ("light.bad", "Badlicht", "Bad"),
    ("light.bad_spiegel", "Spiegelleuchte", "Bad"),
    ("light.keller", "Kellerlicht", "Keller"),
    ("light.garage", "Garagenlicht", "Garage"),
    ("light.garten_terrasse", "Terrassenlicht", "Garten"),
    ("switch.kaffeemaschine", "Kaffeemaschine", "Küche"),
    ("switch.spuelmaschine", "Spülmaschine", "Küche"),
    ("switch.steckdose_terrasse", "Steckdose Terrasse", "Garten"),
    ("switch.gartenbewaesserung", "Gartenbewässerung", "Garten"),
    ("switch.waschmaschine", "Waschmaschine", "Keller"),
    ("switch.buero_monitor", "Monitor Büro", "Büro"),
    ("switch.weihnachtsbeleuchtung", "Weihnachtsbeleuchtung", "Wohnzimmer"),
    ("climate.wohnzimmer", "Thermostat Wohnzimmer", "Wohnzimmer"),
    ("climate.kueche", "Thermostat Küche", "Küche"),
    ("climate.buero", "Thermostat Büro", "Büro"),
    ("climate.schlafzimmer", "Thermostat Schlafzimmer", "Schlafzimmer"),
    ("climate.kinderzimmer", "Thermostat Kinderzimmer", "Kinderzimmer"),
    ("climate.bad", "Thermostat Bad", "Bad"),
    ("cover.rollladen_wohnzimmer", "Rollladen Wohnzimmer", "Wohnzimmer"),
    ("cover.rollladen_esszimmer", "Rollladen Esszimmer", "Esszimmer"),
    ("cover.rollladen_schlafzimmer", "Rollladen Schlafzimmer", "Schlafzimmer"),
    ("cover.rollladen_kinderzimmer", "Rollladen Kinderzimmer", "Kinderzimmer"),
    ("cover.rollladen_buero", "Rollladen Büro", "Büro"),
    ("cover.markise_terrasse", "Markise Terrasse", "Garten"),
    ("cover.garagentor", "Garagentor", "Garage"),
    ("media_player.wohnzimmer", "Fernseher", "Wohnzimmer"),
    ("media_player.wohnzimmer_sonos", "Sonos Wohnzimmer", "Wohnzimmer"),
    ("media_player.kueche_speaker", "Küchen-Lautsprecher", "Küche"),
    ("media_player.buero_speaker", "Büro-Lautsprecher", "Büro"),
    ("media_player.kinderzimmer_speaker", "Kinderzimmer-Lautsprecher", "Kinderzimmer"),
    ("scene.filmabend", "Filmabend", "Wohnzimmer"),
    ("scene.gute_nacht", "Gute Nacht", ""),
    ("scene.aufstehen", "Aufstehen", ""),
    ("script.alles_aus", "Alles aus", ""),
    ("lock.haustuer", "Haustür", "Flur"),
    ("fan.buero", "Ventilator Büro", "Büro"),
    ("fan.schlafzimmer", "Ventilator Schlafzimmer", "Schlafzimmer"),
    ("vacuum.staubsauger", "Staubsauger", "Flur"),
]

# Read-only entities: never packed into the prompt, but their device_classes
# are what the registry's discovery legend advertises (#535/#379).
READONLY = [
    ("sensor.wohnzimmer_temperatur", "Wohnzimmer Temperatur", "temperature"),
    ("sensor.wohnzimmer_luftfeuchte", "Wohnzimmer Luftfeuchte", "humidity"),
    ("sensor.buero_temperatur", "Büro Temperatur", "temperature"),
    ("sensor.waschmaschine_leistung", "Waschmaschine Leistung", "power"),
    ("sensor.hausverbrauch", "Hausverbrauch", "energy"),
    ("sensor.flur_helligkeit", "Flur Helligkeit", "illuminance"),
    ("sensor.tuersensor_batterie", "Türsensor Batterie", "battery"),
    ("binary_sensor.flur_bewegung", "Flur Bewegung", "motion"),
    ("binary_sensor.haustuer_kontakt", "Haustür Kontakt", "door"),
    ("binary_sensor.kueche_fenster", "Küchenfenster", "window"),
    ("binary_sensor.keller_wasser", "Keller Wassermelder", "moisture"),
]

ROOMS = [
    "Wohnzimmer",
    "Esszimmer",
    "Küche",
    "Büro",
    "Schlafzimmer",
    "Kinderzimmer",
    "Flur",
    "Bad",
    "Keller",
    "Garage",
    "Garten",
]

# cover carries its device_class into the prompt line — a garage door is
# confirm-first, an ordinary blind is not.
_COVER_CLASSES = {"cover.garagentor": "garage"}
_COVER_SUPPORTED_FEATURES = 15  # incl. SUPPORT_SET_POSITION, as the real ones do

# (user message, expected tool name or None, expected entity_id substring or None)
TASKS = [
    ("Wie heißt die Hauptstadt von Australien?", None, None),
    ("Schalte das Licht im Büro ein.", "ha_call_service", "light.buero"),
    (
        "Mach die Stehlampe im Wohnzimmer aus.",
        "ha_call_service",
        "light.wohnzimmer_stehlampe",
    ),
    (
        "Stell die Heizung im Schlafzimmer auf 19 Grad.",
        "ha_call_service",
        "climate.schlafzimmer",
    ),
    ("Ist das Garagentor zu?", "ha_get_state", "cover.garagentor"),
    ("Stell einen Timer auf 10 Minuten für die Pizza.", "timer_set", None),
]


def soul_path() -> str:
    """The soul the engine loads: the operator's copy on the box, else the
    shipped household pack in this repo."""
    if Path(settings.soul_path).is_file():
        return settings.soul_path
    return str(_REPO_ROOT / "templates/solaris/skills/household/SOUL.md")


async def _states() -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for entity_id, name, _area in ENTITIES:
        attrs: dict[str, Any] = {"friendly_name": name}
        if entity_id.startswith("cover."):
            attrs["device_class"] = _COVER_CLASSES.get(entity_id, "shade")
            attrs["supported_features"] = _COVER_SUPPORTED_FEATURES
        states.append({"entity_id": entity_id, "attributes": attrs})
    states += [
        {"entity_id": e, "attributes": {"friendly_name": n, "device_class": c}}
        for e, n, c in READONLY
    ]
    return states


def build_household(ollama_url: str) -> EngineClient:
    """The production household profile, with the synthetic house pre-loaded
    into its registry so no HA call is needed.

    Going through `build_engine_clients` is the point: the tool schemas and the
    prompt scaffold are whatever the engine boots with, so the bench prefill
    cannot drift from the deployed one (#1149)."""
    household, *_rest = build_engine_clients(
        db_path="/tmp/solaris-bench.sqlite",
        ollama_url=ollama_url,
        fast_model="",
        thorough_model="",
        soul_path=soul_path(),
        hass_url="http://solaris-bench-ha:8123",
        hass_token="bench",
        notes_dir="/tmp/solaris-bench-notes",
        gatekeeper_url="http://solaris-bench-gatekeeper:10400",
        jellyfin_url="http://solaris-bench-jellyfin:8096",
    )
    registry = household._profile.registry
    registry._fetch_states = _states
    registry._areas._snap = AreaSnapshot(
        rooms=ROOMS, entity_area={e: a for e, _n, a in ENTITIES if a}
    )
    registry._areas._fetched = True
    registry._areas._fetched_at = time.time()
    return household


def build_system(household: EngineClient) -> str:
    """The facade-path system prompt: the engine's own assembly plus the
    tool-discipline tail respond() pins last."""
    system = asyncio.run(household._system_prompt_stateless())
    return "\n\n".join([system, _TOOL_DISCIPLINE])


def check_shape(household: EngineClient, system: str) -> int:
    """Refuse to report latencies for a household that doesn't exist (#1149).

    The engine logs the tools/soul/registry/scaffold breakdown itself
    (`engine.prompt.composition`); this only asserts the total against the box
    measurement, using the engine's own estimator."""
    toolbox = household._profile.toolbox
    est = estimate_tokens(len(system) + toolbox.schema_chars())
    lo, hi = MEASURED_TURN1_TOKENS
    if not lo * (1 - SHAPE_TOLERANCE) <= est <= hi * (1 + SHAPE_TOLERANCE):
        raise SystemExit(
            f"prefill shape drifted: {est} est-tok from {len(toolbox.names())} tools "
            f"is outside {lo}-{hi} ±{SHAPE_TOLERANCE:.0%}. Re-measure prompt_tokens "
            f"on the box and update MEASURED_TURN1_TOKENS."
        )
    return est


def chat(url: str, model: str, messages: list, tools: list, think: bool) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": True,
        "think": think,
    }
    req = urllib.request.Request(
        f"{url}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    ttft = None
    content = ""
    tool_calls = []
    final = {}
    with urllib.request.urlopen(req, timeout=300) as resp:
        for line in resp:
            chunk = json.loads(line)
            msg = chunk.get("message") or {}
            if ttft is None and (msg.get("content") or msg.get("tool_calls")):
                ttft = time.monotonic() - t0
            content += msg.get("content") or ""
            tool_calls += msg.get("tool_calls") or []
            if chunk.get("done"):
                final = chunk
    return {
        "wall": time.monotonic() - t0,
        "ttft": ttft if ttft is not None else time.monotonic() - t0,
        "content": content,
        "tool_calls": tool_calls,
        "prompt_tokens": final.get("prompt_eval_count", 0),
        "prefill_tps": _rate(final, "prompt_eval_count", "prompt_eval_duration"),
        "decode_tps": _rate(final, "eval_count", "eval_duration"),
    }


def _rate(final: dict, count_key: str, dur_key: str) -> float:
    dur = final.get(dur_key) or 0
    return round(final.get(count_key, 0) / (dur / 1e9), 1) if dur else 0.0


def run_model(
    url: str, model: str, runs: int, think: bool, system: str, tools: list
) -> dict:
    turn_walls, ttfts, tool_ok, tool_total = [], [], 0, 0
    prefills: list[int] = []
    samples = []
    for _ in range(runs):
        messages = [{"role": "system", "content": system}]
        for user, want_tool, want_entity in TASKS:
            messages.append({"role": "user", "content": user})
            r = chat(url, model, messages, tools, think)
            turn_walls.append(r["wall"])
            ttfts.append(r["ttft"])
            prefills.append(r["prompt_tokens"])
            if want_tool:
                tool_total += 1
                got = r["tool_calls"][0]["function"] if r["tool_calls"] else None
                got_name = got["name"] if got else None
                got_args = json.dumps(got.get("arguments", {})) if got else ""
                if got_name == want_tool and (
                    not want_entity or want_entity in got_args
                ):
                    tool_ok += 1
                else:
                    samples.append(f"  MISS {user!r} -> {got_name} {got_args[:120]}")
            if r["tool_calls"]:
                messages.append({"role": "assistant", "tool_calls": r["tool_calls"]})
                messages.append({"role": "tool", "content": '{"success": true}'})
                r2 = chat(url, model, messages, tools, think)
                turn_walls.append(r2["wall"])
                messages.append({"role": "assistant", "content": r2["content"]})
            else:
                messages.append({"role": "assistant", "content": r["content"]})
            if len(samples) < 3 and not want_tool and r["content"]:
                samples.append(f"  A: {user!r} -> {r['content'][:160]!r}")
    return {
        "wall_p50": round(statistics.median(turn_walls), 2),
        "wall_p95": round(sorted(turn_walls)[int(len(turn_walls) * 0.95) - 1], 2),
        "ttft_p50": round(statistics.median(ttfts), 2),
        # The first turn of each run: system prompt + tools, no history yet —
        # the prefill the engine pays every turn, straight from Ollama.
        "prompt_tokens": min(prefills) if prefills else 0,
        "tool_acc": f"{tool_ok}/{tool_total}",
        "samples": samples,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:11434")
    ap.add_argument("--models", default="gemma4:e2b,gemma4:e4b,gemma4:12b")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument(
        "--think", action="store_true", help="enable thinking (default off = fast path)"
    )
    args = ap.parse_args()

    household = build_household(args.url)
    system = build_system(household)
    tools = household._profile.toolbox.definitions()
    est = check_shape(household, system)
    lo, hi = MEASURED_TURN1_TOKENS
    print(f"prefill: {len(tools)} tools, {len(ENTITIES)} entities, ~{est} est-tok")
    print(f"         (box turn-1 measurement {lo}-{hi} tok; Ollama's count below)")

    for model in args.models.split(","):
        model = model.strip()
        print(f"\n=== {model} (think={args.think}) ===")
        # one throwaway call so every model starts loaded + prefix-warm
        chat(
            args.url,
            model,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": "Hallo"},
            ],
            tools,
            args.think,
        )
        res = run_model(args.url, model, args.runs, args.think, system, tools)
        for k, v in res.items():
            if k == "samples":
                continue
            print(f"  {k}: {v}")
        for s in res["samples"]:
            print(s)


if __name__ == "__main__":
    main()
