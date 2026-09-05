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

Unlike the neighbour model lease (#1260) this carries no TTL: the operator
ruled out a time window, and a holder that dies without releasing leaves the
units stopped, so the file still describes the truth.
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


def holder(path: str | Path) -> str:
    """Who holds it, for the log line; `""` when the file says nothing."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    name = data.get("holder")
    return name.strip() if isinstance(name, str) else ""
