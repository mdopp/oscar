"""Deterministic confirmation gate for sensitive HA actions (#570).

The prompt-only confirm-before-act (u64/#558) is unreliable on gemma4:e4b —
"Garagentor öffnen" sometimes executes with a bare "Klar.". This locks the
code-enforced gate: a sensitive ha_call_service is NOT executed on the turn the
model issues it (no matter what the model does); it is held, ja/nein chips are
offered, and only an affirmative follow-up actually invokes HA.

HA is stubbed (read-only, per the house rule) and the POST body recorded, so a
held call asserts NO service was invoked; an executed one asserts exactly one.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from solaris_chat import compaction
from solaris_chat.engine import areas as areas_mod
from solaris_chat.engine import confirm, tasks
from solaris_chat.engine.client import EngineClient, EngineProfile
from solaris_chat.engine.ollama import ChatResult
from solaris_chat.engine.tools import Toolbox
from solaris_chat.engine.tools import ha as ha_mod
from solaris_chat.engine.tools.choices import build_choice_tools
from solaris_chat.engine.tools.ha import build_ha_tools
from solaris_chat.engine.tools.tasks_tools import build_tasks_tools
from solaris_chat.engine.trace import TraceRecorder

from tests.test_engine import _SCHEMA, _client  # shared schema + client harness
from tests.test_tasks import _SCHEMA as _TASK_SCHEMA
from tests.test_engine import FakeOllama


class _FakeRegistry:
    """Duck-typed EntityRegistry: resolves a fixed entity->device_class map for
    the gate (so a cover test needn't touch HA)."""

    def __init__(self, classes: dict[str, str]):
        self._classes = classes

    async def device_class(self, entity_id: str) -> str | None:
        return self._classes.get(entity_id)

    async def prompt_block(self) -> str:
        return ""


def _client_with_registry(db, soul, results, classes: dict[str, str]) -> EngineClient:
    fake = FakeOllama(results)
    return EngineClient(
        EngineProfile(
            name="household",
            model="gemma4:e2b",
            soul_path=soul,
            registry=_FakeRegistry(classes),  # type: ignore[arg-type]
            toolbox=Toolbox(_tools()),
        ),
        db_path=db,
        ollama=fake,
        recorder=TraceRecorder(),
        context_window=32768,
    )


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


def _stub_ha(monkeypatch) -> list[tuple[str, dict]]:
    """Stub aiohttp so a ha_call_service POST is recorded, not sent. Returns the
    list of (url, body) POSTs the run actually issued."""
    posts: list[tuple[str, dict]] = []

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {"ok": True}

        async def text(self):
            return ""

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json, **k):
            posts.append((url, json))
            return _Resp()

        def get(self, url, **k):
            return _Resp()

        def ws_connect(self, _url):
            raise OSError("no ws")

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(areas_mod.aiohttp, "ClientSession", _Session)
    return posts


def _tools():
    return build_ha_tools("http://ha", "tok") + build_choice_tools()


def _open_garage_call() -> ChatResult:
    return ChatResult(
        tool_calls=[
            {
                "function": {
                    "name": "ha_call_service",
                    "arguments": {
                        "domain": "cover",
                        "service": "open_cover",
                        "entity_id": "cover.garage_door",
                    },
                }
            }
        ],
        prompt_tokens=40,
    )


def _cover_call(service: str, entity_id: str, data: dict | None = None) -> ChatResult:
    args: dict = {"domain": "cover", "service": service, "entity_id": entity_id}
    if data is not None:
        args["data"] = data
    return ChatResult(
        tool_calls=[{"function": {"name": "ha_call_service", "arguments": args}}]
    )


# -- policy units ---------------------------------------------------------


def test_classify_sensitive_by_service_and_domain():
    assert confirm.is_sensitive("lock", "unlock")
    assert confirm.is_sensitive("alarm_control_panel", "alarm_disarm")
    assert confirm.is_sensitive("lock", "lock")  # whole lock domain is gated
    # routine controls are not sensitive
    assert not confirm.is_sensitive("light", "turn_on")
    assert not confirm.is_sensitive("switch", "turn_off")


