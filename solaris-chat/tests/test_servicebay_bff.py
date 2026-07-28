"""Solaris BFF over ServiceBay — /napi/servicebay/* reads + approval-event bridge.

Solaris is the BFF/hub (ADR 0010, #811): the app talks only to Solaris. Solaris
re-serves ServiceBay's companion reads (`/napi/{home,approvals,services,
upgrades}`, servicebay#2252) under its OWN `/napi/servicebay/*` (device-token,
fail-closed) and republishes SB's `/napi/approvals/events` SSE
(`NewApprovalEvent`, servicebay#2268) onto the Solaris event bus so it reaches
the app over the existing `/napi/portal/events` SSE (#806).

Replays the device-token migration-0021 schema with raw SQL (a chat test must
NOT import alembic).
"""

from __future__ import annotations

import asyncio
import sqlite3

from solaris_chat import device_token_store
from solaris_chat.engine.notify import EventBus
from solaris_chat.engine.sb_events import SbApprovalEventBridge, _to_bus_event
from solaris_chat.server import STATIC_DIR, build_app

_SCHEMA = """
CREATE TABLE device_tokens (
  id         TEXT PRIMARY KEY,
  owner_uid  TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  label      TEXT,
  created    TEXT NOT NULL DEFAULT (datetime('now')),
  last_used  TEXT,
  revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX device_tokens_hash_idx ON device_tokens (token_hash);
CREATE INDEX device_tokens_owner_idx ON device_tokens (owner_uid);
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


class _FakeCompanion:
    """Stand-in for `SbCompanionClient` — the mock the routes read through."""

    def __init__(
        self, enabled=True, bodies=None, verdict=(True, "ok"), operate=(True, "ok")
    ):
        self.enabled = enabled
        self._bodies = bodies or {}
        self._verdict = verdict
        self._operate = operate
        self.calls: list[str] = []
        self.verdicts: list[tuple] = []
        self.operations: list[tuple] = []

    async def read(self, key):
        self.calls.append(key)
        return self._bodies.get(key)

    async def operate(self, name, action):
        self.operations.append((name, action))
        return self._operate

    async def submit_verdict(self, approval_id, verb, authelia_cookie=""):
        from solaris_chat.engine.client import current_admin_identity

        self.verdicts.append(
            (approval_id, verb, current_admin_identity.get(), authelia_cookie)
        )
        return self._verdict


def _app(tmp_path, db, companion=None):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        sb_companion=companion,
    )


# ---- /napi/servicebay/* reads: fail-closed device-token auth ----------------


async def test_napi_servicebay_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db, _FakeCompanion()))
    r = await client.get("/napi/servicebay/home")
    assert r.status == 401
    # Fail-closed: NOT the household default_uid.
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}


async def test_napi_servicebay_service_key_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db, _FakeCompanion()))
    r = await client.get(
        "/napi/servicebay/approvals",
        headers={"Authorization": "Bearer SOLARIS_API_KEY"},
    )
    assert r.status == 401


async def test_napi_servicebay_remote_user_header_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db, _FakeCompanion()))
    r = await client.get("/napi/servicebay/services", headers={"Remote-User": "mdopp"})
    assert r.status == 401


# ---- /napi/servicebay/* reads: aggregate SB's body verbatim -----------------


async def test_napi_servicebay_reads_return_sb_body(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    bodies = {
        "home": {
            "servicesUp": 5,
            "servicesFailed": 0,
            "servicesDown": 1,
            "pendingApprovals": 2,
            "pendingUpdates": 3,
        },
        "approvals": {"approvals": [{"id": "a1", "status": "pending"}]},
        "services": {"services": [{"name": "solaris-chat", "health": "healthy"}]},
        "upgrades": {
            "upgrades": [
                {
                    "name": "media",
                    "kind": "template",
                    "current": "v1",
                    "available": "v2",
                }
            ]
        },
    }
    companion = _FakeCompanion(bodies=bodies)
    client = await aiohttp_client(_app(tmp_path, db, companion))
    hdr = {"Authorization": f"Bearer {token}"}
    for key, body in bodies.items():
        r = await client.get(f"/napi/servicebay/{key}", headers=hdr)
        assert r.status == 200
        assert (await r.json()) == body
    # Each read went through the companion once, keyed by the path segment.
    assert companion.calls == ["home", "approvals", "services", "upgrades"]


async def test_napi_servicebay_unknown_key_is_404(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    companion = _FakeCompanion()
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/napi/servicebay/secrets", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 404
    # An unknown key must never reach ServiceBay.
    assert companion.calls == []


async def test_napi_servicebay_unreachable_sb_is_502(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    # read() returns None → ServiceBay unreachable / non-200 / malformed.
    companion = _FakeCompanion(bodies={})
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/napi/servicebay/home", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 502
    assert (await r.json()) == {"ok": False, "error": "servicebay_unavailable"}


async def test_napi_servicebay_unconfigured_is_503(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    companion = _FakeCompanion(enabled=False)
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/napi/servicebay/home", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 503
    assert (await r.json()) == {"ok": False, "error": "servicebay_unconfigured"}


# ---- /napi/servicebay/services/{name}/operate: lifecycle mutation, #827 ------
#
# The operate half of #827: start/stop/restart a service. Same fail-closed
# device-token surface as the reads (native()); an action outside the allowed
# set never reaches ServiceBay, and an SB failure surfaces as 502.


async def test_napi_operate_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    companion = _FakeCompanion()
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.post(
        "/napi/servicebay/services/solaris-chat/operate", json={"action": "restart"}
    )
    assert r.status == 401
    # Fail-closed: an unauthenticated caller never reaches ServiceBay.
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}
    assert companion.operations == []


async def test_napi_operate_valid_actions_proxy_to_sb(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    companion = _FakeCompanion(operate=(True, "ok"))
    client = await aiohttp_client(_app(tmp_path, db, companion))
    hdr = {"Authorization": f"Bearer {token}"}
    for action in ("start", "stop", "restart"):
        r = await client.post(
            "/napi/servicebay/services/solaris-chat/operate",
            json={"action": action},
            headers=hdr,
        )
        assert r.status == 200
        assert (await r.json()) == {
            "ok": True,
            "name": "solaris-chat",
            "action": action,
        }
    assert companion.operations == [
        ("solaris-chat", "start"),
        ("solaris-chat", "stop"),
        ("solaris-chat", "restart"),
    ]


async def test_napi_operate_bad_action_is_400(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    companion = _FakeCompanion()
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.post(
        "/napi/servicebay/services/solaris-chat/operate",
        json={"action": "delete"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 400
    assert (await r.json()) == {"ok": False, "error": "bad_action"}
    # A rejected action must never reach ServiceBay.
    assert companion.operations == []


async def test_napi_operate_sb_failure_is_502(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    companion = _FakeCompanion(operate=(False, "HTTP 500: boom"))
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.post(
        "/napi/servicebay/services/solaris-chat/operate",
        json={"action": "restart"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 502
    body = await r.json()
    assert body["ok"] is False
    assert body["error"] == "servicebay_unavailable"
    assert companion.operations == [("solaris-chat", "restart")]


async def test_napi_operate_unconfigured_is_503(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    companion = _FakeCompanion(enabled=False)
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.post(
        "/napi/servicebay/services/solaris-chat/operate",
        json={"action": "restart"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 503
    assert (await r.json())["error"] == "servicebay_unconfigured"


# ---- event bridge: SB NewApprovalEvent → event bus → /napi/portal/events ----


def test_to_bus_event_maps_new_approval_and_drops_keepalives():
    ev = _to_bus_event(
        {
            "type": "new-approval",
            "id": "req-1",
            "kind": "media",
            "summary": "Enable providers",
            "created_at": "t0",
        }
    )
    assert ev == {"id": "req-1", "kind": "media", "summary": "Enable providers"}
    # Keep-alive / handshake / malformed frames produce nothing.
    assert _to_bus_event({"type": "connected"}) is None
    assert _to_bus_event({"type": "ping"}) is None
    assert _to_bus_event({"type": "new-approval"}) is None  # no id
    assert _to_bus_event({}) is None


async def test_bridge_republishes_sb_event_onto_bus(monkeypatch, tmp_path):
    """An SB new-approval frame from the SSE lands on the event bus scoped to the
    Wartung uid, so it reaches /napi/portal/events."""
    bus = EventBus()
    bridge = SbApprovalEventBridge(
        "http://sb:5888",
        str(tmp_path / "read"),
        str(tmp_path / "tok"),
        bus,
        "household",
    )

    class _FakeContent:
        def __init__(self, lines):
            self._lines = lines

        def __aiter__(self):
            async def gen():
                for line in self._lines:
                    yield line

            return gen()

    class _FakeResp:
        status = 200

        def __init__(self, lines):
            self.content = _FakeContent(lines)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, headers=None):
            return _FakeResp(
                [
                    b'data: {"type":"connected"}\n',
                    b'data: {"type":"new-approval","id":"req-9",'
                    b'"kind":"media","summary":"Enable providers","created_at":"t"}\n',
                    b'data: {"type":"ping"}\n',
                ]
            )

    monkeypatch.setattr(
        "solaris_chat.engine.sb_events.aiohttp.ClientSession", _FakeSession
    )

    # Subscribe as the app would over /napi/portal/events (the household stream).
    received: list[dict] = []

    async def _drain():
        async for event in bus.subscribe("household"):
            received.append(event)
            break

    drain = asyncio.ensure_future(_drain())
    await asyncio.sleep(0.01)
    await bridge._consume_once()
    await asyncio.wait_for(drain, 2)

    assert received == [
        {
            "kind": "servicebay",
            "data": {"id": "req-9", "kind": "media", "summary": "Enable providers"},
        }
    ]


async def _bridge_auth(monkeypatch, base, read_path, mcp_path) -> str | None:
    """Run one bridge consume against a capturing fake SSE and return the Bearer
    the bridge sent (which token file won)."""
    seen: dict[str, str | None] = {}

    class _EmptyContent:
        def __aiter__(self):
            async def gen():
                return
                yield  # pragma: no cover — makes this an async generator

            return gen()

    class _Resp:
        status = 200
        content = _EmptyContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, headers=None):
            seen["auth"] = (headers or {}).get("Authorization")
            return _Resp()

    monkeypatch.setattr("solaris_chat.engine.sb_events.aiohttp.ClientSession", _Session)
    bridge = SbApprovalEventBridge(base, read_path, mcp_path, EventBus(), "household")
    await bridge._consume_once()
    return seen.get("auth")


async def test_bridge_uses_read_token_when_present(monkeypatch, tmp_path):
    read_path = tmp_path / "read"
    mcp_path = tmp_path / "mcp"
    read_path.write_text("sb_read_TOKEN")
    mcp_path.write_text("sb_admin_TOKEN")
    auth = await _bridge_auth(monkeypatch, "http://sb", str(read_path), str(mcp_path))
    assert auth == "Bearer sb_read_TOKEN"


async def test_bridge_falls_back_to_mcp_token_when_read_absent(monkeypatch, tmp_path):
    mcp_path = tmp_path / "mcp"
    mcp_path.write_text("sb_admin_TOKEN")
    auth = await _bridge_auth(
        monkeypatch, "http://sb", str(tmp_path / "missing"), str(mcp_path)
    )
    assert auth == "Bearer sb_admin_TOKEN"


# ---- /api/servicebay/approvals/{id}/{approve,reject}: Authelia admin gate ----
#
# Owner-chosen option (i): the verdict rides the Authelia forward-auth /api/
# surface (trusted Remote-User/Remote-Groups), NOT the proxy-bypassed /napi/
# device-token surface. is_admin() is trustworthy here.


async def test_api_verdict_forbidden_for_non_admin(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    companion = _FakeCompanion()
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.post("/api/servicebay/approvals/req-1/approve")
    assert r.status == 403
    assert (await r.json())["reason"] == "forbidden"
    # A non-admin caller never reaches ServiceBay.
    assert companion.verdicts == []


async def test_api_verdict_forbidden_for_non_admin_group(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    companion = _FakeCompanion()
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.post(
        "/api/servicebay/approvals/req-1/approve",
        headers={"Remote-User": "bob", "Remote-Groups": "users"},
    )
    assert r.status == 403
    assert companion.verdicts == []


async def test_api_verdict_admin_approve_forwards_identity(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    companion = _FakeCompanion(verdict=(True, "ok"))
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.post(
        "/api/servicebay/approvals/req-42/approve",
        headers={
            "Remote-User": "michael",
            "Remote-Groups": "admins,users",
            "Cookie": "authelia_session=sess-abc",
        },
    )
    assert r.status == 200
    body = await r.json()
    assert body == {"ok": True, "approval_id": "req-42", "detail": "ok"}
    # The verdict ran with the acting admin's forward-auth identity pinned AND the
    # admin's authelia_session cookie forwarded, so the companion's #2285 mint can
    # act as that verified admin against ServiceBay's www portal.
    assert companion.verdicts == [
        ("req-42", "approve", ("michael", "admins,users"), "sess-abc")
    ]


async def test_api_verdict_admin_reject_path(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    companion = _FakeCompanion(verdict=(True, "denied"))
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.post(
        "/api/servicebay/approvals/req-7/reject",
        headers={
            "Remote-User": "michael",
            "Remote-Groups": "admins",
            "Cookie": "authelia_session=sess-xyz",
        },
    )
    assert r.status == 200
    assert (await r.json())["ok"] is True
    assert companion.verdicts == [
        ("req-7", "reject", ("michael", "admins"), "sess-xyz")
    ]


async def test_api_verdict_without_cookie_forwards_empty(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    companion = _FakeCompanion(verdict=(False, "no_authelia_cookie"))
    client = await aiohttp_client(_app(tmp_path, db, companion))
    # An admin request with no authelia_session cookie (shouldn't happen on the
    # Authelia /api surface) → the handler forwards "" and the mint fails cleanly.
    r = await client.post(
        "/api/servicebay/approvals/req-1/approve",
        headers={"Remote-User": "michael", "Remote-Groups": "admins"},
    )
    assert r.status == 502
    assert companion.verdicts == [("req-1", "approve", ("michael", "admins"), "")]


async def test_api_verdict_reports_sb_failure_as_502(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    companion = _FakeCompanion(verdict=(False, "HTTP 403: bad_window"))
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.post(
        "/api/servicebay/approvals/req-9/approve",
        headers={"Remote-User": "michael", "Remote-Groups": "admins"},
    )
    assert r.status == 502
    assert (await r.json())["ok"] is False


async def test_api_verdict_unconfigured_is_503(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    companion = _FakeCompanion(enabled=False)
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.post(
        "/api/servicebay/approvals/req-1/approve",
        headers={"Remote-User": "michael", "Remote-Groups": "admins"},
    )
    assert r.status == 503
    assert (await r.json())["reason"] == "servicebay_unconfigured"


# ---- SbCompanionClient.submit_verdict: mint → verdict, no standing key -------


class _MintVerdictSession:
    """Records the mint POST and the verdict POST so a test can assert the
    #2276 flow: mint from the admin session, then present the assertion +
    service-token to SB's verdict route. NO standing delegation key is used."""

    def __init__(self, calls, mint_status=200, verdict_status=200):
        self._calls = calls
        self._mint_status = mint_status
        self._verdict_status = verdict_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        self._calls.append({"url": url, "headers": headers, "json": json})
        outer = self

        class _Resp:
            status = (
                outer._mint_status
                if "delegated-admin" in url
                else outer._verdict_status
            )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self):
                return {"assertion": "ASSERT", "header": "X-SB-Delegated-Admin"}

            async def text(self):
                return '{"ok":true}'

        return _Resp()


