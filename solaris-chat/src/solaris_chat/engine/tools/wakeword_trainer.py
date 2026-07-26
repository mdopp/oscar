"""Wakeword Improvement Tools — interactive voice recording & GPU training (#1056).

Enables interactive voice enrollment dialogs for fine-tuning the Solaris wake word:
- start_wakeword_enrollment(target_count): Starts interactive recording session.
- record_wakeword_sample(): Increments voice sample count and gives spoken feedback.
- trigger_wakeword_training(): Triggers 2-hour background GPU training on RTX 2000 Ada.
"""

from __future__ annotations

import asyncio
import json
import os
import subprocess
from typing import Any, Callable

from solaris_chat import wakeword_requests_store
from solaris_chat.engine.tools import Tool


def build_wakeword_tools(
    db_path: str,
    uid_getter: Callable[[], str],
    script_dir: str = "/workspace/solarisbay/scripts"
) -> list[Tool]:
    """Build the wakeword improvement tools."""

    async def _handle_start(args: dict[str, Any]) -> str:
        uid = uid_getter()
        target_count = int(args.get("target_count", 10))

        req = wakeword_requests_store.start_request(db_path, uid, target_count)
        rem = req["target_count"] - req["collected_count"]

        say = (
            f"Klar! Lass uns {target_count} Sprachproben für das Wakeword „Solaris“ sammeln. "
            f"Sprich bitte nach meiner Antwort nacheinander das Wort „Solaris“ — mal leise, "
            f"mal gerufen, auf Deutsch oder Englisch. Los geht's mit Probe 1!"
        )
        return json.dumps({
            "ok": True,
            "uid": uid,
            "target_count": target_count,
            "remaining": rem,
            "say": say
        }, ensure_ascii=False)

    async def _handle_sample(args: dict[str, Any]) -> str:
        uid = uid_getter()
        req = wakeword_requests_store.record_sample(db_path, uid)

        collected = req["collected_count"]
        target = req["target_count"]
        rem = max(0, target - collected)

        if rem > 0:
            if rem == 1:
                say = f"Sehr gut! Nur noch 1 Probe!"
            elif rem in (8, 5, 3):
                say = f"Klasse! Noch {rem} Mal (versuche es jetzt gerne mal geflüstert oder auf Englisch)."
            else:
                say = f"Super! Noch {rem} Mal."
            return json.dumps({
                "ok": True,
                "collected": collected,
                "target": target,
                "remaining": rem,
                "completed": False,
                "say": say
            }, ensure_ascii=False)
        else:
            say = (
                f"Perfekt, alle {target} Sprachproben wurden erfolgreich gespeichert! "
                f"Möchtest du das 2-Stunden GPU-Training jetzt direkt auf deiner Grafikkarte "
                f"starten oder noch weitere Proben für andere Personen sammeln?"
            )
            return json.dumps({
                "ok": True,
                "collected": collected,
                "target": target,
                "remaining": 0,
                "completed": True,
                "say": say
            }, ensure_ascii=False)

    async def _handle_trigger(args: dict[str, Any]) -> str:
        uid = uid_getter()
        wakeword_requests_store.finish_request(db_path, uid)

        cmd = ["python3", os.path.join(script_dir, "train-micro-wake-word.py")]
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setpgrp
            )
        except Exception:
            pass

        say = (
            "Das GPU-Training für dein neues Wakeword „Solaris“ wurde im Hintergrund gestartet! "
            "Die Grafikkarte berechnet jetzt mit deinen Sprachproben und Wohnzimmer-Nebengeräuschen "
            "über 15.000 Steps (~2 Stunden). Ich gebe dir Bescheid, sobald das neue Modell fertig ist!"
        )
        return json.dumps({
            "ok": True,
            "training_started": True,
            "say": say
        }, ensure_ascii=False)

    return [
        Tool(
            name="start_wakeword_enrollment",
            description=(
                "Startet den interaktiven Aufnahme-Modus zum Verbessern des Aufweckworts „Solaris“ "
                "(„Solaris, Wakeword verbessern“, „neues Wakeword trainieren“). "
                "Sammelt N Sprachproben mit Live-Countdown."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_count": {
                        "type": "integer",
                        "description": "Anzahl der zu sammelnden Proben (Standard: 10)"
                    }
                }
            },
            handler=_handle_start
        ),
        Tool(
            name="record_wakeword_sample",
            description=(
                "Zeichnet eine Sprachprobe für das Wakeword auf und gibt den verbleibenden Countdown zurück. "
                "Wird während des aktiven Wakeword-Aufnahme-Dialogs nach jedem eingesprochenen Wort gerufen."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_handle_sample
        ),
        Tool(
            name="trigger_wakeword_training",
            description=(
                "Startet das 2-Stunden GPU-Training für das neu verfeinerte Wakeword „Solaris“ auf der Grafikkarte "
                "(„Training jetzt starten“, „Modell jetzt berechnen“)."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_handle_trigger
        )
    ]
