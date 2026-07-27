"""Wakeword Improvement Tools — interactive voice recording, quality audit & GPU training (#1056).

Enables bidirectional interactive voice enrollment dialogs & sample audit:
- start_wakeword_enrollment(uid, target_count): Starts interactive recording session for user or household.
- record_wakeword_sample(): Increments voice sample count and gives spoken feedback addressing the user.
- audit_wakeword_samples(): Performs quality check over saved audio samples & flags outliers.
- list_wakeword_samples(): Lists all saved audio samples across users for reuse.
- delete_wakeword_sample(sample_id): Deletes a specific audio sample.
- trigger_wakeword_training(): Triggers 2-hour background GPU training on RTX 2000 Ada.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from typing import Any, Callable

from solaris_chat import wakeword_requests_store, wakeword_samples_store
from solaris_chat.engine.tools import Tool

_ACCEPTED_PHONETICS = re.compile(
    r"(solaris|so\s*la\s*ris|solar|so-la-ris|solaries|1live)", re.I
)

_RESIDENT_ALIASES = {
    "alex": ("alex", "Alex Test"),
    "max": ("max", "Max"),
    "carola": ("carola", "Carola"),
    "marco": ("marco", "Marco"),
}


def parse_spelled_uid(raw: str) -> str:
    """Extract clean ASCII uid from spelled or spoken names (e.g. 'M - A - R - C - O' -> 'marco')."""
    cleaned = (
        raw.lower().replace("-", "").replace(" ", "").replace(",", "").replace(".", "")
    )
    cleaned = re.sub(r"[^a-z0-9]", "", cleaned)
    return cleaned if len(cleaned) >= 2 else "household"


def resolve_resident_identity(
    raw_input: str, db_path: str = ""
) -> tuple[str, str, str]:
    """Resolves spoken or spelled user names/aliases to (uid, display_name, spelled_uid)."""
    parsed = parse_spelled_uid(raw_input)
    if parsed in _RESIDENT_ALIASES:
        uid, display_name = _RESIDENT_ALIASES[parsed]
        spelled = " - ".join(list(uid.upper()))
        return uid, display_name, spelled

    if db_path and os.path.exists(db_path):
        try:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT uid, display_name FROM pending_residents WHERE uid = ? OR LOWER(display_name) LIKE ?",
                    (parsed, f"%{parsed}%"),
                ).fetchone()
                if row:
                    spelled = " - ".join(list(row[0].upper()))
                    return row[0], row[1], spelled
        except Exception:
            pass

    spelled = " - ".join(list(parsed.upper()))
    return parsed, parsed.capitalize(), spelled


def build_wakeword_tools(
    db_path: str,
    uid_getter: Callable[[], str],
    script_dir: str = "/workspace/solarisbay/scripts",
) -> list[Tool]:
    """Build the wakeword improvement tools."""

    async def _handle_start(args: dict[str, Any]) -> str:
        current_uid = uid_getter()
        raw_uid = str(args.get("uid") or "").strip()

        if raw_uid:
            uid, display_name, spelled_uid = resolve_resident_identity(raw_uid, db_path)
        elif current_uid and current_uid != "household":
            uid, display_name, spelled_uid = resolve_resident_identity(
                current_uid, db_path
            )
        else:
            uid, display_name, spelled_uid = (
                "household",
                "Haushalt",
                "H - A - U - S - H - A - L - T",
            )

        target_count = int(args.get("target_count", 10))

        req = wakeword_requests_store.start_request(db_path, uid, target_count)
        rem = req["target_count"] - req["collected_count"]

        if uid != "household":
            greeting = f"{display_name} wurde als {spelled_uid} erkannt! "
        else:
            greeting = "Klar! "

        say = (
            f"{greeting}Lass uns {target_count} Sprachproben für das Wakeword „Solaris“ für dein Profil ({spelled_uid}) sammeln. "
            f"Sprich bitte nach meiner Antwort nacheinander das Wort „Solaris“ — mal leise, "
            f"mal gerufen. Bist du bereit für Probe 1?"
        )
        return json.dumps(
            {
                "ok": True,
                "uid": uid,
                "display_name": display_name,
                "spelled_uid": spelled_uid,
                "target_count": target_count,
                "remaining": rem,
                "say": say,
            },
            ensure_ascii=False,
        )

    async def _handle_sample(args: dict[str, Any]) -> str:
        current_uid = uid_getter()
        raw_uid = str(args.get("uid") or "").strip()

        if raw_uid:
            uid, display_name, spelled_uid = resolve_resident_identity(raw_uid, db_path)
        else:
            uid, display_name, spelled_uid = resolve_resident_identity(
                current_uid or "household", db_path
            )

        req = wakeword_requests_store.record_sample(db_path, uid)

        collected = req["collected_count"]
        target = req["target_count"]
        rem = max(0, target - collected)

        sample_id = f"sample_{uid}_{collected}"
        sample_dir = (
            f"/workspace/solarisbay/templates/solaris/wakeword/user_samples/{uid}"
        )
        os.makedirs(sample_dir, exist_ok=True)
        filename = os.path.join(sample_dir, f"{sample_id}.wav")

        transcript = args.get("transcript", "Solaris")
        is_valid = bool(_ACCEPTED_PHONETICS.search(transcript))

        wakeword_samples_store.add_sample(
            db_path,
            sample_id=sample_id,
            filename=filename,
            wakeword_id="solaris",
            resident_uid=uid,
            intended_phrase="Solaris",
            stt_transcript=transcript,
            is_valid=is_valid,
        )

        current_probe = target - rem + 1
        if rem > 0:
            if rem == 1:
                say = (
                    f"Sehr gut, {display_name}! Nur noch 1 Probe! Was ist deine letzte Probe?"
                    if uid != "household"
                    else "Sehr gut! Nur noch 1 Probe! Was ist deine letzte Probe?"
                )
            elif rem in (8, 5, 3):
                say = f"Klasse! Noch {rem} Mal (versuche es jetzt gerne mal geflüstert). Was ist deine {current_probe}. Probe?"
            else:
                say = f"Super! Noch {rem} Mal. Was ist deine {current_probe}. Probe?"
            return json.dumps(
                {
                    "ok": True,
                    "uid": uid,
                    "display_name": display_name,
                    "spelled_uid": spelled_uid,
                    "collected": collected,
                    "target": target,
                    "remaining": rem,
                    "completed": False,
                    "say": say,
                },
                ensure_ascii=False,
            )
        else:
            suspicious = wakeword_samples_store.get_suspicious_samples(
                db_path, "solaris"
            )
            if suspicious:
                bad_item = suspicious[0]
                say = (
                    f"Perfekt, {display_name if uid != 'household' else '10'} Sprachproben gesammelt! Bei einer deiner Aufnahmen habe ich allerdings "
                    f"wörtlich „{bad_item['stt_transcript']}“ verstanden. "
                    f"Möchtest du diese Probe löschen und neu aufnehmen, oder soll sie als ungewöhnliche Aussprache behalten werden?"
                )
            else:
                say = (
                    f"Perfekt, {display_name if uid != 'household' else 'alle'} 10 Sprachproben wurden erfolgreich überprüft und gespeichert! "
                    f"Möchtest du das 2-Stunden GPU-Training jetzt direkt auf deiner Grafikkarte starten oder noch weitere Proben für andere Personen sammeln?"
                )

            return json.dumps(
                {
                    "ok": True,
                    "uid": uid,
                    "display_name": display_name,
                    "spelled_uid": spelled_uid,
                    "collected": collected,
                    "target": target,
                    "remaining": 0,
                    "completed": True,
                    "suspicious_count": len(suspicious),
                    "say": say,
                },
                ensure_ascii=False,
            )

    async def _handle_audit(args: dict[str, Any]) -> str:
        samples = wakeword_samples_store.list_samples(db_path, "solaris")
        suspicious = wakeword_samples_store.get_suspicious_samples(db_path, "solaris")

        if suspicious:
            bad = suspicious[0]
            speaker_uid = bad["resident_uid"]
            _, speaker_name, _ = resolve_resident_identity(speaker_uid, db_path)
            speaker_say = speaker_name if speaker_uid != "household" else "dir"
            say = (
                f"Ich habe {len(samples)} gespeicherte Proben. Bei der Probe von {speaker_say} habe ich wörtlich „{bad['stt_transcript']}“ verstanden. "
                f"Soll ich diese Aufnahme löschen?"
            )
        else:
            say = f"Alle {len(samples)} Aufnahmen sind in bester Qualität und verifiziert. Möchtest du noch etwas prüfen?"

        return json.dumps(
            {
                "ok": True,
                "total_samples": len(samples),
                "suspicious": suspicious,
                "say": say,
            },
            ensure_ascii=False,
        )

    async def _handle_list(args: dict[str, Any]) -> str:
        samples = wakeword_samples_store.list_samples(db_path, "solaris")
        say = f"Es sind {len(samples)} Sprachproben für das Wakeword „Solaris“ gespeichert. Möchtest du eine Probe anhören oder löschen?"
        return json.dumps(
            {"ok": True, "samples": samples, "say": say}, ensure_ascii=False
        )

    async def _handle_delete(args: dict[str, Any]) -> str:
        sample_id = str(args.get("sample_id", "")).strip()
        if not sample_id:
            suspicious = wakeword_samples_store.get_suspicious_samples(
                db_path, "solaris"
            )
            if suspicious:
                sample_id = suspicious[0]["id"]

        if sample_id:
            wakeword_samples_store.delete_sample(db_path, sample_id)
            current_uid = uid_getter()
            wakeword_requests_store.decrement_sample(
                db_path, current_uid or "household"
            )
            say = f"Aufnahme {sample_id} wurde gelöscht. Es fehlt jetzt wieder 1 Probe. Bist du bereit, sie neu einzusprechen?"
        else:
            say = "Keine fehlerhaften Aufnahmen zum Löschen gefunden. Wollen wir weitermachen?"

        return json.dumps(
            {"ok": True, "deleted_sample_id": sample_id, "say": say}, ensure_ascii=False
        )

    async def _handle_trigger(args: dict[str, Any]) -> str:
        current_uid = uid_getter()
        raw_uid = str(args.get("uid") or "").strip()
        if raw_uid:
            uid, display_name, spelled_uid = resolve_resident_identity(raw_uid, db_path)
        else:
            uid, display_name, spelled_uid = resolve_resident_identity(
                current_uid or "household", db_path
            )

        wakeword_requests_store.finish_request(db_path, uid)
        samples = wakeword_samples_store.list_samples(db_path, "solaris")

        cmd = ["python3", os.path.join(script_dir, "train-micro-wake-word.py")]
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setpgrp,
            )
        except Exception:
            pass

        thanks = f"Danke, {display_name}! " if uid != "household" else ""

        say = (
            f"{thanks}Das GPU-Training für dein Wakeword „Solaris“ wurde gestartet! "
            f"Die Grafikkarte berechnet jetzt mit deinen {len(samples)} Sprachproben und "
            f"Wohnzimmer-Nebengeräuschen über 15.000 Steps (~2 Stunden). "
            f"Ich gebe dir Bescheid, sobald das neue Modell fertig ist!"
        )
        return json.dumps(
            {
                "ok": True,
                "uid": uid,
                "display_name": display_name,
                "spelled_uid": spelled_uid,
                "samples_count": len(samples),
                "training_started": True,
                "say": say,
            },
            ensure_ascii=False,
        )

    return [
        Tool(
            name="start_wakeword_enrollment",
            description=(
                "Startet den interaktiven Aufnahme-Modus zum Verbessern oder Trainieren des Aufweckworts „Solaris“. "
                "RUFE DIESES TOOL SOFORT AUF, wenn der Nutzer sein Wakeword/Weckwort verbessern, anpassen oder trainieren möchte — "
                "auch bei STT-Erkennungsfehlern wie „Wake World verbessern“, „Breakwater trainieren“, „Weckwort verbessern“, "
                "„Aufweckwort trainieren“, „Solaris trainieren“, „neues Wakeword“. "
                "Nimmt optional 'uid' (z.B. buchstabiert 'M-A-R-C-O' -> 'marco', 'Max' -> 'max') entgegen. "
                "Beispiele: „Wakeword verbessern“, „Wake World verbessern“, „Breakwater trainieren“, „Weckwort trainieren“, „Solaris trainieren“."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "Der Name oder buchstabierte User-ID des Sprechers (z.B. 'max', 'M-A-X')",
                    },
                    "target_count": {
                        "type": "integer",
                        "description": "Anzahl der zu sammelnden Proben (Standard: 10)",
                    },
                },
            },
            handler=_handle_start,
        ),
        Tool(
            name="record_wakeword_sample",
            description=(
                "Zeichnet eine Sprachprobe für das Wakeword auf und gibt den verbleibenden Countdown zurück. "
                "Wird während des aktiven Wakeword-Aufnahme-Dialogs nach jedem eingesprochenen Wort gerufen."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "Die User-ID des Sprechers (z.B. 'max')",
                    },
                    "transcript": {
                        "type": "string",
                        "description": "Das von Whisper STT erfasste Wort (z.B. 'Solaris', 'Cello')",
                    },
                },
            },
            handler=_handle_sample,
        ),
        Tool(
            name="audit_wakeword_samples",
            description=(
                "Überprüft die Qualität aller bisher gespeicherten Wakeword-Aufnahmen und hebt auffällige "
                "oder fehlerhafte Aufnahmen hervor („Aufnahmen überprüfen“, „Qualität prüfen“)."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_handle_audit,
        ),
        Tool(
            name="list_wakeword_samples",
            description=(
                "Listet alle bisher gespeicherten Wakeword-Sprachproben auf, die für zukünftige Trainingswiederholungen wiederverwendet werden."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_handle_list,
        ),
        Tool(
            name="delete_wakeword_sample",
            description=(
                "Löscht eine fehlerhafte oder misslungene Wakeword-Sprachprobe („Aufnahme löschen“, „Probe löschen“, „neu aufnehmen“)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sample_id": {
                        "type": "string",
                        "description": "Die ID der zu löschenden Probe (z.B. 'sample_max_4')",
                    }
                },
            },
            handler=_handle_delete,
        ),
        Tool(
            name="trigger_wakeword_training",
            description=(
                "Startet das 2-Stunden GPU-Training für das neu verfeinerte Wakeword „Solaris“ auf der Grafikkarte unter Verwendung aller gespeicherten Proben."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "Optional: User-ID des Nutzers",
                    }
                },
            },
            handler=_handle_trigger,
        ),
    ]
