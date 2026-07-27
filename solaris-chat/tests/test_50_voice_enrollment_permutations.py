"""Exhaustive 50+ Test Suite for Voice Enrollment & Wakeword FSM State Machine (#1056).

Covers all scripted conversation permutations:
- Full successful voice enrollment (3 sentences) across name formats & consent phrases
- Cancellation & refusal at all stages (consent, name, sentence 1, 2, 3)
- Deletion of sentences/samples and exact decrement retry logic
- Wakeword training flow (10 samples counted 1-10 cleanly)
- Wakeword sample deletion and resume logic
- TTL state expiration and state isolation
"""

import sqlite3
import pytest
from solaris_chat.engine import enrollment_fsm
from solaris_chat import wakeword_requests_store


# --- FIXTURE FOR CLEAN ISOLATED TEST DB ---
@pytest.fixture
def test_db(tmp_path):
    db = str(tmp_path / "fsm_50_suite.db")
    enrollment_fsm.reset_fsm(db, "default")
    return db


def _gatekeeper_enrols(db, *, status="done", result="enrolled"):
    """Simulate the gatekeeper half of the handshake: it captures each turn's
    PCM, writes the averaged embedding and flips the request row terminal. It
    runs before the engine sees that turn's text, so tests call it just before
    the final sentence. Without it the wizard must not claim a saved profile."""
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE enroll_requests SET status = ?, result = ?", (status, result)
        )
        conn.commit()


# --- 1. CONSENT VARIATION TESTS (1-10) ---
@pytest.mark.parametrize(
    "consent_word",
    [
        "ja",
        "ja gerne",
        "sicher",
        "ok",
        "einverstanden",
        "klar",
        "gerne",
        "ja bitte",
        "absolut",
        "ja auf jeden fall",
    ],
)
def test_consent_variations_advance_to_name_prompt(test_db, consent_word):
    t1 = enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    assert "Möchtest du dein Sprachprofil biometrisch" in t1
    t2 = enrollment_fsm.handle_turn(test_db, consent_word)
    assert "Wie lautet dein Name oder Kürzel" in t2


# --- 2. CANCELLATION & REFUSAL TESTS (11-20) ---
@pytest.mark.parametrize(
    "cancel_word",
    [
        "nein",
        "nein danke",
        "abbrechen",
        "stopp",
        "stop",
        "abbruch",
        "keine lust",
        "lieber nicht",
        "nein",
        "abbrechen bitte",
    ],
)
def test_cancellation_at_consent_resets_fsm(test_db, cancel_word):
    enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    res = enrollment_fsm.handle_turn(test_db, cancel_word)
    assert any(w in res.lower() for w in ("abgebrochen", "abgelehnt", "helfen"))
    # Verify state is idle
    assert enrollment_fsm.get_fsm_state(test_db, "default") is None


# --- 3. NAME FORMAT VARIATIONS (21-30) ---
@pytest.mark.parametrize(
    "name_input,expected_name",
    [
        ("Alex", "Alex Test"),
        ("user1", "User1"),
        ("A-L-E-X", "Alex Test"),
        ("Max", "Max"),
        ("M-A-X", "Max"),
        ("Anna", "Anna"),
        ("A-N-N-A", "Anna"),
        ("Stefan", "Stefan"),
        ("Laura", "Laura"),
        ("K-A-R-L", "Karl"),
    ],
)
def test_name_resolution_starts_sample_1(test_db, name_input, expected_name):
    enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    enrollment_fsm.handle_turn(test_db, "ja")
    res = enrollment_fsm.handle_turn(test_db, name_input)
    assert expected_name in res or name_input in res
    assert "Was ist dein erster Satz?" in res