async def test_client_submit_verdict_mints_then_posts_with_assertion(
    monkeypatch, tmp_path
):
    from solaris_chat.engine import sb_companion as mod
    from solaris_chat.engine.client import current_admin_identity

    tok = tmp_path / "sbtok"
    tok.write_text("service-tok")
    calls: list[dict] = []

    def _factory(*a, **k):
        return _MintVerdictSession(calls)

    monkeypatch.setattr(mod.aiohttp, "ClientSession", _factory)
    current_admin_identity.set(("michael", "admins"))

    client = mod.SbCompanionClient("http://sb:5888", str(tok))
    ok, detail = await client.submit_verdict("req-42", "approve", "sess-abc")
    assert ok is True

    mint, verdict = calls
    # 1) Mint from the ACTING admin's forwarded authelia_session cookie — NO Bearer
    # (the mint refuses a token caller), action+target bound to this approval.
    assert mint["url"].endswith("/api/auth/delegated-admin-from-authelia-session")
    assert mint["headers"] == {"Cookie": "authelia_session=sess-abc"}
    assert "Authorization" not in mint["headers"]
    assert mint["json"] == {"action": "approvals.approve", "target": "req-42"}
    # 2) Verdict presents the service-token Bearer PLUS the minted assertion; SB
    # uses "deny" (not "reject"). No standing delegation key anywhere.
    assert verdict["url"] == "http://sb:5888/napi/approvals/req-42/approve"
    assert verdict["headers"]["Authorization"] == "Bearer service-tok"
    assert verdict["headers"]["X-SB-Delegated-Admin"] == "ASSERT"


