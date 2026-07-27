"""Resident-registration tools — the onboarding flow's voice-enrol + file step (#1056)."""

from __future__ import annotations

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
        # If the user only gave consent or generic setup without naming/spelling a user, ask for the name (#1056)
        generic_inputs = {
            "",
            "benutzer",
            "user",
            "profil",
            "einrichten",
            "meinen benutzer",
            "mein benutzer",
            "ja",
            "ja.",
            "yes",
            "ok",
            "einverstanden",
            "zustimmung",
        }
        if not raw_uid or raw_uid.lower() in generic_inputs:
            say_ask = "Danke für deine Zustimmung! Wie lautet dein Name oder welches Kürzel möchtest du nutzen? Bitte buchstabiere das Kürzel?"
            return json.dumps(
                {"ok": False, "reason": "missing_uid", "say": say_ask},
                ensure_ascii=False,
            )
        # Reject junk before the resolver normalises it away: "Bad UID!" would
        # otherwise be coerced to the perfectly valid uid "baduid".
        if re.search(r"[^a-z0-9 ._-]", raw_uid, re.I):
            return json.dumps({"ok": False, "reason": "invalid_uid"})
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
        dn_arg = args.get("display_name")
        from solaris_chat.engine.tools.wakeword_trainer import resolve_resident_identity

        uid, resolved_display, spelled_uid = resolve_resident_identity(raw_uid, db_path)

        if not _UID_RE.match(uid):
            return json.dumps({"ok": False, "reason": "invalid_uid"})
        # An explicitly-passed but blank display_name is a caller error; an
        # omitted one falls back to the resolved name.
        if dn_arg is not None and not str(dn_arg).strip():
            return json.dumps({"ok": False, "reason": "missing_display_name"})
        display_name = str(dn_arg).strip() if dn_arg else resolved_display

        req = enroll_requests_store.read_request(db_path, uid)
        if req is None:
            # Nothing was ever opened — an honest failure, not a silent re-open
            # (a re-open would make the dialog look like it is still capturing).
            return json.dumps({"ok": False, "reason": "no_enroll_request"})

        if req.get("timed_out"):
            # No gatekeeper ever picked the request up — speaker-ID is off.
            enroll_requests_store.clear_request(db_path, uid)
            return json.dumps({"ok": False, "reason": "speaker_id_disabled"})

        if req.get("status") == enroll_requests_store.STATUS_FAILED:
            # A real gatekeeper failure, not "collect more". Drop the stale row
            # so the uid can be re-enrolled instead of being blocked.
            enroll_requests_store.clear_request(db_path, uid)
            return json.dumps({"ok": False, "reason": "enroll_failed"})

        if req.get("status") != enroll_requests_store.STATUS_DONE:
            enroll_requests_store.touch_request(db_path, uid)
            collected = req.get("collected", 1)
            needed = req.get("target_samples", 3)
            rem = needed - collected
            if rem == 2:
                say = f"Danke, {display_name}! Was ist dein zweiter Satz?"
            elif rem == 1:
                say = "Sehr schön! Was ist dein dritter und letzter Satz?"
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
            {
                "ok": True,
                "uid": uid,
                "display_name": display_name,
                "spelled_uid": spelled_uid,
                "request_id": request_id,
                "status": "pending",
                "say": say_done,
            },
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
                "'Welcher Name oder welches Kürzel soll verwendet werden? Bitte buchstabiere das Kürzel (z.B. M - A - X). Wie lautet dein Name?' "
                "SCHRITT 2: Sobald der Nutzer den Namen oder das Kürzel genannt hat, rufe start_voice_enrollment(uid=genannter_name) auf. Lies das zurückgegebene 'say'-Feld EXACT 1:1 VERBATIM vor."
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
