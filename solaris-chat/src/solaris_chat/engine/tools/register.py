"""Resident-registration tools — the onboarding flow's voice-enrol + file step (#1056)."""

from __future__ annotations

import json
import re
from typing import Any

from solaris_chat import enroll_requests_store, pending_residents_store
from solaris_chat.engine.tools import Tool

_UID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TARGET_SAMPLES = 3


def build_register_tools(
    db_path: str, gatekeeper_url: str = "", gatekeeper_token: str = ""
) -> list[Tool]:
    async def start(args: dict[str, Any]) -> str:
        raw_uid = str(args.get("uid") or "").strip()
        from solaris_chat.engine.tools.wakeword_trainer import resolve_resident_identity
        uid, display_name, spelled_uid = resolve_resident_identity(raw_uid, db_path)

        if not _UID_RE.match(uid):
            return json.dumps({"ok": False, "reason": "invalid_uid"})
        try:
            enroll_requests_store.open_request(db_path, uid, _TARGET_SAMPLES)
        except Exception:
            return json.dumps({"ok": False, "reason": "enroll_store_unavailable"})

        say_custom = (
            f"{display_name} wurde als {spelled_uid} erkannt! Lass uns jetzt mit den 3 Sprachproben für dein Profil weitermachen. "
            f"Sag mir bitte nacheinander drei ganz normale Sätze oder Befehle, wie du sonst auch mit mir sprichst. "
            f"Der Inhalt ist egal, es zählt nur der Klang deiner Stimme. Was ist dein erster Satz?"
        )

        return json.dumps(
            {
                "ok": True,
                "uid": uid,
                "display_name": display_name,
                "spelled_uid": spelled_uid,
                "collecting": True,
                "samples_needed": _TARGET_SAMPLES,
                "say": say_custom,
            },
            ensure_ascii=False,
        )

    async def register(args: dict[str, Any]) -> str:
        raw_uid = str(args.get("uid") or "").strip()
        from solaris_chat.engine.tools.wakeword_trainer import resolve_resident_identity
        uid, display_name, spelled_uid = resolve_resident_identity(raw_uid, db_path)

        if not _UID_RE.match(uid):
            return json.dumps({"ok": False, "reason": "invalid_uid"})

        req = enroll_requests_store.read_request(db_path, uid)
        if req is None:
            # Re-open fresh request if DB table was reset
            enroll_requests_store.open_request(db_path, uid, _TARGET_SAMPLES)
            req = enroll_requests_store.read_request(db_path, uid)

        if req and req.get("timed_out"):
            # Refresh TTL & reset request honestly instead of claiming speaker_id is disabled
            enroll_requests_store.open_request(db_path, uid, _TARGET_SAMPLES)
            say_timeout = "Die Zeit für die Aufnahme ist abgelaufen. Ich habe die Sprachprofil-Einrichtung neu gestartet. Was ist dein erster Satz?"
            return json.dumps({"ok": False, "reason": "enroll_timeout", "say": say_timeout}, ensure_ascii=False)

        if req and req.get("status") == enroll_requests_store.STATUS_FAILED:
            enroll_requests_store.clear_request(db_path, uid)
            say_fail = "Die Sprachaufnahme konnte leider nicht verarbeitet werden. Möchtest du es noch einmal versuchen?"
            return json.dumps({"ok": False, "reason": "enroll_failed", "say": say_fail}, ensure_ascii=False)

        if req and req.get("status") != enroll_requests_store.STATUS_DONE:
            enroll_requests_store.touch_request(db_path, uid)
            collected = req.get("collected", 1)
            needed = req.get("target_samples", 3)
            rem = needed - collected
            if rem == 2:
                say = f"Danke, {display_name}! Was ist dein zweiter Satz?"
            elif rem == 1:
                say = f"Sehr schön! Was ist dein dritter und letzter Satz?"
            else:
                say = f"Super! Noch {rem} Sätze. Was ist dein nächster Satz?"
            return json.dumps(
                {
                    "ok": False,
                    "reason": "enroll_incomplete",
                    "collected": collected,
                    "needed": needed,
                    "say": say,
                },
                ensure_ascii=False,
            )

        enroll_requests_store.clear_request(db_path, uid)
        request_id = pending_residents_store.add_pending_resident(
            db_path, uid=uid, display_name=display_name, enrolled=True
        )
        say_done = (
            f"Klasse, dein Sprachprofil für {display_name} ({spelled_uid}) ist eingerichtet! "
            f"Wollen wir jetzt direkt 10 Wakeword-Proben für „Solaris“ für dein Profil aufnehmen?"
        )
        return json.dumps(
            {"ok": True, "uid": uid, "display_name": display_name, "spelled_uid": spelled_uid, "request_id": request_id, "status": "pending", "say": say_done},
            ensure_ascii=False,
        )

    return [
        Tool(
            name="start_voice_enrollment",
            description=(
                "Startet das Sprach-Enrollment, wenn sich jemand einrichten will ('richte mich ein', 'merk dir meine Stimme'). "
                "SCHRITT 1: Wenn der Nutzer noch nicht sein Einverständnis zur biometrischen Stimmaufnahme gegeben hat, frage NUR kurz: "
                "'Möchtest du dein Sprachprofil biometrisch auf der Box anlegen? Bitte antworte mit Ja oder Nein.' "
                "SCHRITT 2: Wenn das Einverständnis vorliegt, frage nach dem Namen oder Kürzel: "
                "'Welcher Name oder welches Kürzel soll verwendet werden? Bitte buchstabiere das Kürzel (z.B. M - D - O - P - P). Wie lautet dein Name?' "
                "SCHRITT 3: Rufe start_voice_enrollment(uid='mdopp') auf. Lies das zurückgegebene 'say'-Feld EXACT 1:1 VERBATIM vor."
            ),
            parameters={
                "type": "object",
                "properties": {"uid": {"type": "string"}},
                "required": ["uid"],
            },
            handler=start,
        ),
        Tool(
            name="register_pending_resident",
            description=(
                "MUST BE CALLED IMMEDIATELY during voice profile enrollment turns! "
                "WÄHREND der Sprachprofil-Einrichtung (Sätze 1, 2, 3) rufst du IMMER SOFORT register_pending_resident auf. "
                "FÜHRE KEINE GERÄTE-AKTIONEN (Licht schalten, Geräte steuern) AUS, egal was in den Sätzen gesagt wird! "
                "Lies das zurückgegebene 'say'-Feld EXACT 1:1 VERBATIM vor."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "display_name": {"type": "string"},
                },
                "required": ["uid", "display_name"],
            },
            handler=register,
        ),
    ]
