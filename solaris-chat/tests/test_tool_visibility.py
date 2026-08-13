"""Visibility class on every read tool + enforcement on the voice path (#1130).

ADR-12 / G-6: every tool declares what its result may reveal when the answer is
spoken out loud. A tool that declares nothing counts as *Vertraulich* — this
file is the lint that makes that default unreachable in practice (a registered
tool without a class fails here) and the proof that the voice path honours it.

Speaker recognition sets the context, it is not authorization: a speaker match
unlocks the *Persönlich* class and never the confidential one, because
far-field recognition is spoofable and the failure mode is a data leak inside
the family.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import sqlite3

import pytest
from solaris_chat.engine.bus import SessionBus
from solaris_chat.engine.client import EngineClient, EngineProfile
from solaris_chat.engine.ollama import ChatResult
from solaris_chat.engine.tools import (
    CHANNEL_VOICE,
    Tool,
    Toolbox,
    Visibility,
    current_channel,
    current_speaker_id_enabled,
    current_speaker_matched,
)
from solaris_chat.engine.tools.calendar_tools import build_calendar_tools
from solaris_chat.engine.tools.choices import build_choice_tools
from solaris_chat.engine.tools.documents import build_document_tools
from solaris_chat.engine.tools.favorites import build_favorites_tools
from solaris_chat.engine.tools.ha import build_ha_tools
from solaris_chat.engine.tools.media import build_media_tools
from solaris_chat.engine.tools.music_query import build_music_query_tools
from solaris_chat.engine.tools.notes import build_notes_tools
from solaris_chat.engine.tools.onboarding_approval import (
    build_onboarding_approval_tools,
)
from solaris_chat.engine.tools.radio import build_radio_tools
from solaris_chat.engine.tools.register import build_register_tools
from solaris_chat.engine.tools.skill_promotion import build_skill_promotion_tools
from solaris_chat.engine.tools.tasks_tools import build_tasks_tools
from solaris_chat.engine.tools.timers import build_timer_tools
from solaris_chat.engine.tools.wakeword_trainer import build_wakeword_tools
from solaris_chat.engine.trace import TraceRecorder
from solaris_chat.server import build_app

from tests.test_engine import _SCHEMA, FakeOllama


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


@pytest.fixture(autouse=True)
def clean_channel():
    """Each test starts off the voice path — the browser/API default — on an
    install where speaker-ID runs, so a test that wants the fallback has to ask
    for it."""
    channel = current_channel.set("")
    matched = current_speaker_matched.set(False)
    enabled = current_speaker_id_enabled.set(True)
    yield
    current_channel.reset(channel)
    current_speaker_matched.reset(matched)
    current_speaker_id_enabled.reset(enabled)


# -- the registry lint ------------------------------------------------------


def _uid() -> str:
    return "household"


# Every `build_*_tools` in the package, with builder args that do no I/O. A new
# builder that isn't listed here fails `test_every_builder_is_covered`, so the
# class can't be forgotten on the next tool.
def _registered_tools() -> dict[str, list[Tool]]:
    return {
        "build_ha_tools": build_ha_tools("http://ha", "tok"),
        "build_calendar_tools": build_calendar_tools(_uid),
        "build_choice_tools": build_choice_tools(),
        "build_timer_tools": build_timer_tools("/tmp/db.sqlite", _uid),
        "build_wakeword_tools": build_wakeword_tools("/tmp/db.sqlite", _uid),
        "build_media_tools": build_media_tools("http://ha", "tok"),
        "build_radio_tools": build_radio_tools("/tmp/notes", "http://ha", "tok", _uid),
        "build_notes_tools": build_notes_tools(
            "/tmp/notes", _uid, db_path="/tmp/db.sqlite"
        ),
        "build_tasks_tools": build_tasks_tools(
            "/tmp/db.sqlite", _uid, notes_dir="/tmp/notes"
        ),
        "build_document_tools": build_document_tools("/tmp/notes", _uid),
        "build_register_tools": build_register_tools("/tmp/db.sqlite"),
        "build_onboarding_approval_tools": build_onboarding_approval_tools(
            "/tmp/db.sqlite", "http://mcp", "/tmp/token", "", ""
        ),
        "build_skill_promotion_tools": build_skill_promotion_tools(
            "/tmp/skills", "http://mcp", "/tmp/token"
        ),
        "build_favorites_tools": build_favorites_tools(
            "/tmp/db.sqlite", _uid, lambda: "sess", None, None, "http://ha", "tok"
        ),
        "build_music_query_tools": build_music_query_tools(
            "/tmp/db.sqlite", _uid, None, hass_url="http://ha", hass_token="tok"
        ),
    }


def test_every_registered_tool_declares_a_visibility_class():
    for builder, tools in _registered_tools().items():
        assert tools, f"{builder} built nothing"
        for tool in tools:
            assert tool.visibility is not None, (
                f"{tool.name} ({builder}) declares no visibility class — "
                "add visibility=Visibility.HOUSEHOLD/PERSONAL/CONFIDENTIAL (G-6)"
            )


def test_every_builder_is_covered():
    import solaris_chat.engine.tools as tools_pkg

    found: set[str] = set()
    for mod in pkgutil.iter_modules(tools_pkg.__path__):
        module = importlib.import_module(f"{tools_pkg.__name__}.{mod.name}")
        found |= {
            name
            for name in dir(module)
            if name.startswith("build_") and name.endswith("_tools")
        }
    missing = found - set(_registered_tools())
    assert not missing, f"tool builders not classified by this lint: {sorted(missing)}"


def test_the_confidential_tools_are_the_documents_and_approval_paths():
    by_name = {t.name: t for g in _registered_tools().values() for t in g}
    confidential = {
        n for n, t in by_name.items() if t.visibility is Visibility.CONFIDENTIAL
    }
    assert confidential == {
        "document_extract",
        "file_resident_approval",
        "check_resident_approval",
        "file_skill_approval",
        "check_skill_approval",
    }
    # The vault is a resident's own material: speaker match required, never free.
    for name in ("notes_search", "notes_read"):
        assert by_name[name].visibility is Visibility.PERSONAL


def test_the_personal_tools_are_the_vault_and_the_calendar_write():
    """Writing to the family calendar is not household business: with speaker-ID
    on, a visitor at the kitchen table must not be able to dictate appointments
    into it. Reading the calendar stays household — there is no read tool yet."""
    by_name = {t.name: t for g in _registered_tools().values() for t in g}
    personal = {n for n, t in by_name.items() if t.visibility is Visibility.PERSONAL}
    assert personal == {"notes_search", "notes_read", "calendar_create"}


# -- G-6: no class means confidential ---------------------------------------


async def _noop(args):  # pragma: no cover — never reached when withheld
    return '{"leaked": true}'


def _tool(name: str, visibility: Visibility | None = None) -> Tool:
    return Tool(
        name=name,
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=_noop,
        visibility=visibility,
    )


def test_undeclared_tool_is_confidential():
    assert _tool("mystery").visibility_class is Visibility.CONFIDENTIAL


# -- enforcement on the voice path ------------------------------------------


async def _dispatch(tool: Tool) -> str:
    return await Toolbox([tool]).dispatch(tool.name, {})


async def test_browser_channel_never_withholds():
    assert (
        await _dispatch(_tool("secret", Visibility.CONFIDENTIAL)) == '{"leaked": true}'
    )
    assert await _dispatch(_tool("mystery")) == '{"leaked": true}'


async def test_voice_speaks_household_content():
    current_channel.set(CHANNEL_VOICE)
    assert await _dispatch(_tool("heizung", Visibility.HOUSEHOLD)) == '{"leaked": true}'


async def test_voice_withholds_confidential_content_with_a_pointer():
    current_channel.set(CHANNEL_VOICE)
    out = json.loads(await _dispatch(_tool("vertrag", Visibility.CONFIDENTIAL)))
    assert out["ok"] is False
    assert out["reason"] == "visibility_withheld_on_voice"
    assert out["visibility"] == "vertraulich"
    assert "Solaris-App" in out["say"]


async def test_speaker_match_never_unlocks_confidential_content():
    # ADR-12: recognition sets the context, it is not authorization.
    current_channel.set(CHANNEL_VOICE)
    current_speaker_matched.set(True)
    out = json.loads(await _dispatch(_tool("vertrag", Visibility.CONFIDENTIAL)))
    assert out["reason"] == "visibility_withheld_on_voice"


async def test_voice_personal_content_needs_a_speaker_match():
    current_channel.set(CHANNEL_VOICE)
    out = json.loads(await _dispatch(_tool("notiz", Visibility.PERSONAL)))
    assert out["visibility"] == "persoenlich"
    assert "Solaris-App" in out["say"]

    current_speaker_matched.set(True)
    assert await _dispatch(_tool("notiz", Visibility.PERSONAL)) == '{"leaked": true}'


async def test_voice_withholds_the_real_calendar_create_without_a_speaker_match():
    # The registered tool, not a stand-in: an unrecognised voice never reaches
    # the CalDAV write.
    current_channel.set(CHANNEL_VOICE)
    tool = next(t for t in build_calendar_tools(_uid) if t.name == "calendar_create")
    out = json.loads(
        await Toolbox([tool]).dispatch(
            "calendar_create", {"title": "Zahnarzt", "when": "morgen"}
        )
    )
    assert out["ok"] is False
    assert out["reason"] == "visibility_withheld_on_voice"
    assert out["visibility"] == "persoenlich"


async def test_calendar_create_still_answers_on_voice_when_speaker_id_is_off():
    # Today's box: nothing can match, so Persönlich falls back to Haushalt and
    # the tool runs as before this classification change.
    current_channel.set(CHANNEL_VOICE)
    current_speaker_id_enabled.set(False)
    tool = next(t for t in build_calendar_tools(_uid) if t.name == "calendar_create")
    assert tool.visibility_class is Visibility.PERSONAL
    out = json.loads(
        await Toolbox([_tool(tool.name, tool.visibility)]).dispatch(tool.name, {})
    )
    assert out == {"leaked": True}


async def test_undeclared_tool_is_withheld_on_voice():
    current_channel.set(CHANNEL_VOICE)
    current_speaker_matched.set(True)
    out = json.loads(await _dispatch(_tool("mystery")))
    assert out["visibility"] == "vertraulich"


# -- speaker-ID off: Persönlich collapses to Haushalt -----------------------


async def test_personal_answers_on_voice_when_speaker_id_is_off():
    # Nothing can ever match on this install, so gating Persönlich would mute a
    # resident's own notes on every spoken turn. It counts as Haushalt instead.
    current_channel.set(CHANNEL_VOICE)
    current_speaker_id_enabled.set(False)
    assert await _dispatch(_tool("notiz", Visibility.PERSONAL)) == '{"leaked": true}'


async def test_confidential_stays_withheld_when_speaker_id_is_off():
    current_channel.set(CHANNEL_VOICE)
    current_speaker_id_enabled.set(False)
    out = json.loads(await _dispatch(_tool("vertrag", Visibility.CONFIDENTIAL)))
    assert out["reason"] == "visibility_withheld_on_voice"
    assert out["visibility"] == "vertraulich"


async def test_undeclared_tool_is_withheld_when_speaker_id_is_off():
    # The fallback moves Persönlich only — a forgotten class is still confidential.
    current_channel.set(CHANNEL_VOICE)
    current_speaker_id_enabled.set(False)
    out = json.loads(await _dispatch(_tool("mystery")))
    assert out["visibility"] == "vertraulich"


# -- the facade actually flips the channel ----------------------------------


def _voice_app(db, soul, results, tools, speaker_id_enabled: bool = True):
    def engine(name: str, fake: FakeOllama) -> EngineClient:
        return EngineClient(
            EngineProfile(
                name=name,
                model="gemma4:e2b",
                soul_path=soul,
                toolbox=Toolbox(tools),
            ),
            db_path=db,
            ollama=fake,
            recorder=TraceRecorder(),
            context_window=32768,
            bus=SessionBus(),
        )

    fake = FakeOllama(results)
    return (
        build_app(
            engine=engine("household", fake),
            remote_user_header="Remote-User",
            default_uid="household",
            solaris_db_path=db,
            speaker_id_enabled=speaker_id_enabled,
            api_key="",
            bus=SessionBus(),
        ),
        fake,
    )


def _stash(db: str, transcript: str, uid: str, *, matched: bool) -> None:
    """The row the gatekeeper writes. Two independent facts: `uid` routes the
    turn, `matched` is the recognition claim — the only thing that may unlock
    the Persönlich class (#1152)."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO voice_uid_stash (transcript, uid, matched) VALUES (?, ?, ?)",
        (transcript, uid, 1 if matched else 0),
    )
    conn.commit()
    conn.close()


