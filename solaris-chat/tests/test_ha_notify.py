"""HA household notices — `POST /api/ha/notify` (#1276).

The automation says what happened; Solaris decides whose phone shows it. What
is pinned here: the payload is closed, a target resolves only to a resident
with a paired device, an unknown target reaches NOBODY, adding a device needs
no automation change, an undeliverable push neither errors nor delays the
caller, the loopback auth of the model lease (#1260) holds, and the endpoint
says out loud that it is not an alarm channel.

The two tables (migrations 0020/0021) are replayed with raw SQL — a chat test
must NOT import alembic (CI runs solaris-chat in a clean env without it).
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from solaris_chat import device_token_store, ha_notify, push_store
from solaris_chat.engine.notify import EventBus, Notifier
from solaris_chat.server import build_app

_SCHEMA = """
CREATE TABLE push_subscriptions (
  id         TEXT PRIMARY KEY,
  owner_uid  TEXT NOT NULL,
  endpoint   TEXT NOT NULL UNIQUE,
  p256dh     TEXT NOT NULL,
  auth       TEXT NOT NULL,
  user_agent TEXT NOT NULL DEFAULT '',
  created    TEXT NOT NULL DEFAULT (datetime('now')),
  last_ok    TEXT
);
CREATE TABLE device_tokens (
  id         TEXT PRIMARY KEY,
  owner_uid  TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  label      TEXT,
  created    TEXT NOT NULL DEFAULT (datetime('now')),
  last_used  TEXT,
  revoked    INTEGER NOT NULL DEFAULT 0
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


class _RecordingBus(EventBus):
    """The real bus, plus a log of what was published on it."""

    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, str, dict]] = []

    def publish(self, uid, kind, data):
        self.published.append((uid, kind, data))
        super().publish(uid, kind, data)


def _app(tmp_path, monkeypatch, *, default_uid: str = "household"):
    """The app plus (bus, sent-endpoints) — Web Push stubbed at the wire."""
    db = _db(tmp_path)
    sent: list[str] = []
    monkeypatch.setattr(
        Notifier, "_send_one", lambda self, sub, payload: sent.append(sub["endpoint"])
    )
    bus = _RecordingBus()
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid=default_uid,
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        event_bus=bus,
        notifier=Notifier(db, "vapid-pub", "vapid-priv"),
    )
    return app, db, bus, sent


async def _settle(sent: list[str], expected: int) -> None:
    """Wait briefly for the fire-and-forget fan-out to run."""
    for _ in range(50):
        if len(sent) >= expected:
            return
        await asyncio.sleep(0.01)


# ---- the payload is closed -------------------------------------------------


def test_payload_key_set_is_closed():
    assert ha_notify.PAYLOAD_KEYS == ("target", "title", "body", "urgency", "actions")
    parsed = ha_notify.parse_payload(
        {"target": "anna", "title": "Waschmaschine", "body": "fertig"}
    )
    assert parsed["target"] == "anna"
    assert parsed["urgency"] == ha_notify.DEFAULT_URGENCY
    assert parsed["actions"] == []
    # An unknown field is refused, not ignored — the contract with HA and with
    # the app cannot drift by accident.
    with pytest.raises(ValueError, match="unexpected_field"):
        ha_notify.parse_payload({"target": "anna", "title": "x", "device": "pixel"})


def test_payload_rejects_bad_values():
    with pytest.raises(ValueError, match="invalid_payload"):
        ha_notify.parse_payload(["anna"])
    with pytest.raises(ValueError, match="invalid_target"):
        ha_notify.parse_payload({"target": "  ", "title": "x"})
    with pytest.raises(ValueError, match="invalid_title"):
        ha_notify.parse_payload({"target": "anna", "title": ""})
    with pytest.raises(ValueError, match="invalid_urgency"):
        ha_notify.parse_payload({"target": "anna", "title": "x", "urgency": "alarm"})
    for bad in ("x", [{"action": "close", "title": ""}], [{"action": "close"}] * 9):
        with pytest.raises(ValueError, match="invalid_actions"):
            ha_notify.parse_payload({"target": "a", "title": "x", "actions": bad})


def test_actions_stay_on_the_apps_existing_action_path():
    # `{action,title}` and nothing else: the app maps this onto its
    # WidgetActionActivity path (confirm dialog + `sensitive_action` gate).
    assert ha_notify.ACTION_KEYS == ("action", "title")
    parsed = ha_notify.parse_payload(
        {
            "target": "anna",
            "title": "Tür offen",
            "actions": [{"action": "lock.front_door", "title": "Schließen"}],
        }
    )
    assert parsed["actions"] == [{"action": "lock.front_door", "title": "Schließen"}]


