"""The neighbour-service GPU lease over HTTP (#1333, contract
mdopp/foundry-chronicle#321).

The endpoint keeps the shape foundry built against in #1260 and now fronts the
box's GPU lease. What is pinned here is the state machine they poll — 200
`ready` / 202 `preparing` / 409 `held` / 400 / 503 — the request file the host
broker acts on, the alias every surface reports the loaded model by, the
holder (#1347) that decides whose window a `DELETE` may close, and the
`releasing` (#1364) a window reads as while the broker has the request but has
not yet given the card back.
"""

from __future__ import annotations

import json

import pytest

from solaris_chat import gpu_lease, model_lease
from solaris_chat.server import build_app


def _db(tmp_path) -> str:
    return str(tmp_path / "solaris.db")


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


def _app(tmp_path, *, enabled: bool = True):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
        notes_dir=str(tmp_path),
        fast_model="gemma4:e4b",
        model_lease_enabled=enabled,
    )


def _hold(
    tmp_path,
    model="foundry",
    *,
    ready=True,
    until=9e9,
    holder="",
    last_renewed_at=1000.0,
    renew_after=300,
):
    """Write the lease file the box's `gpu-lease.py` writes. `None` for either
    heartbeat field leaves it out — that is a lease taken before #1361."""
    path = gpu_lease.lease_path(_db(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "holder": holder or model,
        "mode": model,
        "model": "Gemma 4 12B",
        "alias": model_lease.ALIASES[model],
        "until": until,
        "ready": ready,
    }
    if last_renewed_at is not None:
        record["last_renewed_at"] = last_renewed_at
    if renew_after is not None:
        record["renew_after"] = renew_after
    path.write_text(json.dumps(record), "utf-8")


def _request(tmp_path) -> dict:
    return json.loads(model_lease.request_path(_db(tmp_path)).read_text("utf-8"))


# ---- the payload is closed -------------------------------------------------


def test_payload_key_set_is_exactly_model_ttl_s_and_holder():
    assert model_lease.PAYLOAD_KEYS == ("model", "ttl_s", "holder")
    # Unnamed is the old shape: the profile name is the holder, as it always was.
    assert model_lease.parse_payload({"model": "foundry", "ttl_s": 900}) == (
        "foundry",
        900,
        "foundry",
    )
    assert model_lease.parse_payload({"model": "coding", "ttl_s": 900})[0] == "coding"
    assert model_lease.parse_payload(
        {"model": "foundry", "ttl_s": 900, "holder": "foundry-chronicle"}
    ) == ("foundry", 900, "foundry-chronicle")
    # The key is theirs: the pre-contract `ttl` spelling is an unknown field.
    with pytest.raises(ValueError, match="unexpected_field"):
        model_lease.parse_payload({"model": "foundry", "ttl": 600})
    # No session, round or guild identifier may be smuggled in later.
    for extra in ("session", "session_id", "round", "guild", "guild_id"):
        with pytest.raises(ValueError, match="unexpected_field"):
            model_lease.parse_payload({"model": "foundry", "ttl_s": 900, extra: "x"})


def test_a_holder_is_a_short_service_name_and_nothing_else():
    """`holder` names the service, permanently — not a round, a group or a
    player. Pinning the shape is what keeps it from becoming one of those."""
    for good in ("foundry-chronicle", "a", "9-9", "z" * 64):
        assert model_lease.parse_holder(good) == good
    for bad in (
        "Foundry",  # a display name
        "foundry chronicle",
        "runde-3:tisch-nord@haus",
        "x" * 65,
        "",
        "   ",
        12,
        True,
        {"name": "foundry"},
    ):
        with pytest.raises(ValueError, match="invalid_holder"):
            model_lease.parse_holder(bad)
    with pytest.raises(ValueError, match="invalid_holder"):
        model_lease.parse_payload({"model": "foundry", "ttl_s": 900, "holder": "A B"})


def test_a_delete_body_carries_at_most_the_holder():
    assert model_lease.parse_release(None) == ""
    assert model_lease.parse_release({}) == ""
    assert model_lease.parse_release({"holder": "foundry-chronicle"}) == (
        "foundry-chronicle"
    )
    with pytest.raises(ValueError, match="unexpected_field"):
        model_lease.parse_release({"holder": "foundry-chronicle", "session": "s-1"})
    with pytest.raises(ValueError, match="invalid_payload"):
        model_lease.parse_release(["foundry-chronicle"])


def test_the_model_is_one_of_the_two_the_box_knows():
    """A lease name the box has no profile for would be accepted here and then
    fail in the broker, minutes later and out of sight."""
    for bad in ("gemma4:12b", "", "  ", "exclusive", 12):
        with pytest.raises(ValueError, match="invalid_model"):
            model_lease.parse_payload({"model": bad, "ttl_s": 900})


def test_payload_rejects_bad_values_and_clamps_the_ttl():
    with pytest.raises(ValueError, match="invalid_payload"):
        model_lease.parse_payload(["foundry"])
    for bad in (0, -1, "600", True):
        with pytest.raises(ValueError, match="invalid_ttl"):
            model_lease.parse_payload({"model": "foundry", "ttl_s": bad})
    # The window is OUR safety net: outside 300…86400 it is clamped, not
    # refused. The ceiling is a day since #1374 — "bis morgen 07:00" from the
    # Modell tile is longer than the box's own 4-hour default.
    assert model_lease.parse_payload({"model": "foundry", "ttl_s": 86400})[1] == 86400
    assert model_lease.parse_payload({"model": "foundry", "ttl_s": 999999})[1] == 86400
    assert model_lease.parse_payload({"model": "foundry", "ttl_s": 30})[1] == 300
    # A payload that names no window keeps the 4 hours it always got.
    assert model_lease.parse_payload({"model": "foundry"})[1] == 14400


def test_the_renewal_cadence_leaves_room_for_two_missed_renewals():
    assert model_lease.renew_after(900) == 300
    assert model_lease.renew_after(14400) == 4800


# ---- the state the holder polls -------------------------------------------


def test_no_lease_reads_as_the_household_model(tmp_path):
    assert model_lease.state(_db(tmp_path)) == {
        "state": "none",
        "model": "",
        "alias": "gemma-4-e4b",
        "expires_at": None,
        "holder": "",
        "last_renewed_at": None,
        "renew_after": None,
    }


def test_the_alias_follows_the_weights_the_operator_deployed(tmp_path):
    """`llama-profile.json` is what the box recorded at install; an operator on
    other weights must not be reported as e4b."""
    model_lease.profile_path(_db(tmp_path)).parent.mkdir(parents=True, exist_ok=True)
    model_lease.profile_path(_db(tmp_path)).write_text(
        json.dumps({"alias": "gemma-4-12b"}), "utf-8"
    )
    assert model_lease.state(_db(tmp_path))["alias"] == "gemma-4-12b"


def test_a_lease_still_loading_is_preparing_and_still_answers_as_the_household(
    tmp_path,
):
    """`alias` is what llama-server has loaded *now* — during the swap that is
    still the household model, never the one being loaded."""
    _hold(tmp_path, "foundry", ready=False)
    assert model_lease.state(_db(tmp_path)) == {
        "state": "preparing",
        "model": "foundry",
        "alias": "gemma-4-e4b",
        "expires_at": None,
        "holder": "foundry",
        "last_renewed_at": 1000.0,
        "renew_after": 300,
    }


def test_a_ready_lease_reports_its_alias_and_deadline(tmp_path):
    _hold(tmp_path, "coding", until=1234.0, holder="foundry-chronicle")
    assert model_lease.state(_db(tmp_path)) == {
        "state": "ready",
        "model": "coding",
        "alias": "qwen3.8-27b",
        "expires_at": 1234.0,
        "holder": "foundry-chronicle",
        "last_renewed_at": 1000.0,
        "renew_after": 300,
    }


def test_a_request_the_broker_has_not_picked_up_yet_is_preparing(tmp_path):
    """Between the POST and the broker's first write there is no lease file at
    all; reading that as "none" would tell the holder its request was lost."""
    db = _db(tmp_path)
    model_lease.write_request(
        db, "acquire", "foundry", 900, holder="foundry-chronicle", now=7.0
    )
    assert model_lease.state(db)["state"] == "preparing"
    # Named before the broker has written a lease file at all — otherwise the
    # service that just asked could be told a stranger holds the card.
    assert model_lease.state(db)["holder"] == "foundry-chronicle"
    # Once the broker has answered that request, it is no longer pending.
    model_lease.status_path(db).write_text(
        json.dumps({"requested_at": 7.0, "state": "error"}), "utf-8"
    )
    assert model_lease.state(db)["state"] == "none"


def test_the_heartbeat_is_reported_so_a_holder_can_see_its_own_grace(tmp_path):
    """#1361: the window ends a grace of two missed renewals after the last
    POST, so the holder is told when it last renewed and how often it should."""
    _hold(tmp_path, "coding", last_renewed_at=1700.0, renew_after=4800)
    seen = model_lease.state(_db(tmp_path))
    assert seen["last_renewed_at"] == 1700.0
    assert seen["renew_after"] == 4800


def test_a_lease_taken_before_the_heartbeat_reports_no_heartbeat(tmp_path):
    """A window still standing across the deploy that introduced #1361 was
    written without those fields; reporting a zero would read as "long
    overdue" to a holder that is renewing perfectly well."""
    _hold(tmp_path, "coding", last_renewed_at=None, renew_after=None)
    seen = model_lease.state(_db(tmp_path))
    assert seen["last_renewed_at"] is None
    assert seen["renew_after"] is None


def test_a_pending_request_reports_the_post_as_its_last_renewal(tmp_path):
    """Between the POST and the broker's first write there is no lease file, but
    the POST *is* the heartbeat — the holder must not read that gap as one."""
    db = _db(tmp_path)
    model_lease.write_request(db, "acquire", "foundry", 900, holder="x", now=7.0)
    seen = model_lease.state(db)
    assert seen["last_renewed_at"] == 7.0
    assert seen["renew_after"] == model_lease.renew_after(900)


def test_an_unreadable_or_exclusive_lease_reads_as_no_lease(tmp_path):
    """Fail open: an exclusive lease (#1320) is not a neighbour's window, and
    half a file is not a contract."""
    path = gpu_lease.lease_path(_db(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    for junk in ("{not json", json.dumps(["a"]), json.dumps({"mode": "exclusive"})):
        path.write_text(junk, "utf-8")
        assert model_lease.state(_db(tmp_path))["state"] == "none"


# ---- the window on its way out (#1364) ------------------------------------


def test_a_release_the_broker_has_not_run_yet_is_releasing(tmp_path):
    """The DELETE only writes the request; the box starts the units back up and
    waits for the household model before it drops the lease file. Reporting
    `ready` for those seconds sends the holder after a window that is going."""
    db = _db(tmp_path)
    _hold(tmp_path, "foundry", until=777.0, holder="foundry-chronicle")
    model_lease.write_request(db, "release", holder="foundry-chronicle")
    seen = model_lease.state(db)
    assert seen["state"] == "releasing"
    assert seen["model"] == "foundry"
    assert seen["holder"] == "foundry-chronicle"
    # No deadline to plan against — the window ends when the broker says so.
    assert seen["expires_at"] is None
    # The leased model is still the one llama-server has loaded until then.
    assert seen["alias"] == "gemma-4-12b"


def test_a_release_of_a_window_still_loading_keeps_the_household_alias(tmp_path):
    db = _db(tmp_path)
    _hold(tmp_path, "foundry", ready=False, holder="foundry-chronicle")
    model_lease.write_request(db, "release", holder="foundry-chronicle")
    seen = model_lease.state(db)
    assert seen["state"] == "releasing"
    assert seen["alias"] == "gemma-4-e4b"


def test_a_lease_written_after_the_release_is_a_new_window_not_a_releasing_one(
    tmp_path,
):
    """Newer than the lease file is the whole test: an operator who took the
    card by hand after a release holds a window that stands."""
    db = _db(tmp_path)
    model_lease.write_request(db, "release", holder="foundry-chronicle")
    _hold(tmp_path, "foundry", until=777.0, holder="foundry-chronicle")
    assert model_lease.state(db)["state"] == "ready"


def test_the_window_is_gone_once_the_broker_removed_the_lease(tmp_path):
    """`none` is what "really released" looks like — the release request stays
    on disk after the broker has run it."""
    db = _db(tmp_path)
    _hold(tmp_path, "foundry", holder="foundry-chronicle")
    model_lease.write_request(db, "release", holder="foundry-chronicle")
    gpu_lease.lease_path(db).unlink()
    seen = model_lease.state(db)
    assert seen["state"] == "none"
    assert seen["holder"] == ""
    assert seen["alias"] == "gemma-4-e4b"


# ---- the endpoint ----------------------------------------------------------


async def test_post_asks_the_broker_and_answers_preparing(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post("/api/model-lease", json={"model": "foundry", "ttl_s": 900})
    # Not a failure: the first foundry lease downloads 8 GB.
    assert r.status == 202
    body = await r.json()
    assert body["ok"] is True
    assert body["state"] == "preparing"
    assert body["alias"] == "gemma-4-12b"
    assert body["retry_after"] == model_lease.RETRY_AFTER_SECONDS
    assert body["expires_at"] is None
    # The engine cannot switch a unit; the host broker reads this file.
    written = _request(tmp_path)
    assert written["op"] == "acquire"
    assert written["model"] == "foundry"
    assert written["ttl_s"] == 900
    assert written["holder"] == "foundry"
    assert written["requested_at"] > 0


async def test_post_on_a_standing_lease_renews_it(aiohttp_client, tmp_path):
    _hold(tmp_path, "foundry", until=4242.0)
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post("/api/model-lease", json={"model": "foundry", "ttl_s": 900})
    assert r.status == 200
    body = await r.json()
    assert body["state"] == "ready"
    assert body["alias"] == "gemma-4-12b"
    assert body["expires_at"] == 4242.0
    assert body["renew_after"] == model_lease.renew_after(900)
    assert _request(tmp_path)["op"] == "acquire"


async def test_post_for_the_other_model_is_refused_with_the_deadline(
    aiohttp_client, tmp_path
):
    _hold(tmp_path, "coding", until=555.0, holder="coder")
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post("/api/model-lease", json={"model": "foundry", "ttl_s": 900})
    assert r.status == 409
    body = await r.json()
    assert body == {
        "ok": False,
        "reason": "held",
        "holder": "coder",
        "expires_at": 555.0,
    }
    # A refused request never reaches the broker.
    assert not model_lease.request_path(_db(tmp_path)).exists()


async def test_post_files_the_window_under_the_service_that_named_itself(
    aiohttp_client, tmp_path
):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/model-lease",
        json={"model": "foundry", "ttl_s": 900, "holder": "foundry-chronicle"},
    )
    assert r.status == 202
    assert _request(tmp_path)["holder"] == "foundry-chronicle"
    # And the holder polling GET is told it is theirs, before the broker has
    # written any lease file at all.
    body = await (await client.get("/api/model-lease")).json()
    assert body["state"] == "preparing"
    assert body["holder"] == "foundry-chronicle"


async def test_post_for_the_same_model_under_another_name_is_refused(
    aiohttp_client, tmp_path
):
    """Two services on one profile used to renew each other's window silently;
    now the second one is told who has it and until when."""
    _hold(tmp_path, "foundry", until=777.0, holder="foundry-chronicle")
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/model-lease",
        json={"model": "foundry", "ttl_s": 900, "holder": "some-other-service"},
    )
    assert r.status == 409
    assert await r.json() == {
        "ok": False,
        "reason": "held",
        "holder": "foundry-chronicle",
        "expires_at": 777.0,
    }
    assert not model_lease.request_path(_db(tmp_path)).exists()
    # Its own holder still renews.
    r = await client.post(
        "/api/model-lease",
        json={"model": "foundry", "ttl_s": 900, "holder": "foundry-chronicle"},
    )
    assert r.status == 200
    assert _request(tmp_path)["op"] == "acquire"


async def test_delete_releases_only_the_callers_own_window(aiohttp_client, tmp_path):
    """A restarting service closing "its" window must not close a stranger's —
    that was the whole of #1347."""
    _hold(tmp_path, "foundry", until=777.0, holder="foundry-chronicle")
    client = await aiohttp_client(_app(tmp_path))
    r = await client.delete("/api/model-lease", json={"holder": "some-other-service"})
    assert r.status == 409
    assert await r.json() == {
        "ok": False,
        "reason": "held",
        "holder": "foundry-chronicle",
        "expires_at": 777.0,
    }
    assert not model_lease.request_path(_db(tmp_path)).exists()
    r = await client.delete("/api/model-lease", json={"holder": "foundry-chronicle"})
    assert r.status == 200
    assert await r.json() == {
        "ok": True,
        "state": "releasing",
        "retry_after": model_lease.RETRY_AFTER_SECONDS,
    }
    assert _request(tmp_path)["op"] == "release"


async def test_a_holder_that_is_not_a_service_name_is_refused(aiohttp_client, tmp_path):
    """A round, a table or a player's name is not a service — and the 400 says
    so before anything about the group could reach the box."""
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/model-lease",
        json={"model": "foundry", "ttl_s": 900, "holder": "runde-3 tisch-nord"},
    )
    assert r.status == 400
    assert (await r.json())["reason"] == "invalid_holder"
    r = await client.delete("/api/model-lease", json={"holder": "x" * 65})
    assert r.status == 400
    assert (await r.json())["reason"] == "invalid_holder"
    r = await client.delete("/api/model-lease", data="not json")
    assert r.status == 400
    assert not model_lease.request_path(_db(tmp_path)).exists()


async def test_post_refuses_an_identifier_or_malformed_body(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/model-lease", json={"model": "foundry", "ttl_s": 900, "session": "s-1"}
    )
    assert r.status == 400
    assert (await r.json())["reason"] == "unexpected_field"
    r = await client.post("/api/model-lease", data="not json")
    assert r.status == 400
    assert not model_lease.request_path(_db(tmp_path)).exists()


async def test_get_answers_what_is_loaded_right_now(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    body = await (await client.get("/api/model-lease")).json()
    assert body["state"] == "none"
    assert body["alias"] == "gemma-4-e4b"
    assert body["retry_after"] == model_lease.RETRY_AFTER_SECONDS
    assert body["holder"] == ""
    _hold(
        tmp_path,
        "foundry",
        until=99.0,
        holder="foundry-chronicle",
        last_renewed_at=42.0,
        renew_after=300,
    )
    body = await (await client.get("/api/model-lease")).json()
    assert body == {
        "state": "ready",
        "model": "foundry",
        "alias": "gemma-4-12b",
        "expires_at": 99.0,
        "holder": "foundry-chronicle",
        # The heartbeat (#1361): a consumer reads its own grace off these two
        # instead of having to know the box's arithmetic.
        "last_renewed_at": 42.0,
        "renew_after": 300,
        "retry_after": model_lease.RETRY_AFTER_SECONDS,
    }


async def test_delete_without_a_body_ends_the_window_and_is_idempotent(
    aiohttp_client, tmp_path
):
    """The bodyless call is the operator's way out and stays unconditional."""
    _hold(tmp_path, "foundry", holder="foundry-chronicle")
    client = await aiohttp_client(_app(tmp_path))
    r = await client.delete("/api/model-lease")
    assert r.status == 200
    assert await r.json() == {
        "ok": True,
        "state": "releasing",
        "retry_after": model_lease.RETRY_AFTER_SECONDS,
    }
    assert _request(tmp_path)["op"] == "release"
    # Nothing held: still a 200, and no work handed to the broker — a release
    # of nothing would restart the units behind their backs.
    model_lease.request_path(_db(tmp_path)).unlink()
    gpu_lease.lease_path(_db(tmp_path)).unlink()
    r = await client.delete("/api/model-lease")
    assert r.status == 200
    assert not model_lease.request_path(_db(tmp_path)).exists()


async def test_delete_answers_releasing_until_the_card_is_really_back(
    aiohttp_client, tmp_path
):
    """What the caller is told after a DELETE, and what it is told when it
    polls: the same word, and a `retry_after` to poll on. `released` is
    reserved for a window that is demonstrably gone (#1364)."""
    _hold(tmp_path, "foundry", until=777.0, holder="foundry-chronicle")
    client = await aiohttp_client(_app(tmp_path))
    r = await client.delete("/api/model-lease", json={"holder": "foundry-chronicle"})
    assert r.status == 200
    assert await r.json() == {
        "ok": True,
        "state": "releasing",
        "retry_after": model_lease.RETRY_AFTER_SECONDS,
    }
    body = await (await client.get("/api/model-lease")).json()
    assert body["state"] == "releasing"
    assert body["holder"] == "foundry-chronicle"
    assert body["expires_at"] is None
    assert body["retry_after"] == model_lease.RETRY_AFTER_SECONDS
    # The broker gets there: the lease file goes, and the answer is `none` —
    # which is what a consumer waits for instead of sending a second DELETE.
    gpu_lease.lease_path(_db(tmp_path)).unlink()
    body = await (await client.get("/api/model-lease")).json()
    assert body["state"] == "none"
    # And a DELETE on nothing is still the idempotent `released`.
    r = await client.delete("/api/model-lease", json={"holder": "foundry-chronicle"})
    assert await r.json() == {"ok": True, "state": "released"}


async def test_disabled_setting_refuses_the_endpoint(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path, enabled=False))
    r = await client.post("/api/model-lease", json={"model": "foundry", "ttl_s": 900})
    assert r.status == 503
    assert (await r.json())["reason"] == "disabled"
    assert (await client.get("/api/model-lease")).status == 503
    assert (await client.delete("/api/model-lease")).status == 503
    assert not model_lease.request_path(_db(tmp_path)).exists()


async def test_proxy_forwarded_request_is_not_a_neighbour(aiohttp_client, tmp_path):
    # NPM is hostNetwork on this same box, so its peer address is loopback too;
    # a forwarding header is what tells an outside caller from a neighbour.
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/model-lease",
        json={"model": "foundry", "ttl_s": 900},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert r.status == 403
    assert not model_lease.request_path(_db(tmp_path)).exists()


# ---- one alias, three surfaces --------------------------------------------


def test_whoami_names_the_same_alias_the_holder_was_given(tmp_path):
    """`/api/whoami.gpu_lease`, `GET /api/model-lease` and the `model` field of
    a `/v1` answer are one string, so no two of them can disagree."""
    _hold(tmp_path, "foundry")
    shown = gpu_lease.state(gpu_lease.lease_path(_db(tmp_path)))
    assert shown["alias"] == model_lease.state(_db(tmp_path))["alias"]
    assert shown["alias"] == "gemma-4-12b"


def test_the_lease_modes_still_mute_exactly_what_they_muted(tmp_path):
    """#1333 changed who asks, not what the resident gets: a foundry lease and
    a ready coding lease both keep answering, and only the swap itself mutes."""
    path = gpu_lease.lease_path(_db(tmp_path))
    _hold(tmp_path, "foundry")
    assert gpu_lease.mutes_chat(path) is False
    _hold(tmp_path, "coding")
    assert gpu_lease.mutes_chat(path) is False
    _hold(tmp_path, "coding", ready=False)
    assert gpu_lease.mutes_chat(path) is True
