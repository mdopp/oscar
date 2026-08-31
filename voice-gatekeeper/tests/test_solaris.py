"""Tests for the SolarisClient — the gatekeeper's engine-facade client."""

from __future__ import annotations

import json

import httpx
from gatekeeper.solaris import MODEL, SolarisClient, _extract_reply


class _FakeEngine:
    """Captures /api/chat bodies and replies with scripted content."""

    def __init__(self, replies: list[str] | None = None, status: int = 200):
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        self._replies = replies or ["Erledigt."]
        self._status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        if self._status != 200:
            return httpx.Response(self._status, text="boom")
        reply = self._replies[min(len(self.bodies) - 1, len(self._replies) - 1)]
        return httpx.Response(
            200,
            json={
                "model": "solaris",
                "message": {"role": "assistant", "content": reply},
                "done": True,
            },
        )


def _client(fake: _FakeEngine, token: str = "") -> SolarisClient:
    return SolarisClient(
        "http://engine/ollama", token, transport=httpx.MockTransport(fake.handler)
    )


async def test_fast_turn_runs_on_sol():
    fake = _FakeEngine(["Licht ist an."])
    client = _client(fake)
    reply = await client.converse(
        text="schalte das licht an",
        uid="michael",
        endpoint="e",
        trace_id="t",
        remember=True,
    )
    assert reply == "Licht ist an."
    assert fake.bodies[0]["model"] == MODEL
    assert fake.bodies[0]["stream"] is False
    assert fake.bodies[0]["user"] == "michael"


async def test_thorough_cue_stays_on_the_one_model():
    # `solaris-deep` is gone (#1121): a "think harder" cue no longer routes to a
    # second facade model — that model name would 404 and the voice turn would
    # answer with silence.
    fake = _FakeEngine(["Lass mich nachdenken …"])
    client = _client(fake)
    await client.converse(
        text="denk gründlich nach: warum ist der himmel blau",
        uid="michael",
        endpoint="e",
        trace_id="t",
        remember=True,
    )
    assert fake.bodies[0]["model"] == MODEL


async def test_room_hint_prefixes_turn():
    fake = _FakeEngine()
    client = _client(fake)
    await client.converse(
        text="mach das licht aus",
        uid="michael",
        endpoint="e",
        trace_id="t",
        remember=True,
        location="Büro",
    )
    sent = fake.bodies[0]["messages"][-1]["content"]
    assert sent.startswith("[room: Büro]\n")
    assert sent.endswith("mach das licht aus")


async def test_history_rides_following_turns():
    fake = _FakeEngine(["Antwort eins.", "Antwort zwei."])
    client = _client(fake)
    await client.converse(
        text="erste frage", uid="michael", endpoint="e", trace_id="t", remember=True
    )
    await client.converse(
        text="zweite frage", uid="michael", endpoint="e", trace_id="t", remember=True
    )
    second = fake.bodies[1]["messages"]
    contents = [m["content"] for m in second]
    assert "erste frage" in contents
    assert "Antwort eins." in contents
    assert second[-1]["content"] == "zweite frage"


async def test_unmatched_speakers_never_share_a_history_bucket():
    # Two people speaker-ID failed to match — a delivery person at the door,
    # then hours later a babysitter — both arrive as uid `guest`. Before #1289
    # that single sentinel keyed one rolling history, so the second person's
    # turn was prompted with the first person's exchange.
    fake = _FakeEngine(["In Tokio ist es 20 Uhr.", "Es ist 12 Uhr."])
    client = _client(fake)
    await client.converse(
        text="wie spät ist es in tokio",
        uid="guest",
        endpoint="voice-pe:flur",
        trace_id="t1",
        remember=False,
    )
    await client.converse(
        text="wie spät ist es",
        uid="guest",
        endpoint="voice-pe:flur",
        trace_id="t2",
        remember=False,
    )
    second = fake.bodies[1]["messages"]
    assert second == [{"role": "user", "content": "wie spät ist es"}]
    assert client._history == {}


async def test_error_returns_empty_and_keeps_history_clean():
    fake = _FakeEngine(status=500)
    client = _client(fake)
    reply = await client.converse(
        text="hallo", uid="michael", endpoint="e", trace_id="t", remember=True
    )
    assert reply == ""
    # The failed turn must not pollute the rolling history: the next turn
    # starts with only its own user message.
    fake2 = _FakeEngine(["Hi."])
    client2 = SolarisClient(
        "http://engine/ollama", "", transport=httpx.MockTransport(fake2.handler)
    )
    client2._history = client._history  # carry over the (empty) history map
    await client2.converse(
        text="hallo", uid="michael", endpoint="e", trace_id="t", remember=True
    )
    assert len(fake2.bodies[0]["messages"]) == 1


async def test_bearer_token_sent_when_set():
    fake = _FakeEngine()
    client = _client(fake, token="secret")
    await client.converse(
        text="hallo", uid="michael", endpoint="e", trace_id="t", remember=True
    )
    assert fake.headers[0].get("authorization") == "Bearer secret"


def test_extract_reply_shapes():
    assert _extract_reply({"message": {"content": "x"}}) == "x"
    assert _extract_reply({"message": {}}) == ""
    assert _extract_reply({}) == ""
    assert _extract_reply("nope") == ""