def test_classify_cover_is_class_specific():
    # A garage/door/gate cover is gated for any opening/moving service (F1)...
    for svc in (
        "open_cover",
        "toggle",
        "set_cover_position",
        "set_cover_tilt_position",
        "open_cover_tilt",
    ):
        assert confirm.is_sensitive("cover", svc, "garage")
    assert confirm.is_sensitive("cover", "toggle", "door")
    assert confirm.is_sensitive("cover", "set_cover_position", "gate")
    # ...but an ordinary blind/shade/curtain/awning/window is NOT — don't annoy.
    for dc in ("blind", "shade", "curtain", "awning", "window"):
        assert not confirm.is_sensitive("cover", "set_cover_position", dc)
        assert not confirm.is_sensitive("cover", "open_cover", dc)
        assert not confirm.is_sensitive("cover", "toggle", dc)
    # close_cover is always ungated (re-secures), even for a garage.
    assert not confirm.is_sensitive("cover", "close_cover", "garage")
    # Unresolvable device_class on an open-direction service fails SAFE (gated).
    assert confirm.is_sensitive("cover", "open_cover", None)
    assert confirm.is_sensitive("cover", "set_cover_position", "")
    # but a non-opening service on an unknown cover is not gated by the rule
    assert not confirm.is_sensitive("cover", "close_cover", None)


def test_affirmative_negative_detection():
    assert confirm.is_affirmative("ja")
    assert confirm.is_affirmative("Ja bitte öffnen")
    assert confirm.is_affirmative("ja mach auf")  # "ja" token still confirms
    assert not confirm.is_affirmative("nein")
    # common filler words must NOT detonate a pending action (F2)
    assert not confirm.is_affirmative("mach")
    assert not confirm.is_affirmative("Mach mal das Licht an")
    assert not confirm.is_affirmative("los")
    assert not confirm.is_affirmative("klar")
    assert not confirm.is_affirmative("gerne")
    assert not confirm.is_affirmative("go")
    assert not confirm.is_affirmative("auf")
    # "bitte" is a politeness particle, not a confirmation — a fresh "bitte …"
    # command must not detonate a pending sensitive action (re-review residual)
    assert not confirm.is_affirmative("bitte")
    assert not confirm.is_affirmative("Bitte mach das Tor zu")
    assert confirm.is_affirmative("ja bitte")  # explicit "ja" still confirms
    assert confirm.is_negative("nein")
    assert confirm.is_negative("Stop, abbrechen")
    # negative wins on a mixed reply — never auto-execute on ambiguity
    assert not confirm.is_affirmative("nein doch nicht")


# -- the gate, end to end -------------------------------------------------


@pytest.mark.asyncio
async def test_sensitive_call_held_not_executed_on_turn1(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    results = [
        _open_garage_call(),
        ChatResult(
            content="Soll ich das Garagentor wirklich öffnen?", prompt_tokens=50
        ),
    ]
    client, _ = _client(db, soul, results, tools=_tools())
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]

    # The underlying HA service was NOT invoked.
    assert posts == []
    # ja/nein chips were offered so the mic stays open / chips show.
    qr = [e for e in events if e["type"] == "quick_replies"]
    assert qr and qr[0]["data"]["options"] == ["ja", "nein"]
    # The pending action is stashed for the next turn.
    pending = client._pending.peek(sid)
    assert pending is not None
    assert (pending.domain, pending.service, pending.entity_id) == (
        "cover",
        "open_cover",
        "cover.garage_door",
    )