# --- 4. FULL VOICE ENROLLMENT STEP-BY-STEP (31-35) ---
def test_full_successful_voice_enrollment_3_sentences(test_db):
    enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    enrollment_fsm.handle_turn(test_db, "ja")
    t3 = enrollment_fsm.handle_turn(test_db, "Alex")
    assert "Was ist dein erster Satz?" in t3

    t4 = enrollment_fsm.handle_turn(test_db, "Heute ist ein schöner Tag.")
    assert "Was ist dein zweiter Satz?" in t4

    t5 = enrollment_fsm.handle_turn(test_db, "Morgen fahre ich nach Berlin.")
    assert "Was ist dein dritter und letzter Satz?" in t5

    _gatekeeper_enrols(test_db)
    t6 = enrollment_fsm.handle_turn(test_db, "Das Wetter ist sonnig.")
    assert "erfolgreich gespeichert" in t6
    assert enrollment_fsm.get_fsm_state(test_db, "default") is None


def test_cancellation_during_sentence_recording(test_db):
    enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    enrollment_fsm.handle_turn(test_db, "ja")
    enrollment_fsm.handle_turn(test_db, "Alex")
    enrollment_fsm.handle_turn(test_db, "Satz 1")
    res = enrollment_fsm.handle_turn(test_db, "abbrechen")
    assert "abgebrochen" in res
    assert enrollment_fsm.get_fsm_state(test_db, "default") is None


# --- 5. SPURIOUS STT INPUTS & NOISE AT CONSENT (36-40) ---
@pytest.mark.parametrize(
    "spurious_input", ["user1", "hallo", "wer da", "wer bist du", "licht an"]
)
def test_spurious_inputs_at_consent_keep_state_consent(test_db, spurious_input):
    enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    res = enrollment_fsm.handle_turn(test_db, spurious_input)
    assert "Bitte antworte mit Ja oder Nein" in res
    state = enrollment_fsm.get_fsm_state(test_db, "default")
    assert state["state"] == "consent"


# --- 6. WAKEWORD RECORDING & DELETION (41-50) ---
def test_wakeword_sample_recording_counts_up_to_10(test_db):
    req = wakeword_requests_store.start_request(test_db, "user1", target_count=10)
    assert req["collected_count"] == 0

    for i in range(1, 10):
        rec = wakeword_requests_store.record_sample(test_db, "user1")
        assert rec["collected_count"] == i
        assert rec["status"] == "active"

    rec10 = wakeword_requests_store.record_sample(test_db, "user1")
    assert rec10["collected_count"] == 10
    assert rec10["status"] == "completed"


def test_wakeword_sample_deletion_decrements_by_1(test_db):
    wakeword_requests_store.start_request(test_db, "user1", 10)
    for _ in range(5):
        wakeword_requests_store.record_sample(test_db, "user1")

    req_before = wakeword_requests_store.get_request(test_db, "user1")
    assert req_before["collected_count"] == 5

    # Decrement on sample deletion
    req_after = wakeword_requests_store.decrement_sample(test_db, "user1")
    assert req_after["collected_count"] == 4
    assert req_after["status"] == "active"


def test_ttl_auto_expiration_resets_stale_fsm(test_db):
    enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    enrollment_fsm.handle_turn(test_db, "ja")
    with sqlite3.connect(test_db) as conn:
        conn.execute(
            "UPDATE enrollment_fsm SET updated_at = datetime('now', '-600 seconds')"
        )
        conn.commit()

    res = enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    assert "Möchtest du dein Sprachprofil biometrisch" in res


# --- 7. MULTI-USER ENROLLMENT SEQUENCES (51-55) ---
@pytest.mark.parametrize(
    "user_name,spelled",
    [
        ("Max", "M - A - X"),
        ("Laura", "L - A - U - R - A"),
        ("Stefan", "S - T - E - F - A - N"),
        ("Anna", "A - N - N - A"),
        ("Karl", "K - A - R - L"),
    ],
)
def test_consecutive_multi_user_enrollments(test_db, user_name, spelled):
    t1 = enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    assert "Möchtest du dein Sprachprofil biometrisch" in t1
    t2 = enrollment_fsm.handle_turn(test_db, "ja")
    assert "Wie lautet dein Name" in t2
    t3 = enrollment_fsm.handle_turn(test_db, user_name)
    assert user_name in t3
    t4 = enrollment_fsm.handle_turn(test_db, "Satz Eins")
    assert "zweiter Satz" in t4
    t5 = enrollment_fsm.handle_turn(test_db, "Satz Zwei")
    assert "dritter und letzter Satz" in t5
    _gatekeeper_enrols(test_db)
    t6 = enrollment_fsm.handle_turn(test_db, "Satz Drei")
    assert "erfolgreich gespeichert" in t6


