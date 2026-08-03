"""Timer/alarm/reminder tools backed by the engine scheduler."""

from __future__ import annotations

import json
from typing import Any

from solaris_chat.engine import scheduler
from solaris_chat.engine.tools import Tool, Visibility


def build_timer_tools(db_path: str, uid_getter) -> list[Tool]:
    """`uid_getter()` supplies the current turn's resident uid — tools are
    built per profile, but a timer belongs to whoever asked for it."""

    async def timer_set(args: dict[str, Any]) -> str:
        timer = scheduler.add_timer(
            db_path,
            uid_getter(),
            duration_s=args.get("duration_s"),
            fire_at=args.get("at"),
            kind=str(args.get("kind") or "timer"),
            label=str(args.get("label") or ""),
        )
        return json.dumps(timer, ensure_ascii=False)

    async def timer_list(args: dict[str, Any]) -> str:
        return json.dumps(
            scheduler.list_timers(db_path, uid_getter()), ensure_ascii=False
        )

    async def timer_cancel(args: dict[str, Any]) -> str:
        ok = scheduler.cancel_timer(db_path, uid_getter(), str(args.get("id") or ""))
        return json.dumps({"cancelled": ok})

    return [
        Tool(
            name="timer_set",
            description=(
                "Stellt Timer, Wecker oder Erinnerung. Entweder duration_s"
                " (Timer ab jetzt) oder at (ISO-Zeitpunkt, Wecker/Erinnerung)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "duration_s": {"type": "integer"},
                    "at": {"type": "string", "description": "ISO 8601, lokale Zeit"},
                    "kind": {"type": "string", "enum": ["timer", "alarm", "reminder"]},
                    "label": {"type": "string"},
                },
            },
            handler=timer_set,
            visibility=Visibility.HOUSEHOLD,
        ),
        Tool(
            name="timer_list",
            description="Listet die laufenden Timer/Wecker/Erinnerungen.",
            parameters={"type": "object", "properties": {}},
            handler=timer_list,
            visibility=Visibility.HOUSEHOLD,
        ),
        Tool(
            name="timer_cancel",
            description="Bricht einen Timer per id ab.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            handler=timer_cancel,
            visibility=Visibility.HOUSEHOLD,
        ),
    ]
