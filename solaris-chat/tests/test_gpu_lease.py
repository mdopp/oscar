"""The GPU lease seen from the Engine (#1320) and its coding window (#1319).

While foundry holds the card, `llama.service` is stopped. The turn must end in
one honest German sentence instead of a timeout against a dead socket — and it
must not reach the network at all, because there is nothing there to answer.

A coding lease is the opposite case: llama-server is up on the coding model, so
the turn must actually be sent, and the chat has to say whose answers these are
and until when.
"""

from __future__ import annotations

import json
import pathlib

from solaris_chat import gpu_lease
from solaris_chat.engine.llama_server import LlamaServerChat
from solaris_chat.server import STATIC_DIR, build_app


def _held(tmp_path, holder="foundry"):
    path = tmp_path / gpu_lease.LEASE_FILENAME
    path.write_text(json.dumps({"holder": holder, "since": 1.0}), "utf-8")
    return path


def test_lease_path_sits_beside_the_database():
    assert gpu_lease.lease_path("/var/lib/solaris/solaris.db") == pathlib.Path(
        "/var/lib/solaris/gpu_lease.json"
    )


def test_no_file_is_no_lease(tmp_path):
    assert gpu_lease.is_leased(tmp_path / gpu_lease.LEASE_FILENAME) is False
    assert gpu_lease.is_leased("") is False


def test_a_half_written_lease_still_counts_as_held(tmp_path):
    """The units are stopped either way; reading a truncated file as "free"
    would send the turn into a dead socket."""
    path = tmp_path / gpu_lease.LEASE_FILENAME
    path.write_text('{"holder": "found', "utf-8")
    assert gpu_lease.is_leased(path) is True
    assert gpu_lease.holder(path) == ""


def test_holder_names_the_job(tmp_path):
    assert gpu_lease.holder(_held(tmp_path, "coder")) == "coder"


async def test_a_leased_turn_answers_the_fixed_sentence_without_a_request(
    tmp_path, monkeypatch
):
    def explode(*a, **k):  # pragma: no cover - only runs on a regression
        raise AssertionError("talked to llama-server while the card was leased")

    monkeypatch.setattr("aiohttp.ClientSession", explode)
    chat = LlamaServerChat("http://127.0.0.1:11435", lease_path=str(_held(tmp_path)))

    events = [ev async for ev in chat.stream("m", [{"role": "user", "content": "hi"}])]

    assert events[0] == ("delta", gpu_lease.BUSY_REPLY)
    kind, result = events[-1]
    assert kind == "done"
    assert result.content == gpu_lease.BUSY_REPLY
    assert result.tool_calls == []


async def test_a_leased_turn_calls_no_tool(tmp_path, monkeypatch):
    """ "Licht an" during a lease must be refused honestly, not half-executed:
    the model that would decide is the one that is unloaded."""
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: 1 / 0)
    chat = LlamaServerChat("http://127.0.0.1:11435", lease_path=str(_held(tmp_path)))
    tools = [{"type": "function", "function": {"name": "ha_turn_on"}}]

    events = [
        ev
        async for ev in chat.stream(
            "m", [{"role": "user", "content": "Licht an"}], tools=tools
        )
    ]

    assert [k for k, _ in events] == ["delta", "done"]
    assert events[-1][1].tool_calls == []


# ── #1319: the coding lease answers instead of muting ──────────────────────


def _coding(tmp_path, ready=True, until=2_000_000_000.0):
    path = tmp_path / gpu_lease.LEASE_FILENAME
    path.write_text(
        json.dumps(
            {
                "holder": "coder",
                "since": 1.0,
                "until": until,
                "mode": "coding",
                "model": "Qwen 3.8 27B",
                "ready": ready,
            }
        ),
        "utf-8",
    )
    return path


def test_an_exclusive_lease_mutes_the_chat(tmp_path):
    assert gpu_lease.mutes_chat(_held(tmp_path)) is True


def test_a_live_coding_lease_does_not_mute_the_chat(tmp_path):
    """Mode B: llama-server is serving the coding model, so the household turn
    goes to it — a fixed sentence would be a worse answer than a real one."""
    assert gpu_lease.mutes_chat(_coding(tmp_path)) is False


def test_a_coding_lease_still_loading_mutes(tmp_path):
    """The ~2 minutes between the swap and the model answering are the dead
    socket the fixed sentence exists for."""
    assert gpu_lease.mutes_chat(_coding(tmp_path, ready=False)) is True


def test_no_lease_mutes_nothing(tmp_path):
    assert gpu_lease.mutes_chat(tmp_path / gpu_lease.LEASE_FILENAME) is False


def test_the_state_the_banner_is_built_from(tmp_path):
    assert gpu_lease.state(_coding(tmp_path)) == {
        "mode": "coding",
        "model": "Qwen 3.8 27B",
        "until": 2_000_000_000.0,
        "answers": True,
    }
    assert gpu_lease.state(_held(tmp_path)) == {
        "mode": "exclusive",
        "model": "",
        "until": 0.0,
        "answers": False,
    }
    assert gpu_lease.state(tmp_path / "free" / gpu_lease.LEASE_FILENAME) is None


async def test_a_coding_turn_reaches_the_model(tmp_path, monkeypatch):
    """The whole point of mode B — the request must actually be sent."""
    sent: list[object] = []
    monkeypatch.setattr(
        "aiohttp.ClientSession",
        lambda *a, **k: sent.append(True) or _raise_stop(),
    )
    chat = LlamaServerChat("http://127.0.0.1:11435", lease_path=str(_coding(tmp_path)))
    try:
        async for _ in chat.stream("m", [{"role": "user", "content": "hi"}]):
            pass
    except _Sent:
        pass
    assert sent == [True]


class _Sent(Exception):
    pass


def _raise_stop():
    raise _Sent


# ── the resident's surface ─────────────────────────────────────────────────


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


def _app(db_path: str):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db_path,
    )


async def test_whoami_carries_the_window_the_banner_names(aiohttp_client, tmp_path):
    _coding(tmp_path)
    client = await aiohttp_client(_app(str(tmp_path / "solaris.db")))

    lease = (await (await client.get("/api/whoami")).json())["gpu_lease"]

    assert lease["mode"] == "coding"
    assert lease["model"] == "Qwen 3.8 27B"
    assert lease["answers"] is True


async def test_whoami_says_nothing_when_the_card_is_free(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(str(tmp_path / "solaris.db")))

    assert (await (await client.get("/api/whoami")).json())["gpu_lease"] is None


def test_the_banner_is_wired_to_the_whoami_lease_token():
    """Frontend contract, same shape as the outage notice: one element above
    the views, driven by `gpu_lease`, re-asked on the running 30 s poll, and
    worded so the resident knows what changed and until when."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="gpu-notice"' in html
    assert "function applyGpuLease(lease)" in html
    assert "applyGpuLease(j && j.gpu_lease)" in html
    assert "🖥️ Programmierfenster" in html