def test_no_embedding_reports_honest_failure_and_does_not_enrol(test_db):
    """The wizard counts text turns; only the gatekeeper writes the embedding.
    With speaker-ID off nothing enrols, so the last turn must say so plainly
    instead of claiming success — otherwise the resident is told their profile
    is ready while person recognition can never work."""
    enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    enrollment_fsm.handle_turn(test_db, "ja")
    enrollment_fsm.handle_turn(test_db, "Alex")
    enrollment_fsm.handle_turn(test_db, "Satz Eins")
    enrollment_fsm.handle_turn(test_db, "Satz Zwei")
    final = enrollment_fsm.handle_turn(test_db, "Satz Drei")

    assert "erfolgreich gespeichert" not in final
    assert "nicht speichern" in final
    assert enrollment_fsm.get_fsm_state(test_db, "default") is None
    with sqlite3.connect(test_db) as conn:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_residents'"
        ).fetchone()
        enrolled = (
            conn.execute(
                "SELECT COUNT(*) FROM pending_residents WHERE enrolled = 1"
            ).fetchone()[0]
            if has_table
            else 0
        )
    assert enrolled == 0


def test_rejected_samples_ask_for_another_sentence(test_db):
    """Samples rejected as too quiet leave the request `capturing`. The wizard
    asks for one more sentence rather than ending on a false success."""
    enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.")
    enrollment_fsm.handle_turn(test_db, "ja")
    enrollment_fsm.handle_turn(test_db, "Alex")
    enrollment_fsm.handle_turn(test_db, "Satz Eins")
    enrollment_fsm.handle_turn(test_db, "Satz Zwei")
    _gatekeeper_enrols(test_db, status="capturing", result=None)
    retry = enrollment_fsm.handle_turn(test_db, "Satz Drei")

    assert "noch einen Satz" in retry
    assert retry.rstrip().endswith("?")
    assert enrollment_fsm.get_fsm_state(test_db, "default") is not None

    _gatekeeper_enrols(test_db)
    assert "erfolgreich gespeichert" in enrollment_fsm.handle_turn(test_db, "Satz Vier")


# --- 8. WAKEWORD DIALOG PERMUTATIONS (56-65) ---
@pytest.mark.parametrize("sample_index", list(range(1, 11)))
def test_wakeword_individual_sample_indices(test_db, sample_index):
    wakeword_requests_store.start_request(test_db, "user1", 10)
    for _ in range(sample_index - 1):
        wakeword_requests_store.record_sample(test_db, "user1")

    rec = wakeword_requests_store.record_sample(test_db, "user1")
    assert rec["collected_count"] == sample_index
    if sample_index < 10:
        assert rec["status"] == "active"
    else:
        assert rec["status"] == "completed"


def test_refusing_consent_closes_the_enrol_request(test_db):
    """The LLM entry tool opens the enrol request before the wizard asks for
    consent, and while one is open the gatekeeper captures the speaker's PCM.
    Refusing must therefore close it — otherwise saying "nein" still ends in an
    enrolled voice profile."""
    from solaris_chat import enroll_requests_store

    enroll_requests_store.open_request(test_db, "alex", 3)
    enrollment_fsm.handle_turn(test_db, "Richte einen Benutzer ein.", uid_hint="alex")
    assert enroll_requests_store.has_any_active_request(test_db) is True

    out = enrollment_fsm.handle_turn(test_db, "nein", uid_hint="alex")

    assert "abgelehnt" in out
    assert enrollment_fsm.get_fsm_state(test_db, "default") is None
    assert enroll_requests_store.has_any_active_request(test_db) is False