@pytest.mark.asyncio
async def test_affirmative_followup_executes_stashed_action(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            _open_garage_call(),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
            # turn 2: the model's natural report after the confirmed tool result
            ChatResult(content="Erledigt, das Garagentor ist offen."),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]
    assert posts == []  # still not executed

    _ = [e async for e in client.chat_stream(sid, "ja")]
    # exactly one HA service call, the stashed open_cover
    assert len(posts) == 1
    url, body = posts[0]
    assert url.endswith("/api/services/cover/open_cover")
    assert body["entity_id"] == "cover.garage_door"
    # the pending slot is cleared
    assert client._pending.peek(sid) is None


@pytest.mark.asyncio
async def test_negative_followup_drops_stashed_action(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            _open_garage_call(),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
            ChatResult(content="Ok, ich lasse es zu."),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]
    _ = [e async for e in client.chat_stream(sid, "nein")]
    # nothing was ever executed and the pending slot is cleared
    assert posts == []
    assert client._pending.peek(sid) is None


@pytest.mark.asyncio
async def test_routine_action_executes_directly(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "ha_call_service",
                            "arguments": {
                                "domain": "light",
                                "service": "turn_on",
                                "entity_id": "light.kitchen",
                            },
                        }
                    }
                ]
            ),
            ChatResult(content="Licht ist an."),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Licht an")]
    # executed directly, no confirmation held, no chips
    assert len(posts) == 1
    assert posts[0][0].endswith("/api/services/light/turn_on")
    assert client._pending.peek(sid) is None
    assert not any(e["type"] == "quick_replies" for e in events)


@pytest.mark.asyncio
async def test_gate_normalizes_cover_open_alias(db, soul, monkeypatch):
    # The model often issues the bare verb "open" for a cover; call_service
    # aliases it to open_cover, so the gate must classify it sensitive too.
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "ha_call_service",
                            "arguments": {
                                "domain": "cover",
                                "service": "open",
                                "entity_id": "cover.garage_door",
                            },
                        }
                    }
                ]
            ),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garage auf")]
    assert posts == []
    pending = client._pending.peek(sid)
    assert pending is not None and pending.service == "open_cover"


# -- F1: cover gate is device_class-specific ------------------------------


@pytest.mark.asyncio
async def test_garage_set_cover_position_is_gated(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client = _client_with_registry(
        db,
        soul,
        [
            _cover_call("set_cover_position", "cover.garage_door", {"position": 100}),
            ChatResult(content="Soll ich das Garagentor wirklich verstellen?"),
        ],
        {"cover.garage_door": "garage"},
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Garage auf 100")]
    # NOT executed on turn 1 — held for confirmation.
    assert posts == []
    qr = [e for e in events if e["type"] == "quick_replies"]
    assert qr and qr[0]["data"]["options"] == ["ja", "nein"]
    pending = client._pending.peek(sid)
    assert pending is not None and pending.service == "set_cover_position"


@pytest.mark.asyncio
async def test_garage_toggle_is_gated(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client = _client_with_registry(
        db,
        soul,
        [
            _cover_call("toggle", "cover.garage_door"),
            ChatResult(content="Soll ich das Garagentor wirklich umschalten?"),
        ],
        {"cover.garage_door": "garage"},
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garage umschalten")]
    assert posts == []
    assert client._pending.peek(sid) is not None


@pytest.mark.asyncio
async def test_blind_set_cover_position_not_gated(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client = _client_with_registry(
        db,
        soul,
        [
            _cover_call("set_cover_position", "cover.living_blind", {"position": 50}),
            ChatResult(content="Rollo ist auf 50%."),
        ],
        {"cover.living_blind": "blind"},
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Rollo auf 50")]
    # A blind is NOT sensitive — it executes directly, no confirmation chips.
    assert len(posts) == 1
    assert posts[0][0].endswith("/api/services/cover/set_cover_position")
    assert client._pending.peek(sid) is None
    assert not any(e["type"] == "quick_replies" for e in events)


# -- #632: gate on the entity's real domain, not the model's routing ------


@pytest.mark.asyncio
async def test_misrouted_domain_garage_open_is_gated(db, soul, monkeypatch):
    # e4b mis-routes a garage open as the WRONG domain (light.open_cover on a
    # real garage cover). The gate must key on the entity's true domain (cover)
    # and still hold the call for confirmation (#632).
    posts = _stub_ha(monkeypatch)
    client = _client_with_registry(
        db,
        soul,
        [
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "ha_call_service",
                            "arguments": {
                                "domain": "light",
                                "service": "open_cover",
                                "entity_id": "cover.garage_door",
                            },
                        }
                    }
                ]
            ),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
        ],
        {"cover.garage_door": "garage"},
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Garage auf")]
    # NOT executed on turn 1 despite the bogus domain — held for confirmation.
    assert posts == []
    qr = [e for e in events if e["type"] == "quick_replies"]
    assert qr and qr[0]["data"]["options"] == ["ja", "nein"]
    assert client._pending.peek(sid) is not None


@pytest.mark.asyncio
async def test_misrouted_bare_open_alias_on_garage_is_gated(db, soul, monkeypatch):
    # The natural verb "open" mis-routed to domain=switch on a real garage
    # cover: alias normalisation keys on the true domain (cover), so "open"
    # becomes open_cover and the gate fires (#632).
    posts = _stub_ha(monkeypatch)
    client = _client_with_registry(
        db,
        soul,
        [
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "ha_call_service",
                            "arguments": {
                                "domain": "switch",
                                "service": "open",
                                "entity_id": "cover.garage_door",
                            },
                        }
                    }
                ]
            ),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
        ],
        {"cover.garage_door": "garage"},
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garage auf")]
    assert posts == []
    pending = client._pending.peek(sid)
    assert pending is not None and pending.service == "open_cover"