async def test_client_submit_verdict_reject_maps_to_deny(monkeypatch, tmp_path):
    from solaris_chat.engine import sb_companion as mod
    from solaris_chat.engine.client import current_admin_identity

    tok = tmp_path / "sbtok"
    tok.write_text("service-tok")
    calls: list[dict] = []
    monkeypatch.setattr(
        mod.aiohttp, "ClientSession", lambda *a, **k: _MintVerdictSession(calls)
    )
    current_admin_identity.set(("michael", "admins"))

    client = mod.SbCompanionClient("http://sb:5888", str(tok))
    ok, _ = await client.submit_verdict("req-7", "reject", "sess-abc")
    assert ok is True
    assert calls[0]["json"] == {"action": "approvals.deny", "target": "req-7"}
    # SB's verdict verb is "deny" for a reject.
    assert calls[1]["url"] == "http://sb:5888/napi/approvals/req-7/deny"


# ---- #811 last mile / servicebay#2285: mint via www portal, forward cookie ---
# The delegated-admin mint is a no-Bearer forward-auth POST that carries the
# admin's authelia_session cookie; it 403s on the loopback :5888 path (no CSRF-
# exempt X-SB-Internal-Token) and 401s on the bare apex (Authelia default-deny).
# It passes only through the *.dopp.cloud NPM portal host (www), where forward-
# auth validates the cookie and injects the internal token. So ONLY the mint
# targets sb_mint_url; the verdict (Bearer → CSRF-exempt) stays on the loopback.


