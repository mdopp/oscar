#!/usr/bin/env python3
"""Realistic end-to-end latency benchmark for the Solaris chat path.

Hits the REAL `/api/chat` endpoint (solaris-chat → Hermes → llama-server) with a
multi-turn conversation and full generation — the latency a resident actually
feels, not an isolated warm-prefill micro-benchmark. Reports per-turn wall time
and p50/p95 across repeats.

Run from inside the pod (shares localhost with the chat server) or point
`--url` at a reachable chat server:

    python -m solaris_chat.scripts.bench_chat            # localhost:8787
    python bench_chat.py --url http://127.0.0.1:8787 --runs 3

Turns reuse one session so turn 2+ exercise the warm-prefix path, matching a
real conversation. Ephemeral so it never writes to a resident's history.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request

# A realistic household conversation: greeting, factual, creative, follow-up.
# Keep replies naturally short — the point is per-turn latency, not output length.
DEFAULT_TURNS = [
    "Hallo Solaris, wie geht es dir?",
    "Welche Farbe hat der Himmel?",
    "Erzähl mir einen kurzen Spruch zum Tag.",
    "Danke. Und was war meine erste Frage?",
]


def _post(url: str, uid: str, text: str, session_id: str | None) -> tuple[float, dict]:
    body = {"input": text, "ephemeral": True}
    if session_id:
        body["session_id"] = session_id
    req = urllib.request.Request(
        url.rstrip("/") + "/api/chat",
        data=json.dumps(body).encode(),
        headers={"Remote-User": uid, "Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = json.loads(r.read())
    return time.time() - t0, payload


def run_conversation(url: str, uid: str, turns: list[str]) -> list[float]:
    session_id: str | None = None
    walls: list[float] = []
    for i, q in enumerate(turns, 1):
        wall, payload = _post(url, uid, q, session_id)
        session_id = payload.get("session_id") or session_id
        walls.append(wall)
        reply = (payload.get("reply") or "").replace("\n", " ")
        print(f"  T{i}  {wall:5.2f}s  A={reply[:50]!r}")
    return walls


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:8787")
    ap.add_argument("--uid", default="household")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--target", type=float, default=2.0, help="per-turn budget (s)")
    args = ap.parse_args()

    all_walls: list[float] = []
    for run in range(1, args.runs + 1):
        print(f"=== run {run}/{args.runs} ===")
        all_walls += run_conversation(args.url, args.uid, DEFAULT_TURNS)

    # Turn 1 of each run carries session-creation cost; report it separately.
    per_turn = len(DEFAULT_TURNS)
    first = [all_walls[i] for i in range(0, len(all_walls), per_turn)]
    rest = [w for i, w in enumerate(all_walls) if i % per_turn != 0]
    print("\n=== summary ===")
    print(f"  first-turn   p50={statistics.median(first):.2f}s  max={max(first):.2f}s")
    if rest:
        rest_sorted = sorted(rest)
        p95 = rest_sorted[min(len(rest_sorted) - 1, int(len(rest_sorted) * 0.95))]
        print(
            f"  follow-turns p50={statistics.median(rest):.2f}s  "
            f"p95={p95:.2f}s  max={max(rest):.2f}s"
        )
    worst = max(all_walls)
    verdict = "PASS" if worst <= args.target else "OVER BUDGET"
    print(f"  worst turn   {worst:.2f}s  (target ≤{args.target:.1f}s)  → {verdict}")


if __name__ == "__main__":
    main()