def _call(name: str) -> ChatResult:
    return ChatResult(
        content="",
        tool_calls=[{"function": {"name": name, "arguments": {}}}],
        prompt_tokens=10,
    )


async def _voice_turn(aiohttp_client, app, transcript: str) -> None:
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": transcript}],
            "user": "household",
        },
    )
    assert resp.status == 200


async def test_facade_turn_withholds_a_confidential_tool(aiohttp_client, db, soul):
    ran = []

    async def handler(args):
        ran.append(args)
        return '{"betrag": "412,50 EUR"}'

    tool = Tool(
        name="vertrag_lesen",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        visibility=Visibility.CONFIDENTIAL,
    )
    app, fake = _voice_app(
        db,
        soul,
        [_call("vertrag_lesen"), ChatResult(content="Schau in die App.")],
        [tool],
    )
    await _voice_turn(aiohttp_client, app, "was kostet meine Versicherung")
    assert ran == [], "a confidential tool must not run on the voice path"
    fed_back = json.dumps(fake.calls[1]["messages"], ensure_ascii=False)
    assert "visibility_withheld_on_voice" in fed_back
    assert "412,50" not in fed_back


async def test_facade_turn_speaks_a_household_tool(aiohttp_client, db, soul):
    ran = []

    async def handler(args):
        ran.append(args)
        return '{"temperatur": 21}'

    tool = Tool(
        name="heizung_lesen",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        visibility=Visibility.HOUSEHOLD,
    )
    app, _ = _voice_app(
        db,
        soul,
        [_call("heizung_lesen"), ChatResult(content="21 Grad.")],
        [tool],
    )
    await _voice_turn(aiohttp_client, app, "wie warm ist es")
    assert ran == [{}]


