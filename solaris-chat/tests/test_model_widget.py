"""The Modell tile — the GPU lease as a `kind: tool` widget (#1374).

What is pinned here is what a resident's window needs that a service's never
did: a duration or a target time instead of `ttl_s`, one row per profile
instead of one state, and a renewal loop that runs in the ENGINE because the
phone that tapped the button is back in a pocket.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import pytest

from solaris_chat import gpu_lease, model_lease, model_widget
from solaris_chat.server import build_app


def _db(tmp_path) -> str:
    return str(tmp_path / "solaris.db")


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


def _app(tmp_path):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
        notes_dir=str(tmp_path),
    )


def _at(*, hour: int, minute: int = 0, day: int = 8) -> float:
    return datetime(2026, 9, day, hour, minute).timestamp()


def _hold(tmp_path, model: str, *, holder: str = "widget", hours: float = 2.0) -> None:
    """The lease file the box's `gpu-lease.py` writes for a live window."""
    path = gpu_lease.lease_path(_db(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "holder": holder,
                "mode": model,
                "ready": True,
                "alias": model_lease.ALIASES[model],
                "until": time.time() + hours * 3600,
            }
        ),
        encoding="utf-8",
    )


# ---- the window a resident says out loud -----------------------------------


def test_a_fixed_duration_is_seconds():
    now = _at(hour=14)
    assert model_widget.parse_until("1h", now=now) == 3600
    assert model_widget.parse_until("2h", now=now) == 7200
    assert model_widget.parse_until("4 Stunden", now=now) == 14400
    assert model_widget.parse_until("90m", now=now) == 5400


def test_a_target_time_today_is_the_distance_to_it():
    now = _at(hour=14, minute=30)
    assert model_widget.parse_until("16:30", now=now) == 7200
    assert model_widget.parse_until("bis 16:30", now=now) == 7200
    assert model_widget.parse_until("heute 16:30", now=now) == 7200


def test_a_clock_that_has_gone_by_means_tomorrow():
    # "bis 07:00" said at teatime is the operator's "bis morgen 07:00" — the
    # rollover is what makes the plain clock usable at all.
    now = _at(hour=18)
    assert model_widget.parse_until("07:00", now=now) == 13 * 3600
    assert model_widget.parse_until("bis morgen 07:00", now=now) == 13 * 3600
    # …and an explicit "morgen" never resolves to today, even before that time.
    morning = _at(hour=6)
    assert model_widget.parse_until("07:00", now=morning) == 3600
    # 25 hours away is past the ceiling, so it comes back as the ceiling — the
    # one hour the small print costs, and only between midnight and 07:00.
    assert model_widget.parse_until("morgen 07:00", now=morning) == 86400


def test_a_dated_target_is_taken_as_written():
    now = _at(hour=14)
    assert model_widget.parse_until("2026-09-09T07:00", now=now) == 17 * 3600
    assert model_widget.parse_until("2026-09-09 07:00", now=now) == 17 * 3600


def test_the_window_is_capped_at_a_day_and_floored_at_the_swap():
    now = _at(hour=14)
    # 24 h is the new ceiling on both sides of the contract; a target further
    # out is clamped, not refused.
    assert model_widget.parse_until("2026-09-30T07:00", now=now) == 86400
    assert model_widget.parse_until("48h", now=now) == 86400
    # Below the floor a lease would expire inside its own swap.
    assert model_widget.parse_until("1m", now=now) == model_lease.TTL_MIN_SECONDS


def test_an_unreadable_window_is_refused_not_guessed():
    now = _at(hour=14)
    for bad in ("", None, "irgendwann", "25:00", "16:70", "bis", "2026-13-01T07:00"):
        with pytest.raises(ValueError):
            model_widget.parse_until(bad, now=now)
    with pytest.raises(ValueError, match="until_in_the_past"):
        model_widget.parse_until("2026-09-07T07:00", now=now)


def test_the_household_profile_is_a_lease_name_here_and_nowhere_else():
    assert model_widget.lease_model("household") == "household"
    assert model_widget.lease_model("coding") == "coding"
    with pytest.raises(ValueError, match="invalid_model"):
        model_widget.lease_model("gemma")
    # The contract itself still knows only the two real windows.
    assert "household" not in model_lease.MODELS


def test_the_cap_rose_to_a_day_without_moving_the_default():
    assert model_lease.TTL_MAX_SECONDS == 86400
    assert model_lease.parse_payload({"model": "coding", "ttl_s": 86400})[1] == 86400
    # A payload that names no window still gets the box's own 4 hours.
    assert model_lease.parse_payload({"model": "coding"})[1] == 14400


