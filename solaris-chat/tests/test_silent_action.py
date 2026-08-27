"""A turn that switched devices always says so (#1267).

Measured on the box (v0.44.3): "Mach das Licht im Wohnzimmer an" dispatched
`light.turn_on` on four real entities in one model pass, and `/api/chat` came
back `{"ok": true, "reply": ""}` — four lamps changed in the flat and the
resident was told nothing. Switching every light of the room is the RIGHT
reading of a room-wide command; the bug is only the missing sentence. Silence
is unreadable: it looks exactly like a command that never arrived, so the
resident says it again.

Same family as #1263 in the other direction — there a deed was claimed that
never happened, here a deed happens and is never named.

HA is stubbed (read-only, per the house rule) and every POST recorded, so
"actually switched the lamps" is an assertion about the real service calls.
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat.engine.client import EngineClient, EngineProfile
from solaris_chat.engine.ollama import ChatResult
from solaris_chat.engine.tools import Toolbox
from solaris_chat.engine.tools.choices import build_choice_tools
from solaris_chat.engine.tools.ha import build_ha_tools
from solaris_chat.engine.trace import TraceRecorder

from tests.test_confirm_gate import _stub_ha
from tests.test_engine import _SCHEMA, FakeOllama
from tests.test_entity_resolution import _Registry

# The real Wohnzimmer as Home Assistant files it — the dining-room devices sit
# in the area "Wohnzimmer" by house configuration, which is the operator's to
# change, not ours (#1267 correction).
_WOHNZIMMER = {
    "light.dimmer_2_10": "Wohnzimmerwandlicht",
    "light.dimmer_2_3": "Esszimmertischlicht",
    "light.dimmer_2_4": "Esszimmerwandlicht",
    "light.dimmer_2_5": "Sofalicht",
    "cover.esszimmerjalousie": "Esszimmerjalousie",
}


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
        ollama=FakeOllama(results),
        recorder=TraceRecorder(),
        context_window=32768,
    )


def _calls(*specs: tuple[str, str, str]) -> ChatResult:
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
            for domain, service, entity_id in specs
        ]
    )


def _answer(events: list[dict]) -> str:
    completed = [e for e in events if e["type"] == "run.completed"][0]
    return completed["data"]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_multi_call_round_never_ends_silent(db, soul, monkeypatch):
    """The measured failure: four lamps switched, the model says nothing."""
    posts = _stub_ha(monkeypatch)
    client = _client(
        db,
        soul,
        [
            _calls(
                ("light", "turn_on", "light.dimmer_2_10"),
                ("light", "turn_on", "light.dimmer_2_3"),
                ("light", "turn_on", "light.dimmer_2_4"),
                ("light", "turn_on", "light.dimmer_2_5"),
            ),
            # what the box returned: an empty closing turn
            ChatResult(content=""),
        ],
        _Registry(_WOHNZIMMER),
    )
    sid = await client.create_session("anna")
    events = [
        e async for e in client.chat_stream(sid, "Mach das Licht im Wohnzimmer an")
    ]

    # Switching every light of the room stays right — that is not the bug.
    assert len(posts) == 4
    answer = _answer(events)
    assert answer, "a turn that switched four lamps must not return an empty reply"
    # The sentence names what was done, by the names the resident knows.
    assert (
        answer == "Erledigt — Wohnzimmerwandlicht, Esszimmertischlicht, "
        "Esszimmerwandlicht und Sofalicht sind jetzt an."
    )


@pytest.mark.asyncio
async def test_mixed_actions_are_named_by_their_own_outcome(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client = _client(
        db,
        soul,
        [
            _calls(
                ("light", "turn_off", "light.dimmer_2_5"),
                ("cover", "close", "cover.esszimmerjalousie"),
            ),
            ChatResult(content="   "),
        ],
        _Registry(_WOHNZIMMER),
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Mach Feierabend im Wohnzimmer")]

    assert len(posts) == 2
    # The cover's natural verb is normalised the way the POST normalises it.
    assert posts[1][0].endswith("/api/services/cover/close_cover")
    assert (
        _answer(events)
        == "Erledigt — Sofalicht ist jetzt aus, Esszimmerjalousie ist jetzt zu."
    )


@pytest.mark.asyncio
async def test_single_tool_turn_is_unchanged(db, soul, monkeypatch):
    """The one-call round already answered correctly — leave it alone."""
    posts = _stub_ha(monkeypatch)
    client = _client(
        db,
        soul,
        [
            _calls(("light", "turn_on", "light.dimmer_2_5")),
            ChatResult(content="Klar."),
        ],
        _Registry(_WOHNZIMMER),
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Mach das Sofalicht an")]

    assert len(posts) == 1
    assert _answer(events) == "Klar."


@pytest.mark.asyncio
async def test_a_turn_that_switched_nothing_gets_no_confirmation(db, soul, monkeypatch):
    """The guard must never invent a deed: an empty turn after a REFUSED action
    still reads as nothing done (#1263 stays in force)."""
    posts = _stub_ha(monkeypatch)
    client = _client(
        db,
        soul,
        [
            _calls(("light", "turn_on", "light.deckenlicht")),
            ChatResult(content=""),
        ],
        _Registry({"light.a1": "Deckenlicht Küche", "light.a2": "Deckenlicht Bad"}),
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Mach das Deckenlicht an")]

    assert posts == []
    answer = _answer(events)
    assert "Erledigt" not in answer
    assert answer.startswith("Welches Gerät meinst du")