# ---- it is not an alarm channel, and says so -------------------------------


def test_the_endpoint_documents_that_it_is_not_an_alarm_channel():
    # In the code, not only in the ticket — otherwise somebody eventually hangs
    # a smoke alarm off a best-effort push path.
    assert "alarm" in ha_notify.NOT_AN_ALARM
    assert "NOT AN ALARM CHANNEL" in (ha_notify.__doc__ or "")
    # No urgency level promises delivery; the highest one is presentation.
    assert ha_notify.URGENCY_LEVELS == ("low", "normal", "high")


async def test_every_response_repeats_the_best_effort_caveat(
    aiohttp_client, tmp_path, monkeypatch
):
    app, db, _bus, _sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    client = await aiohttp_client(app)
    r = await client.post("/api/ha/notify", json={"target": "anna", "title": "Post da"})
    assert r.status == 202
    assert (await r.json())["delivery"] == ha_notify.NOT_AN_ALARM


# ---- fan-out ---------------------------------------------------------------


async def test_a_notice_reaches_every_paired_device_of_that_resident(
    aiohttp_client, tmp_path, monkeypatch
):
    app, db, bus, sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    push_store.upsert(db, "anna", "https://push/a2", "p", "a")
    push_store.upsert(db, "bernd", "https://push/b1", "p", "a")
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/ha/notify",
        json={"target": "anna", "title": "Waschmaschine", "body": "ist fertig"},
    )
    assert r.status == 202
    assert (await r.json())["residents"] == ["anna"]
    await _settle(sent, 2)
    # Both of Anna's phones, and nobody else's.
    assert sorted(sent) == ["https://push/a1", "https://push/a2"]
    assert bus.published == [
        (
            "anna",
            "ha",
            {
                "kind": "ha",
                "target": "anna",
                "title": "Waschmaschine",
                "body": "ist fertig",
                "urgency": "normal",
                "actions": [],
            },
        )
    ]


async def test_adding_a_device_changes_no_automation(
    aiohttp_client, tmp_path, monkeypatch
):
    app, db, _bus, sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    client = await aiohttp_client(app)
    body = {"target": "anna", "title": "Post da"}
    assert (await client.post("/api/ha/notify", json=body)).status == 202
    await _settle(sent, 1)
    assert sent == ["https://push/a1"]
    # A new phone is paired. The automation is untouched — it never named one.
    push_store.upsert(db, "anna", "https://push/a2", "p", "a")
    assert (await client.post("/api/ha/notify", json=body)).status == 202
    await _settle(sent, 3)
    assert sorted(sent[1:]) == ["https://push/a1", "https://push/a2"]


async def test_a_resident_known_only_by_a_paired_native_device_is_addressable(
    aiohttp_client, tmp_path, monkeypatch
):
    # The native app has no Web Push subscription (SSE only), so its device
    # token is what makes the resident an addressable target at all.
    app, db, bus, _sent = _app(tmp_path, monkeypatch)
    device_token_store.create(db, "carla", "Pixel")
    assert ha_notify.known_uids(db) == {"carla"}
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/ha/notify", json={"target": "Carla", "title": "Fenster offen"}
    )
    assert r.status == 202
    assert [uid for uid, _kind, _data in bus.published] == ["carla"]


async def test_the_household_group_publishes_once_on_the_shared_stream(
    aiohttp_client, tmp_path, monkeypatch
):
    app, db, bus, sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    push_store.upsert(db, "bernd", "https://push/b1", "p", "a")
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/ha/notify", json={"target": "household", "title": "Müll rausstellen"}
    )
    assert r.status == 202
    assert (await r.json())["residents"] == ["anna", "bernd"]
    await _settle(sent, 2)
    assert sorted(sent) == ["https://push/a1", "https://push/b1"]
    # Once, on the shared stream every open client already subscribes to.
    assert [(uid, kind) for uid, kind, _d in bus.published] == [("household", "ha")]


# ---- an unknown target reaches nobody --------------------------------------


async def test_an_unknown_target_does_not_broadcast(
    aiohttp_client, tmp_path, monkeypatch
):
    app, db, bus, sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    push_store.upsert(db, "bernd", "https://push/b1", "p", "a")
    client = await aiohttp_client(app)
    for target in ("annna", "an", "anna ist da", "%", "unknown"):
        r = await client.post("/api/ha/notify", json={"target": target, "title": "x"})
        assert r.status == 404
        assert (await r.json())["reason"] == "unknown_target"
    await asyncio.sleep(0.05)
    # Nobody's phone, nobody's stream — a typo must never become a broadcast.
    assert sent == []
    assert bus.published == []


