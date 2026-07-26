"""Resident-registration tools — the onboarding flow's voice-enrol + file step.

Live-voice enrolment uses the reverse enroll-stash (#376): the engine can't pass
PCM (it only ever sees text), so instead of shipping base64 samples it opens an
`enroll_requests` row for the candidate uid and the gatekeeper — while it is HA's
Wyoming STT provider — captures the speaker's audio across the next few onboarding
turns, enrols the voice in-process, and writes the result back.

Two tools drive the dialog:

  * `start_voice_enrollment(uid)` opens the request, then the dialog prompts the
    speaker to say their name N times (one utterance = one captured turn).
  * `register_pending_resident(uid, display_name)` reads the result and, only on
    a successful enrol, files a `pending_residents` row (#376) for the admin
    step (#355). A timeout (speaker-ID off, so no gatekeeper picked the request
    up) or a `failed` result is surfaced honestly — no pending row, no false
    success — and the dialog reports it instead of hanging.

Biometric care: the raw audio never reaches the engine or any log line — only the
uid, display name and the gatekeeper's status surface. These are onboarding-only
tools, not part of the household or general guest toolset (see profiles.py).
"""

from __future__ import annotations

import json
import re
from typing import Any

from solaris_chat import enroll_requests_store, pending_residents_store
from solaris_chat.engine.tools import Tool

# Same uid shape the gatekeeper's /enrol enforces — validate before opening the
# request so a malformed uid is a clear local error.
_UID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TARGET_SAMPLES = 3

# Prompt-only SOUL steering is too high-variance on the small household model
# (gemma4:e4b ignores "drei Sätze" and falls back to its "sage deinen Namen"
# prior — #404). So the tool hands the model the exact line to echo: it speaks
# this verbatim instead of inventing the next prompt from a weak instruction.
_COLLECT_PROMPT = (
    "Alles klar! Sag mir jetzt bitte nacheinander drei ganz normale Sätze oder Befehle, "
    "wie du sonst auch mit mir sprichst — zum Beispiel „Schalte das Licht im Wohnzimmer an“ "
    "oder „Stell einen Timer auf zehn Minuten“. Der Inhalt ist egal, es zählt nur der Klang deiner Stimme. "
    "Was ist dein erster Satz?"
)


def build_register_tools(
    db_path: str, gatekeeper_url: str = "", gatekeeper_token: str = ""
) -> list[Tool]:
    async def start(args: dict[str, Any]) -> str:
        uid = str(args.get("uid") or "").strip()
        if not _UID_RE.match(uid):
            return json.dumps({"ok": False, "reason": "invalid_uid"})
        try:
            enroll_requests_store.open_request(db_path, uid, _TARGET_SAMPLES)
        except Exception:  # noqa: BLE001 — table/DB missing surfaces as not-ok
            return json.dumps({"ok": False, "reason": "enroll_store_unavailable"})
        return json.dumps(
            {
                "ok": True,
                "uid": uid,
                "collecting": True,
                "samples_needed": _TARGET_SAMPLES,
                "say": _COLLECT_PROMPT,
            },
            ensure_ascii=False,
        )

    async def register(args: dict[str, Any]) -> str:
        uid = str(args.get("uid") or "").strip()
        display_name = str(args.get("display_name") or "").strip()
        if not _UID_RE.match(uid):
            return json.dumps({"ok": False, "reason": "invalid_uid"})
        if not display_name:
            return json.dumps({"ok": False, "reason": "missing_display_name"})

        req = enroll_requests_store.read_request(db_path, uid)
        if req is None:
            return json.dumps({"ok": False, "reason": "no_enroll_request"})
        if req["timed_out"]:
            # No gatekeeper ever picked the request up — speaker-ID is off, so
            # voice onboarding can't enrol. Honest failure, not a hang.
            enroll_requests_store.clear_request(db_path, uid)
            return json.dumps({"ok": False, "reason": "speaker_id_disabled"})
        if req["status"] == enroll_requests_store.STATUS_FAILED:
            # The gatekeeper could not extract an embedding (silent/short audio,
            # ECAPA error) — a real failure, not "collect more". Surface it and
            # drop the stale row so the uid can be re-enrolled, not blocked.
            enroll_requests_store.clear_request(db_path, uid)
            return json.dumps({"ok": False, "reason": "enroll_failed"})
        if req["status"] != enroll_requests_store.STATUS_DONE:
            collected = req.get("collected", 1)
            needed = req.get("target_samples", 3)
            rem = needed - collected
            if rem == 2:
                say = "Danke! Was ist dein zweiter Satz?"
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
        return json.dumps(
            {"ok": True, "uid": uid, "request_id": request_id, "status": "pending"},
            ensure_ascii=False,
        )

    return [
        Tool(
            name="start_voice_enrollment",
            description=(
                "Startet das Sprach-Enrollment, wenn sich jemand einrichten will"
                " ('richte mich ein', 'merk dir meine Stimme'). Vorher: kurz"
                " Einverständnis zur Stimmaufnahme einholen (biometrisch) und nach"
                " dem Kürzel oder Vornamen fragen (z.B. 'mdopp', 'michael' oder 'carola') — "
                "uid selbst ableiten (kleinbuchstaben, ASCII: 'mdopp' ⇒ 'mdopp', 'Michael' ⇒ 'michael'). "
                "Gibt 'say' zurück: sprich GENAU diese Zeile — bitte NIE den Namen zu wiederholen. "
                "Jede folgende Äußerung ist eine Probe; nach drei Äußerungen register_pending_resident rufen."
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
                "Schließt die Registrierung ab, NACHDEM start_voice_enrollment mit"
                " collecting=true geantwortet hat UND die Person drei Sätze gesagt"
                " hat. Ruf es NIE vorher. Übergib dieselbe uid und"
                " den Anzeigenamen. Prüft das Enrollment-Ergebnis und legt nur bei"
                " Erfolg eine Freigabe-Anfrage an — es entsteht KEIN Konto und kein"
                " Bewohner-Zugang, bis ein Admin freigibt (auch beim ersten Bewohner)."
                " Bei 'enroll_incomplete' noch eine Äußerung sammeln und erneut rufen;"
                " bei 'speaker_id_disabled' oder Fehler nichts vortäuschen, keine"
                " Anfrage."
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
