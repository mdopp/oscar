"""Resident-registration tools — the onboarding flow's voice-enrol + file step (#1056)."""

from __future__ import annotations

import json
import re
from typing import Any

from solaris_chat import enroll_requests_store, pending_residents_store
from solaris_chat.engine.tools import Tool, Visibility

_UID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def build_register_tools(
    db_path: str, gatekeeper_url: str = "", gatekeeper_token: str = ""
) -> list[Tool]:
    async def start(args: dict[str, Any]) -> str:
        """Hand the resident to the deterministic wizard and read out its line.

        This used to run its own consent + name logic and open the enrol request
        itself. That put TWO state machines on one dialog: opening the request
        is exactly what activates the wizard, the wizard then started over at
        its consent step, and every sentence the resident spoke afterwards was
        read as a yes/no answer — the dialog could never reach the samples.
        Observed on the box as an endless "Bitte antworte mit Ja oder Nein".

        The wizard owns consent -> name -> samples, and opens the request at the
        name step. The `uid` argument is deliberately ignored: the wizard asks
        for the name itself, so the model cannot invent one (#1067).
        """
        from solaris_chat.engine import enrollment_fsm

        try:
            enrollment_fsm.reset_fsm(db_path)
            say = enrollment_fsm.handle_turn(db_path, "einrichten")
        except Exception:
            return json.dumps(
                {
                    "ok": False,
                    "reason": "enroll_store_unavailable",
                    "say": "Ich kann die Einrichtung gerade nicht starten. Versuchen wir es später nochmal?",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"ok": True, "collecting": True, "say": say}, ensure_ascii=False
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
            needed = req.get(
                "target_samples", enroll_requests_store.VOICE_PROFILE_SAMPLES
            )
            rem = needed - collected
            if rem == 1:
                say = f"Sehr schön, {display_name}! Was ist dein letzter Satz?"
            else:
                say = f"Danke, {display_name}! Noch {rem} Sätze. Was ist dein nächster Satz?"
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
        # No trailing "?" — the Voice PE keeps the microphone open on a question,
        # and there is nothing left to answer: the wake word cannot be recorded
        # over a wake-word-gated channel at all (#1081).
        say_done = (
            f"Klasse, dein Sprachprofil für {display_name} ({spelled_uid}) ist eingerichtet! "
            f"Für das Wakeword „Solaris“ nimm die Aufnahmen bitte in der Solaris-App oder im Browser auf."
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
                "Rufe es SOFORT auf, ohne vorher nach Einverständnis oder Namen zu fragen — beides erfragt der Assistent danach selbst. "
                "Lies das zurückgegebene 'say'-Feld EXAKT 1:1 vor und ergänze nichts."
            ),
            parameters={
                "type": "object",
                "properties": {},
            },
            handler=start,
            visibility=Visibility.HOUSEHOLD,
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
            visibility=Visibility.HOUSEHOLD,
        ),
    ]
