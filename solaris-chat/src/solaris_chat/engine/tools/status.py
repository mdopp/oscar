"""The household profile's own status probe (#1310).

Measured on the box against `gemma4:e4b` with the real household prompt and 36
tools: `/notes wohnzimmer` hit `notes_search` 3 of 3, a status question hit
nothing 3 of 3 — the model answered it by calling `ha_get_state` on sensor
entities it had invented, 32 of them in parallel in one run. Same prompt, same
tool list, same call shape; the only difference is whether a fitting tool
exists. The household toolbox held no health tool at all, so `status/SKILL.md`
was instructing ServiceBay's `get_health_checks`/`diagnose`, which only the
admin profile carries.

This is the household half, and it is deliberately not the admin one. It reaches
no ServiceBay API, takes no arguments at all, and reports one boolean per part a
resident would notice going quiet: whether Solaris still remembers (solaris.db),
whether it can reach the house (Home Assistant) and whether voice still answers
(the gatekeeper's own `/health`). No service or container names, no addresses, no
versions, no logs, and nothing that restarts, deploys or mutates — the narrowness
is in code, not in the prompt (the register.py lesson).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import aiohttp

from solaris_chat import db_health
from solaris_chat.engine.tools import Tool, Visibility

_TIMEOUT = aiohttp.ClientTimeout(total=3)

# The answer is read out loud, so the parts are named the way a resident names
# them (design guideline 3) — never by service or container name.
LABEL_STORE = "Gedächtnis"
LABEL_HOME = "Haussteuerung"
LABEL_VOICE = "Sprachsteuerung"


async def _answers(url: str, headers: dict[str, str] | None = None) -> bool:
    try:
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT) as session,
            session.get(url, headers=headers or {}) as response,
        ):
            return response.status < 400
    except Exception:
        return False


def build_status_tools(
    *,
    db_path: str = "",
    hass_url: str = "",
    hass_token: str = "",
    gatekeeper_url: str = "",
) -> list[Tool]:
    """A single no-argument read of the parts a resident asks about.

    Each part registers only where it is configured, so an install without HA or
    without the voice bridge reports on what it actually has instead of claiming
    a part is down."""
    checks: list[tuple[str, Callable[[], Awaitable[bool]]]] = []

    if db_path:

        async def store() -> bool:
            # probe() returns a reason string carrying the database path; only
            # the verdict ever leaves this module.
            return db_health.probe(db_path) is None

        checks.append((LABEL_STORE, store))

    if hass_url and hass_token:

        async def home() -> bool:
            return await _answers(
                hass_url.rstrip("/") + "/api/",
                {"Authorization": f"Bearer {hass_token}"},
            )

        checks.append((LABEL_HOME, home))

    if gatekeeper_url:

        async def voice() -> bool:
            return await _answers(gatekeeper_url.rstrip("/") + "/health")

        checks.append((LABEL_VOICE, voice))

    if not checks:
        return []

    async def get_solaris_status(_arguments: dict) -> str:
        verdicts = await asyncio.gather(*(probe() for _label, probe in checks))
        parts = [
            {"teil": label, "ok": bool(ok)}
            for (label, _probe), ok in zip(checks, verdicts, strict=True)
        ]
        return json.dumps(
            {"alles_ok": all(p["ok"] for p in parts), "teile": parts},
            ensure_ascii=False,
        )

    return [
        Tool(
            name="get_solaris_status",
            description=(
                "Sagt, wie es Solaris selbst geht: Gedächtnis, Haussteuerung,"
                " Sprachsteuerung. Rufe es auf, sobald jemand nach deinem"
                " Befinden, deiner Erreichbarkeit oder deinem Zustand fragt —"
                " „wie geht es dir“, „alles gut bei dir“, „bist du da“,"
                " „läuft alles“, „hast du Probleme“. Ohne Parameter, ändert"
                " nichts. NICHT für einzelne Geräte — dafür ha_get_state."
            ),
            parameters={"type": "object", "properties": {}},
            handler=get_solaris_status,
            visibility=Visibility.HOUSEHOLD,
        )
    ]
