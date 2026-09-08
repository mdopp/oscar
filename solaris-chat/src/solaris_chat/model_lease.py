"""The neighbour-service GPU lease over HTTP (#1333, contract
mdopp/foundry-chronicle#321).

`POST/DELETE /api/model-lease` was the Ollama-era model swap (#1260): for the
duration of a declared window Solaris answered with the model foundry already
held. With llama.cpp serving one model at a time (#1318) that swap no longer
exists — the real primitive is the box's GPU lease (#1319/#1325), which
reloads llama-server on the leased profile. This module keeps the HTTP shape
foundry built against and makes it the front for that primitive.

The engine cannot switch systemd units from inside its container, so it does
not try: a request is *written* to `gpu_lease_request.json` and a host
`solaris-gpu-lease-broker.path` runs `gpu-lease.py` for it. Which means the
HTTP layer never blocks on a 12 GB download — it answers from the two files
the box owns:

* `gpu_lease.json` — the lease itself, written by `gpu-lease.py`. Its `ready`
  flag is what separates `preparing` from `ready`.
* `gpu_lease_status.json` — what the broker did with the last request. Its
  `requested_at` is compared against the request's, so a request still to be
  picked up reads as `preparing` instead of as no lease at all.

Everything here fails **open**: an unreadable or missing file reads as "no
lease", i.e. the household model on a normal box.

Since #1347 a window also names the **holder** that asked for it, so a service
restarting can tell its own open window from a stranger's instead of closing
whatever it finds: `POST` takes an optional `holder`, `GET` reports it, and
`DELETE {"holder": ...}` releases only a matching window. A holder is the
identity of the *service* — one permanent name, the same for every window it
ever takes — never a session, round, group or person, which is why the shape is
pinned to a short token rather than left as free text.

Since #1361 a window also **ends when its holder stops renewing**, not only at
the deadline. Two rules, one on each side:

* **Broker side.** Every `POST` stamps `last_renewed_at` on the window, and the
  box arms its release for the **grace** — `2 x renew_after`, i.e. two missed
  renewals — instead of for the full TTL. A holder that keeps renewing never
  notices; a holder that dies without a `DELETE` loses the card after the grace
  (10 minutes on a 900 s window) rather than after the TTL, which for a 4-hour
  coding window would have kept the household on the wrong model all afternoon.
  The TTL stays the outer net: the release is never armed past it. `GET`
  reports both `last_renewed_at` and `renew_after` so a holder can see this
  arithmetic instead of having to know it.
* **Consumer side.** A service that died with a window open cannot tell from
  its own memory that it still holds one — that state died with the process.
  So **on start it does a `GET`, and if `holder` is its own name it `DELETE`s
  with that holder first.** The call is idempotent and, on a clean start, a
  no-op: there is no window to find. This is the rule
  `mdopp/foundry-chronicle#333` was waiting for, and `templates/pi-web`
  follows it in its host lease unit.

Since #1364 the giving back is as asynchronous as the taking, and says so.
`DELETE` only writes the request; the broker starts the units back up and
waits for the household model before it removes the lease file, which is
seconds during which the window is neither held nor gone. That in-between is
`releasing` — reported by `GET` and answered by `DELETE` itself — and the
contract addendum on `mdopp/foundry-chronicle#321` is:

* `releasing` always carries `retry_after`, from the same source `preparing`
  takes it from.
* After a `DELETE` a consumer waits for `none`. It never sends a second
  `DELETE` for a window that reads `releasing` or `preparing`.
* Giving up on waiting is harmless: the release runs on the host whether
  anyone polls or not. Nothing is left half-done by walking away.
* The start-cleanup above applies only to a window that is `ready` **and**
  carries the consumer's own holder — never to one that is already going
  away.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from solaris_chat import gpu_lease

# The two leases a neighbour may ask for; anything else is a 400. Both are
# `gpu-lease.py --model` values — the HTTP name and the box's profile name are
# deliberately one word, so a lease cannot be requested under a name the box
# does not know.
MODELS = ("foundry", "coding")

# 5 minutes to 24 hours. The floor keeps a lease from expiring inside its own
# swap (the 12B cold-loads in ~40 s). The ceiling was the box's own default
# lease duration (4 h) until #1374: the Modell tile lets a resident say "bis
# morgen 07:00", which at teatime is longer than that, so the cap is a day —
# the contract addendum on mdopp/foundry-chronicle#321. The deadline is still
# the outer net and the grace (#1361) still ends a window nobody renews, so a
# longer ceiling buys a longer *asked-for* window, not a longer orphan.
TTL_MIN_SECONDS = 300
TTL_MAX_SECONDS = 86400
# What a payload that names no window gets: unchanged at the box's own default
# lease duration, so raising the ceiling above did not silently give every
# existing caller a 24-hour window.
TTL_DEFAULT_SECONDS = 14400

# What a `preparing` answer tells the caller to wait before polling `GET`.
RETRY_AFTER_SECONDS = 30

# The model name llama-server reports (`--alias`) per lease, and for the
# household model when no lease is held. The box sets the same three strings
# in `templates/llama/post-deploy.py`; the leased ones are read back out of
# the lease file rather than assumed, so only the household default lives in
# two places (and `llama-profile.json` overrides it).
ALIASES = {"foundry": "gemma-4-12b", "coding": "qwen3.8-27b"}
HOUSEHOLD_ALIAS = "gemma-4-e4b"

# The complete payload. Adding a key here is a contract change on both sides.
PAYLOAD_KEYS = ("model", "ttl_s", "holder")

# What a `holder` may look like: at most 64 characters of `a-z`, `0-9` and `-`.
# The shape is the guarantee — a name this small cannot carry a session, round,
# group or player, so the field stays what the contract says it is.
HOLDER_PATTERN = re.compile(r"[a-z0-9-]{1,64}\Z")

REQUEST_FILENAME = "gpu_lease_request.json"
STATUS_FILENAME = "gpu_lease_status.json"
PROFILE_FILENAME = "llama-profile.json"


def request_path(db_path: str) -> Path:
    return Path(db_path).parent / REQUEST_FILENAME


def status_path(db_path: str) -> Path:
    return Path(db_path).parent / STATUS_FILENAME


def profile_path(db_path: str) -> Path:
    return Path(db_path).parent / PROFILE_FILENAME


def renew_after(ttl: int) -> int:
    """When the holder should POST again — a third of the window, so a missed
    renewal has two more chances before the lease expires."""
    return max(ttl // 3, 60)


def parse_holder(value: Any, default: str = "") -> str:
    """A service name from a payload field, or `ValueError("invalid_holder")`."""
    if value is None:
        return default
    if not isinstance(value, str) or not HOLDER_PATTERN.match(value.strip()):
        raise ValueError("invalid_holder")
    return value.strip()


def parse_payload(body: Any) -> tuple[str, int, str]:
    """`(model, ttl, holder)` from a `{model, ttl_s, holder}` body, or
    `ValueError(<reason>)`. `holder` defaults to the profile name, which is
    what a caller that never names itself has always been filed under.

    The key set is closed: an unknown field is refused rather than ignored, so
    a session/round/guild identifier cannot quietly appear later.
    """
    if not isinstance(body, dict):
        raise ValueError("invalid_payload")
    if set(body) - set(PAYLOAD_KEYS):
        raise ValueError("unexpected_field")
    model = body.get("model")
    if not isinstance(model, str) or model.strip() not in MODELS:
        raise ValueError("invalid_model")
    raw_ttl = body.get("ttl_s", TTL_DEFAULT_SECONDS)
    if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int) or raw_ttl <= 0:
        raise ValueError("invalid_ttl")
    return (
        model.strip(),
        min(max(raw_ttl, TTL_MIN_SECONDS), TTL_MAX_SECONDS),
        parse_holder(body.get("holder"), model.strip()),
    )


def parse_release(body: Any) -> str:
    """The holder a `DELETE` names, or `""` for the bodyless operator path."""
    if body is None:
        return ""
    if not isinstance(body, dict):
        raise ValueError("invalid_payload")
    if set(body) - {"holder"}:
        raise ValueError("unexpected_field")
    return parse_holder(body.get("holder"))


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_request(db_path: str) -> dict:
    return _read(request_path(db_path))


def read_status(db_path: str) -> dict:
    return _read(status_path(db_path))


def release_pending(db_path: str) -> bool:
    """True while a `release` request stands that the broker has not run yet.

    Newer *than the lease file* is the whole test: a release that has been
    executed took the lease file with it, and a lease file written after the
    request belongs to a new window rather than to the one being given back.
    """
    try:
        newer = (
            request_path(db_path).stat().st_mtime
            > gpu_lease.lease_path(db_path).stat().st_mtime
        )
    except OSError:
        return False
    return newer and read_request(db_path).get("op") == "release"


def household_alias(db_path: str) -> str:
    """The alias llama-server answers with when no lease is held.

    Taken from the profile the llama post-deploy records beside the lease, so
    an operator who deployed other weights is reported as those weights.
    """
    alias = _read(profile_path(db_path)).get("alias")
    return (
        alias.strip() if isinstance(alias, str) and alias.strip() else HOUSEHOLD_ALIAS
    )


def write_request(
    db_path: str,
    op: str,
    model: str = "",
    ttl_s: int = 0,
    *,
    holder: str = "",
    now: float | None = None,
) -> None:
    """Ask the host broker for a lease change. The write itself is the signal —
    `solaris-gpu-lease-broker.path` watches this file."""
    path = request_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "op": op,
                "model": model,
                "ttl_s": ttl_s,
                # Who the window belongs to (#1347): the service that named
                # itself, else the profile name — the holder `gpu-lease.py
                # acquire foundry` has always written.
                "holder": holder or model,
                "requested_at": time.time() if now is None else now,
            }
        ),
        "utf-8",
    )


def _number(value: Any) -> float | None:
    """A clock or interval the box wrote, or `None` when it wrote none — a
    lease taken before #1361 carries neither field."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value > 0 else None


