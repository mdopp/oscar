"""HA household notices — `POST /api/ha/notify` (#1276, #1280).

The automation says what happened; Solaris decides whose phone shows it. What
is pinned here: the payload is closed, a target resolves only to a resident
with a paired device, an unknown target reaches NOBODY, adding a device needs
no automation change, an undeliverable push neither errors nor delays the
caller, the loopback auth of the model lease (#1260) holds, and the endpoint
says out loud that it is not an alarm channel.

Also pinned (#1280): a fired timer or reminder rides the **same** event kind,
`category` tells the two apart so they stay separately mutable, a payload in
the shipped v0.46.0 shape (no `category`) is still accepted, and Web Push keeps
running unchanged next to the new event.

And pinned (#1284): the bus keeps no backlog, so a notice published while
nobody was listening used to be lost rather than delayed. It is now also
written to a short, self-pruning backlog that `GET /napi/notifications?since=`
replays — bounded by age and by row count, scoped to the same two streams the
SSE serves, and still promising nothing about delivery.

The three tables (migrations 0020/0021/0034) are replayed with raw SQL — a chat
test must NOT import alembic (CI runs solaris-chat in a clean env without it).
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3

import pytest

from solaris_chat import (
    device_token_store,
    ha_notify,
    notice_backlog,
    push_store,
    server,
)
from solaris_chat.engine.notify import EventBus, Notifier
from solaris_chat.engine.scheduler import TimerScheduler
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
CREATE TABLE ha_notice_backlog (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  target_uid TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
  payload    TEXT NOT NULL
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


def _app(
    tmp_path,
    monkeypatch,
    *,
    default_uid: str = "household",
    hass_url: str = "",
    hass_token: str = "",
):
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
        hass_url=hass_url,
        hass_token=hass_token,
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
    assert ha_notify.PAYLOAD_KEYS == (
        "target",
        "title",
        "body",
        "urgency",
        "actions",
        "category",
    )
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
    close = {
        "entity_id": "cover.kueche",
        "service": "cover.close_cover",
        "title": "Schließen",
    }
    for bad in (
        "x",
        [close | {"title": ""}],
        [close] * 9,
        # The retired one-string shape: refused, not guessed at.
        [{"action": "cover.close_cover", "title": "Schließen"}],
        # Both halves must be named, and the service must belong to the entity.
        [{"entity_id": "cover.kueche", "title": "Schließen"}],
        [close | {"service": "close_cover"}],
        [close | {"service": "light.turn_on"}],
        [close | {"entity_id": "kueche"}],
        # `confirm` is a boolean, not a string a receiver has to interpret.
        [close | {"confirm": "true"}],
    ):
        with pytest.raises(ValueError, match="invalid_actions"):
            ha_notify.parse_payload({"target": "a", "title": "x", "actions": bad})


def test_an_action_names_the_entity_and_the_service_separately():
    # `{entity_id,service,title,confirm}` and nothing else (#1283): the receiver
    # copies entity/service verbatim into the app's existing
    # WidgetActionActivity path (confirm dialog + `sensitive_action` gate) and
    # infers nothing from the shape of a string.
    assert ha_notify.ACTION_KEYS == ("entity_id", "service", "title", "confirm")
    action = {
        "entity_id": "cover.kueche",
        "service": "cover.close_cover",
        "title": "Schließen",
    }
    parsed = ha_notify.parse_payload(
        {"target": "anna", "title": "Fenster offen", "actions": [action]}
    )
    # `confirm` is added by the server; the caller supplied three keys.
    assert parsed["actions"] == [action | {"confirm": True}]


def test_a_notification_can_never_carry_an_unlock():
    # The one outcome the confirm gate exists to prevent, on the one surface
    # that reaches the lock screen. `lock` and `alarm_control_panel` are not
    # values this field can hold, so an unlock is unrepresentable — not merely
    # discouraged. Same choice as the missing `alarm` category.
    assert "lock" not in ha_notify.ACTIONABLE_DOMAINS
    assert "alarm_control_panel" not in ha_notify.ACTIONABLE_DOMAINS
    for entity_id, service in (
        ("lock.haustuer", "lock.unlock"),
        ("lock.haustuer", "lock.open"),
        ("lock.haustuer", "lock.lock"),
        ("alarm_control_panel.haus", "alarm_control_panel.alarm_disarm"),
        ("button.tueroeffner", "button.press"),
    ):
        forbidden = [{"entity_id": entity_id, "service": service, "title": "Auf"}]
        with pytest.raises(ValueError, match="forbidden_action_domain"):
            ha_notify.parse_payload(
                {"target": "anna", "title": "Tür", "actions": forbidden}
            )
        # And not through a producer that never saw the HTTP edge either.
        with pytest.raises(ValueError, match="forbidden_action_domain"):
            ha_notify.event_data("anna", "Tür", actions=forbidden)


# ---- `confirm`: the receiver is told, never left to guess (#1283) ----------


def _cover_action(entity_id: str, **extra) -> dict:
    return {
        "entity_id": entity_id,
        "service": "cover.open_cover",
        "title": "Öffnen",
    } | extra


def _parse_one(action: dict, device_classes: dict | None = None) -> dict:
    parsed = ha_notify.parse_payload(
        {"target": "anna", "title": "x", "actions": [action]}, device_classes
    )
    return parsed["actions"][0]


def test_a_garage_door_action_says_it_needs_a_confirmation():
    # `cover` is two unrelated things wearing one domain. The garage door stays
    # openable from the notification — the receiver is simply told to ask first,
    # instead of guessing that from the entity's name.
    assert ha_notify.CONFIRM_COVER_CLASSES == frozenset(
        {"garage", "door", "gate", "window"}
    )
    for device_class in sorted(ha_notify.CONFIRM_COVER_CLASSES):
        action = _cover_action("cover.tor")
        assert _parse_one(action, {"cover.tor": device_class})["confirm"] is True
    # Case is HA's business, not the contract's.
    assert _parse_one(_cover_action("cover.tor"), {"cover.tor": "Garage"})["confirm"]


def test_a_shutter_action_does_not_ask_and_neither_do_the_harmless_domains():
    # The daily blind must not grow a confirmation dialog — that is how a
    # confirmation stops meaning anything.
    for device_class in ("shutter", "blind", "awning", "curtain", "shade"):
        action = _cover_action("cover.kueche")
        assert _parse_one(action, {"cover.kueche": device_class})["confirm"] is False
    for entity_id, service in (
        ("light.flur", "light.turn_on"),
        ("switch.kaffee", "switch.turn_on"),
        ("climate.bad", "climate.set_temperature"),
    ):
        action = {"entity_id": entity_id, "service": service, "title": "An"}
        assert _parse_one(action)["confirm"] is False


def test_a_cover_whose_class_cannot_be_resolved_confirms():
    # HA unreachable, entity unknown, or simply no device_class set: unknown
    # means confirm. Every garage door confirming is correct-but-annoying; a
    # garage door not confirming is the bug.
    action = _cover_action("cover.unbekannt")
    for classes in (None, {}, {"cover.andere": "shutter"}, {"cover.unbekannt": ""}):
        assert _parse_one(action, classes)["confirm"] is True
    # Same through the producer path that never sees the HTTP edge.
    event = ha_notify.event_data("anna", "Tor", actions=[action])
    assert event["actions"][0]["confirm"] is True


def test_a_caller_can_raise_confirm_but_never_lower_it():
    # The server computes it; the caller's flag is OR'd in, never substituted.
    lowered = _cover_action("cover.tor", confirm=False)
    assert _parse_one(lowered, {"cover.tor": "garage"})["confirm"] is True
    assert _parse_one(lowered, None)["confirm"] is True
    # Raising is allowed — an automation may want its own light confirmed.
    raised = {
        "entity_id": "light.flur",
        "service": "light.turn_on",
        "title": "An",
        "confirm": True,
    }
    assert _parse_one(raised)["confirm"] is True
    # And a raise survives the re-validation in event_data.
    event = ha_notify.event_data("anna", "Flur", actions=[_parse_one(raised)])
    assert event["actions"][0]["confirm"] is True


async def test_the_endpoint_confirms_a_cover_it_cannot_classify(
    aiohttp_client, tmp_path, monkeypatch
):
    # HA is not configured here, so no device_class resolves — the delivered
    # event must still carry `confirm: true` rather than omitting the question.
    app, db, bus, _sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/ha/notify",
        json={
            "target": "anna",
            "title": "Tor offen",
            "actions": [_cover_action("cover.garagentor")],
        },
    )
    assert r.status == 202
    assert bus.published[0][2]["actions"][0]["confirm"] is True


async def test_the_endpoint_lowers_confirm_only_for_a_resolved_harmless_cover(
    aiohttp_client, tmp_path, monkeypatch
):
    # The one path that may lower it: the read-only entity-state read the card
    # path already does. A garage keeps its confirmation, a shutter loses it.
    classes = {"cover.garagentor": "garage", "cover.kueche": "shutter"}

    async def _fake_card(_url, _token, entity_id):
        if entity_id not in classes:
            return None
        return {"entity_id": entity_id, "device_class": classes[entity_id]}

    monkeypatch.setattr(server, "fetch_card", _fake_card)
    app, db, bus, _sent = _app(
        tmp_path, monkeypatch, hass_url="http://ha", hass_token="t"
    )
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/ha/notify",
        json={
            "target": "anna",
            "title": "Haus",
            "actions": [
                _cover_action("cover.garagentor"),
                _cover_action("cover.kueche"),
                _cover_action("cover.unbekannt"),
            ],
        },
    )
    assert r.status == 202
    delivered = bus.published[0][2]["actions"]
    assert [a["confirm"] for a in delivered] == [True, False, True]


# ---- the category, and that it was added additively ------------------------


def test_a_category_picks_the_channel_and_defaults_to_house():
    # Reminders and house notices must stay separately mutable: whoever mutes
    # the laundry notice must not thereby lose a timer.
    assert ha_notify.CATEGORIES == ("house", "timer", "reminder")
    assert ha_notify.DEFAULT_CATEGORY == "house"
    for category in ha_notify.CATEGORIES:
        parsed = ha_notify.parse_payload(
            {"target": "anna", "title": "x", "category": category}
        )
        assert parsed["category"] == category
    with pytest.raises(ValueError, match="invalid_category"):
        ha_notify.parse_payload({"target": "anna", "title": "x", "category": "alarm"})


def test_a_payload_without_a_category_is_still_accepted():
    # `category` stays optional: the shipped key set without it must keep
    # parsing, and land on `house`. (The action *shape* did change — #1283.)
    action = {
        "entity_id": "cover.kueche",
        "service": "cover.close_cover",
        "title": "Schließen",
    }
    shipped = {
        "target": "anna",
        "title": "Fenster offen",
        "body": "Küche",
        "urgency": "high",
        "actions": [action],
    }
    parsed = ha_notify.parse_payload(shipped)
    assert parsed["category"] == "house"
    assert parsed["urgency"] == "high"
    assert parsed["actions"] == [action | {"confirm": True}]


def test_every_producer_builds_the_same_event_shape():
    # One receiver, one shape — the producers differ only by `category`.
    house = ha_notify.event_data("anna", "Post da", "im Briefkasten")
    timer = ha_notify.event_data("anna", "Timer abgelaufen", "Tee", category="timer")
    assert set(house) == set(timer)
    assert house["kind"] == timer["kind"] == ha_notify.EVENT_KIND
    assert house["category"] == "house" and timer["category"] == "timer"


def test_a_wecker_is_not_given_a_category_called_alarm():
    # `engine_timers.kind == "alarm"` is a wake-up clock, but a category named
    # "alarm" on a best-effort channel invites exactly the misreading the
    # module docstring exists to prevent.
    assert ha_notify.TIMER_CATEGORIES["alarm"] == "timer"
    assert "alarm" not in ha_notify.CATEGORIES


# ---- it is not an alarm channel, and says so -------------------------------


def _handler_comment() -> str:
    """The comment block above the `/api/ha/notify` handler."""
    src = inspect.getsource(server)
    return src.split("# -- the household notice", 1)[1].split("async def", 1)[0]


def test_the_endpoint_documents_that_it_is_not_an_alarm_channel():
    # In the code, not only in the ticket — otherwise somebody eventually hangs
    # a smoke alarm off a best-effort push path. Four places, all four load
    # bearing: the constant every response echoes, the module docstring, the
    # handler comment, and this test.
    assert "alarm" in ha_notify.NOT_AN_ALARM
    assert "NOT AN ALARM CHANNEL" in (ha_notify.__doc__ or "")
    assert "NOT AN ALARM CHANNEL" in _handler_comment()
    # No urgency level promises delivery; the highest one is presentation.
    assert ha_notify.URGENCY_LEVELS == ("low", "normal", "high")


def test_the_not_an_alarm_statement_covers_the_timers_that_moved_onto_this_kind():
    # With reminders in scope the statement is easier to read as over-cautious,
    # which is precisely when somebody hangs a smoke alarm off it. Both places
    # must say out loud that a category does not change the promise, and that
    # the speaker announce stays the primary path for a timer.
    for text in ((ha_notify.__doc__ or ""), _handler_comment()):
        lowered = text.lower()
        assert "timer" in lowered
        assert "category" in lowered
        assert "scheduler" in lowered or "announcement" in lowered


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
                "category": "house",
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


# ---- timers and reminders ride the same event kind (#1280) -----------------

_TIMERS_SCHEMA = (
    "CREATE TABLE engine_timers (id TEXT PRIMARY KEY, owner_uid TEXT, kind TEXT,"
    " label TEXT, fire_at TEXT, session_id TEXT, room TEXT DEFAULT '',"
    " status TEXT DEFAULT 'pending')"
)


class _RecordingNotifier:
    """Stands in for the Web Push notifier, recording what it was asked to send."""

    def __init__(self) -> None:
        self.pushes: list[tuple] = []

    async def push(self, uid, title, body, data=None):
        self.pushes.append((uid, title, body, data))


def _timer_db(tmp_path, *, kind: str, label: str) -> str:
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(_TIMERS_SCHEMA)
    conn.execute(
        "INSERT INTO engine_timers (id, owner_uid, kind, label, fire_at, status)"
        " VALUES ('t1', 'anna', ?, ?, '2000-01-01T00:00:00+00:00', 'pending')",
        (kind, label),
    )
    conn.commit()
    conn.close()
    return db


@pytest.mark.parametrize(
    ("kind", "category", "title"),
    [
        ("timer", "timer", "Timer abgelaufen"),
        ("alarm", "timer", "Wecker"),
        ("reminder", "reminder", "Erinnerung"),
    ],
)
async def test_a_fired_timer_reaches_a_device_on_the_ha_event_kind(
    tmp_path, kind, category, title
):
    db = _timer_db(tmp_path, kind=kind, label="Tee")
    bus = _RecordingBus()
    # No HA configured, so the announce fails — the notice goes out regardless.
    sched = TimerScheduler(db, "", "", event_bus=bus)
    await sched._fire_due()
    assert bus.published == [
        (
            "anna",
            ha_notify.EVENT_KIND,
            {
                "kind": ha_notify.EVENT_KIND,
                "target": "anna",
                "title": title,
                "body": "Tee",
                "urgency": "normal",
                "actions": [],
                "category": category,
            },
        )
    ]


async def test_a_timer_and_a_house_notice_carry_different_categories(
    aiohttp_client, tmp_path, monkeypatch
):
    # The whole reason for the field: muting the house's notices must not mute
    # the resident's timers.
    app, db, bus, _sent = _app(tmp_path, monkeypatch)
    push_store.upsert(db, "anna", "https://push/a1", "p", "a")
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/ha/notify", json={"target": "anna", "title": "Waschmaschine"}
    )
    assert r.status == 202
    conn = sqlite3.connect(db)
    conn.execute(_TIMERS_SCHEMA)
    conn.execute(
        "INSERT INTO engine_timers (id, owner_uid, kind, label, fire_at, status)"
        " VALUES ('t1', 'anna', 'timer', 'Tee', '2000-01-01T00:00:00+00:00',"
        " 'pending')"
    )
    conn.commit()
    conn.close()
    await TimerScheduler(db, "", "", event_bus=bus)._fire_due()
    kinds = {k for _uid, k, _d in bus.published}
    categories = [d["category"] for _uid, _k, d in bus.published]
    # One kind for the receiver to handle, two channels for it to offer.
    assert kinds == {ha_notify.EVENT_KIND}
    assert categories == ["house", "timer"]


async def test_web_push_for_a_fired_timer_is_unchanged_by_the_native_event(tmp_path):
    # The native channel is ADDITIVE: as long as anyone has the PWA installed,
    # Web Push must keep delivering exactly what it delivered in v0.46.0.
    db = _timer_db(tmp_path, kind="reminder", label="Wäsche")
    notifier = _RecordingNotifier()
    bus = _RecordingBus()
    await TimerScheduler(db, "", "", notifier=notifier, event_bus=bus)._fire_due()
    assert notifier.pushes == [
        ("anna", "Erinnerung", "Wäsche", {"kind": "reminder", "timer_id": "t1"})
    ]
    assert len(bus.published) == 1


async def test_a_scheduler_without_an_event_bus_still_fires(tmp_path):
    # The bus is optional; a timer must never depend on it to ring.
    db = _timer_db(tmp_path, kind="timer", label="Tee")
    notifier = _RecordingNotifier()
    await TimerScheduler(db, "", "", notifier=notifier)._fire_due()
    assert len(notifier.pushes) == 1


# ---- catch-up after a gap (#1284) ------------------------------------------


async def _catch_up(client, token: str, since: str | None = None):
    url = "/napi/notifications" + (f"?since={since}" if since else "")
    return await client.get(url, headers={"Authorization": f"Bearer {token}"})


async def test_a_notice_emitted_while_nobody_listens_is_fetched_afterwards(
    aiohttp_client, tmp_path, monkeypatch
):
    # The screen-off case the channel exists for: nothing is subscribed to the
    # bus when the notice goes out, and it must still be there on the next wake.
    app, db, bus, _sent = _app(tmp_path, monkeypatch)
    _, token = device_token_store.create(db, "anna")
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/ha/notify",
        json={"target": "anna", "title": "Waschmaschine", "body": "ist fertig"},
    )
    assert r.status == 202
    assert not bus.has_subscriber("anna")

    body = await (await _catch_up(client, token)).json()
    assert body["ok"] is True
    assert body["retention_hours"] == notice_backlog.RETENTION_HOURS
    assert body["delivery"] == ha_notify.NOT_AN_ALARM
    (notice,) = body["notifications"]
    # The payload replayed is the event AS EMITTED — same object the stream
    # carried, plus only the two catch-up fields.
    assert {k: v for k, v in notice.items() if k not in ("id", "ts")} == (
        bus.published[0][2]
    )
    assert notice["ts"].endswith("Z")


async def test_the_catch_up_returns_only_what_is_newer_than_the_cursor(
    aiohttp_client, tmp_path, monkeypatch
):
    app, db, _bus, _sent = _app(tmp_path, monkeypatch)
    _, token = device_token_store.create(db, "anna")
    client = await aiohttp_client(app)
    await client.post("/api/ha/notify", json={"target": "anna", "title": "Post da"})
    first = (await (await _catch_up(client, token)).json())["notifications"]
    assert [n["title"] for n in first] == ["Post da"]

    await client.post("/api/ha/notify", json={"target": "anna", "title": "Fenster"})
    later = await (await _catch_up(client, token, since=first[0]["ts"])).json()
    assert [n["title"] for n in later["notifications"]] == ["Fenster"]
    # Nothing new since the newest one it just read.
    empty = await (await _catch_up(client, token, since=later["now"])).json()
    assert empty["notifications"] == []


async def test_the_catch_up_serves_the_same_two_streams_as_the_live_one(
    aiohttp_client, tmp_path, monkeypatch
):
    # `/napi/portal/events` subscribes to own + household; the replay must show
    # exactly those, never another resident's.
    app, db, _bus, _sent = _app(tmp_path, monkeypatch)
    _, token = device_token_store.create(db, "anna")
    device_token_store.create(db, "bernd")
    client = await aiohttp_client(app)
    await client.post("/api/ha/notify", json={"target": "anna", "title": "Für Anna"})
    await client.post("/api/ha/notify", json={"target": "bernd", "title": "Für Bernd"})
    await client.post(
        "/api/ha/notify", json={"target": "household", "title": "Für uns"}
    )

    body = await (await _catch_up(client, token)).json()
    assert [n["title"] for n in body["notifications"]] == ["Für Anna", "Für uns"]


async def test_a_fired_timer_is_catchable_after_the_fact(
    aiohttp_client, tmp_path, monkeypatch
):
    # The second producer: a timer that fired into an empty bus is not lost
    # either — same kind, same replay.
    app, db, _bus, _sent = _app(tmp_path, monkeypatch)
    _, token = device_token_store.create(db, "anna")
    conn = sqlite3.connect(db)
    conn.execute(_TIMERS_SCHEMA)
    conn.execute(
        "INSERT INTO engine_timers (id, owner_uid, kind, label, fire_at, status)"
        " VALUES ('t1', 'anna', 'timer', 'Tee', '2000-01-01T00:00:00+00:00',"
        " 'pending')"
    )
    conn.commit()
    conn.close()
    await TimerScheduler(db, "", "")._fire_due()

    client = await aiohttp_client(app)
    (notice,) = (await (await _catch_up(client, token)).json())["notifications"]
    assert (notice["title"], notice["body"], notice["category"]) == (
        "Timer abgelaufen",
        "Tee",
        "timer",
    )


async def test_the_catch_up_is_device_token_only(aiohttp_client, tmp_path, monkeypatch):
    app, _db, _bus, _sent = _app(tmp_path, monkeypatch)
    client = await aiohttp_client(app)
    assert (await client.get("/napi/notifications")).status == 401


async def test_an_unreadable_cursor_is_refused_rather_than_guessed(
    aiohttp_client, tmp_path, monkeypatch
):
    app, db, _bus, _sent = _app(tmp_path, monkeypatch)
    _, token = device_token_store.create(db, "anna")
    client = await aiohttp_client(app)
    r = await _catch_up(client, token, since="gestern")
    assert r.status == 400
    assert (await r.json())["reason"] == "invalid_since"


# ---- the backlog is short and bounded --------------------------------------


def test_the_retention_window_is_short():
    # These rows name residents and describe their home. A convenience channel
    # earns hours, not days.
    assert notice_backlog.RETENTION_HOURS <= 12


def test_a_notice_older_than_the_window_is_neither_served_nor_kept(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO ha_notice_backlog (target_uid, created_at, payload)"
        " VALUES ('anna', strftime('%Y-%m-%d %H:%M:%f', 'now', '-2 days'),"
        ' \'{"title": "alt"}\')'
    )
    conn.commit()
    conn.close()
    # Not served, even before anything has pruned it.
    assert notice_backlog.fetch(db, ["anna"]) == []
    # And the next write reaps it — no cron to forget to run.
    notice_backlog.record(db, "anna", ha_notify.event_data("anna", "neu"))
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT payload FROM ha_notice_backlog").fetchall()
    conn.close()
    assert len(rows) == 1 and "neu" in rows[0][0]


def test_the_backlog_cannot_grow_without_bound_inside_the_window(tmp_path, monkeypatch):
    # A misfiring automation must not be able to fill the disk between prunes.
    db = _db(tmp_path)
    monkeypatch.setattr(notice_backlog, "MAX_ROWS_PER_TARGET", 3)
    for i in range(10):
        notice_backlog.record(db, "anna", ha_notify.event_data("anna", f"n{i}"))
    kept = [n["title"] for n in notice_backlog.fetch(db, ["anna"])]
    assert kept == ["n7", "n8", "n9"]


def test_a_box_without_the_migration_degrades_to_no_backlog(tmp_path):
    # The engine ships before the schema-init sidecar has run: no table, no
    # backlog, no exception into the producer.
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()
    notice_backlog.record(db, "anna", ha_notify.event_data("anna", "Post da"))
    assert notice_backlog.fetch(db, ["anna"]) == []
