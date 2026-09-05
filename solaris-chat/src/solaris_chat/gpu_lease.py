"""The whole-card GPU lease (#1320).

foundry (Gemma 4 26B-A4B, 14.1 GB) and the coding run (Qwen 3.8 27B, 15.0 GB)
each need the entire 16.4 GB card — neither fits beside Solaris' own
llama-server (3.9 GB), let alone the voice stack. The operator's decision of
2026-09-05 is that they take it on request, with no time window and no
presence check, and that Solaris says so instead of hanging.

`gpu-lease.py acquire <holder>` on the box writes this file and then stops
Ollama, the four voice units and `llama.service`; `release` starts them again,
waits for llama-server's `/health` (the household model is warm) and only then
removes it. Its presence is therefore exactly "the household model is not
loaded", and reading it costs a stat instead of a request to a server that is
not running.

Since #1319 a lease also has a **mode** and a **deadline**:

* `exclusive` — foundry's shape, the one above: nothing answers, so a turn gets
  one honest German sentence instead of a timeout against a dead socket.
* `coding` — the card goes to the coding model, but llama-server keeps serving
  it, so Solaris answers the household from that model for the window and the
  chat carries a banner naming it. Only the swap itself mutes (`ready: false`).

The deadline is enforced on the box by a transient systemd timer that runs
`release`, not here — an end signal alone was not enough in #1260. What this
module does with `until` is show the resident when their assistant is back to
normal.
"""

from __future__ import annotations

import json
from pathlib import Path

LEASE_FILENAME = "gpu_lease.json"

# What the resident hears while another job holds the card. A fixed sentence,
# because the model that would phrase something friendlier is the one that is
# unloaded: it says what is happening and when to come back, and nothing else.
BUSY_REPLY = (
    "Ich rechne gerade an einer großen Aufgabe und brauche dafür die ganze "
    "Grafikkarte. Sobald sie frei ist, bin ich wieder da — frag mich in ein "
    "paar Minuten noch einmal."
)


def lease_path(db_path: str) -> Path:
    """The lease file beside `solaris.db` — the chat pod mounts that directory,
    and the box writes into the same host path."""
    return Path(db_path).parent / LEASE_FILENAME


def is_leased(path: str | Path) -> bool:
    """True while another job holds the card.

    The file existing *is* the lease: a truncated or half-written one still
    means the units are stopped, so it counts as held rather than being read
    as no lease and answered into a dead socket.
    """
    return bool(path) and Path(path).exists()


def _read(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def holder(path: str | Path) -> str:
    """Who holds it, for the log line; `""` when the file says nothing."""
    name = _read(path).get("holder")
    return name.strip() if isinstance(name, str) else ""


def mutes_chat(path: str | Path) -> bool:
    """True when no model can answer this turn.

    A `coding` lease does not mute: llama-server is serving the coding model
    and the household turn goes to it (#1319, mode B). The exception is the
    swap itself — `ready` is false while the coding model is still loading,
    and those ~2 minutes are exactly the dead socket the fixed sentence exists
    for. Anything unreadable counts as muting, as it did in #1320.
    """
    if not is_leased(path):
        return False
    lease = _read(path)
    return lease.get("mode") != "coding" or not lease.get("ready")


def state(path: str | Path) -> dict | None:
    """What the chat surface shows about the lease, or `None` when there is
    none. `until` is epoch seconds — the browser formats it in local time."""
    if not is_leased(path):
        return None
    lease = _read(path)
    until = lease.get("until")
    return {
        "mode": "coding" if lease.get("mode") == "coding" else "exclusive",
        "model": str(lease.get("model") or ""),
        "until": float(until) if isinstance(until, (int, float)) else 0.0,
        "answers": not mutes_chat(path),
    }
