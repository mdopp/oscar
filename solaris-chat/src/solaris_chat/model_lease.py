"""The neighbour-service model lease (#1260, contract mdopp/foundry-chronicle#299).

foundry-chronicle shares this box and holds `gemma4:12b` for hours; that model
and the household's `gemma4:e4b` do not fit beside the voice stack (measured:
voice 4508 MiB, e4b 3.26 GB, 12b 8.09 GB of 15.6 GiB usable). Insisting on the
fast model during their evening costs a ~56 s model swap **each way, per turn**,
so for the duration of a declared lease Solaris answers with the model they
already hold.

The negotiated contract, in one place:

* they `POST` `{model, ttl_s}` over the loopback and `DELETE` at the end —
  no token, no shared secret, and the payload carries **no** session, round or
  guild identifier (`PAYLOAD_KEYS` and its test pin that shut).
* `LEASE_TTL_SECONDS` is the safety net and `RENEW_INTERVAL_SECONDS` is derived
  from it, so the two cadences cannot drift apart: a crashed evening lets the
  lease expire instead of pinning the household on the big model forever.
* Everything here fails **open**: no lease, an unreadable lease or an expired
  lease all read as "no lease", i.e. normal operation on `FAST_MODEL`.

The lease is persisted beside `solaris.db` — deliberately ours alone, not a
file shared with them: surviving a `solaris-chat` restart is our problem by
agreement, not a coupling of two containers to one path.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

# 15 minutes. The renewal cadence is derived, never a second literal — a
# renewal interval that drifts past the TTL rebuilds exactly the model-swap
# thrash the contract exists to prevent (foundry-chronicle's own catch).
LEASE_TTL_SECONDS = 900
RENEW_INTERVAL_SECONDS = LEASE_TTL_SECONDS // 3

# The complete payload. Adding a key here is a contract change on both sides.
PAYLOAD_KEYS = ("model", "ttl_s")

# How often the expiry watch looks; small against the 15-minute TTL so the
# re-warm starts promptly after a chronicle run dies without its DELETE.
WATCH_INTERVAL_SECONDS = 30.0


def lease_path(db_path: str) -> Path:
    return Path(db_path).parent / "model_lease.json"


def parse_payload(body: Any) -> tuple[str, int]:
    """`(model, ttl)` from a `{model, ttl_s}` body, or `ValueError(<reason>)`.

    The key set is closed: an unknown field is refused rather than ignored, so
    a session/round/guild identifier cannot quietly appear later. The TTL is
    capped at `LEASE_TTL_SECONDS` — the safety net is ours to enforce.
    """
    if not isinstance(body, dict):
        raise ValueError("invalid_payload")
    if set(body) - set(PAYLOAD_KEYS):
        raise ValueError("unexpected_field")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("invalid_model")
    raw_ttl = body.get("ttl_s", LEASE_TTL_SECONDS)
    if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int) or raw_ttl <= 0:
        raise ValueError("invalid_ttl")
    return model.strip(), min(raw_ttl, LEASE_TTL_SECONDS)


def grant(db_path: str, model: str, ttl: int, *, now: float | None = None) -> float:
    """Persist the lease; returns its absolute expiry (epoch seconds)."""
    expires_at = (time.time() if now is None else now) + ttl
    path = lease_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": model, "expires_at": expires_at}), "utf-8")
    return expires_at


def clear(db_path: str) -> None:
    lease_path(db_path).unlink(missing_ok=True)


def _read(db_path: str) -> tuple[str, float]:
    """`(model, expires_at)` as stored, or `("", 0.0)` for anything unreadable."""
    try:
        data = json.loads(lease_path(db_path).read_text("utf-8"))
    except (OSError, ValueError):
        return "", 0.0
    if not isinstance(data, dict):
        return "", 0.0
    model = data.get("model")
    expires_at = data.get("expires_at")
    if not isinstance(model, str) or not model.strip():
        return "", 0.0
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return "", 0.0
    return model.strip(), float(expires_at)


def active_model(db_path: str, *, now: float | None = None) -> str:
    """The leased model while the lease is live, else `""` (fail-open)."""
    model, expires_at = _read(db_path)
    return model if model and expires_at > (time.time() if now is None else now) else ""


def expired(db_path: str, *, now: float | None = None) -> bool:
    """True only for a readable lease whose TTL has run out.

    A DELETE removes the file, so this is the window-*end-without-a-DELETE*
    signal — which is what the re-warm hangs off, and why an explicit DELETE
    and an expiry never both warm.
    """
    model, expires_at = _read(db_path)
    return bool(model) and expires_at <= (time.time() if now is None else now)


async def expiry_watch(
    db_path: str,
    on_expire: Callable[[], Awaitable[None]],
    *,
    interval: float = WATCH_INTERVAL_SECONDS,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Clear an expired lease and re-warm, without waiting for the next turn.

    A crashed chronicle run never sends its DELETE; without this the household
    would keep answering on the big model until someone asked something, and
    then pay the cold-load on that first turn.
    """
    if sleep is None:
        import asyncio

        sleep = asyncio.sleep
    while True:
        if expired(db_path):
            clear(db_path)
            await on_expire()
        await sleep(interval)
