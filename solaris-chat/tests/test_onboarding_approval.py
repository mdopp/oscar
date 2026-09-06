"""Tests for the resident-onboarding approval + provisioning step (#355).

The SB-MCP calls (file_access_request / get_access_request_status) are mocked by
monkeypatching `call_sb_tool`, so we assert the Solaris side only: the request
is filed with the right shape (uid as the LLDAP username, candidate name as the
subject, the request id stored on the row); status is polled; the tri-state
verdict (servicebay#1824) is handled — "approved" marks the row approved and
confirms the voice-profile binding (never dropping the biometric); "denied"
drops the captured voice profile via the gatekeeper seam and closes the row;
"pending" provisions nothing; "not-found" cleans up gracefully. The gatekeeper
DELETE is mocked, so no real SB account is created and no real audio is touched.
"""

from __future__ import annotations

import json
import sqlite3

from solaris_chat import pending_residents_store
from solaris_chat.engine.tools import onboarding_approval

# Schema migrations 0013 + 0014 create, replayed locally (no alembic in the
# chat test env), plus voice_embeddings from the baseline for the binding check.
_SCHEMA = """
CREATE TABLE pending_residents (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  uid          TEXT NOT NULL,
  display_name TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  enrolled     INTEGER NOT NULL DEFAULT 0,
  requested_at TEXT NOT NULL DEFAULT (datetime('now')),
  request_id   TEXT,
  email        TEXT
);
CREATE TABLE voice_embeddings (
  uid          TEXT PRIMARY KEY,
  embedding    BLOB NOT NULL,
  enrolled_at  TEXT NOT NULL DEFAULT (datetime('now')),
  enrolled_via TEXT NOT NULL,
  sample_count INTEGER NOT NULL DEFAULT 1,
  last_seen_at TEXT
);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _enrol(db: str, uid: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO voice_embeddings (uid, embedding, enrolled_via) VALUES (?, ?, ?)",
        (uid, b"\x00" * 1024, "voice"),
    )
    conn.commit()
    conn.close()


def _stub_sb(monkeypatch, replies: dict[str, dict]) -> list[tuple]:
    """Replace call_sb_tool; record (name, args) calls and return canned JSON."""
    calls: list[tuple] = []

    async def fake(url, token_path, name, arguments):
        calls.append((name, arguments))
        return json.dumps(replies[name])

    monkeypatch.setattr(onboarding_approval, "call_sb_tool", fake)
    return calls


def _stub_delete(monkeypatch, removed: bool = True) -> list[str]:
    """Replace _delete_voice_profile (the gatekeeper DELETE /enrolments/{uid}
    seam); record the uids it was asked to drop and return a canned verdict."""
    dropped: list[str] = []

    async def fake(gatekeeper_url, gatekeeper_token, uid):
        dropped.append(uid)
        return removed

    monkeypatch.setattr(onboarding_approval, "_delete_voice_profile", fake)
    return dropped


def _tools(db):
    return {
        t.name: t
        for t in onboarding_approval.build_onboarding_approval_tools(
            db, "http://sb/mcp", "/tmp/token", "http://gatekeeper", "gk-token"
        )
    }


async def test_file_approval_files_request_with_right_shape(tmp_path, monkeypatch):
    db = _db(tmp_path)
    pending_residents_store.add_pending_resident(db, "lena", "Lena", enrolled=True)
    calls = _stub_sb(monkeypatch, {"file_access_request": {"id": "req-42"}})

    out = json.loads(
        await _tools(db)["file_resident_approval"].handler({"uid": "lena"})
    )
    assert out == {"ok": True, "request_id": "req-42", "status": "filed"}

    name, args = calls[0]
    assert name == "file_access_request"
    assert args["subject"] == "Lena"
    assert args["username"] == "lena"
    assert args["kind"] == "resident"

    row = pending_residents_store.get_pending_by_uid(db, "lena")
    assert row["request_id"] == "req-42"


async def test_file_approval_no_pending_request(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = _stub_sb(monkeypatch, {})
    out = json.loads(await _tools(db)["file_resident_approval"].handler({"uid": "x"}))
    assert out == {"ok": False, "reason": "no_pending_request"}
    assert calls == []


async def test_file_approval_is_idempotent(tmp_path, monkeypatch):
    db = _db(tmp_path)
    rid = pending_residents_store.add_pending_resident(
        db, "lena", "Lena", enrolled=True
    )
    pending_residents_store.set_request_id(db, rid, "req-7")
    calls = _stub_sb(monkeypatch, {"file_access_request": {"id": "should-not-call"}})

    out = json.loads(
        await _tools(db)["file_resident_approval"].handler({"uid": "lena"})
    )
    assert out == {"ok": True, "request_id": "req-7", "status": "filed"}
    assert calls == []  # already filed → no second SB call


async def test_check_approval_approved_provisions_and_binds(tmp_path, monkeypatch):
    db = _db(tmp_path)
    rid = pending_residents_store.add_pending_resident(
        db, "lena", "Lena", enrolled=True
    )
    pending_residents_store.set_request_id(db, rid, "req-42")
    _enrol(db, "lena")
    _stub_sb(monkeypatch, {"get_access_request_status": {"status": "approved"}})
    dropped = _stub_delete(monkeypatch)

    out = json.loads(
        await _tools(db)["check_resident_approval"].handler({"uid": "lena"})
    )
    assert out["status"] == "approved"
    assert out["provisioned"] is True
    assert out["voice_profile_bound"] is True

    # Approval never drops the biometric, and the enrolment row survives.
    assert dropped == []
    assert onboarding_approval._voice_profile_bound(db, "lena") is True

    # Pending row flipped → no longer surfaced as pending.
    assert pending_residents_store.get_pending_by_uid(db, "lena") is None
    rows = pending_residents_store.list_pending_residents(db)
    assert rows == []


async def test_check_approval_denied_drops_biometric_no_provision(
    tmp_path, monkeypatch
):
    db = _db(tmp_path)
    rid = pending_residents_store.add_pending_resident(
        db, "lena", "Lena", enrolled=True
    )
    pending_residents_store.set_request_id(db, rid, "req-42")
    _enrol(db, "lena")
    _stub_sb(monkeypatch, {"get_access_request_status": {"status": "denied"}})
    dropped = _stub_delete(monkeypatch, removed=True)

    out = json.loads(
        await _tools(db)["check_resident_approval"].handler({"uid": "lena"})
    )
    assert out["status"] == "denied"
    assert out["provisioned"] is False
    assert out["biometric_dropped"] is True

    # The voice profile is dropped (via the gatekeeper seam) and the local row
    # is closed: no resident, no pending request left behind.
    assert dropped == ["lena"]
    assert pending_residents_store.get_pending_by_uid(db, "lena") is None
    assert pending_residents_store.list_pending_residents(db) == []


async def test_check_approval_not_found_cleans_up_gracefully(tmp_path, monkeypatch):
    db = _db(tmp_path)
    rid = pending_residents_store.add_pending_resident(
        db, "lena", "Lena", enrolled=True
    )
    pending_residents_store.set_request_id(db, rid, "req-42")
    # SB no longer knows the request, and there is nothing to drop locally.
    _stub_sb(monkeypatch, {"get_access_request_status": {"status": "not-found"}})
    dropped = _stub_delete(monkeypatch, removed=False)

    out = json.loads(
        await _tools(db)["check_resident_approval"].handler({"uid": "lena"})
    )
    assert out["status"] == "not-found"
    assert out["provisioned"] is False
    assert out["biometric_dropped"] is False

    # The (idempotent) delete still fired and the pending row is closed.
    assert dropped == ["lena"]
    assert pending_residents_store.get_pending_by_uid(db, "lena") is None
    assert pending_residents_store.list_pending_residents(db) == []


async def test_check_approval_pending_provisions_nothing(tmp_path, monkeypatch):
    db = _db(tmp_path)
    rid = pending_residents_store.add_pending_resident(
        db, "lena", "Lena", enrolled=True
    )
    pending_residents_store.set_request_id(db, rid, "req-42")
    _stub_sb(monkeypatch, {"get_access_request_status": {"status": "pending"}})

    out = json.loads(
        await _tools(db)["check_resident_approval"].handler({"uid": "lena"})
    )
    assert out == {"ok": True, "status": "pending", "provisioned": False}
    # Still pending in the store; no approval flip.
    assert pending_residents_store.get_pending_by_uid(db, "lena") is not None


async def test_check_approval_not_filed(tmp_path, monkeypatch):
    db = _db(tmp_path)
    pending_residents_store.add_pending_resident(db, "lena", "Lena", enrolled=True)
    calls = _stub_sb(monkeypatch, {})
    out = json.loads(
        await _tools(db)["check_resident_approval"].handler({"uid": "lena"})
    )
    assert out == {"ok": False, "reason": "not_filed"}
    assert calls == []


def test_onboarding_tools_are_admin_only(tmp_path):
    """The onboarding-approval tools join only the admin profile — never the
    household/guest toolset (no self-approval surface for a guest)."""
    from solaris_chat.engine import profiles

    household, admin, guest, _lib, _enroll, _rec, _bus = profiles.build_engine_clients(
        db_path=str(tmp_path / "solaris.db"),
        llama_server_url="http://ollama",
        fast_model="m",
        thorough_model="m",
        soul_path="",
        sb_mcp_url="http://sb/mcp",
        sb_mcp_token_path="/tmp/token",
    )
    admin_names = set(admin._profile.toolbox.names())
    assert {"file_resident_approval", "check_resident_approval"} <= admin_names
    for client in (household, guest):
        names = set(client._profile.toolbox.names())
        assert "file_resident_approval" not in names
        assert "check_resident_approval" not in names