@pytest.mark.asyncio
async def test_cover_domain_turn_on_not_gated(db, soul, monkeypatch):
    # A cover-domain call with a non-cover-open service (turn_on) is not a
    # perimeter-opening action, so it must NOT be gated (#632 acceptance).
    posts = _stub_ha(monkeypatch)
    client = _client_with_registry(
        db,
        soul,
        [
            _cover_call("turn_on", "cover.living_blind"),
            ChatResult(content="Ok."),
        ],
        {"cover.living_blind": "blind"},
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Rollo an")]
    # Not held — turn_on is not a cover-open service.
    assert client._pending.peek(sid) is None
    assert not any(e["type"] == "quick_replies" for e in events)
    assert len(posts) == 1


# -- F2: a fresh request must not detonate the pending action -------------


@pytest.mark.asyncio
async def test_fresh_request_drops_pending_without_executing(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client = _client_with_registry(
        db,
        soul,
        [
            _open_garage_call(),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
            # turn 2 is a NEW request — the model turns on the light.
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "ha_call_service",
                            "arguments": {
                                "domain": "light",
                                "service": "turn_on",
                                "entity_id": "light.kitchen",
                            },
                        }
                    }
                ]
            ),
            ChatResult(content="Licht ist an."),
        ],
        {"cover.garage_door": "garage"},
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]
    assert posts == []  # held

    _ = [e async for e in client.chat_stream(sid, "Mach mal das Licht an")]
    # The pending garage is DROPPED, not executed — only the light was switched.
    assert len(posts) == 1
    assert posts[0][0].endswith("/api/services/light/turn_on")
    assert all("cover" not in url for url, _ in posts)
    assert client._pending.peek(sid) is None