async def test_facade_turn_unlocks_personal_on_a_speaker_hit(aiohttp_client, db, soul):
    ran = []

    async def handler(args):
        ran.append(args)
        return '{"notiz": "Zahnarzt am Freitag"}'

    def build(visibility):
        return Tool(
            name="notiz_lesen",
            description="x",
            parameters={"type": "object", "properties": {}},
            handler=handler,
            visibility=visibility,
        )

    # No stash row: speaker-ID matched nothing, so the personal tool is withheld.
    app, _ = _voice_app(
        db,
        soul,
        [_call("notiz_lesen"), ChatResult(content="Schau in die App.")],
        [build(Visibility.PERSONAL)],
    )
    await _voice_turn(aiohttp_client, app, "was steht in meiner Notiz")
    assert ran == []

    # The gatekeeper stashed {transcript -> anna} AND asserted the match.
    _stash(db, "lies meine Notiz", "anna", matched=True)
    app, _ = _voice_app(
        db,
        soul,
        [_call("notiz_lesen"), ChatResult(content="Zahnarzt am Freitag.")],
        [build(Visibility.PERSONAL)],
    )
    await _voice_turn(aiohttp_client, app, "lies meine Notiz")
    assert ran == [{}]


async def test_facade_turn_speaks_personal_when_speaker_id_is_off(
    aiohttp_client, db, soul
):
    """The box runs with SOLARIS_SPEAKER_ID_ENABLED=false: no stash row can ever
    appear, so the personal tool answers instead of pointing at the app."""
    ran = []

    async def handler(args):
        ran.append(args)
        return '{"notiz": "Zahnarzt am Freitag"}'

    tool = Tool(
        name="notiz_lesen",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        visibility=Visibility.PERSONAL,
    )
    app, fake = _voice_app(
        db,
        soul,
        [_call("notiz_lesen"), ChatResult(content="Zahnarzt am Freitag.")],
        [tool],
        speaker_id_enabled=False,
    )
    await _voice_turn(aiohttp_client, app, "was steht in meiner Notiz")
    assert ran == [{}]
    fed_back = json.dumps(fake.calls[1]["messages"], ensure_ascii=False)
    assert "Zahnarzt am Freitag" in fed_back
    assert "visibility_withheld_on_voice" not in fed_back


