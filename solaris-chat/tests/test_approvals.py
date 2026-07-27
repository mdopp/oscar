"""ServiceBay approval-request poller → Wartung approval-cards (Wartung P3, #790).

Covers: `mark_seen` dedupe (a pending request cards once, degrades to new without
the table); `_pending` filters the feed to still-pending requests; `poll_once`
injects an approval action-card into the Wartung chat per NEW pending request and
dedupes across ticks (canned SB feed via a fake `_get`), and pushes to the phone
when backgrounded; `submit_verdict` POSTs to the approve/reject endpoint; the
[Approve]/[Deny] handlers are registered admin=True (approve also destructive), so
`/api/action-callback` refuses a non-admin caller and confirm-gates a bare admin
approve; a confirmed admin verdict reaches ServiceBay's endpoint. Tables are raw
SQL (a chat test must NOT import alembic — CI runs solaris-chat clean).
"""

from __future__ import annotations

import sqlite3

from solaris_chat.engine import action_cards, approvals, store
from solaris_chat.engine.approvals import ApprovalPoller, mark_seen
from solaris_chat.server import build_app

_SCHEMA = """
CREATE TABLE engine_sessions (
  id            TEXT PRIMARY KEY,
  owner_uid     TEXT NOT NULL,
  title         TEXT NOT NULL DEFAULT '',
  profile       TEXT NOT NULL DEFAULT 'household',
  system_prompt TEXT NOT NULL DEFAULT '',
  ephemeral     INTEGER NOT NULL DEFAULT 0,
  maintenance   INTEGER NOT NULL DEFAULT 0,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  last_activity TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE engine_messages (
  session_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL DEFAULT '',
  reasoning   TEXT,
  tool_calls  TEXT,
  images      TEXT,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  conversation_id TEXT,
  in_prompt   INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (session_id, seq)
);
CREATE TABLE wartung_seen_approvals (
  approval_id TEXT PRIMARY KEY,
  seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


# ---- dedupe ----------------------------------------------------------------


def test_mark_seen_cards_once_then_dedupes(tmp_path):
    db = _db(tmp_path)
    assert mark_seen(db, "req-a") is True
    # Same pending request the next tick — already carded, no re-card.
    assert mark_seen(db, "req-a") is False
    # A different request is a new id → cards.
    assert mark_seen(db, "req-b") is True


def test_mark_seen_degrades_to_new_without_table(tmp_path):
    # No table (schema-init hasn't run) ⇒ treat as new rather than crash.
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    assert mark_seen(path, "req-x") is True


# ---- pending filter --------------------------------------------------------


def test_pending_keeps_only_pending_with_id():
    feed = [
        {"id": "a", "status": "pending", "service": "immich", "title": "t"},
        {"id": "b", "status": "approved", "service": "media", "title": "t"},
        {"id": "c", "status": "rejected", "service": "adguard", "title": "t"},
        {"id": "", "status": "pending", "service": "x", "title": "t"},
        {"status": "pending", "service": "y", "title": "t"},
    ]
    ids = [a["id"] for a in approvals._pending(feed)]
    assert ids == ["a"]


# ---- poll_once → card injection --------------------------------------------


class _FakePoller(ApprovalPoller):
    """ApprovalPoller with the HTTP GET stubbed by a canned feed."""

    def __init__(self, *a, feed=None, **kw):
        super().__init__(*a, **kw)
        self._feed = feed if feed is not None else {"approvals": []}

    async def _get(self, url, headers):
        return self._feed


async def test_poll_once_cards_new_requests_into_wartung(tmp_path):
    db = _db(tmp_path)
    from solaris_chat.engine.notify import EventBus

    poller = _FakePoller(
        db,
        "http://sb",
        str(tmp_path / "read"),
        str(tmp_path / "token"),
        EventBus(),
        "household",
        feed={
            "approvals": [
                {
                    "id": "req-1",
                    "status": "pending",
                    "service": "immich",
                    "title": "Move album to trash",
                    "description": "Requested by immich cleanup",
                },
                {
                    "id": "req-2",
                    "status": "approved",  # already decided → not carded
                    "service": "media",
                    "title": "x",
                },
            ]
        },
    )
    injected = await poller.poll_once()
    assert injected == 1  # only the pending one

    sid = store.wartung_session_id("household")
    hist = store.history(db, sid)
    assert len(hist) == 1
    assert hist[0]["role"] == "assistant"
    assert "immich" in hist[0]["content"]

    # A second identical tick cards nothing (deduped).
    assert await poller.poll_once() == 0


async def test_poll_once_no_requests_is_quiet(tmp_path):
    from solaris_chat.engine.notify import EventBus

    db = _db(tmp_path)
    poller = _FakePoller(
        db,
        "http://sb",
        str(tmp_path / "read"),
        str(tmp_path / "t"),
        EventBus(),
        "household",
    )
    assert await poller.poll_once() == 0


async def test_poll_once_carding_pushes_when_backgrounded(tmp_path):
    from solaris_chat.engine.notify import EventBus

    db = _db(tmp_path)
    pushes: list = []

    class _Notifier:
        async def push(self, uid, title, body, data):
            pushes.append((uid, body))

    poller = _FakePoller(
        db,
        "http://sb",
        str(tmp_path / "read"),
        str(tmp_path / "t"),
        EventBus(),  # nobody watching → backgrounded
        "household",
        notifier=_Notifier(),
        feed={
            "approvals": [
                {"id": "r", "status": "pending", "service": "adguard", "title": "t"}
            ]
        },
    )
    assert await poller.poll_once() == 1
    assert len(pushes) == 1  # phone push for the new card
    assert pushes[0][0] == "household"


# ---- token selection: the unattended poll uses the read token, #838 ---------


class _AuthCapturePoller(ApprovalPoller):
    """Captures the Bearer the poll GET carried (which token file won)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.auth: str | None = None

    async def _get(self, url, headers):
        self.auth = headers.get("Authorization")
        return {"approvals": []}