@pytest.mark.asyncio
async def test_clear_ja_executes_pending(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client = _client_with_registry(
        db,
        soul,
        [
            _open_garage_call(),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
            ChatResult(content="Erledigt."),
        ],
        {"cover.garage_door": "garage"},
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]
    assert posts == []
    _ = [e async for e in client.chat_stream(sid, "ja")]
    assert len(posts) == 1
    assert posts[0][0].endswith("/api/services/cover/open_cover")
    assert client._pending.peek(sid) is None


@pytest.mark.asyncio
async def test_new_sensitive_request_while_pending_does_not_execute_old(
    db, soul, monkeypatch
):
    posts = _stub_ha(monkeypatch)
    client = _client_with_registry(
        db,
        soul,
        [
            _open_garage_call(),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
            # turn 2: a DIFFERENT sensitive action (unlock), no yes/no.
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "ha_call_service",
                            "arguments": {
                                "domain": "lock",
                                "service": "unlock",
                                "entity_id": "lock.front",
                            },
                        }
                    }
                ]
            ),
            ChatResult(content="Soll ich das Schloss wirklich entsperren?"),
        ],
        {"cover.garage_door": "garage"},
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]
    _ = [e async for e in client.chat_stream(sid, "Schloss aufschließen")]
    # Neither the old garage nor the new unlock ran — the new one is re-gated.
    assert posts == []
    pending = client._pending.peek(sid)
    assert pending is not None
    assert (pending.domain, pending.service) == ("lock", "unlock")


# -- F3: ephemeral key isolates two callers -------------------------------


@pytest.mark.asyncio
async def test_ephemeral_conversation_id_isolates_callers(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    # One ephemeral client (one profile source) serves two distinct
    # conversations; caller B's "ja" must not confirm caller A's held garage.
    client = _client_with_registry(
        db,
        soul,
        [
            _open_garage_call(),  # A turn 1: gated
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
            ChatResult(content="Hallo!"),  # B turn 1: just a greeting
        ],
        {"cover.garage_door": "garage"},
    )
    a = [{"role": "user", "content": "Garagentor öffnen"}]
    _ = [
        e
        async for e in client.respond(
            a, uid="household", source="solaris-guest", conversation_id="conv-A"
        )
    ]
    assert posts == []  # A's garage is held under conv-A

    # Caller B (different conversation) says "ja" — must NOT execute A's pending.
    b = [{"role": "user", "content": "ja"}]
    _ = [
        e
        async for e in client.respond(
            b, uid="household", source="solaris-guest", conversation_id="conv-B"
        )
    ]
    assert posts == []  # B's "ja" never reached A's pending action
    # A's pending still sits under its own conversation key, untouched.
    assert client._pending.peek("conv-A") is not None
    assert client._pending.peek("conv-B") is None


# -- #1183: a held action goes stale ---------------------------------------


@pytest.mark.asyncio
async def test_stale_pending_is_not_fired_by_a_later_affirmative(db, soul, monkeypatch):
    """A held action used to wait forever in the SHARED household session, so an
    unrelated later turn that merely contained "genau" opened the garage (#1183).
    Past its window it is gone — and the resident is told so, or a confirmation
    that fired nothing would be indistinguishable from one that opened the house.
    """
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            _open_garage_call(),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
            ChatResult(content="Diese Antwort darf nie kommen."),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]
    assert posts == []

    class _TwoHoursLater:
        """The resident walked off and never answered the question."""

        @staticmethod
        def monotonic() -> float:
            return time.monotonic() + 7200.0

    monkeypatch.setattr(confirm, "time", _TwoHoursLater, raising=False)

    events = [
        e async for e in client.chat_stream(sid, "genau, das habe ich auch gesagt")
    ]
    assert posts == [], "a stale pending action must never actuate"
    assert client._pending.peek(sid) is None
    answer = events[-1]["data"]["messages"][-1]["content"]
    assert "abgelaufen" in answer and "nichts ausgeführt" in answer


@pytest.mark.asyncio
async def test_confirmation_inside_the_window_still_executes(db, soul, monkeypatch):
    """The window must not make confirming unnatural: a plain "ja" right after
    the question still opens the garage."""
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            _open_garage_call(),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
            ChatResult(content="Erledigt."),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]
    _ = [e async for e in client.chat_stream(sid, "ja")]
    assert len(posts) == 1


# -- #1244: a task hard-delete is gated the same way ------------------------