async def test_facade_turn_withholds_confidential_when_speaker_id_is_off(
    aiohttp_client, db, soul
):
    """The fallback moves Persönlich only — Vertraulich is never spoken."""
    ran = []

    async def handler(args):
        ran.append(args)
        return '{"betrag": "412,50 EUR"}'

    tool = Tool(
        name="vertrag_lesen",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        visibility=Visibility.CONFIDENTIAL,
    )
    app, fake = _voice_app(
        db,
        soul,
        [_call("vertrag_lesen"), ChatResult(content="Schau in die App.")],
        [tool],
        speaker_id_enabled=False,
    )
    await _voice_turn(aiohttp_client, app, "was kostet meine Versicherung")
    assert ran == [], "speaker-ID off must not unlock a confidential tool"
    fed_back = json.dumps(fake.calls[1]["messages"], ensure_ascii=False)
    assert "visibility_withheld_on_voice" in fed_back
    assert "412,50" not in fed_back


async def test_facade_turn_withholds_personal_when_speaker_id_is_on_and_unmatched(
    aiohttp_client, db, soul
):
    """The other direction: with speaker-ID on and no match the gate still bites,
    and the note never reaches the model."""
    ran = []

    async def handler(args):
        ran.append(args)
        return '{"notiz": "Zahnarzt am Freitag"}'

    tool = Tool(
        name="notiz_lesen",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        visibility=Visibility.PERSONAL,
    )
    app, fake = _voice_app(
        db,
        soul,
        [_call("notiz_lesen"), ChatResult(content="Schau in die App.")],
        [tool],
        speaker_id_enabled=True,
    )
    await _voice_turn(aiohttp_client, app, "was steht in meiner Notiz")
    assert ran == []
    fed_back = json.dumps(fake.calls[1]["messages"], ensure_ascii=False)
    assert "visibility_withheld_on_voice" in fed_back
    assert "Zahnarzt" not in fed_back


