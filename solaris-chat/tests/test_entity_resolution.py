"""The guessed entity_id is resolved in code, at the dispatch boundary (#1263).

#1241 stopped Solaris from switching the wrong device, but gemma4:e4b cannot use
the candidate list it gets back: measured on the box, "Mach das Sofalicht an"
guesses `light.sofalicht` 10/10, retries 0/6 after the rejection, and in 2 of 3
runs tells the resident "Klar. Das Sofalicht ist jetzt an." — a fabricated
success over a house that never moved. These tests lock the two halves of the
fix: one clear match is resolved and dispatched, everything else asks, and a
success sentence never survives a turn in which nothing was switched.

HA is stubbed (read-only, per the house rule) and every POST recorded, so
"actually switched the lamp" is an assertion about the real service call.
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat.engine import registry as registry_mod
from solaris_chat.engine.client import EngineClient, EngineProfile
from solaris_chat.engine.llama_server import ChatResult
from solaris_chat.engine.tools import Toolbox
from solaris_chat.engine.tools.choices import build_choice_tools
from solaris_chat.engine.tools.ha import build_ha_tools
from solaris_chat.engine.trace import TraceRecorder

from tests.test_confirm_gate import _stub_ha
from tests.test_engine import _SCHEMA, FakeChat

_HOUSE = {
    "light.dimmer_2_5": "Sofalicht",
    "light.dimmer_2_3": "Esszimmertischlicht",
    "cover.esszimmerjalousie": "Esszimmerjalousie",
    "lock.schloss_1": "Haustürschloss",
}


class _Registry:
    """Duck-typed EntityRegistry over a fixed inventory — the real ranking and
    resolution policy, without HA."""

    def __init__(self, index: dict[str, str], classes: dict[str, str] | None = None):
        self._index = dict(index)
        self._classes = classes or {}

    async def check_entity(self, entity_id: str) -> tuple[bool, list[str]]:
        if entity_id in self._index:
            return True, []
        return False, registry_mod.suggest_entities(entity_id, self._index)

    async def resolve(self, entity_id: str) -> registry_mod.Resolution:
        if entity_id in self._index:
            return registry_mod.Resolution(
                entity_id=entity_id, name=self._index[entity_id]
            )
        return registry_mod.resolve_entity(entity_id, self._index)

    async def device_class(self, entity_id: str) -> str | None:
        return self._classes.get(entity_id)

    async def prompt_block(self) -> str:
        return ""


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def soul(tmp_path) -> str:
    path = tmp_path / "SOUL.md"
    path.write_text("Du bist Solaris.", encoding="utf-8")
    return str(path)


def _client(db, soul, results, registry: _Registry) -> EngineClient:
    tools = (
        build_ha_tools("http://ha", "tok", check_entity=registry.check_entity)
        + build_choice_tools()
    )
    return EngineClient(
        EngineProfile(
            name="household",
            model="gemma4:e2b",
            soul_path=soul,
            registry=registry,  # type: ignore[arg-type]
            toolbox=Toolbox(tools),
        ),
        db_path=db,
        chat=FakeChat(results),
        recorder=TraceRecorder(),
        context_window=32768,
    )


def _call(domain: str, service: str, entity_id: str) -> ChatResult:
    return ChatResult(
        tool_calls=[
            {
                "function": {
                    "name": "ha_call_service",
                    "arguments": {
                        "domain": domain,
                        "service": service,
                        "entity_id": entity_id,
                    },
                }
            }
        ]
    )


def _answer(events: list[dict]) -> str:
    completed = [e for e in events if e["type"] == "run.completed"][0]
    return completed["data"]["messages"][0]["content"]


def _streamed(events: list[dict]) -> str:
    return "".join(e["data"]["delta"] for e in events if e["type"] == "assistant.delta")


def _chips(events: list[dict]) -> list[str]:
    qr = [e for e in events if e["type"] == "quick_replies"]
    return qr[0]["data"]["options"] if qr else []


@pytest.mark.asyncio
async def test_guessed_light_is_resolved_and_actually_switched(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client = _client(
        db,
        soul,
        [
            _call("light", "turn_on", "light.sofalicht"),
            ChatResult(content="Klar, das Sofalicht ist jetzt an."),
        ],
        _Registry(_HOUSE),
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Mach das Sofalicht an")]

    assert len(posts) == 1
    url, body = posts[0]
    assert url.endswith("/api/services/light/turn_on")
    assert body["entity_id"] == "light.dimmer_2_5"
    # The lamp really moved, so the model's report stands unchanged.
    assert _answer(events) == "Klar, das Sofalicht ist jetzt an."


@pytest.mark.asyncio
async def test_ambiguous_guess_asks_and_no_fabricated_success_survives(
    db, soul, monkeypatch
):
    posts = _stub_ha(monkeypatch)
    house = {"light.dimmer_1": "Deckenlicht Küche", "light.dimmer_2": "Deckenlicht Bad"}
    client = _client(
        db,
        soul,
        [
            _call("light", "turn_on", "light.deckenlicht"),
            # what e4b actually said on the box after the refusal
            ChatResult(content="Klar. Das Deckenlicht ist jetzt an."),
        ],
        _Registry(house),
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Mach das Deckenlicht an")]

    assert posts == []
    answer = _answer(events)
    assert answer.startswith("Welches Gerät meinst du")
    assert set(_chips(events)) == {"Deckenlicht Küche", "Deckenlicht Bad"}
    # The fabricated confirmation reached the resident nowhere — not as the
    # final answer, and not streamed token-by-token on the way there.
    assert "ist jetzt an" not in answer
    assert "ist jetzt an" not in _streamed(events)


@pytest.mark.asyncio
async def test_cover_slug_on_the_light_domain_asks_instead_of_switching(
    db, soul, monkeypatch
):
    posts = _stub_ha(monkeypatch)
    client = _client(
        db,
        soul,
        [
            _call("light", "turn_on", "light.esszimmerjalousie"),
            ChatResult(content="Das Licht im Esszimmer ist jetzt an."),
        ],
        _Registry(_HOUSE),
    )
    sid = await client.create_session("anna")
    events = [
        e async for e in client.chat_stream(sid, "Mach im Esszimmer das Licht an")
    ]

    assert posts == []
    assert _answer(events).startswith("Welches Gerät meinst du")


@pytest.mark.asyncio
async def test_resolved_lock_is_never_switched_without_a_confirmation(
    db, soul, monkeypatch
):
    """A wrong light is forgivable, a wrong lock is not.

    The resolution runs BEFORE the confirmation gate, so a guessed lock id
    resolves to the real one and then hits the gate under the REAL device's
    name — held, ja/nein, nothing dispatched. There is no path on which a
    resolved lock action reaches HA silently.
    """
    posts = _stub_ha(monkeypatch)
    client = _client(
        db,
        soul,
        [
            _call("lock", "unlock", "lock.haustuerschloss"),
            ChatResult(content="Die Haustür ist jetzt entsperrt."),
        ],
        _Registry(_HOUSE),
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Schließ die Haustür auf")]

    assert posts == []
    assert _chips(events) == ["ja", "nein"]
    # The held action carries the RESOLVED id, and the question names the real
    # device — a mis-resolution is visible before anything opens.
    pending = client._pending.peek(sid)
    assert pending is not None
    assert pending.entity_id == "lock.schloss_1"
    assert _answer(events) == "Soll ich Haustürschloss wirklich aufschließen?"


@pytest.mark.asyncio
async def test_confirmed_lock_executes_the_resolved_entity(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client = _client(
        db,
        soul,
        [
            _call("lock", "unlock", "lock.haustuerschloss"),
            ChatResult(content="Soll ich Haustürschloss wirklich aufschließen?"),
            ChatResult(content="Erledigt, die Haustür ist offen."),
        ],
        _Registry(_HOUSE),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Schließ die Haustür auf")]
    assert posts == []

    _ = [e async for e in client.chat_stream(sid, "ja")]
    assert len(posts) == 1
    url, body = posts[0]
    assert url.endswith("/api/services/lock/unlock")
    assert body["entity_id"] == "lock.schloss_1"