def _task_db(tmp_path) -> tuple[str, str]:
    """A db carrying the engine tables PLUS the projection tables a task lives in."""
    path = str(tmp_path / "tasks.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executescript(_TASK_SCHEMA)
    conn.commit()
    conn.close()
    return path, str(tmp_path / "notes")


def _delete_call(**arguments) -> ChatResult:
    return ChatResult(
        tool_calls=[{"function": {"name": "task_delete", "arguments": arguments}}]
    )


@pytest.mark.asyncio
async def test_task_delete_is_held_and_names_the_task(tmp_path, soul):
    db, notes = _task_db(tmp_path)
    tid = tasks.create_task(db_path=db, notes_dir=notes, uid="anna", title="Milch")
    tools = build_choice_tools() + build_tasks_tools(
        db, lambda: "anna", notes_dir=notes
    )
    client, _ = _client(
        db,
        soul,
        [_delete_call(title="milch"), ChatResult(content="Frage gestellt.")],
        tools=tools,
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "lösch die Aufgabe Milch")]

    # Nothing was deleted on the turn the model asked for it.
    assert [t["id"] for t in tasks.list_tasks(db, "anna")] == [tid]
    # The held result NAMES the task, so the resident can catch a wrong match.
    pending = client._pending.peek(sid)
    assert pending is not None
    assert (pending.domain, pending.service, pending.entity_id) == (
        "task",
        "delete",
        tid,
    )
    assert "Milch" in pending.prompt and "löschen" in pending.prompt
    qr = [e for e in events if e["type"] == "quick_replies"]
    assert qr and qr[0]["data"]["options"] == ["ja", "nein"]


@pytest.mark.asyncio
async def test_task_delete_runs_only_after_a_yes(tmp_path, soul):
    db, notes = _task_db(tmp_path)
    tasks.create_task(db_path=db, notes_dir=notes, uid="anna", title="Milch")
    tools = build_choice_tools() + build_tasks_tools(
        db, lambda: "anna", notes_dir=notes
    )
    client, _ = _client(
        db,
        soul,
        [
            _delete_call(title="milch"),
            ChatResult(content="Soll ich die Aufgabe Milch wirklich löschen?"),
            ChatResult(content="Gelöscht."),
        ],
        tools=tools,
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "lösch die Aufgabe Milch")]
    assert len(tasks.list_tasks(db, "anna")) == 1

    _ = [e async for e in client.chat_stream(sid, "ja")]
    assert tasks.list_tasks(db, "anna", include_done=True) == []
    assert client._pending.peek(sid) is None


@pytest.mark.asyncio
async def test_task_delete_claiming_confirmation_is_still_held(tmp_path, soul):
    """The model cannot opt out of the gate: a call that already carries the
    engine's `confirmed` marker is intercepted and re-asked like any other."""
    db, notes = _task_db(tmp_path)
    tasks.create_task(db_path=db, notes_dir=notes, uid="anna", title="Milch")
    tools = build_choice_tools() + build_tasks_tools(
        db, lambda: "anna", notes_dir=notes
    )
    client, _ = _client(
        db,
        soul,
        [_delete_call(title="milch", confirmed=True), ChatResult(content="Frage.")],
        tools=tools,
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "lösch Milch, ist schon abgeklärt")]
    assert len(tasks.list_tasks(db, "anna")) == 1
    assert client._pending.peek(sid) is not None


@pytest.mark.asyncio
async def test_task_delete_unknown_target_errors_without_holding(tmp_path, soul):
    db, notes = _task_db(tmp_path)
    tasks.create_task(db_path=db, notes_dir=notes, uid="anna", title="Milch")
    tools = build_choice_tools() + build_tasks_tools(
        db, lambda: "anna", notes_dir=notes
    )
    client, _ = _client(
        db,
        soul,
        [_delete_call(id="geraten"), ChatResult(content="Welche?")],
        tools=tools,
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "lösch die Aufgabe")]
    # A guessed id is an error naming the candidates — never a wrong delete (#1241).
    assert len(tasks.list_tasks(db, "anna")) == 1
    assert client._pending.peek(sid) is None