# ---- one row per profile ----------------------------------------------------


def _rows(lease, *, now=None):
    return {
        r["id"]: r
        for r in model_widget.rows(
            lease, household_alias="gemma-4-e4b", now=now or _at(hour=14)
        )
    }


def test_no_lease_reads_as_the_household_model_loaded():
    rows = _rows({"state": "none", "model": "", "holder": ""})
    assert list(rows) == ["household", "coding", "foundry"]
    assert rows["household"]["state"] == "active"
    assert rows["household"]["alias"] == "gemma-4-e4b"
    assert rows["household"]["meta"] == "gerade geladen"
    assert rows["coding"]["state"] == "available"
    assert rows["coding"]["alias"] == "qwen3.8-27b"
    assert rows["coding"]["meta"] == "nicht geladen"
    assert rows["foundry"]["alias"] == "gemma-4-12b"


def test_a_held_window_says_which_profile_and_until_when():
    rows = _rows(
        {
            "state": "ready",
            "model": "coding",
            "holder": "widget",
            "expires_at": _at(hour=16, minute=30),
        }
    )
    assert rows["coding"]["state"] == "active"
    assert rows["coding"]["status_text"] == "aktiv"
    assert rows["coding"]["meta"] == "gerade geladen · bis 16:30"
    assert rows["coding"]["remaining_s"] == 9000
    assert rows["household"]["state"] == "available"


def test_a_stranger_holding_the_card_is_named_on_the_row():
    rows = _rows(
        {
            "state": "ready",
            "model": "foundry",
            "holder": "pi-web",
            "expires_at": _at(hour=7, day=9),
        }
    )
    assert rows["foundry"]["meta"] == "gerade geladen · bis morgen 07:00 · von pi-web"
    assert rows["foundry"]["holder"] == "pi-web"


def test_the_house_keeps_the_card_until_the_swap_actually_lands():
    # `preparing` is the 12B still loading: llama-server is answering the
    # household model, so saying the house is not loaded would be a lie.
    rows = _rows({"state": "preparing", "model": "foundry", "holder": "widget"})
    assert rows["household"]["state"] == "active"
    assert rows["foundry"]["state"] == "preparing"
    assert "wird geladen" in rows["foundry"]["meta"]
    # …and while the card comes back the house is the one that is preparing.
    rows = _rows({"state": "releasing", "model": "foundry", "holder": "widget"})
    assert rows["household"]["state"] == "preparing"
    assert rows["foundry"]["state"] == "releasing"


# ---- the rows endpoint ------------------------------------------------------


async def test_the_rows_endpoint_serves_the_tile(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.get("/api/portal/models")
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert [row["id"] for row in body["models"]] == ["household", "coding", "foundry"]
    assert body["models"][0]["state"] == "active"
    assert body["retry_after"] == model_lease.RETRY_AFTER_SECONDS


async def test_the_native_twin_is_device_token_only(aiohttp_client, tmp_path):
    # The `/napi/` prefix is proxy-BYPASSED (#757): without a device token the
    # tile's own endpoint must never fall back to the household resident.
    client = await aiohttp_client(_app(tmp_path))
    r = await client.get("/napi/portal/models")
    assert r.status == 401


# ---- lease / release, wired to the action callback --------------------------


def _request(tmp_path) -> dict:
    return json.loads(
        model_lease.request_path(_db(tmp_path)).read_text(encoding="utf-8")
    )


async def test_an_enumerated_duration_takes_the_window_it_names(
    aiohttp_client, tmp_path
):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/action-callback",
        json={"action_id": "model.lease.4h", "params": {"model": "coding"}},
    )
    body = await r.json()
    assert body["ok"] is True
    request = _request(tmp_path)
    assert request["op"] == "acquire"
    assert request["model"] == "coding"
    assert request["holder"] == model_widget.HOLDER
    # The window is measured from the tap, so the sub-second on the way to the
    # broker is the only difference from the four hours it names.
    assert 14395 <= request["ttl_s"] <= 14400
    # The answer is the plain sentence the card shows, not a state machine.
    assert "Programmieren (Qwen)" in body["detail"]
    assert "bis " in body["detail"]