async def test_client_mint_targets_www_portal_verdict_stays_on_api_base(
    monkeypatch, tmp_path
):
    from solaris_chat.engine import sb_companion as mod
    from solaris_chat.engine.client import current_admin_identity

    tok = tmp_path / "sbtok"
    tok.write_text("service-tok")
    calls: list[dict] = []
    monkeypatch.setattr(
        mod.aiohttp, "ClientSession", lambda *a, **k: _MintVerdictSession(calls)
    )
    current_admin_identity.set(("michael", "admins"))

    client = mod.SbCompanionClient(
        "http://127.0.0.1:5888", str(tok), sb_mint_url="https://www.dopp.cloud"
    )
    ok, _ = await client.submit_verdict("req-42", "approve", "sess-abc")
    assert ok is True

    mint, verdict = calls
    # The mint hits the www portal host (Authelia-validated, X-SB-Internal-Token
    # injected) and forwards the admin's authelia_session cookie verbatim.
    assert (
        mint["url"]
        == "https://www.dopp.cloud/api/auth/delegated-admin-from-authelia-session"
    )
    assert mint["headers"]["Cookie"] == "authelia_session=sess-abc"
    # The verdict (Bearer) stays on the loopback control-plane base.
    assert verdict["url"] == "http://127.0.0.1:5888/napi/approvals/req-42/approve"