async def test_poll_uses_read_token_when_present(tmp_path):
    from solaris_chat.engine.notify import EventBus

    read_path = tmp_path / "read"
    mcp_path = tmp_path / "mcp"
    read_path.write_text("sb_read_TOKEN")
    mcp_path.write_text("sb_admin_TOKEN")
    poller = _AuthCapturePoller(
        _db(tmp_path),
        "http://sb",
        str(read_path),
        str(mcp_path),
        EventBus(),
        "household",
    )
    await poller.poll_once()
    assert poller.auth == "Bearer sb_read_TOKEN"


async def test_poll_falls_back_to_mcp_token_when_read_absent(tmp_path):
    from solaris_chat.engine.notify import EventBus

    mcp_path = tmp_path / "mcp"
    mcp_path.write_text("sb_admin_TOKEN")
    poller = _AuthCapturePoller(
        _db(tmp_path),
        "http://sb",
        str(tmp_path / "missing"),
        str(mcp_path),
        EventBus(),
        "household",
    )
    await poller.poll_once()
    assert poller.auth == "Bearer sb_admin_TOKEN"


# ---- verdict callback ------------------------------------------------------


async def test_submit_verdict_posts_to_approve(monkeypatch):
    calls: list = []

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return '{"ok":true}'

    class _Session:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, headers=None):
            calls.append((url, headers))
            return _Resp()

    monkeypatch.setattr(approvals.aiohttp, "ClientSession", _Session)
    ok, detail = await approvals.submit_verdict("http://sb", "tok", "req-9", True)
    assert ok is True
    assert calls[0][0] == "http://sb/api/approvals/req-9/approve"
    assert calls[0][1] == {"Authorization": "Bearer tok"}


async def test_submit_verdict_reports_non_2xx_as_failure(monkeypatch):
    class _Resp:
        status = 403

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return "self-approve refused"

    class _Session:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, headers=None):
            return _Resp()

    monkeypatch.setattr(approvals.aiohttp, "ClientSession", _Session)
    ok, detail = await approvals.submit_verdict("http://sb", "tok", "req-9", False)
    assert ok is False
    assert "403" in detail


# ---- [Approve]/[Deny] handler registration + gate --------------------------


def _app(tmp_path, sb_api_url=""):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(tmp_path),
        sb_api_url=sb_api_url,
    )


def test_verdict_actions_registered_admin(tmp_path):
    _app(tmp_path)
    approve = action_cards.get(approvals.APPROVE_ACTION)
    deny = action_cards.get(approvals.DENY_ACTION)
    assert approve is not None and deny is not None
    # Approve runs the request's declared side effect → admin + destructive.
    assert approve.admin is True
    assert approve.destructive is True
    # Deny cancels the proposal → admin-only, no confirm.
    assert deny.admin is True
    assert deny.destructive is False


async def test_approve_forbidden_for_non_admin(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/action-callback",
        json={"action_id": approvals.APPROVE_ACTION, "params": {"approval_id": "r"}},
    )
    assert r.status == 403
    assert (await r.json())["reason"] == "forbidden"


async def test_deny_forbidden_for_non_admin(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/action-callback",
        json={"action_id": approvals.DENY_ACTION, "params": {"approval_id": "r"}},
    )
    assert r.status == 403
    assert (await r.json())["reason"] == "forbidden"


async def test_approve_confirm_gated_for_admin(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    # An admin bare tap on Approve is confirm-gated — no verdict must be sent.
    r = await client.post(
        "/api/action-callback",
        headers={"Remote-Groups": "admins"},
        json={"action_id": approvals.APPROVE_ACTION, "params": {"approval_id": "r"}},
    )
    assert r.status == 403
    assert (await r.json())["reason"] == "confirm_required"


async def test_confirmed_admin_approve_reaches_verdict(
    aiohttp_client, tmp_path, monkeypatch
):
    # A confirmed admin tap runs the handler; with a token minted, submit_verdict
    # is called with approve=True and its outcome surfaced — proving the gate let
    # it through only for a confirmed admin AND the verdict path is wired.
    seen: dict = {}

    async def _fake_exchange(url):
        return "session-tok"

    async def _fake_verdict(sb_api_url, token, approval_id, approve):
        seen["args"] = (sb_api_url, token, approval_id, approve)
        return True, "ok"

    monkeypatch.setattr("solaris_chat.server.exchange_sb_token", _fake_exchange)
    monkeypatch.setattr(approvals, "submit_verdict", _fake_verdict)

    client = await aiohttp_client(_app(tmp_path, sb_api_url="http://sb"))
    r = await client.post(
        "/api/action-callback",
        headers={"Remote-Groups": "admins", "Remote-User": "michael"},
        json={
            "action_id": approvals.APPROVE_ACTION,
            "confirmed": True,
            "params": {"approval_id": "req-42"},
        },
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert seen["args"] == ("http://sb", "session-tok", "req-42", True)


async def test_deny_without_sb_api_reports_reason(aiohttp_client, tmp_path):
    # Deny is admin-only, not confirm-gated, so a plain admin tap runs the
    # handler; with no SB API base wired it reports no_sb_api (not 403/404).
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/action-callback",
        headers={"Remote-Groups": "admins", "Remote-User": "michael"},
        json={"action_id": approvals.DENY_ACTION, "params": {"approval_id": "r"}},
    )
    assert r.status == 200
    assert (await r.json())["reason"] == "no_sb_api"
