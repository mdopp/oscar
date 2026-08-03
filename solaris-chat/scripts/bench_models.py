"""Compare gemma4 variants for the Solaris Engine model map (Phase 0b).

Benches each candidate model against raw Ollama `/api/chat` with an
engine-shaped payload: a lean German household system prompt with an
injected HA entity registry plus four hand-written tool definitions —
the prompt the Solaris Engine will actually send, not the Hermes-era 12k
prefill. Measures TTFT, wall time, prefill/decode rates per turn and
scores tool-call accuracy (right tool, right entity) on control turns.

Run on the box (host network) or anywhere that reaches Ollama:

    python3 bench_models.py --url http://127.0.0.1:11434 \
        --models gemma4:e2b,gemma4:e4b,gemma4:12b --runs 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request

ENTITIES = [
    ("light.wohnzimmer_decke", "Wohnzimmer Deckenlicht", "Wohnzimmer"),
    ("light.wohnzimmer_stehlampe", "Stehlampe", "Wohnzimmer"),
    ("light.kueche", "Küchenlicht", "Küche"),
    ("light.buero", "Bürolicht", "Büro"),
    ("light.schlafzimmer", "Schlafzimmerlicht", "Schlafzimmer"),
    ("light.kinderzimmer", "Kinderzimmerlicht", "Kinderzimmer"),
    ("light.flur", "Flurlicht", "Flur"),
    ("light.bad", "Badlicht", "Bad"),
    ("switch.kaffeemaschine", "Kaffeemaschine", "Küche"),
    ("switch.steckdose_terrasse", "Steckdose Terrasse", "Garten"),
    ("climate.wohnzimmer", "Thermostat Wohnzimmer", "Wohnzimmer"),
    ("climate.buero", "Thermostat Büro", "Büro"),
    ("climate.schlafzimmer", "Thermostat Schlafzimmer", "Schlafzimmer"),
    ("cover.rollladen_wohnzimmer", "Rollladen Wohnzimmer", "Wohnzimmer"),
    ("cover.rollladen_schlafzimmer", "Rollladen Schlafzimmer", "Schlafzimmer"),
    ("cover.garagentor", "Garagentor", "Garage"),
    ("media_player.wohnzimmer", "Fernseher", "Wohnzimmer"),
    ("media_player.kueche_speaker", "Küchen-Lautsprecher", "Küche"),
    ("scene.filmabend", "Filmabend", "Wohnzimmer"),
    ("scene.gute_nacht", "Gute Nacht", ""),
    ("lock.haustuer", "Haustür", "Flur"),
    ("fan.buero", "Ventilator Büro", "Büro"),
] + [
    # pad to a realistic registry size (~60 controllable entities)
    (f"light.zone_{i}", f"Zusatzlicht {i}", f"Zone {i}")
    for i in range(1, 39)
]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ha_call_service",
            "description": "Steuert ein Home-Assistant-Gerät, z.B. light.turn_on.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "z.B. light, switch, climate",
                    },
                    "service": {
                        "type": "string",
                        "description": "z.B. turn_on, turn_off, set_temperature",
                    },
                    "entity_id": {
                        "type": "string",
                        "description": "Ziel-Entity aus der Geräteliste",
                    },
                    "data": {
                        "type": "object",
                        "description": 'optionale Service-Daten, z.B. {"temperature": 21}',
                    },
                },
                "required": ["domain", "service", "entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ha_get_state",
            "description": "Liest den aktuellen Zustand einer Entity.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timer_set",
            "description": "Stellt einen Timer oder Wecker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_s": {
                        "type": "integer",
                        "description": "Dauer in Sekunden",
                    },
                    "label": {"type": "string"},
                },
                "required": ["duration_s"],
            },
        },
    },
]

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


def build_system() -> str:
    lines = [
        "Du bist Solaris, der Assistent dieses Haushalts. Antworte knapp,",
        "warm und auf Deutsch. Erledige Aufgaben direkt per Tool-Aufruf",
        "statt sie zu beschreiben. Für Gerätesteuerung nutze exakt die",
        "entity_id aus der Geräteliste. Für Zustandsfragen lies den",
        "Live-Zustand mit ha_get_state, rate nie aus dem Gedächtnis.",
        "",
        "Geräte (entity_id | Name | Raum):",
    ]
    lines += [f"{e} | {n} | {a}" for e, n, a in ENTITIES]
    return "\n".join(lines)


def chat(url: str, model: str, messages: list, think: bool) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
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


def run_model(url: str, model: str, runs: int, think: bool) -> dict:
    system = build_system()
    turn_walls, ttfts, tool_ok, tool_total = [], [], 0, 0
    samples = []
    for _ in range(runs):
        messages = [{"role": "system", "content": system}]
        for user, want_tool, want_entity in TASKS:
            messages.append({"role": "user", "content": user})
            r = chat(url, model, messages, think)
            turn_walls.append(r["wall"])
            ttfts.append(r["ttft"])
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
                r2 = chat(url, model, messages, think)
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
        "prompt_tokens": None,
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

    for model in args.models.split(","):
        model = model.strip()
        print(f"\n=== {model} (think={args.think}) ===")
        # one throwaway call so every model starts loaded + prefix-warm
        chat(
            args.url,
            model,
            [
                {"role": "system", "content": build_system()},
                {"role": "user", "content": "Hallo"},
            ],
            args.think,
        )
        res = run_model(args.url, model, args.runs, args.think)
        for k, v in res.items():
            if k == "samples":
                continue
            print(f"  {k}: {v}")
        for s in res["samples"]:
            print(s)


if __name__ == "__main__":
    main()