async def test_client_mint_falls_back_to_api_base_when_mint_url_empty(
    monkeypatch, tmp_path
):
    from solaris_chat.engine import sb_companion as mod
    from solaris_chat.engine.client import current_admin_identity

    tok = tmp_path / "sbtok"
    tok.write_text("service-tok")
    calls: list[dict] = []
    monkeypatch.setattr(
        mod.aiohttp, "ClientSession", lambda *a, **k: _MintVerdictSession(calls)
    )
    current_admin_identity.set(("michael", "admins"))

    # No mint URL (LAN/no-portal deploy) → mint falls back to the loopback base.
    client = mod.SbCompanionClient("http://127.0.0.1:5888", str(tok))
    ok, _ = await client.submit_verdict("req-9", "approve", "sess-abc")
    assert ok is True
    assert (
        calls[0]["url"]
        == "http://127.0.0.1:5888/api/auth/delegated-admin-from-authelia-session"
    )


async def test_client_submit_verdict_no_admin_identity_fails_closed(
    monkeypatch, tmp_path
):
    from solaris_chat.engine import sb_companion as mod
    from solaris_chat.engine.client import current_admin_identity

    calls: list[dict] = []
    monkeypatch.setattr(
        mod.aiohttp, "ClientSession", lambda *a, **k: _MintVerdictSession(calls)
    )
    # No verified admin identity → no mint attempted, fail closed.
    current_admin_identity.set(("", ""))
    client = mod.SbCompanionClient("http://sb:5888", str(tmp_path / "t"))
    ok, detail = await client.submit_verdict("req-1", "approve", "sess-abc")
    assert ok is False
    assert detail == "no_admin_identity"
    assert calls == []


