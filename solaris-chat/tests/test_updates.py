"""ServiceBay update poller → Wartung update-cards (Wartung P2b, #788).

Covers: `mark_seen` dedupe (a pending update cards once, a further version cards
again); `poll_once` injects an update action-card into the Wartung chat per NEW
pending update and dedupes across ticks (canned SB payloads via a fake `_get`);
the [Deploy] handler is registered admin=True AND destructive=True, so the
`/api/action-callback` endpoint refuses a non-admin caller and confirm-gates a
bare admin tap before install_template ever runs. Tables are raw SQL (a chat test
must NOT import alembic — CI runs solaris-chat clean).
"""

from __future__ import annotations

import sqlite3

from solaris_chat.engine import action_cards, store, updates
from solaris_chat.engine.notify import EventBus
from solaris_chat.engine.updates import UpdatePoller, mark_seen
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
CREATE TABLE wartung_seen_updates (
  update_id TEXT PRIMARY KEY,
  seen_at   TEXT NOT NULL DEFAULT (datetime('now'))
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
    assert mark_seen(db, "image:immich:sha256:aaa") is True
    # Same pending update the next tick — already carded, no re-card.
    assert mark_seen(db, "image:immich:sha256:aaa") is False
    # A FURTHER update (new digest) is a new id → cards again.
    assert mark_seen(db, "image:immich:sha256:bbb") is True


def test_mark_seen_degrades_to_new_without_table(tmp_path):
    # No table (schema-init hasn't run) ⇒ treat as new rather than crash.
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    assert mark_seen(path, "template:adguard:5") is True


# ---- identity extraction ---------------------------------------------------


def test_image_updates_skips_unavailable_and_missing_digest():
    services = [
        {"service": "immich", "registryDigest": "sha256:x", "updateAvailable": True},
        {"service": "media", "registryDigest": "sha256:y", "updateAvailable": False},
        {"service": "nginx", "registryDigest": None, "updateAvailable": True},
    ]
    ids = [uid for uid, _label in updates._image_updates(services)]
    assert ids == ["image:immich:sha256:x"]


def test_template_upgrades_keys_on_current_version():
    pending = [
        {"name": "adguard", "currentVersion": 5, "hasBreakingChange": True},
        {"name": "file-share", "currentVersion": 3, "hasBreakingChange": False},
        {"name": "", "currentVersion": 9},
    ]
    ids = [uid for uid, _label in updates._template_upgrades(pending)]
    assert ids == ["template:adguard:5", "template:file-share:3"]


# ---- poll_once → card injection --------------------------------------------


class _FakePoller(UpdatePoller):
    """UpdatePoller with the two HTTP GETs stubbed by canned payloads."""

    def __init__(self, *a, image=None, template=None, **kw):
        super().__init__(*a, **kw)
        self._image = image or {"services": []}
        self._template = template or {"pending": []}

    async def _get(self, url, headers):
        return self._image if url.endswith(updates._IMAGE_PATH) else self._template


async def test_poll_once_cards_new_updates_into_wartung(tmp_path):
    db = _db(tmp_path)
    bus = EventBus()
    poller = _FakePoller(
        db,
        "http://sb",
        str(tmp_path / "read-token"),
        str(tmp_path / "token"),
        bus,
        "household",
        image={
            "services": [
                {
                    "service": "immich",
                    "registryDigest": "sha256:new",
                    "updateAvailable": True,
                }
            ]
        },
        template={
            "pending": [{"name": "adguard", "currentVersion": 7}],
        },
    )
    injected = await poller.poll_once()
    assert injected == 2  # one image, one template

    sid = store.wartung_session_id("household")
    hist = store.history(db, sid)
    assert len(hist) == 2
    assert all(m["role"] == "assistant" for m in hist)
    assert "immich" in hist[0]["content"]

    # A second identical tick cards nothing (deduped).
    assert await poller.poll_once() == 0


async def test_poll_once_no_updates_is_quiet(tmp_path):
    db = _db(tmp_path)
    poller = _FakePoller(
        db,
        "http://sb",
        str(tmp_path / "r"),
        str(tmp_path / "t"),
        EventBus(),
        "household",
    )
    assert await poller.poll_once() == 0


async def test_poll_once_carding_pushes_when_backgrounded(tmp_path):
    db = _db(tmp_path)
    pushes: list = []

    class _Notifier:
        async def push(self, uid, title, body, data):
            pushes.append((uid, body))

    poller = _FakePoller(
        db,
        "http://sb",
        str(tmp_path / "r"),
        str(tmp_path / "t"),
        EventBus(),  # nobody watching → backgrounded
        "household",
        notifier=_Notifier(),
        template={"pending": [{"name": "adguard", "currentVersion": 3}]},
    )
    assert await poller.poll_once() == 1
    assert len(pushes) == 1  # phone push for the new card
    assert pushes[0][0] == "household"


# ---- token selection: read-token with fallback (#818) ----------------------


class _CapturePoller(UpdatePoller):
    """Capture the Bearer the poll sends, so we can assert which token file won."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen_auth: str | None = None

    async def _get(self, url, headers):
        self.seen_auth = headers.get("Authorization")
        return (
            {"services": []} if url.endswith(updates._IMAGE_PATH) else {"pending": []}
        )


async def test_poll_uses_read_token_when_present(tmp_path):
    db = _db(tmp_path)
    read_path = tmp_path / "read"
    mcp_path = tmp_path / "mcp"
    read_path.write_text("sb_read_TOKEN")
    mcp_path.write_text("sb_admin_TOKEN")
    poller = _CapturePoller(
        db, "http://sb", str(read_path), str(mcp_path), EventBus(), "household"
    )
    await poller.poll_once()
    assert poller.seen_auth == "Bearer sb_read_TOKEN"


async def test_poll_falls_back_to_mcp_token_when_read_absent(tmp_path):
    db = _db(tmp_path)
    mcp_path = tmp_path / "mcp"
    mcp_path.write_text("sb_admin_TOKEN")
    poller = _CapturePoller(
        db,
        "http://sb",
        str(tmp_path / "missing-read"),
        str(mcp_path),
        EventBus(),
        "household",
    )
    await poller.poll_once()
    assert poller.seen_auth == "Bearer sb_admin_TOKEN"


# ---- [Deploy] handler registration + gate ----------------------------------


def _app(tmp_path):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(tmp_path),
    )


def test_deploy_action_registered_admin_and_destructive(tmp_path):
    # Registration happens in build_app; confirm the policy on the id.
    _app(tmp_path)
    handler = action_cards.get(updates.DEPLOY_ACTION)
    assert handler is not None
    assert handler.admin is True
    assert handler.destructive is True


async def test_deploy_forbidden_for_non_admin(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/action-callback",
        json={"action_id": updates.DEPLOY_ACTION, "params": {"service": "immich"}},
    )
    assert r.status == 403
    assert (await r.json())["reason"] == "forbidden"


async def test_deploy_confirm_gated_for_admin(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    # An admin bare tap is confirm-gated — install_template must not run.
    r = await client.post(
        "/api/action-callback",
        headers={"Remote-Groups": "admins"},
        json={"action_id": updates.DEPLOY_ACTION, "params": {"service": "immich"}},
    )
    assert r.status == 403
    assert (await r.json())["reason"] == "confirm_required"


async def test_deploy_confirmed_admin_reports_no_mcp(aiohttp_client, tmp_path):
    # No SB-MCP toolbox is wired in this test app, so a confirmed admin tap runs
    # the handler and reports no_mcp (rather than 403/404) — proving the gate let
    # it through only for a confirmed admin.
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/action-callback",
        headers={"Remote-Groups": "admins"},
        json={
            "action_id": updates.DEPLOY_ACTION,
            "confirmed": True,
            "params": {"service": "immich"},
        },
    )
    assert r.status == 200
    assert (await r.json())["reason"] == "no_mcp"
