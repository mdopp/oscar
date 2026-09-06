#!/usr/bin/env python3
"""Does the household model reach for the right tool on a natural sentence? (#1336)

Registration, schema and dispatch are covered by unit tests; what they cannot
show is whether the model *picks* the tool when a resident phrases the request
its own way. This bench measures exactly that, and nothing else:

* the prompt is the production one — `build_engine_clients()`'s household
  toolbox, the shipped SOUL.md and the real registry block, borrowed from
  `bench_models.py` so a second copy cannot drift;
* every sentence is prepared the way `/api/chat` prepares it — `server._now_hint()`
  in front, which is what `topic_turn_text()` prepends to a household turn — so
  the text measured here is the text a resident's turn actually carries;
* the call goes straight to `LlamaServerChat`, the chat backend the engine uses,
  through the engine's own `remember.routed_pass()` — so a sentence the engine
  routes deterministically is measured routed here too, not as a bench-only
  free choice;
* **nothing is executed.** Only `tool_calls` is read. No fact is stored, no
  radio plays, no note is written — the box is asked to decide, not to act.

The four sentences and the ≥4/5 target come from the #1336 measurement on the
box (gemma4:e4b + MTP, 2026-09-06): `play_radio` 5/5 and `notes_search` 5/5 were
already good, `fact_store` was 1/9 and `get_solaris_status` 0/9 because the SOUL
pointer and the tool descriptions named an example sentence instead of the
intent. `get_solaris_status` reached 5/5 after #1337; `fact_store` stayed at 0/5
however its description was worded, which is why that intent is now routed in
code (#1336). A run is a pass when every sentence hits its tool at least 4 of 5
times and the reported prefill stays inside the budget.

Attempt 2 of that routing passed here 5/5 and failed on the box, because the
bench sent the bare sentence while a real turn arrives behind `[Aktuelle Zeit: …]`
and the trigger is anchored at the start. Preparing the turn the same way is the
whole reason this bench is trustworthy — keep it.

Needs `solaris_chat` importable (the container, or `pip install -e solaris-chat`)
— a dev script, not shipped in the runtime image.

One command, on the box:

    python3 solaris-chat/scripts/bench_tool_decision.py --url http://127.0.0.1:11435

Exits non-zero if a sentence misses the target or the prefill busts the budget.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

from solaris_chat.engine import remember
from solaris_chat.engine.llama_server import LlamaServerChat
from solaris_chat.server import _now_hint

_SCRIPT_DIR = Path(__file__).resolve().parent

# (Satz, erwartetes Tool) — the #1336 table, in its order.
SENTENCES = [
    ("spiel Radio Bayern 3", "play_radio"),
    ("such in meinen Notizen nach Urlaub", "notes_search"),
    ("merk dir, dass der Müll dienstags kommt", "fact_store"),
    ("wie geht es dir", "get_solaris_status"),
]
RUNS = 5
TARGET = 4
# The household turn-1 prefill this change budgets for: #1291 took it to
# 8202-8207 on the box (baseline ~7800, tolerance +200), and trimming the tool
# descriptions brings it back under 8000. llama-server's own `prompt_tokens` is
# the number that counts — the char estimator undercounts German by ~4%.
PREFILL_BUDGET = 8000


def _bench_models():
    spec = importlib.util.spec_from_file_location(
        "bench_models", _SCRIPT_DIR / "bench_models.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _turn_text(sentence: str) -> str:
    """The sentence as `/api/chat` hands it to the engine.

    `server.topic_turn_text()` is a closure over the request app, so the bench
    reuses the piece it always prepends — `server._now_hint()` — joined the same
    way. A household chat with no active topic adds nothing else, which is the
    shape this bench measures.
    """
    return f"{_now_hint()}\n\n{sentence}"


async def _decide(chat, model, system, tools, tool_choice, text, temperature):
    """One turn, generation only — the result is never dispatched."""
    options = {"temperature": temperature} if temperature is not None else None
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
    result = None
    async for kind, payload in chat.stream(
        model,
        messages,
        tools=tools,
        think=False,
        options=options,
        tool_choice=tool_choice,
    ):
        if kind == "done":
            result = payload
    return result


def _prepare(model: str) -> tuple:
    """Prompt assembly, outside any event loop: `bench_models.build_system()`
    drives its own `asyncio.run`, which raises inside a running one."""
    bench = _bench_models()
    household = bench.build_household("http://127.0.0.1:11434")
    system = bench.build_system(household)
    tools = household._profile.toolbox.definitions()
    profile = household._profile
    return system, tools, model or profile.model, profile.temperature


async def _run(url, runs, system, tools, model, temperature) -> int:
    chat = LlamaServerChat(url)

    print(f"model {model}, {len(tools)} tools, {runs} runs per sentence")
    prefills: list[int] = []
    failed = 0
    for text, want in SENTENCES:
        hits = 0
        got: list[str] = []
        tool_choice = ""
        for _ in range(runs):
            # The turn as /api/chat builds it, and the engine's own routing
            # (#1336) — not bench-local copies of either. A memory intent goes
            # out with fact_store alone under a forced tool_choice, so what is
            # measured here is the path a resident actually gets.
            turn = _turn_text(text)
            turn_tools, tool_choice = remember.routed_pass(turn, tools)
            result = await _decide(
                chat, model, system, turn_tools, tool_choice, turn, temperature
            )
            # A routed turn ships one tool schema, so its prefill is not the
            # household one the budget is about.
            if result.prompt_tokens and not tool_choice:
                prefills.append(result.prompt_tokens)
            names = [c["function"]["name"] for c in result.tool_calls]
            if want in names:
                hits += 1
            else:
                got.append(",".join(names) or "-")
        target = TARGET if runs == RUNS else (runs * TARGET + RUNS - 1) // RUNS
        ok = hits >= target
        failed += not ok
        miss = f"   miss: {'; '.join(got)}" if got else ""
        route = " routed" if tool_choice else ""
        print(
            f"  {'ok  ' if ok else 'FAIL'} {hits}/{runs} {want:<20} {text!r}"
            f"{route}{miss}"
        )

    prefill = min(prefills) if prefills else 0
    over = prefill > PREFILL_BUDGET
    failed += over
    print(
        f"  {'FAIL' if over else 'ok  '} prefill {prefill} tok (budget {PREFILL_BUDGET})"
    )
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:11435", help="llama-server")
    ap.add_argument("--model", default="", help="default: the household profile's")
    ap.add_argument("--runs", type=int, default=RUNS)
    args = ap.parse_args()
    sys.exit(asyncio.run(_run(args.url, args.runs, *_prepare(args.model))))


if __name__ == "__main__":
    main()
