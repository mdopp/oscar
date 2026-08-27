"""The foundry-chronicle model lease (#1260, contract mdopp/foundry-chronicle#299).

Fail-open in every direction, a closed payload, and a re-warm at the window's
end — the three things the two sides agreed on and neither may drift from.
"""

from __future__ import annotations

import json

import pytest

from solaris_chat import model_lease, settings_store
from solaris_chat.engine.ollama import OllamaChat
from solaris_chat.engine.profiles import build_engine_clients
from solaris_chat.server import build_app


def _db(tmp_path) -> str:
    return str(tmp_path / "solaris.db")


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


def _app(tmp_path, *, enabled: bool = True, fast_model: str = "gemma4:e4b"):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
        notes_dir=str(tmp_path),
        fast_model=fast_model,
        model_lease_enabled=enabled,
    )


# ---- the contract's cadence ------------------------------------------------


def test_ttl_and_renewal_come_from_one_constant():
    # 15 minutes, renewed every 5 — one literal, one derived. A second literal
    # is how the two cadences drift apart and the swap thrash comes back.
    assert model_lease.LEASE_TTL_SECONDS == 900
    assert model_lease.RENEW_INTERVAL_SECONDS * 3 == model_lease.LEASE_TTL_SECONDS


# ---- the payload is closed -------------------------------------------------


def test_payload_key_set_is_exactly_model_and_ttl_s():
    assert model_lease.PAYLOAD_KEYS == ("model", "ttl_s")
    assert model_lease.parse_payload({"model": "gemma4:12b", "ttl_s": 600}) == (
        "gemma4:12b",
        600,
    )
    # The key is theirs: the pre-contract `ttl` spelling is an unknown field.
    with pytest.raises(ValueError, match="unexpected_field"):
        model_lease.parse_payload({"model": "gemma4:12b", "ttl": 600})
    # No session, round or guild identifier may be smuggled in later.
    for extra in ("session", "session_id", "round", "guild", "guild_id"):
        with pytest.raises(ValueError, match="unexpected_field"):
            model_lease.parse_payload({"model": "gemma4:12b", "ttl_s": 600, extra: "x"})


def test_payload_rejects_bad_values_and_caps_the_ttl():
    with pytest.raises(ValueError, match="invalid_payload"):
        model_lease.parse_payload(["gemma4:12b"])
    with pytest.raises(ValueError, match="invalid_model"):
        model_lease.parse_payload({"model": "  "})
    for bad in (0, -1, "600", True):
        with pytest.raises(ValueError, match="invalid_ttl"):
            model_lease.parse_payload({"model": "gemma4:12b", "ttl_s": bad})
    # The TTL is OUR safety net: a longer one is capped, not honoured.
    assert model_lease.parse_payload({"model": "gemma4:12b", "ttl_s": 86400})[1] == 900
    # Absent ttl_s means the agreed one.
    assert model_lease.parse_payload({"model": "gemma4:12b"})[1] == 900


# ---- fail-open in every direction ------------------------------------------


def test_no_lease_reads_as_no_lease(tmp_path):
    assert model_lease.active_model(_db(tmp_path)) == ""
    assert model_lease.expired(_db(tmp_path)) is False


def test_unreadable_lease_reads_as_no_lease(tmp_path):
    for junk in ("{not json", json.dumps(["a"]), json.dumps({"model": "x"})):
        model_lease.lease_path(_db(tmp_path)).write_text(junk, "utf-8")
        assert model_lease.active_model(_db(tmp_path)) == ""
        assert model_lease.expired(_db(tmp_path)) is False


def test_expired_lease_never_pins_the_household(tmp_path):
    db = _db(tmp_path)
    model_lease.grant(db, "gemma4:12b", 900, now=0.0)
    assert model_lease.active_model(db, now=100.0) == "gemma4:12b"
    assert model_lease.active_model(db, now=901.0) == ""
    assert model_lease.expired(db, now=901.0) is True


# ---- which model a turn runs on -------------------------------------------


def _household_and_guest(db: str):
    household, _admin, guest, _lib, _enroll, _rec, _bus = build_engine_clients(
        db_path=db,
        ollama_url="http://x",
        fast_model="gemma4:e4b",
        thorough_model="gemma4:e4b",
        soul_path="/nonexistent/SOUL.md",
    )
    return household, guest


def test_live_lease_answers_with_the_leased_model(tmp_path):
    db = _db(tmp_path)
    household, guest = _household_and_guest(db)
    assert household._model() == "gemma4:e4b"

    model_lease.grant(db, "gemma4:12b", 900)
    # No restart: the resolver reads the persisted lease per turn, so a
    # solaris-chat restart mid-lease keeps honouring it too.
    assert household._model() == "gemma4:12b"
    assert guest._model() == "gemma4:12b"
    # The lease outranks the admin's household pick while it is live — both
    # name one model, and the leased one is already in VRAM.
    settings_store.set_household_model(db, "gemma4:e4b")
    assert household._model() == "gemma4:12b"

    model_lease.clear(db)
    assert household._model() == "gemma4:e4b"
    assert guest._model() == "gemma4:e4b"