async def test_client_submit_verdict_no_cookie_fails_closed(monkeypatch, tmp_path):
    from solaris_chat.engine import sb_companion as mod
    from solaris_chat.engine.client import current_admin_identity

    calls: list[dict] = []
    monkeypatch.setattr(
        mod.aiohttp, "ClientSession", lambda *a, **k: _MintVerdictSession(calls)
    )
    # Verified admin but no authelia_session cookie to forward → no mint, fail
    # closed (there is nothing to authenticate the mint with).
    current_admin_identity.set(("michael", "admins"))
    client = mod.SbCompanionClient("http://sb:5888", str(tmp_path / "t"))
    ok, detail = await client.submit_verdict("req-1", "approve", "")
    assert ok is False
    assert detail == "no_authelia_cookie"
    assert calls == []


async def test_client_submit_verdict_mint_refusal_fails_closed(monkeypatch, tmp_path):
    from solaris_chat.engine import sb_companion as mod
    from solaris_chat.engine.client import current_admin_identity

    tok = tmp_path / "sbtok"
    tok.write_text("service-tok")
    calls: list[dict] = []
    # Mint 403s (e.g. a Bearer leaked in) → never reach the verdict route.
    monkeypatch.setattr(
        mod.aiohttp,
        "ClientSession",
        lambda *a, **k: _MintVerdictSession(calls, mint_status=403),
    )
    current_admin_identity.set(("michael", "admins"))
    client = mod.SbCompanionClient("http://sb:5888", str(tok))
    ok, detail = await client.submit_verdict("req-1", "approve", "sess-abc")
    assert ok is False
    assert detail == "mint_failed"
    # Only the mint was attempted; no verdict posted without an assertion.
    assert len(calls) == 1


# ---- SbCompanionClient.operate: POST to SB with the lifecycle-scoped token ----


class _OperateSession:
    """Records the operate POST so a test can assert the URL, the lifecycle
    SB-MCP Bearer, and the {action} body."""

    def __init__(self, calls, status=200):
        self._calls = calls
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        self._calls.append({"url": url, "headers": headers, "json": json})
        outer = self

        class _Resp:
            status = outer._status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def text(self):
                return '{"ok":true}'

        return _Resp()


async def test_client_operate_posts_to_sb_with_lifecycle_token(monkeypatch, tmp_path):
    from solaris_chat.engine import sb_companion as mod

    tok = tmp_path / "sbtok"
    tok.write_text("service-tok")
    calls: list[dict] = []
    monkeypatch.setattr(
        mod.aiohttp, "ClientSession", lambda *a, **k: _OperateSession(calls)
    )

    client = mod.SbCompanionClient("http://sb:5888", str(tok))
    ok, _ = await client.operate("solaris-chat", "restart")
    assert ok is True
    (call,) = calls
    # The SAME read+lifecycle+mutate SB-MCP token the reads use — no new credential;
    # SB's lifecycle scope is what authorises the operate.
    assert call["url"] == "http://sb:5888/napi/services/solaris-chat/operate"
    assert call["headers"]["Authorization"] == "Bearer service-tok"
    assert call["json"] == {"action": "restart"}


async def test_client_operate_bad_action_never_calls_sb(monkeypatch, tmp_path):
    from solaris_chat.engine import sb_companion as mod

    tok = tmp_path / "sbtok"
    tok.write_text("service-tok")
    calls: list[dict] = []
    monkeypatch.setattr(
        mod.aiohttp, "ClientSession", lambda *a, **k: _OperateSession(calls)
    )

    client = mod.SbCompanionClient("http://sb:5888", str(tok))
    ok, detail = await client.operate("solaris-chat", "delete")
    assert ok is False
    assert detail == "bad_action"
    # An out-of-set action is rejected before any network call.
    assert calls == []