async def test_the_household_row_is_the_release_button(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    await client.post(
        "/api/action-callback",
        json={"action_id": "model.lease.1h", "params": {"model": "foundry"}},
    )
    _hold(tmp_path, "foundry")
    r = await client.post(
        "/api/action-callback",
        json={"action_id": "model.lease", "params": {"model": "household"}},
    )
    body = await r.json()
    assert body["ok"] is True
    assert _request(tmp_path)["op"] == "release"
    assert _request(tmp_path)["holder"] == model_widget.HOLDER


async def test_an_unreadable_window_answers_in_plain_language(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/action-callback",
        json={
            "action_id": "model.lease",
            "params": {"model": "coding", "until": "gleich"},
        },
    )
    body = await r.json()
    assert body["ok"] is False
    assert "Zeitangabe" in body["detail"]
    r = await client.post(
        "/api/action-callback",
        json={"action_id": "model.lease", "params": {"model": "gemma"}},
    )
    body = await r.json()
    assert body["ok"] is False
    assert "kenne ich nicht" in body["detail"]


async def test_a_window_somebody_else_holds_is_left_alone(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    _hold(tmp_path, "coding", holder="pi-web")
    r = await client.post(
        "/api/action-callback",
        json={"action_id": "model.lease.1h", "params": {"model": "foundry"}},
    )
    body = await r.json()
    assert body["ok"] is False
    assert "pi-web" in body["detail"]
    # Nothing was asked of the broker.
    assert not model_lease.request_path(_db(tmp_path)).exists()


async def test_every_button_the_def_offers_has_a_handler(aiohttp_client, tmp_path):
    """A def names its actions and the server auto-registers them from its
    handler pool (#1004). A button the callback answers `unknown_action` for is
    a dead end on the tile — the one thing a declarative plugin must not ship."""
    from pathlib import Path

    from solaris_chat import skills

    pack = str(
        Path(__file__).resolve().parents[2] / "templates/solaris/skills/household"
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
        notes_dir=str(tmp_path),
        skills_dir=pack,
    )
    client = await aiohttp_client(app)
    model = {d["tool-id"]: d for d in skills.list_tool_defs(pack)}["model"]
    for action_id in model["tool-actions"]:
        r = await client.post(
            "/api/action-callback",
            json={"action_id": action_id, "params": {"model": "household"}},
        )
        assert r.status == 200, action_id
        assert (await r.json())["ok"] is True, action_id


# ---- the renewal loop -------------------------------------------------------


def test_the_engine_renews_until_the_chosen_end_and_then_releases():
    """The box arms its release for two missed renewals, not for the TTL — so a
    four-hour window nobody renews would vanish after 40 minutes."""
    clock = {"t": 0.0}
    calls: list[tuple[str, int]] = []

    async def acquire(ttl):
        calls.append(("acquire", ttl))

    async def release():
        calls.append(("release", 0))

    async def sleep(seconds):
        clock["t"] += seconds

    asyncio.run(
        model_widget.hold(
            until=3600,
            acquire=acquire,
            release=release,
            sleep=sleep,
            now=lambda: clock["t"],
        )
    )
    assert calls[0] == ("acquire", 3600)
    # Every renewal asks for what is LEFT of the window, so the deadline never
    # walks forward: no auto-extension.
    assert [ttl for kind, ttl in calls if kind == "acquire"] == [
        3600,
        2400,
        1600,
        1067,
        712,
        475,
        317,
    ]
    assert calls[-1] == ("release", 0)
    assert clock["t"] >= 3600


def test_a_cancelled_loop_leaves_the_window_to_its_caller():
    """Switching profiles cancels the loop and does the release itself; a
    release from the loop as well would fight it — or, when the same profile is
    simply extended, would give back the window it is about to keep."""
    calls: list[str] = []

    async def acquire(_ttl):
        calls.append("acquire")

    async def release():  # pragma: no cover - must not run
        calls.append("release")

    async def go():
        task = asyncio.ensure_future(
            model_widget.hold(until=9e9, acquire=acquire, release=release)
        )
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert calls == ["acquire"]


def test_the_loop_waits_for_the_card_before_taking_the_next_profile():
    """A `DELETE` is answered while the broker is still restarting units
    (#1364); acquiring inside that window would be refused as `held`."""
    order: list[str] = []

    async def wait_free():
        order.append("waited")

    async def acquire(_ttl):
        order.append("acquire")

    async def release():
        order.append("release")

    asyncio.run(
        model_widget.hold(
            until=0,
            acquire=acquire,
            release=release,
            wait_free=wait_free,
            now=lambda: 0.0,
        )
    )
    # `until` already reached: it waits, takes nothing, and hands back.
    assert order == ["waited", "release"]