# -- #1247: a compaction between the question and the "ja" -------------------
#
# Compaction (#210) runs two LLM turns on the session and then continues the
# resident in a session with a NEW id. Both used to eat an unanswered
# confirmation, so the resident's "ja" reached no held action and NOTHING
# happened — no unlock, no delete, and no word about why. The held action now
# travels with the resident, keeping its original deadline: answered in time it
# still fires, answered too late it says so.


def _unlock_call() -> ChatResult:
    return ChatResult(
        tool_calls=[
            {
                "function": {
                    "name": "ha_call_service",
                    "arguments": {
                        "domain": "lock",
                        "service": "unlock",
                        "entity_id": "lock.haustuer",
                    },
                }
            }
        ]
    )


async def _compact(client, sid: str) -> str:
    new_id = await compaction.compact_session(
        client, "anna", sid, context_window=100, force=True
    )
    assert new_id and new_id != sid
    return new_id


@pytest.mark.asyncio
async def test_unlock_confirmation_survives_a_compaction(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            _unlock_call(),
            ChatResult(content="Soll ich haustuer wirklich aufschließen?"),
            ChatResult(content="2 Fakten gespeichert."),  # compaction: extract
            ChatResult(content="Kurzfassung."),  # compaction: summary
            ChatResult(content="Ist offen."),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Haustür aufschließen")]
    assert posts == []

    new_id = await _compact(client, sid)
    assert client._pending.peek(new_id) is not None
    assert client._pending.peek(sid) is None

    _ = [e async for e in client.chat_stream(new_id, "ja")]
    assert len(posts) == 1
    assert posts[0][1]["entity_id"] == "lock.haustuer"


@pytest.mark.asyncio
async def test_task_delete_confirmation_survives_a_compaction(tmp_path, soul):
    db, notes = _task_db(tmp_path)
    tasks.create_task(db_path=db, notes_dir=notes, uid="anna", title="Milch")
    tools = build_choice_tools() + build_tasks_tools(
        db, lambda: "anna", notes_dir=notes
    )
    client, _ = _client(
        db,
        soul,
        [
            _delete_call(title="milch"),
            ChatResult(content="Soll ich die Aufgabe Milch wirklich löschen?"),
            ChatResult(content="1 Fakt gespeichert."),  # compaction: extract
            ChatResult(content="Kurzfassung."),  # compaction: summary
            ChatResult(content="Gelöscht."),
        ],
        tools=tools,
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "lösch die Aufgabe Milch")]
    assert len(tasks.list_tasks(db, "anna")) == 1

    new_id = await _compact(client, sid)

    _ = [e async for e in client.chat_stream(new_id, "ja")]
    assert tasks.list_tasks(db, "anna", include_done=True) == []


@pytest.mark.asyncio
async def test_compaction_does_not_extend_the_confirmation_window(
    db, soul, monkeypatch
):
    """Carrying the question across a compaction must not let it outlive its
    window and fire later against a different intent: the deadline travels with
    it, so a late "ja" gets the plain expiry answer — never silence, never a
    surprise unlock."""
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            _unlock_call(),
            ChatResult(content="Soll ich haustuer wirklich aufschließen?"),
            ChatResult(content="2 Fakten gespeichert."),
            ChatResult(content="Kurzfassung."),
            ChatResult(content="Diese Antwort darf nie kommen."),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Haustür aufschließen")]
    new_id = await _compact(client, sid)

    class _TwoHoursLater:
        @staticmethod
        def monotonic() -> float:
            return time.monotonic() + 7200.0

    monkeypatch.setattr(confirm, "time", _TwoHoursLater, raising=False)

    events = [e async for e in client.chat_stream(new_id, "ja")]
    assert posts == []
    answer = events[-1]["data"]["messages"][-1]["content"]
    assert "abgelaufen" in answer and "nichts ausgeführt" in answer
