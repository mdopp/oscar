"""Stage 2 of the onboarding wizard: training the wake word "Solaris" (#1060).

Stage 1 saves the voice profile and used to end the wizard right there, so the
resident was never asked for the wake-word samples the combined flow promises.
The wizard now offers stage 2 and drives it deterministically — and counts only
what the gatekeeper really recorded, never turns.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from solaris_chat import wakeword_requests_store, wakeword_samples_store
from solaris_chat.engine import enrollment_fsm


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "wizard.db")
    enrollment_fsm.reset_fsm(path, "default")
    return path


def _gatekeeper_enrols(db_path: str) -> None:
    """The stage-1 half of the handshake: the gatekeeper enrolled the averaged
    embedding and flipped the request row terminal."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE enroll_requests SET status = 'done', result = 'ok'")
        conn.commit()


def _gatekeeper_records_wakeword(db_path: str, uid: str = "alex") -> None:
    """The stage-2 half: the gatekeeper wrote this turn's PCM as the next `.wav`
    and counted it. Mirrors `gatekeeper/wakeword_stash.record_sample`."""
    wakeword_requests_store.record_sample(db_path, uid)


def _through_stage_one(db_path: str) -> str:
    enrollment_fsm.handle_turn(db_path, "Richte meinen Benutzer ein.")
    enrollment_fsm.handle_turn(db_path, "ja")
    enrollment_fsm.handle_turn(db_path, "Alex")
    enrollment_fsm.handle_turn(db_path, "Satz eins")
    enrollment_fsm.handle_turn(db_path, "Satz zwei")
    _gatekeeper_enrols(db_path)
    return enrollment_fsm.handle_turn(db_path, "Satz drei")


def test_profile_success_offers_the_wakeword_stage(db):
    offer = _through_stage_one(db)

    assert "erfolgreich gespeichert" in offer
    assert "Solaris" in offer and "10" in offer
    assert offer.rstrip().endswith("?")
    assert (
        enrollment_fsm.get_fsm_state(db, "default")["state"]
        == enrollment_fsm.STATE_WAKEWORD_OFFER
    )
    # Nothing is recorded until the resident agrees.
    assert wakeword_requests_store.get_request(db, "alex") is None


def test_declining_the_offer_closes_the_wizard(db):
    _through_stage_one(db)

    out = enrollment_fsm.handle_turn(db, "nein")

    assert "Sprachprofil ist gespeichert" in out
    assert enrollment_fsm.get_fsm_state(db, "default") is None
    assert wakeword_requests_store.has_any_active_request(db) is False


def test_unclear_answer_reasks_the_offer(db):
    _through_stage_one(db)

    out = enrollment_fsm.handle_turn(db, "was meinst du damit")

    assert "Ja oder Nein" in out
    assert out.rstrip().endswith("?")
    assert (
        enrollment_fsm.get_fsm_state(db, "default")["state"]
        == enrollment_fsm.STATE_WAKEWORD_OFFER
    )


def test_accepting_opens_the_wakeword_request(db):
    _through_stage_one(db)

    out = enrollment_fsm.handle_turn(db, "ja gerne")

    assert "Solaris" in out
    assert out.rstrip().endswith("?")
    request = wakeword_requests_store.get_request(db, "alex")
    assert request["status"] == "active"
    assert request["target_count"] == 10
    assert request["collected_count"] == 0
    assert (
        enrollment_fsm.get_fsm_state(db, "default")["state"]
        == enrollment_fsm.STATE_WAKEWORD_RECORDING
    )


def test_captured_sample_counts_down_and_is_filed(db):
    _through_stage_one(db)
    enrollment_fsm.handle_turn(db, "ja")

    _gatekeeper_records_wakeword(db)
    out = enrollment_fsm.handle_turn(db, "Solaris")

    assert "Noch 9 Mal" in out
    assert out.rstrip().endswith("?")

    sample = wakeword_samples_store.get_sample(db, "sample_alex_1")
    assert sample["resident_uid"] == "alex"
    assert sample["is_valid"] == 1
    assert sample["filename"] == wakeword_samples_store.sample_path(db, "alex", 1)
    assert sample["filename"].endswith(
        os.path.join("wakeword", "user_samples", "alex", "sample_alex_1.wav")
    )