def test_expired_lease_returns_the_household_to_the_fast_model(tmp_path):
    db = _db(tmp_path)
    household, _guest = _household_and_guest(db)
    model_lease.grant(db, "gemma4:12b", 1)
    model_lease.grant(db, "gemma4:12b", 1, now=0.0)  # long since expired
    assert household._model() == "gemma4:e4b"


def test_disabled_setting_ignores_a_live_lease(tmp_path, monkeypatch):
    import dataclasses

    from solaris_chat.engine import profiles

    db = _db(tmp_path)
    household, guest = _household_and_guest(db)
    model_lease.grant(db, "gemma4:12b", 900)
    # Settings is a frozen dataclass — swap the module-level instance.
    monkeypatch.setattr(
        profiles,
        "settings",
        dataclasses.replace(profiles.settings, model_lease_enabled=False),
    )
    assert household._model() == "gemma4:e4b"
    assert guest._model() == "gemma4:e4b"


# ---- the endpoint ----------------------------------------------------------


async def test_post_grants_and_answers_the_renewal_cadence(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/model-lease", json={"model": "gemma4:12b", "ttl_s": 900}
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert body["model"] == "gemma4:12b"
    # Both sides read one cadence from one constant — theirs is answered, not
    # assumed.
    assert body["renew_after"] == model_lease.RENEW_INTERVAL_SECONDS
    assert model_lease.active_model(_db(tmp_path)) == "gemma4:12b"


async def test_post_refuses_an_identifier_or_malformed_body(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/model-lease", json={"model": "gemma4:12b", "ttl_s": 900, "session": "s-1"}
    )
    assert r.status == 400
    assert (await r.json())["reason"] == "unexpected_field"
    r = await client.post("/api/model-lease", data="not json")
    assert r.status == 400
    # A refused lease is no lease at all — normal operation, nothing persisted.
    assert model_lease.active_model(_db(tmp_path)) == ""


async def test_delete_ends_the_window_and_rewarms(
    aiohttp_client, tmp_path, monkeypatch
):
    warmed: list[str] = []

    async def fake_warm(self, model):
        warmed.append(model)
        return True

    monkeypatch.setattr(OllamaChat, "warm", fake_warm)
    client = await aiohttp_client(_app(tmp_path))
    await client.post("/api/model-lease", json={"model": "gemma4:12b", "ttl_s": 900})
    r = await client.delete("/api/model-lease")
    assert r.status == 200
    assert model_lease.active_model(_db(tmp_path)) == ""
    # The re-warm is fired, not awaited, so the caller isn't held for ~56 s.
    for _ in range(20):
        if warmed:
            break
        import asyncio

        await asyncio.sleep(0.01)
    assert warmed == ["gemma4:e4b"]


async def test_delete_without_a_live_lease_does_not_warm(
    aiohttp_client, tmp_path, monkeypatch
):
    warmed: list[str] = []

    async def fake_warm(self, model):  # pragma: no cover - must not run
        warmed.append(model)
        return True

    monkeypatch.setattr(OllamaChat, "warm", fake_warm)
    client = await aiohttp_client(_app(tmp_path))
    r = await client.delete("/api/model-lease")
    assert r.status == 200
    assert warmed == []


async def test_disabled_setting_refuses_the_endpoint(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path, enabled=False))
    r = await client.post(
        "/api/model-lease", json={"model": "gemma4:12b", "ttl_s": 900}
    )
    assert r.status == 503
    assert (await r.json())["reason"] == "disabled"
    assert model_lease.active_model(_db(tmp_path)) == ""
    r = await client.delete("/api/model-lease")
    assert r.status == 503


async def test_proxy_forwarded_request_is_not_a_neighbour(aiohttp_client, tmp_path):
    # NPM is hostNetwork on this same box, so its peer address is loopback too;
    # a forwarding header is what tells an outside caller from a neighbour.
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/model-lease",
        json={"model": "gemma4:12b", "ttl_s": 900},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert r.status == 403
    assert model_lease.active_model(_db(tmp_path)) == ""


# ---- the window end nobody announces ---------------------------------------


async def test_expiry_watch_clears_and_rewarms_once(tmp_path):
    db = _db(tmp_path)
    model_lease.grant(db, "gemma4:12b", 900, now=0.0)  # already expired
    calls: list[int] = []

    async def on_expire():
        calls.append(1)

    async def sleep(_seconds):
        raise StopAsyncIteration  # one pass, then out

    with pytest.raises(StopAsyncIteration):
        await model_lease.expiry_watch(db, on_expire, sleep=sleep)
    assert calls == [1]
    # Cleared, so a second pass finds nothing to warm — the crashed run's
    # lease is gone, not re-warmed forever.
    assert model_lease.active_model(db) == ""
    assert model_lease.expired(db) is False