# -- #1152: the claim unlocks Persönlich, never the row or the uid -----------


@pytest.mark.parametrize("uid", ["guest", "unknown", "anna"])
async def test_facade_turn_keeps_personal_shut_for_an_unmatched_row(
    aiohttp_client, db, soul, uid
):
    """A stash row exists and names someone — but the gatekeeper asserted no
    recognition. Persönlich stays shut for *every* uid: today's `guest`
    sentinel, a renamed one, and even a real resident's name. Under the old
    contract the last two both unlocked it."""
    ran = []

    async def handler(args):
        ran.append(args)
        return '{"notiz": "Zahnarzt am Freitag"}'

    tool = Tool(
        name="notiz_lesen",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        visibility=Visibility.PERSONAL,
    )
    _stash(db, "lies meine Notiz", uid, matched=False)
    app, fake = _voice_app(
        db,
        soul,
        [_call("notiz_lesen"), ChatResult(content="Schau in die App.")],
        [tool],
    )
    await _voice_turn(aiohttp_client, app, "lies meine Notiz")
    assert ran == []
    fed_back = json.dumps(fake.calls[1]["messages"], ensure_ascii=False)
    assert "visibility_withheld_on_voice" in fed_back
    assert "Zahnarzt" not in fed_back


async def test_facade_turn_keeps_personal_shut_for_a_legacy_shaped_row(
    aiohttp_client, db, soul
):
    """A gatekeeper image that predates the `matched` column writes the old
    two-column row. The column's DEFAULT 0 is what makes that window fail
    closed: the turn is still attributed to anna for routing, and Persönlich
    stays shut until the writer is upgraded."""
    ran = []

    async def handler(args):
        ran.append(args)
        return '{"notiz": "Zahnarzt am Freitag"}'

    tool = Tool(
        name="notiz_lesen",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        visibility=Visibility.PERSONAL,
    )
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO voice_uid_stash (transcript, uid) VALUES (?, ?)",
        ("lies meine Notiz", "anna"),
    )
    conn.commit()
    conn.close()
    app, fake = _voice_app(
        db,
        soul,
        [_call("notiz_lesen"), ChatResult(content="Schau in die App.")],
        [tool],
    )
    await _voice_turn(aiohttp_client, app, "lies meine Notiz")
    assert ran == []
    fed_back = json.dumps(fake.calls[1]["messages"], ensure_ascii=False)
    assert "visibility_withheld_on_voice" in fed_back
    assert "Zahnarzt" not in fed_back
