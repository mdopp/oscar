"""Deterministic Finite State Machine (FSM) for Voice Enrollment (#1056).

Eliminates LLM non-determinism, hallucinations, and latency during voice onboarding.
Runs 100% deterministically in Python with 0 LLM calls and <1ms latency per turn.
"""

from __future__ import annotations
import json, re, sqlite3
from pathlib import Path
from typing import Any

from solaris_chat import enroll_requests_store, pending_residents_store
from solaris_chat.engine.tools.wakeword_trainer import resolve_resident_identity

STATE_IDLE = "idle"
STATE_CONSENT = "consent"
STATE_NAME = "name"
STATE_RECORDING = "recording"

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
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()

def get_fsm_state(db_path: str, session_key: str = "default") -> dict[str, Any] | None:
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
                (session_key,)
            ).fetchone()
            if row:
                data = dict(row)
                # Auto-expiration (#1056): If FSM state is older than 180s (3 minutes), auto-reset it
                if data.get("age_s") is not None and data["age_s"] > 180:
                    conn.execute("DELETE FROM enrollment_fsm WHERE session_key = ?", (session_key,))
                    conn.commit()
                    return None
                return data
            return None
    except Exception:
        return None

def is_active(db_path: str, session_key: str = "default") -> bool:
    state = get_fsm_state(db_path, session_key)
    if state:
        return True
    return enroll_requests_store.has_any_active_request(db_path)

def reset_fsm(db_path: str, session_key: str = "default") -> None:
    if not Path(db_path).exists():
        return
    try:
        init_db(db_path)
        with _connect(db_path) as conn:
            conn.execute("DELETE FROM enrollment_fsm WHERE session_key = ?", (session_key,))
            conn.commit()
    except Exception:
        pass

def handle_turn(db_path: str, text: str, uid_hint: str = "", session_key: str = "default") -> str:
    """Process one turn through the deterministic enrollment FSM. Returns spoken response text."""
    init_db(db_path)
    clean_text = text.strip().lower()

    # Cancel / Abort trigger
    if any(w in clean_text for w in ("abbrechen", "stopp", "stop", "abbruch", "nein danke", "keine lust")):
        reset_fsm(db_path, session_key)
        enroll_requests_store.clear_request(db_path, uid_hint or "user1")
        return "Die Einrichtung des Sprachprofils wurde abgebrochen. Kann ich dir sonst noch helfen?"

    state_data = get_fsm_state(db_path, session_key)
    state = state_data["state"] if state_data else STATE_IDLE

    # Trigger phrase detection: if user requests setup/new enrollment, reset state to start fresh (#1056)
    trigger_phrases = ("richte", "einrichten", "anlegen", "neuer benutzer", "neues profil", "neu starten", "neu anfangen")
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
                (session_key, STATE_CONSENT, STATE_CONSENT)
            )
            conn.commit()
        return "Möchtest du dein Sprachprofil biometrisch auf der Box anlegen? Bitte antworte mit Ja oder Nein?"

    # STATE 1: Consent step
    if state == STATE_CONSENT:
        if any(w in clean_text for w in ("ja", "yes", "ok", "einverstanden", "klar", "gerne", "sicher")):
            with _connect(db_path) as conn:
                conn.execute(
                    "UPDATE enrollment_fsm SET state = ?, updated_at = datetime('now') WHERE session_key = ?",
                    (STATE_NAME, session_key)
                )
                conn.commit()
            return "Danke für deine Zustimmung! Wie lautet dein Name oder Kürzel? Bitte buchstabiere das Kürzel?"
        elif any(w in clean_text for w in ("nein", "no", "nicht")):
            reset_fsm(db_path, session_key)
            return "Alles klar, die biometrische Sprachprofil-Einrichtung wurde abgelehnt. Kann ich dir bei etwas anderem helfen?"
        else:
            return "Bitte antworte mit Ja oder Nein: Möchtest du dein Sprachprofil biometrisch anlegen?"

    # STATE 2: Name / UID step
    if state == STATE_NAME:
        raw_name = text.strip()
        uid, display_name, spelled_uid = resolve_resident_identity(raw_name, db_path)
        target_samples = 3

        enroll_requests_store.open_request(db_path, uid, target_samples)
        with _connect(db_path) as conn:
            conn.execute(
                """UPDATE enrollment_fsm
                   SET state = ?, uid = ?, display_name = ?, spelled_uid = ?,
                       target_samples = ?, collected = 0, updated_at = datetime('now')
                   WHERE session_key = ?""",
                (STATE_RECORDING, uid, display_name, spelled_uid, target_samples, session_key)
            )
            conn.commit()

        return (
            f"{display_name} wurde als {spelled_uid} erkannt! Lass uns jetzt mit den 3 Sprachproben für dein Profil weitermachen. "
            f"Sag mir bitte nacheinander drei ganz normale Sätze oder Befehle. Was ist dein erster Satz?"
        )

    # STATE 3: Sample recording (Sätze 1, 2, 3)
    if state == STATE_RECORDING:
        uid = state_data.get("uid") or "user1"
        display_name = state_data.get("display_name") or "Alex"
        spelled_uid = state_data.get("spelled_uid") or "A - L - E - X"
        target = state_data.get("target_samples") or 3

        # Increment sample count deterministically on each turn
        current_collected = state_data.get("collected", 0) + 1
        rem = target - current_collected

        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE enrollment_fsm SET collected = ?, updated_at = datetime('now') WHERE session_key = ?",
                (current_collected, session_key)
            )
            conn.commit()

        # Update enroll_requests table
        with _connect(db_path) as conn_req:
            conn_req.execute(
                "UPDATE enroll_requests SET collected = ? WHERE uid = ?",
                (current_collected, uid)
            )
            conn_req.commit()

        if rem == 2:
            return f"Danke, {display_name}! Was ist dein zweiter Satz?"
        elif rem == 1:
            return f"Sehr schön! Was ist dein dritter und letzter Satz?"
        elif rem <= 0:
            reset_fsm(db_path, session_key)
            enroll_requests_store.clear_request(db_path, uid)
            pending_residents_store.add_pending_resident(db_path, uid=uid, display_name=display_name, enrolled=True)
            return (
                f"Klasse, dein Sprachprofil für {display_name} ({spelled_uid}) wurde erfolgreich gespeichert! "
                f"Die Einrichtung ist damit abgeschlossen."
            )
        else:
            return f"Super! Noch {rem} Sätze. Was ist dein nächster Satz?"

    return "Wie kann ich dir helfen?"