def test_uncaptured_sample_does_not_count(db):
    _through_stage_one(db)
    enrollment_fsm.handle_turn(db, "ja")

    out = enrollment_fsm.handle_turn(db, "Solaris")

    assert "nicht mitbekommen" in out
    assert out.rstrip().endswith("?")
    assert wakeword_requests_store.get_request(db, "alex")["collected_count"] == 0
    assert wakeword_samples_store.list_samples(db, "solaris") == []

    # ... and the next turn the gatekeeper does capture counts as the first.
    _gatekeeper_records_wakeword(db)
    assert "Noch 9 Mal" in enrollment_fsm.handle_turn(db, "Solaris")


def test_a_dead_microphone_stops_instead_of_looping(db):
    _through_stage_one(db)
    enrollment_fsm.handle_turn(db, "ja")

    replies = [enrollment_fsm.handle_turn(db, "Solaris") for _ in range(4)]

    assert all("nicht mitbekommen" in r for r in replies[:3])
    assert "nicht mitschneiden" in replies[3]
    assert enrollment_fsm.get_fsm_state(db, "default") is None
    assert wakeword_requests_store.has_any_active_request(db) is False


def test_ten_captured_samples_close_the_wizard(db):
    _through_stage_one(db)
    replies = [enrollment_fsm.handle_turn(db, "ja")]

    for _ in range(10):
        _gatekeeper_records_wakeword(db)
        replies.append(enrollment_fsm.handle_turn(db, "Solaris"))

    # Every continuing reply keeps the Voice PE microphone open; only the
    # terminal close drops the question mark.
    assert all(r.rstrip().endswith("?") for r in replies[:-1])
    assert "Nur noch einmal" in replies[-2]
    assert "Alle 10 Aufnahmen" in replies[-1]
    assert not replies[-1].rstrip().endswith("?")
    assert enrollment_fsm.get_fsm_state(db, "default") is None
    assert wakeword_requests_store.has_any_active_request(db) is False
    assert len(wakeword_samples_store.list_samples(db, "solaris")) == 10


@pytest.mark.parametrize("cancel_word", ["abbrechen", "stopp", "nein danke"])
def test_cancel_words_work_mid_stage(db, cancel_word):
    _through_stage_one(db)
    enrollment_fsm.handle_turn(db, "ja")
    _gatekeeper_records_wakeword(db)
    enrollment_fsm.handle_turn(db, "Solaris")

    out = enrollment_fsm.handle_turn(db, cancel_word)

    assert "Sprachprofil bleibt gespeichert" in out
    assert enrollment_fsm.get_fsm_state(db, "default") is None
    # The request must close, or the gatekeeper keeps recording the next
    # speaker's turns into this resident's wake-word set.
    assert wakeword_requests_store.has_any_active_request(db) is False


def test_ttl_expires_the_wakeword_stage(db):
    _through_stage_one(db)
    enrollment_fsm.handle_turn(db, "ja")

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE enrollment_fsm SET updated_at = datetime('now', '-600 seconds')"
        )
        conn.commit()

    assert enrollment_fsm.get_fsm_state(db, "default") is None
    assert "Möchtest du dein Sprachprofil biometrisch" in enrollment_fsm.handle_turn(
        db, "Solaris"
    )


def test_misheard_sample_is_filed_as_suspicious(db):
    _through_stage_one(db)
    enrollment_fsm.handle_turn(db, "ja")

    _gatekeeper_records_wakeword(db)
    enrollment_fsm.handle_turn(db, "Cello")

    suspicious = wakeword_samples_store.get_suspicious_samples(db, "solaris")
    assert [s["id"] for s in suspicious] == ["sample_alex_1"]
    assert suspicious[0]["stt_transcript"] == "Cello"
