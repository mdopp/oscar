"""Deterministic Finite State Machine (FSM) for Voice Enrollment (#1056).

Eliminates LLM non-determinism, hallucinations, and latency during voice onboarding.
Runs 100% deterministically in Python with 0 LLM calls and <1ms latency per turn.
"""

from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Any

from solaris_chat import (
    enroll_requests_store,
    pending_residents_store,
)
from solaris_chat.enroll_requests_store import VOICE_PROFILE_SAMPLES
from solaris_chat.engine.tools.wakeword_trainer import resolve_resident_identity

STATE_IDLE = "idle"
STATE_CONSENT = "consent"
STATE_NAME = "name"
STATE_RECORDING = "recording"

# Extra sentences the wizard may ask for when the gatekeeper rejected samples
# as too quiet, so a bad mic can't loop the dialog forever.
_MAX_EXTRA_SAMPLE_TURNS = 3

_YES_WORDS = ("ja", "yes", "ok", "einverstanden", "klar", "gerne", "sicher", "absolut")
_NO_WORDS = ("nein", "no", "nicht")

# The row a turn that carries no dialog handle falls back to (#1184).
SHARED_SESSION_KEY = "default"


def session_key(conversation_id: str | None) -> str:
    """The enrolment dialog this turn belongs to.

    Keyed on Home Assistant's per-conversation id — the only per-turn handle the
    voice bridge has, since speaker-ID is off on this install and a uid is
    therefore `household` for everyone. Two residents on two satellites are two
    conversations, so one cannot answer the other's consent question.

    A turn that arrives WITHOUT a conversation id is unidentifiable: it falls
    back to the shared row, i.e. the pre-#1184 single-dialog behaviour. That is
    deliberate — an unkeyed turn given its own row would open a wizard nothing
    could ever reach again.
    """
    return (conversation_id or "").strip() or SHARED_SESSION_KEY


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS enrollment_fsm (
                session_key TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'idle',
                uid TEXT,
                display_name TEXT,
                spelled_uid TEXT,
                target_samples INTEGER DEFAULT 3,
                collected INTEGER DEFAULT 0,
                misses INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # `misses` counted turns the gatekeeper failed to capture during the
        # wake-word dialog, which is gone (#1081). The column is kept — dropping
        # it needs a migration and it costs nothing — and no path writes it.
        # A box that ran an earlier build already has the table, and CREATE
        # TABLE IF NOT EXISTS never adds a column to it.
        try:
            conn.execute(
                "ALTER TABLE enrollment_fsm ADD COLUMN misses INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()


def get_fsm_state(
    db_path: str, session_key: str = SHARED_SESSION_KEY
) -> dict[str, Any] | None:
    if not Path(db_path).exists():
        return None
    try:
        init_db(db_path)
        with _connect(db_path) as conn:
            row = conn.execute(
                """SELECT *,
                           CAST((julianday('now') - julianday(updated_at)) * 86400 AS INTEGER) as age_s
                      FROM enrollment_fsm
                     WHERE session_key = ? AND state != 'idle'""",
                (session_key,),
            ).fetchone()
            if row:
                data = dict(row)
                # Auto-expiration (#1056): If FSM state is older than 180s (3 minutes), auto-reset it
                if data.get("age_s") is not None and data["age_s"] > 180:
                    conn.execute(
                        "DELETE FROM enrollment_fsm WHERE session_key = ?",
                        (session_key,),
                    )
                    conn.commit()
                    return None
                return data
            return None
    except Exception:
        return None


def is_active(db_path: str, session_key: str = SHARED_SESSION_KEY) -> bool:
    """True when THIS dialog is inside the wizard.

    Scoped to the caller's dialog (#1184). It used to also return True whenever
    ANY uid had an open enrol request, which routed every other resident's voice
    turn into the enrolling resident's FSM — their speech answered its consent
    step, and they got no normal service meanwhile. The wizard opens that
    request itself, at its name step, so its own row is the authority.
    """
    return get_fsm_state(db_path, session_key) is not None


def reset_fsm(db_path: str, session_key: str = SHARED_SESSION_KEY) -> None:
    if not Path(db_path).exists():
        return
    try:
        init_db(db_path)
        with _connect(db_path) as conn:
            conn.execute(
                "DELETE FROM enrollment_fsm WHERE session_key = ?", (session_key,)
            )
            conn.commit()
    except Exception:
        pass


def handle_turn(
    db_path: str, text: str, uid_hint: str = "", session_key: str = SHARED_SESSION_KEY
) -> str:
    """Process one turn through the deterministic enrollment FSM. Returns spoken response text."""
    init_db(db_path)
    clean_text = text.strip().lower()
    state_data = get_fsm_state(db_path, session_key)
    state = state_data["state"] if state_data else STATE_IDLE

    # Cancel / Abort trigger
    if any(
        w in clean_text
        for w in ("abbrechen", "stopp", "stop", "abbruch", "nein danke", "keine lust")
    ):
        uid = (state_data or {}).get("uid") or uid_hint or "user1"
        reset_fsm(db_path, session_key)
        enroll_requests_store.clear_request(db_path, uid)
        return "Die Einrichtung des Sprachprofils wurde abgebrochen. Kann ich dir sonst noch helfen?"

    # Trigger phrase detection: if user requests setup/new enrollment, reset state to start fresh (#1056)
    trigger_phrases = (
        "richte",
        "einrichten",
        "anlegen",
        "neuer benutzer",
        "neues profil",
        "neu starten",
        "neu anfangen",
    )
    if state != STATE_IDLE and any(p in clean_text for p in trigger_phrases):
        reset_fsm(db_path, session_key)
        enroll_requests_store.clear_request(db_path, uid_hint or "user1")
        state = STATE_IDLE

    # STATE 0: Trigger turn ("Richte meinen Benutzer ein", "Sprachprofil anlegen")
    if state == STATE_IDLE:
        with _connect(db_path) as conn:
            conn.execute(
                """INSERT INTO enrollment_fsm (session_key, state, updated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(session_key) DO UPDATE SET state = ?, updated_at = datetime('now')""",
                (session_key, STATE_CONSENT, STATE_CONSENT),
            )
            conn.commit()
        return "Möchtest du dein Sprachprofil biometrisch auf der Box anlegen? Bitte antworte mit Ja oder Nein?"

    # STATE 1: Consent step
    if state == STATE_CONSENT:
        if any(w in clean_text for w in _YES_WORDS):
            with _connect(db_path) as conn:
                conn.execute(
                    "UPDATE enrollment_fsm SET state = ?, updated_at = datetime('now') WHERE session_key = ?",
                    (STATE_NAME, session_key),
                )
                conn.commit()
            return "Danke für deine Zustimmung! Wie lautet dein Name oder Kürzel? Bitte buchstabiere das Kürzel?"
        elif any(w in clean_text for w in _NO_WORDS):
            # Refusing consent must also close the enrol request. While one is
            # open the gatekeeper keeps capturing the speaker's PCM, so leaving
            # it behind would enrol a voice profile the resident just declined.
            reset_fsm(db_path, session_key)
            enroll_requests_store.clear_request(
                db_path, (state_data or {}).get("uid") or uid_hint or "user1"
            )
            return "Alles klar, die biometrische Sprachprofil-Einrichtung wurde abgelehnt. Kann ich dir bei etwas anderem helfen?"
        else:
            return "Bitte antworte mit Ja oder Nein: Möchtest du dein Sprachprofil biometrisch anlegen?"

    # STATE 2: Name / UID step
    if state == STATE_NAME:
        raw_name = text.strip()
        uid, display_name, spelled_uid = resolve_resident_identity(raw_name, db_path)
        target_samples = VOICE_PROFILE_SAMPLES

        enroll_requests_store.open_request(db_path, uid, target_samples)
        with _connect(db_path) as conn:
            conn.execute(
                """UPDATE enrollment_fsm
                   SET state = ?, uid = ?, display_name = ?, spelled_uid = ?,
                       target_samples = ?, collected = 0, updated_at = datetime('now')
                   WHERE session_key = ?""",
                (
                    STATE_RECORDING,
                    uid,
                    display_name,
                    spelled_uid,
                    target_samples,
                    session_key,
                ),
            )
            conn.commit()

        return (
            f"{display_name} wurde als {spelled_uid} erkannt! Lass uns jetzt mit den "
            f"{VOICE_PROFILE_SAMPLES} Sprachproben für dein Profil weitermachen. "
            f"Sag mir bitte nacheinander {VOICE_PROFILE_SAMPLES} ganz normale Sätze "
            f"oder Befehle. Was ist dein erster Satz?"
        )

    # STATE 3: Sample recording (Sätze 1, 2, 3)
    if state == STATE_RECORDING:
        uid = state_data.get("uid") or "user1"
        display_name = state_data.get("display_name") or "Alex"
        spelled_uid = state_data.get("spelled_uid") or "A - L - E - X"
        target = state_data.get("target_samples") or VOICE_PROFILE_SAMPLES

        # Increment sample count deterministically on each turn
        current_collected = state_data.get("collected", 0) + 1
        rem = target - current_collected

        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE enrollment_fsm SET collected = ?, updated_at = datetime('now') WHERE session_key = ?",
                (current_collected, session_key),
            )
            conn.commit()

        # Update enroll_requests table
        with _connect(db_path) as conn_req:
            conn_req.execute(
                "UPDATE enroll_requests SET collected = ? WHERE uid = ?",
                (current_collected, uid),
            )
            conn_req.commit()

        if rem == 1:
            return f"Sehr schön, {display_name}! Was ist dein letzter Satz?"
        elif rem <= 0:
            # The gatekeeper owns the embedding, so its terminal status — not
            # this turn counter — decides whether a profile actually exists.
            request = enroll_requests_store.read_request(db_path, uid)
            status = request["status"] if request else None

            if status == enroll_requests_store.STATUS_DONE:
                enroll_requests_store.clear_request(db_path, uid)
                pending_residents_store.add_pending_resident(
                    db_path, uid=uid, display_name=display_name, enrolled=True
                )
                reset_fsm(db_path, session_key)
                # The Voice PE detects "Solaris" on-device and consumes it as an
                # activation, so the wake word can never arrive here as content
                # (#1081) — point at the browser recorder, and end without a
                # question mark so the microphone closes.
                return (
                    f"Klasse, dein Sprachprofil für {display_name} ({spelled_uid}) wurde erfolgreich gespeichert! "
                    f"Für das Wakeword „Solaris“ nimm die Aufnahmen bitte in der Solaris-App oder im Browser auf."
                )

            # Still capturing: samples were rejected as too quiet or unclear, so
            # ask for another sentence instead of ending on a false success.
            if (
                request is not None
                and status == enroll_requests_store.STATUS_CAPTURING
                and not request["timed_out"]
                and current_collected < target + _MAX_EXTRA_SAMPLE_TURNS
            ):
                return (
                    f"Diese Aufnahme war leider noch nicht deutlich genug, {display_name}. "
                    f"Sag mir bitte noch einen Satz?"
                )

            reset_fsm(db_path, session_key)
            enroll_requests_store.clear_request(db_path, uid)
            return (
                f"Ich konnte dein Sprachprofil leider nicht speichern, {display_name}. "
                f"Die Stimmerkennung ist auf dieser Box gerade nicht aktiv. "
                f"Kann ich dir sonst helfen?"
            )
        else:
            return (
                f"Danke, {display_name}! Noch {rem} Sätze. Was ist dein nächster Satz?"
            )

    return "Wie kann ich dir helfen?"