async def test_client_operate_surfaces_sb_error(monkeypatch, tmp_path):
    from solaris_chat.engine import sb_companion as mod

    tok = tmp_path / "sbtok"
    tok.write_text("service-tok")
    calls: list[dict] = []
    monkeypatch.setattr(
        mod.aiohttp,
        "ClientSession",
        lambda *a, **k: _OperateSession(calls, status=500),
    )

    client = mod.SbCompanionClient("http://sb:5888", str(tok))
    ok, detail = await client.operate("solaris-chat", "stop")
    assert ok is False
    assert detail.startswith("HTTP 500")


# ---- SbCompanionClient token selection: reads use the read token, #838 -------


class _ReadSession:
    """Records the read GET so a test can assert which token file won."""

    def __init__(self, seen, status=200):
        self._seen = seen
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, headers=None):
        self._seen["auth"] = (headers or {}).get("Authorization")
        outer = self

        class _Resp:
            status = outer._status

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self):
                return {"ok": True}

        return _Resp()


async def test_client_read_uses_read_token_when_present(monkeypatch, tmp_path):
    from solaris_chat.engine import sb_companion as mod

    read_path = tmp_path / "read"
    mcp_path = tmp_path / "mcp"
    read_path.write_text("sb_read_TOKEN")
    mcp_path.write_text("sb_admin_TOKEN")
    seen: dict[str, str | None] = {}
    monkeypatch.setattr(
        mod.aiohttp, "ClientSession", lambda *a, **k: _ReadSession(seen)
    )

    client = mod.SbCompanionClient(
        "http://sb:5888", str(mcp_path), sb_read_token_path=str(read_path)
    )
    assert await client.read("home") == {"ok": True}
    assert seen["auth"] == "Bearer sb_read_TOKEN"


async def test_client_read_falls_back_to_mcp_token_when_read_absent(
    monkeypatch, tmp_path
):
    from solaris_chat.engine import sb_companion as mod

    mcp_path = tmp_path / "mcp"
    mcp_path.write_text("sb_admin_TOKEN")
    seen: dict[str, str | None] = {}
    monkeypatch.setattr(
        mod.aiohttp, "ClientSession", lambda *a, **k: _ReadSession(seen)
    )

    client = mod.SbCompanionClient(
        "http://sb:5888", str(mcp_path), sb_read_token_path=str(tmp_path / "missing")
    )
    assert await client.read("home") == {"ok": True}
    assert seen["auth"] == "Bearer sb_admin_TOKEN"


# ---- GET /api/servicebay/approvals/{id}: what the verdict page shows (#1085) ----
#
# The companion may not decide an approval with its device token
# (servicebay#2249), so it opens `#/p/servicebay/approvals/<id>` in a Custom Tab
# carrying the Authelia cookies. That page reads this route before it offers the
# Approve/Reject buttons.

_PENDING = {
    "approvals": [
        {
            "id": "req-42",
            "service": "mcp",
            "title": "one-shot exec token",
            "description": "An MCP agent asked for a one-shot exec token.",
            "payload": {"tokenRequestId": "tok-99", "caller": "token:some-agent"},
            "on_approve": {"mintToken": {"tokenRequestId": "tok-99"}},
            "node": "Local",
            "created_at": "2026-07-28T01:12:32.377Z",
            "status": "pending",
        }
    ]
}


async def test_api_approval_detail_forbidden_for_non_admin(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    companion = _FakeCompanion(bodies={"approvals": _PENDING})
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get("/api/servicebay/approvals/req-42")
    assert r.status == 403
    # Whoever may not decide never even learns what was requested.
    assert companion.calls == []


async def test_api_approval_detail_forbidden_for_non_admin_group(
    aiohttp_client, tmp_path
):
    db = _db(tmp_path)
    companion = _FakeCompanion(bodies={"approvals": _PENDING})
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/api/servicebay/approvals/req-42",
        headers={"Remote-User": "bob", "Remote-Groups": "users"},
    )
    assert r.status == 403
    assert companion.calls == []


async def test_api_approval_detail_serves_the_pending_request(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    companion = _FakeCompanion(bodies={"approvals": _PENDING})
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/api/servicebay/approvals/req-42",
        headers={"Remote-User": "michael", "Remote-Groups": "admins"},
    )
    assert r.status == 200
    assert (await r.json()) == {
        "ok": True,
        "approval": {
            "id": "req-42",
            "service": "mcp",
            "title": "one-shot exec token",
            "description": "An MCP agent asked for a one-shot exec token.",
            "node": "Local",
            "created_at": "2026-07-28T01:12:32.377Z",
        },
    }