def state(db_path: str) -> dict:
    """`{state, model, alias, expires_at, holder, last_renewed_at,
    renew_after}` — what `GET` answers.

    `alias` is always the model llama-server has loaded *now*, which is what
    the `/v1` responses carry: the leased one only once the swap is through,
    the household one before and after.

    `last_renewed_at`/`renew_after` are the heartbeat (#1361): the window ends
    a grace of `2 x renew_after` after the last renewal, or at `expires_at`,
    whichever comes first.
    """
    path = gpu_lease.lease_path(db_path)
    idle = {
        "state": "none",
        "model": "",
        "alias": household_alias(db_path),
        "expires_at": None,
        "holder": "",
        "last_renewed_at": None,
        "renew_after": None,
    }
    if gpu_lease.is_leased(path):
        lease = gpu_lease.record(path)
        mode = lease.get("mode")
        if mode not in MODELS:
            return idle
        held_by = str(lease.get("holder") or mode)
        beat = {
            "last_renewed_at": _number(lease.get("last_renewed_at")),
            "renew_after": _number(lease.get("renew_after")),
        }
        alias = (
            str(lease.get("alias") or ALIASES[mode])
            if lease.get("ready")
            else household_alias(db_path)
        )
        if release_pending(db_path):
            # The window is on its way out: no deadline to plan against, and
            # `none` is what says it is really gone.
            return {
                "state": "releasing",
                "model": mode,
                "alias": alias,
                "expires_at": None,
                "holder": held_by,
                **beat,
            }
        if not lease.get("ready"):
            return {
                "state": "preparing",
                "model": mode,
                "alias": alias,
                "expires_at": None,
                "holder": held_by,
                **beat,
            }
        until = lease.get("until")
        return {
            "state": "ready",
            "model": mode,
            "alias": alias,
            "expires_at": until if isinstance(until, (int, float)) else None,
            "holder": held_by,
            **beat,
        }
    request = read_request(db_path)
    handled = read_status(db_path).get("requested_at")
    if (
        request.get("op") == "acquire"
        and request.get("model") in MODELS
        and request.get("requested_at") != handled
    ):
        ttl = _number(request.get("ttl_s"))
        return {
            "state": "preparing",
            "model": request["model"],
            "alias": household_alias(db_path),
            "expires_at": None,
            "holder": str(request.get("holder") or request["model"]),
            "last_renewed_at": _number(request.get("requested_at")),
            "renew_after": renew_after(int(ttl)) if ttl else None,
        }
    return idle