def test_resolution_is_exact_and_never_falls_back_to_everyone(tmp_path):
    db = _db(tmp_path)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    assert ha_notify.resolve(db, "ANNA") == ("anna", ["anna"])
    # No prefix, no fuzzy, no nearest match, and no empty-target catch-all.
    for miss in ("ann", "annab", "", "  ", "*"):
        assert ha_notify.resolve(db, miss) is None
    # A household with nobody paired resolves to an empty fan-out, never to a
    # guessed resident.
    assert ha_notify.resolve(str(tmp_path / "nope.db"), "household") == (
        "household",
        [],
    )


# ---- fail-open: the automation is never held up ----------------------------


async def test_an_undeliverable_push_neither_errors_nor_delays_the_caller(
    aiohttp_client, tmp_path, monkeypatch
):
    app, db, _bus, _sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    released = asyncio.Event()
    entered = asyncio.Event()

    async def hanging_push(self, uid, title, body, data=None):
        entered.set()
        await released.wait()

    monkeypatch.setattr(Notifier, "push", hanging_push)
    client = await aiohttp_client(app)
    # An unreachable phone holds the push open; HA is answered anyway.
    r = await asyncio.wait_for(
        client.post("/api/ha/notify", json={"target": "anna", "title": "Post da"}), 2
    )
    assert r.status == 202
    await asyncio.wait_for(entered.wait(), 2)
    released.set()


async def test_a_push_that_raises_is_swallowed(aiohttp_client, tmp_path, monkeypatch):
    app, db, _bus, _sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    calls: list[str] = []

    async def boom(self, uid, title, body, data=None):
        calls.append(uid)
        raise RuntimeError("push service is down")

    monkeypatch.setattr(Notifier, "push", boom)
    client = await aiohttp_client(app)
    r = await client.post("/api/ha/notify", json={"target": "anna", "title": "x"})
    assert r.status == 202
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0.01)
    assert calls == ["anna"]
    # Still serving: the failure never escaped the background task.
    r = await client.post("/api/ha/notify", json={"target": "anna", "title": "y"})
    assert r.status == 202


async def test_an_open_client_is_not_double_notified(
    aiohttp_client, tmp_path, monkeypatch
):
    app, db, bus, sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    received: list[dict] = []

    async def collect():
        async for event in bus.subscribe("anna"):
            received.append(event)

    task = asyncio.ensure_future(collect())
    for _ in range(50):
        if bus.has_subscriber("anna"):
            break
        await asyncio.sleep(0.01)
    client = await aiohttp_client(app)
    r = await client.post("/api/ha/notify", json={"target": "anna", "title": "Post da"})
    assert r.status == 202
    await asyncio.sleep(0.05)
    task.cancel()
    # The SSE stream carried it; no Web Push on top of that.
    assert [e["kind"] for e in received] == ["ha"]
    assert sent == []


# ---- auth: the model lease's loopback pattern, no third scheme --------------


async def test_a_proxy_forwarded_request_is_refused(
    aiohttp_client, tmp_path, monkeypatch
):
    # NPM is hostNetwork on this same box, so its peer address is loopback too;
    # a forwarding header is what tells an outside caller from a neighbour.
    app, db, bus, sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    client = await aiohttp_client(app)
    for header in ("X-Forwarded-For", "X-Real-IP"):
        r = await client.post(
            "/api/ha/notify",
            json={"target": "anna", "title": "x"},
            headers={header: "203.0.113.9"},
        )
        assert r.status == 403
    await asyncio.sleep(0.05)
    assert sent == [] and bus.published == []


async def test_the_route_is_not_mirrored_under_napi(
    aiohttp_client, tmp_path, monkeypatch
):
    # `/napi/` is Authelia-bypassed and device-token-only/fail-closed; an
    # unauthenticated route under that prefix would punch a hole in its one
    # invariant. This endpoint lives on `/api/` and is peer-bound instead.
    app, _db_path, _bus, _sent = _app(tmp_path, monkeypatch)
    client = await aiohttp_client(app)
    r = await client.post("/napi/ha/notify", json={"target": "anna", "title": "x"})
    assert r.status == 404


async def test_malformed_json_is_refused_without_delivering(
    aiohttp_client, tmp_path, monkeypatch
):
    app, db, bus, sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    client = await aiohttp_client(app)
    r = await client.post("/api/ha/notify", data="not json")
    assert r.status == 400
    assert (await r.json())["reason"] == "invalid_json"
    await asyncio.sleep(0.05)
    assert sent == [] and bus.published == []