async def test_api_approval_detail_omits_the_operation_mechanics(
    aiohttp_client, tmp_path
):
    """`payload`/`on_approve` carry token-request ids and tool args. The page
    renders none of it, so it has no business in the browser."""
    db = _db(tmp_path)
    companion = _FakeCompanion(bodies={"approvals": _PENDING})
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/api/servicebay/approvals/req-42",
        headers={"Remote-User": "michael", "Remote-Groups": "admins"},
    )
    raw = await r.text()
    assert "tok-99" not in raw
    assert "payload" not in raw
    assert "on_approve" not in raw


async def test_api_approval_detail_unknown_id_is_gone_not_found(
    aiohttp_client, tmp_path
):
    """SB's companion feed lists only pending requests, so an id that is absent
    was almost certainly just decided elsewhere — say that, don't cry 404-broken."""
    db = _db(tmp_path)
    companion = _FakeCompanion(bodies={"approvals": _PENDING})
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/api/servicebay/approvals/req-does-not-exist",
        headers={"Remote-User": "michael", "Remote-Groups": "admins"},
    )
    assert r.status == 404
    assert (await r.json())["reason"] == "gone"


async def test_api_approval_detail_unreachable_servicebay_is_502(
    aiohttp_client, tmp_path
):
    db = _db(tmp_path)
    companion = _FakeCompanion(bodies={})  # read() returns None
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/api/servicebay/approvals/req-42",
        headers={"Remote-User": "michael", "Remote-Groups": "admins"},
    )
    assert r.status == 502
    assert (await r.json())["reason"] == "servicebay_unavailable"


async def test_api_approval_detail_without_companion_is_503(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db, None))
    r = await client.get(
        "/api/servicebay/approvals/req-42",
        headers={"Remote-User": "michael", "Remote-Groups": "admins"},
    )
    assert r.status == 503


async def test_api_approval_detail_does_not_shadow_the_verdict_route(
    aiohttp_client, tmp_path
):
    """`/{id}` and `/{id}/{verb}` are neighbours; a GET on the detail route must
    not swallow the POST verdict, and the verdict must stay POST-only."""
    db = _db(tmp_path)
    companion = _FakeCompanion(bodies={"approvals": _PENDING}, verdict=(True, "ok"))
    client = await aiohttp_client(_app(tmp_path, db, companion))
    admin = {"Remote-User": "michael", "Remote-Groups": "admins"}
    assert (
        await client.get("/api/servicebay/approvals/req-42", headers=admin)
    ).status == 200
    assert companion.verdicts == []
    r = await client.post("/api/servicebay/approvals/req-42/approve", headers=admin)
    assert r.status == 200
    assert companion.verdicts[0][:2] == ("req-42", "approve")
    # A GET on the verdict path is not a way to mutate anything.
    assert (
        await client.get("/api/servicebay/approvals/req-42/approve", headers=admin)
    ).status == 405


# ---- The verdict page's markup contract (#1085) ----------------------------
#
# `static/index.html` is plain HTML/JS with no test harness in this repo — the
# rendering and the taps are browser checks. What IS locked down here is the
# contract the Android app codes against: the route it deep-links to, and the
# fact that approving asks first.

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_approval_page_route_is_the_one_the_app_deep_links_to():
    """mdopp/solaris-android replaces `PwaLauncher.Routes.SERVICEBAY` with this.
    Renaming it here silently breaks every notification tap in the app."""
    assert '"servicebay/approvals/"' in _HTML
    assert "openApprovalPage" in _HTML


def test_approval_page_reads_the_detail_route_before_offering_buttons():
    assert '"/api/servicebay/approvals/" + encodeURIComponent(approvalId)' in _HTML


def test_approving_asks_first():
    """Approve makes ServiceBay actually run the requested operation. Reject
    only cancels, so it is deliberately NOT gated."""
    approve_gate = 'verb === "approve" && !window.confirm('
    assert approve_gate in _HTML


async def test_bookmarkable_p_route_does_not_cover_the_approval_path(
    aiohttp_client, tmp_path
):
    """Why the app deep-links `#/p/…` and not `/p/…`: the server's bookmarkable
    shell route matches ONE path segment, so the three-segment approval path
    never reaches the SPA. The hash form is handled entirely client-side."""
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db, _FakeCompanion()))
    assert (await client.get("/p/start")).status == 200
    assert (await client.get("/p/servicebay/approvals/req-42")).status == 404
