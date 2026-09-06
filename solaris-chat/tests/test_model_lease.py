"""The neighbour-service GPU lease over HTTP (#1333, contract
mdopp/foundry-chronicle#321).

The endpoint keeps the shape foundry built against in #1260 and now fronts the
box's GPU lease. What is pinned here is the state machine they poll — 200
`ready` / 202 `preparing` / 409 `held` / 400 / 503 — the request file the host
broker acts on, and the alias every surface reports the loaded model by.
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


def _hold(tmp_path, model="foundry", *, ready=True, until=9e9, holder=""):
    """Write the lease file the box's `gpu-lease.py` writes."""
    path = gpu_lease.lease_path(_db(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "holder": holder or model,
                "mode": model,
                "model": "Gemma 4 12B",
                "alias": model_lease.ALIASES[model],
                "until": until,
                "ready": ready,
            }
        ),
        "utf-8",
    )


def _request(tmp_path) -> dict:
    return json.loads(model_lease.request_path(_db(tmp_path)).read_text("utf-8"))


# ---- the payload is closed -------------------------------------------------


def test_payload_key_set_is_exactly_model_and_ttl_s():
    assert model_lease.PAYLOAD_KEYS == ("model", "ttl_s")
    assert model_lease.parse_payload({"model": "foundry", "ttl_s": 900}) == (
        "foundry",
        900,
    )
    assert model_lease.parse_payload({"model": "coding", "ttl_s": 900})[0] == "coding"
    # The key is theirs: the pre-contract `ttl` spelling is an unknown field.
    with pytest.raises(ValueError, match="unexpected_field"):
        model_lease.parse_payload({"model": "foundry", "ttl": 600})
    # No session, round or guild identifier may be smuggled in later.
    for extra in ("session", "session_id", "round", "guild", "guild_id"):
        with pytest.raises(ValueError, match="unexpected_field"):
            model_lease.parse_payload({"model": "foundry", "ttl_s": 900, extra: "x"})


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
    # The window is OUR safety net: outside 300…14400 it is clamped, not refused.
    assert model_lease.parse_payload({"model": "foundry", "ttl_s": 86400})[1] == 14400
    assert model_lease.parse_payload({"model": "foundry", "ttl_s": 30})[1] == 300
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
    }


def test_a_ready_lease_reports_its_alias_and_deadline(tmp_path):
    _hold(tmp_path, "coding", until=1234.0)
    assert model_lease.state(_db(tmp_path)) == {
        "state": "ready",
        "model": "coding",
        "alias": "qwen3.8-27b",
        "expires_at": 1234.0,
    }


def test_a_request_the_broker_has_not_picked_up_yet_is_preparing(tmp_path):
    """Between the POST and the broker's first write there is no lease file at
    all; reading that as "none" would tell the holder its request was lost."""
    db = _db(tmp_path)
    model_lease.write_request(db, "acquire", "foundry", 900, now=7.0)
    assert model_lease.state(db)["state"] == "preparing"
    # Once the broker has answered that request, it is no longer pending.
    model_lease.status_path(db).write_text(
        json.dumps({"requested_at": 7.0, "state": "error"}), "utf-8"
    )
    assert model_lease.state(db)["state"] == "none"


def test_an_unreadable_or_exclusive_lease_reads_as_no_lease(tmp_path):
    """Fail open: an exclusive lease (#1320) is not a neighbour's window, and
    half a file is not a contract."""
    path = gpu_lease.lease_path(_db(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    for junk in ("{not json", json.dumps(["a"]), json.dumps({"mode": "exclusive"})):
        path.write_text(junk, "utf-8")
        assert model_lease.state(_db(tmp_path))["state"] == "none"


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
    _hold(tmp_path, "foundry", until=99.0)
    body = await (await client.get("/api/model-lease")).json()
    assert body == {
        "state": "ready",
        "model": "foundry",
        "alias": "gemma-4-12b",
        "expires_at": 99.0,
        "retry_after": model_lease.RETRY_AFTER_SECONDS,
    }


async def test_delete_ends_the_window_and_is_idempotent(aiohttp_client, tmp_path):
    _hold(tmp_path, "foundry")
    client = await aiohttp_client(_app(tmp_path))
    r = await client.delete("/api/model-lease")
    assert r.status == 200
    assert await r.json() == {"ok": True, "state": "released"}
    assert _request(tmp_path)["op"] == "release"
    # Nothing held: still a 200, and no work handed to the broker — a release
    # of nothing would restart the units behind their backs.
    model_lease.request_path(_db(tmp_path)).unlink()
    gpu_lease.lease_path(_db(tmp_path)).unlink()
    r = await client.delete("/api/model-lease")
    assert r.status == 200
    assert not model_lease.request_path(_db(tmp_path)).exists()


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
