"""Code-enforced 'merk dir …' capture (#621).

The SOUL asks Solaris to proactively store a memorable statement via
fact_store/note_write, but gemma4:e4b confirms conversationally ('Klar, merke
ich mir.') and skips the tool about as often as it obeys — so nothing durably
persists. This locks the code path: when the user's turn is an explicit
remember-this request and the model dispatched NO store tool, the engine stores
the fact itself, scoped to the speaker; when the model DID store it, the engine
does not double-write.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from solaris_chat.engine import remember
from solaris_chat.engine.ollama import ChatResult
from solaris_chat.engine.tools.notes import build_notes_tools

from tests.test_engine import _SCHEMA, _client


# -- policy units ---------------------------------------------------------


def test_detects_merk_dir_directives():
    assert (
        remember.wants_remember("Merk dir, dass das Auto in der Tiefgarage steht.")
        == "das Auto in der Tiefgarage steht"
    )
    assert remember.wants_remember("merke dir: Anna mag Tee") == "Anna mag Tee"
    assert (
        remember.wants_remember("Notier dir den Geburtstag am 3. Mai")
        == "den Geburtstag am 3. Mai"
    )
    assert remember.wants_remember("Behalte, dass die Tür klemmt") == "die Tür klemmt"
    assert (
        remember.wants_remember("Remember that the wifi password is on the fridge")
        == "the wifi password is on the fridge"
    )
    assert (
        remember.wants_remember("Bitte merk dir, wo der Schlüssel liegt")
        == "wo der Schlüssel liegt"
    )


def test_ignores_non_remember_turns():
    assert remember.wants_remember("Wo steht mein Auto?") is None
    assert remember.wants_remember("Mach das Licht an") is None
    assert remember.wants_remember("") is None
    # a trigger word with no following content stores nothing
    assert remember.wants_remember("merk dir") is None
    assert remember.wants_remember("Danke, gut gemerkt") is None


# -- the gate, end to end -------------------------------------------------


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


def _notes_tools(notes_dir: str):
    # uid getter reads the turn's pinned resident, same as the real profile.
    from solaris_chat.engine.client import current_uid

    return build_notes_tools(notes_dir, current_uid.get)


def _stored_facts(notes_dir: str, uid: str) -> list[str]:
    facts_dir = Path(notes_dir) / "users" / uid / "facts"
    if not facts_dir.is_dir():
        return []
    return [p.read_text(encoding="utf-8") for p in sorted(facts_dir.glob("*.md"))]


@pytest.mark.asyncio
async def test_merk_dir_stores_fact_when_model_skips_tool(db, soul, tmp_path):
    notes_dir = str(tmp_path / "notes")
    # The model just confirms conversationally, calling no store tool — the gap.
    client, _ = _client(
        db,
        soul,
        [ChatResult(content="Klar, merke ich mir.")],
        tools=_notes_tools(notes_dir),
    )
    sid = await client.create_session("anna")
    _ = [
        e
        async for e in client.chat_stream(
            sid, "Merk dir, dass das Auto in der Tiefgarage steht."
        )
    ]

    facts = _stored_facts(notes_dir, "anna")
    assert len(facts) == 1
    assert "das Auto in der Tiefgarage steht" in facts[0]
    assert "added_by: anna" in facts[0]


@pytest.mark.asyncio
async def test_no_double_write_when_model_stores_it(db, soul, tmp_path):
    notes_dir = str(tmp_path / "notes")
    client, _ = _client(
        db,
        soul,
        [
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "fact_store",
                            "arguments": {"fact": "das Auto steht in der Tiefgarage"},
                        }
                    }
                ]
            ),
            ChatResult(content="Erledigt, gemerkt."),
        ],
        tools=_notes_tools(notes_dir),
    )
    sid = await client.create_session("anna")
    _ = [
        e
        async for e in client.chat_stream(
            sid, "Merk dir, dass das Auto in der Tiefgarage steht."
        )
    ]
    # exactly one fact file — the model's own store, no code-enforced duplicate
    assert len(_stored_facts(notes_dir, "anna")) == 1


@pytest.mark.asyncio
async def test_ordinary_turn_stores_nothing(db, soul, tmp_path):
    notes_dir = str(tmp_path / "notes")
    client, _ = _client(
        db,
        soul,
        [ChatResult(content="Dein Auto steht in der Tiefgarage.")],
        tools=_notes_tools(notes_dir),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Wo steht mein Auto?")]
    assert _stored_facts(notes_dir, "anna") == []


# -- deterministic routing (#1336) ----------------------------------------


def test_detects_the_wider_memory_intents():
    assert (
        remember.wants_remember("Denk daran, dass der Müll dienstags kommt")
        == "der Müll dienstags kommt"
    )
    assert remember.wants_remember("denk dran: Öl nachfüllen") == "Öl nachfüllen"
    assert (
        remember.wants_remember("Vergiss nicht, dass Oma Montag kommt")
        == "Oma Montag kommt"
    )
    assert (
        remember.wants_remember("Kannst du dir merken, dass der Code 1234 ist")
        == "der Code 1234 ist"
    )


def test_a_trigger_word_inside_another_word_is_not_a_directive():
    # A trigger must end on whitespace — these are ordinary sentences.
    assert remember.wants_remember("merkwürdig, dass das Licht an ist") is None
    assert remember.wants_remember("Notizen von gestern zeigen") is None
    assert remember.wants_remember("Denk daran!") is None
    assert remember.wants_remember("Vergiss nicht") is None


_FACT_STORE_DEF = {"type": "function", "function": {"name": "fact_store"}}
_RADIO_DEF = {"type": "function", "function": {"name": "play_radio"}}


def test_routed_pass_forces_fact_store_alone():
    tools, choice = remember.routed_pass(
        "merk dir, dass der Müll dienstags kommt", [_RADIO_DEF, _FACT_STORE_DEF]
    )
    assert tools == [_FACT_STORE_DEF]
    assert choice == "required"


def test_routed_pass_leaves_other_turns_alone():
    toolbox = [_RADIO_DEF, _FACT_STORE_DEF]
    assert remember.routed_pass("spiel Radio Bayern 3", toolbox) == (toolbox, "")
    # nothing to force when the profile has no fact_store
    assert remember.routed_pass("merk dir das Auto", [_RADIO_DEF]) == ([_RADIO_DEF], "")
    assert remember.routed_pass("merk dir das Auto", None) == (None, "")


@pytest.mark.asyncio
async def test_merk_dir_turn_forces_the_tool_call_on_the_first_pass(db, soul, tmp_path):
    notes_dir = str(tmp_path / "notes")
    client, fake = _client(
        db,
        soul,
        [
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "fact_store",
                            "arguments": {"fact": "der Müll kommt dienstags"},
                        }
                    }
                ]
            ),
            ChatResult(content="Gemerkt."),
        ],
        tools=_notes_tools(notes_dir),
    )
    sid = await client.create_session("anna")
    _ = [
        e
        async for e in client.chat_stream(
            sid, "Merk dir, dass der Müll dienstags kommt"
        )
    ]

    first, second = fake.calls[0], fake.calls[1]
    assert first["tool_choice"] == "required"
    assert [t["function"]["name"] for t in first["tools"]] == ["fact_store"]
    # the follow-up pass writes the sentence with the whole toolbox back
    assert second["tool_choice"] == ""
    assert len(second["tools"]) > 1


@pytest.mark.asyncio
async def test_ordinary_turn_is_not_routed(db, soul, tmp_path):
    notes_dir = str(tmp_path / "notes")
    client, fake = _client(
        db,
        soul,
        [ChatResult(content="Dein Auto steht in der Tiefgarage.")],
        tools=_notes_tools(notes_dir),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Wo steht mein Auto?")]

    assert fake.calls[0]["tool_choice"] == ""
    assert len(fake.calls[0]["tools"]) > 1


# -- the real turn shape (#1336 attempt 3) ---------------------------------

# What `/api/chat` actually sends: `server.topic_turn_text()` puts the
# wall-clock block in front of every turn, and the voice gatekeeper adds the
# room. Attempt 2 of the routing measured 5/5 on the bench and 0/5 on the box
# because it only ever saw the bare sentence.
_REAL_TURN = (
    "[Aktuelle Zeit: Montag, 09.06.2026, 23:40 Uhr CEST]\n\n"
    "Merk dir, dass der Müll dienstags kommt"
)


def test_the_time_hint_does_not_hide_the_directive():
    assert remember.wants_remember(_REAL_TURN) == "der Müll dienstags kommt"
    assert (
        remember.wants_remember(
            "[Aktuelle Zeit: Montag, 09.06.2026, 23:40 Uhr CEST]\n"
            "[room: Küche]\n"
            "denk dran: Öl nachfüllen"
        )
        == "Öl nachfüllen"
    )


def test_a_prefixed_ordinary_turn_is_still_not_a_directive():
    assert (
        remember.wants_remember(
            "[Aktuelle Zeit: Montag, 09.06.2026, 23:40 Uhr CEST]\n\ndas ist merkwürdig"
        )
        is None
    )


def test_routed_pass_routes_the_real_turn_shape():
    tools, choice = remember.routed_pass(_REAL_TURN, [_RADIO_DEF, _FACT_STORE_DEF])
    assert tools == [_FACT_STORE_DEF]
    assert choice == "required"


@pytest.mark.asyncio
async def test_prefixed_merk_dir_turn_forces_the_tool_call(db, soul, tmp_path):
    notes_dir = str(tmp_path / "notes")
    client, fake = _client(
        db,
        soul,
        [
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "fact_store",
                            "arguments": {"fact": "der Müll kommt dienstags"},
                        }
                    }
                ]
            ),
            ChatResult(content="Gemerkt."),
        ],
        tools=_notes_tools(notes_dir),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, _REAL_TURN)]

    first = fake.calls[0]
    assert first["tool_choice"] == "required"
    assert [t["function"]["name"] for t in first["tools"]] == ["fact_store"]
