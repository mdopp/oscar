"""Chat tools for the Aufgaben (to-do) surface (#todo, plan slice 1).

`task_add` / `task_list` / `task_done` let the resident (or Solaris, when it
decides something is actionable) put items on and work through the one shared task
list. Backed by `engine.tasks` (projection-only `task` entities); owner-scoped via
the passed `uid_getter`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from solaris_chat.engine import tasks
from solaris_chat.engine.document_deadlines_sync import cascade_task_event_configured
from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.tools import Tool, Visibility


def build_tasks_tools(db_path: str, uid_getter, *, notes_dir: str) -> list[Tool]:
    def _caller() -> str:
        return uid_getter() or projection.SHARED_UID

    async def task_add(args: dict[str, Any]) -> str:
        title = str(args.get("title") or "").strip()
        if not title:
            return json.dumps({"error": "title fehlt"})
        due = str(args.get("due") or "").strip()
        uid = _caller()
        tid = await asyncio.to_thread(
            tasks.create_task,
            db_path=db_path,
            notes_dir=notes_dir,
            uid=uid,
            title=title,
            due=due,
            task_source="chat",
        )
        await cascade_task_event_configured(db_path, tid)
        return json.dumps(
            {"ok": True, "id": tid, "title": title, "due": due}, ensure_ascii=False
        )

    async def task_list(args: dict[str, Any]) -> str:
        include_done = bool(args.get("include_done"))
        items = await asyncio.to_thread(
            tasks.list_tasks, db_path, _caller(), include_done=include_done
        )
        return json.dumps({"tasks": items}, ensure_ascii=False)

    async def task_done(args: dict[str, Any]) -> str:
        uid = _caller()
        status = "dismissed" if args.get("dismiss") else "done"
        tid = str(args.get("id") or "").strip()
        if not tid:
            # Resolve by title among the open tasks (the model knows them from
            # task_list); an ambiguous match returns the candidates to disambiguate.
            title = str(args.get("title") or "").strip().lower()
            if not title:
                return json.dumps({"error": "id oder title nötig"})
            open_tasks = await asyncio.to_thread(tasks.list_tasks, db_path, uid)
            hits = [t for t in open_tasks if title in t["title"].lower()]
            if not hits:
                return json.dumps(
                    {"error": "keine offene Aufgabe passt", "title": title}
                )
            if len(hits) > 1:
                return json.dumps(
                    {"error": "mehrdeutig", "candidates": [t["title"] for t in hits]},
                    ensure_ascii=False,
                )
            tid = hits[0]["id"]
        ok = await asyncio.to_thread(
            tasks.set_status, db_path=db_path, uid=uid, entity_id=tid, status=status
        )
        if ok:
            await cascade_task_event_configured(db_path, tid)
        return json.dumps({"ok": ok, "status": status})

    return [
        Tool(
            name="task_add",
            description=(
                "Setzt eine Aufgabe auf die To-Do-Liste. Rufe das SOFORT auf, sobald"
                " der Nutzer etwas Zu-Erledigendes nennt — 'wir müssen X', 'ich muss"
                " X', 'denk an X', 'X besorgen/kaufen', 'morgen X'. NICHT vorher um"
                " Erlaubnis fragen (eine Aufgabe ist leicht wieder zu löschen);"
                " einfach eintragen und danach kurz bestätigen ('Notiert: X ✓')."
                " Nennt der Nutzer einen Tag ('morgen', '1. August'), gib due als"
                " ISO-Datum YYYY-MM-DD mit (wird dann auch ein Kalendereintrag)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "due": {
                        "type": "string",
                        "description": "ISO YYYY-MM-DD, optional",
                    },
                },
                "required": ["title"],
            },
            handler=task_add,
            visibility=Visibility.HOUSEHOLD,
        ),
        Tool(
            name="task_list",
            description=(
                "Listet die offenen Aufgaben (To-Do). Nutze DIESES Tool — nicht"
                " notes_search — für 'was müssen wir tun/erledigen', 'was steht an',"
                " 'unsere To-Dos', 'haben wir was notiert/für morgen'. Aufgaben"
                " stehen hier, nicht in den Notizen. include_done=true zeigt auch"
                " erledigte/verworfene."
            ),
            parameters={
                "type": "object",
                "properties": {"include_done": {"type": "boolean"}},
            },
            handler=task_list,
            visibility=Visibility.HOUSEHOLD,
        ),
        Tool(
            name="task_done",
            description=(
                "Hakt eine Aufgabe ab (erledigt). Per title (aus task_list) oder id."
                " dismiss=true verwirft sie stattdessen (kein Interesse)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "id": {"type": "string"},
                    "dismiss": {"type": "boolean"},
                },
            },
            handler=task_done,
            visibility=Visibility.HOUSEHOLD,
        ),
    ]
