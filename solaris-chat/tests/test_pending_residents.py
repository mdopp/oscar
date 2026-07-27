"""Tests for the registration flow (#376): the pending_residents store, the
reverse enroll-stash engine store, and the `start_voice_enrollment` /
`register_pending_resident` tools.

Live-voice enrolment no longer ships PCM through the engine; the engine opens an
`enroll_requests` row and the gatekeeper writes the result back. The tools are
exercised against a real sqlite db with both schemas replayed locally (no
alembic, no gatekeeper process), and a row is hand-set to the status the
gatekeeper would have written.
"""

from __future__ import annotations

import json
import sqlite3

from solaris_chat import enroll_requests_store, pending_residents_store
from solaris_chat.engine.tools.register import build_register_tools

# Both schemas migrations 0013/0014 create, replayed locally.
_SCHEMA = """
CREATE TABLE pending_residents (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  uid          TEXT NOT NULL,
  display_name TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  enrolled     INTEGER NOT NULL DEFAULT 0,
  requested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE enroll_requests (
  uid            TEXT PRIMARY KEY,
  status         TEXT NOT NULL DEFAULT 'pending',
  target_samples INTEGER NOT NULL DEFAULT 3,
  collected      INTEGER NOT NULL DEFAULT 0,
  result         TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _set_status(db: str, uid: str, status: str, *, collected: int = 3) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE enroll_requests SET status = ?, collected = ? WHERE uid = ?",
        (status, collected, uid),
    )
    conn.commit()
    conn.close()


def _age_request(db: str, uid: str, seconds: int) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE enroll_requests SET created_at = datetime('now', ?) WHERE uid = ?",
        (f"-{seconds} seconds", uid),
    )
    conn.commit()
    conn.close()


def _tools(db: str):
    return {t.name: t for t in build_register_tools(db)}


# --- pending_residents_store (unchanged contract) ---------------------------


def test_store_writes_and_reads_a_pending_request(tmp_path):
    db = _db(tmp_path)
    rid = pending_residents_store.add_pending_resident(
        db, uid="lena", display_name="Lena", enrolled=True
    )
    assert rid > 0
    rows = pending_residents_store.list_pending_residents(db)
    assert len(rows) == 1
    assert rows[0]["uid"] == "lena"
    assert rows[0]["display_name"] == "Lena"
    assert rows[0]["status"] == "pending"
    assert rows[0]["enrolled"] == 1


def test_store_reads_empty_when_db_missing(tmp_path):
    assert (
        pending_residents_store.list_pending_residents(str(tmp_path / "absent.db"))
        == []
    )


# --- enroll_requests_store --------------------------------------------------


def test_open_request_writes_pending_row(tmp_path):
    db = _db(tmp_path)
    enroll_requests_store.open_request(db, "lena", target_samples=3)
    req = enroll_requests_store.read_request(db, "lena")
    assert req["status"] == "pending"
    assert req["target_samples"] == 3
    assert req["collected"] == 0
    assert req["timed_out"] is False


def test_open_request_resets_existing_row(tmp_path):
    db = _db(tmp_path)
    enroll_requests_store.open_request(db, "lena")
    _set_status(db, "lena", "done", collected=3)
    enroll_requests_store.open_request(db, "lena")  # re-open
    req = enroll_requests_store.read_request(db, "lena")
    assert req["status"] == "pending"
    assert req["collected"] == 0


def test_read_request_reports_timeout_past_ttl(tmp_path):
    db = _db(tmp_path)
    enroll_requests_store.open_request(db, "lena")
    _age_request(db, "lena", enroll_requests_store.ENROLL_TTL_SECONDS + 10)
    req = enroll_requests_store.read_request(db, "lena")
    assert req["timed_out"] is True


def test_read_request_done_row_never_times_out(tmp_path):
    db = _db(tmp_path)
    enroll_requests_store.open_request(db, "lena")
    _set_status(db, "lena", "done")
    _age_request(db, "lena", enroll_requests_store.ENROLL_TTL_SECONDS + 10)
    req = enroll_requests_store.read_request(db, "lena")
    assert req["timed_out"] is False
    assert req["status"] == "done"


def test_read_request_missing_db_is_none(tmp_path):
    assert enroll_requests_store.read_request(str(tmp_path / "absent.db"), "x") is None


def test_clear_request_drops_row(tmp_path):
    db = _db(tmp_path)
    enroll_requests_store.open_request(db, "lena")
    enroll_requests_store.clear_request(db, "lena")
    assert enroll_requests_store.read_request(db, "lena") is None


# --- start_voice_enrollment tool --------------------------------------------


async def test_start_hands_off_to_the_wizard(tmp_path):
    """The entry tool must not run a second state machine. It used to resolve a
    name and open the request itself — which is exactly what activates the
    wizard, so the wizard restarted at consent and every sentence the resident
    spoke was read as a yes/no answer. Observed on the box as an endless
    "Bitte antworte mit Ja oder Nein"."""
    db = _db(tmp_path)
    out = json.loads(await _tools(db)["start_voice_enrollment"].handler({}))
    assert out["ok"] is True
    assert out["collecting"] is True
    # It opens no request: the wizard does that once it has a name.
    assert enroll_requests_store.read_request(db, "lena") is None
    # And it speaks the wizard's own first line, ending in a question.
    assert "biometrisch" in out["say"]
    assert out["say"].rstrip().endswith("?")


async def test_start_ignores_any_uid_the_model_passes(tmp_path):
    """The model cannot name the resident. It used to pass whatever it plucked
    from the sentence and the resolver turned it into an identity — "mein
    Sprachprofil" became a resident called "Meinname" (#1067). The wizard asks
    for the name itself, so there is nothing to invent."""
    db = _db(tmp_path)
    for passed in ("lena", "mein Sprachprofil", "Bad UID!", ""):
        out = json.loads(
            await _tools(db)["start_voice_enrollment"].handler({"uid": passed})
        )
        assert out["ok"] is True, passed
        assert "uid" not in out, passed
        assert enroll_requests_store.read_request(db, "lena") is None, passed


# --- register_pending_resident tool -----------------------------------------


async def test_register_files_pending_when_enroll_done(tmp_path):
    db = _db(tmp_path)
    enroll_requests_store.open_request(db, "lena")
    _set_status(db, "lena", "done")  # gatekeeper finished the capture
    out = json.loads(
        await _tools(db)["register_pending_resident"].handler(
            {"uid": "lena", "display_name": "Lena"}
        )
    )
    assert out["ok"] is True
    assert out["status"] == "pending"
    rows = pending_residents_store.list_pending_residents(db)
    assert len(rows) == 1 and rows[0]["uid"] == "lena" and rows[0]["enrolled"] == 1
    # The request row is consumed once acted on.
    assert enroll_requests_store.read_request(db, "lena") is None


async def test_register_times_out_when_speaker_id_off(tmp_path):
    db = _db(tmp_path)
    enroll_requests_store.open_request(db, "lena")
    # No gatekeeper picks it up (speaker-ID off); the row ages past the TTL.
    _age_request(db, "lena", enroll_requests_store.ENROLL_TTL_SECONDS + 10)
    out = json.loads(
        await _tools(db)["register_pending_resident"].handler(
            {"uid": "lena", "display_name": "Lena"}
        )
    )
    assert out == {"ok": False, "reason": "speaker_id_disabled"}
    assert pending_residents_store.list_pending_residents(db) == []
    assert enroll_requests_store.read_request(db, "lena") is None


async def test_register_incomplete_while_still_capturing(tmp_path):
    db = _db(tmp_path)
    enroll_requests_store.open_request(db, "lena")
    _set_status(db, "lena", "capturing", collected=1)
    out = json.loads(
        await _tools(db)["register_pending_resident"].handler(
            {"uid": "lena", "display_name": "Lena"}
        )
    )
    assert out["ok"] is False
    assert out["reason"] == "enroll_incomplete"
    assert out["collected"] == 1
    assert pending_residents_store.list_pending_residents(db) == []


async def test_register_failed_result_is_honest_failure_not_incomplete(tmp_path):
    db = _db(tmp_path)
    enroll_requests_store.open_request(db, "lena")
    _set_status(db, "lena", "failed")
    out = json.loads(
        await _tools(db)["register_pending_resident"].handler(
            {"uid": "lena", "display_name": "Lena"}
        )
    )
    # A real gatekeeper failure must not look like "still capturing", or the
    # dialog loops asking for more samples until the TTL.
    assert out == {"ok": False, "reason": "enroll_failed"}
    assert pending_residents_store.list_pending_residents(db) == []
    # Stale failed row cleared so the uid can be re-enrolled (not blocked).
    assert enroll_requests_store.read_request(db, "lena") is None


async def test_register_rejects_missing_display_name(tmp_path):
    db = _db(tmp_path)
    enroll_requests_store.open_request(db, "lena")
    _set_status(db, "lena", "done")
    out = json.loads(
        await _tools(db)["register_pending_resident"].handler(
            {"uid": "lena", "display_name": "  "}
        )
    )
    assert out == {"ok": False, "reason": "missing_display_name"}
    assert pending_residents_store.list_pending_residents(db) == []


async def test_register_no_request_is_honest_failure(tmp_path):
    db = _db(tmp_path)
    out = json.loads(
        await _tools(db)["register_pending_resident"].handler(
            {"uid": "lena", "display_name": "Lena"}
        )
    )
    assert out == {"ok": False, "reason": "no_enroll_request"}
