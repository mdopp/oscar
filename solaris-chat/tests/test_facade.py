"""Tests for the Ollama-compatible facade + the engine's stateless respond().

The facade is what HA's `ollama` integration and the voice-gatekeeper speak:
GET /ollama/api/tags for config-flow validation, POST /ollama/api/chat for
turns (NDJSON stream or single JSON). respond() runs the same agent loop
statelessly — caller-owned history, nothing persisted to the store.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

import pytest
from solaris_chat.engine import store
from solaris_chat.engine.bus import SessionBus
from solaris_chat.engine.client import NO_ANSWER, EngineClient, EngineProfile
from solaris_chat.engine.ollama import ChatResult, OllamaError
from solaris_chat.engine.tools import Tool, Toolbox, Visibility
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


def _engine(db, soul, results, tools=None, name="household", bus=None):
    fake = FakeOllama(results)
    client = EngineClient(
        EngineProfile(
            name=name,
            model="gemma4:e2b",
            soul_path=soul,
            toolbox=Toolbox(tools or []),
        ),
        db_path=db,
        ollama=fake,
        recorder=TraceRecorder(),
        context_window=32768,
        bus=bus,
    )
    return client, fake


# -- respond() -------------------------------------------------------------


async def test_respond_is_stateless_and_folds_system(db, soul):
    client, fake = _engine(
        db, soul, [ChatResult(content="Klar.", prompt_tokens=10, completion_tokens=2)]
    )
    messages = [
        {"role": "system", "content": "Antworte kurz."},
        {"role": "user", "content": "Licht an"},
    ]
    events = [e async for e in client.respond(messages, uid="michael")]
    assert events[-1]["type"] == "run.completed"
    sent = fake.calls[0]["messages"]
    # One folded system block: soul first, HA's prompt after.
    assert sent[0]["role"] == "system"
    assert sent[0]["content"].startswith("Du bist Solaris.")
    assert "Antworte kurz." in sent[0]["content"]
    assert sum(1 for m in sent if m["role"] == "system") == 1
    # Time hint rides the last user message, not the system block.
    assert sent[-1]["content"].startswith("[Aktuelle Zeit:")
    assert sent[-1]["content"].endswith("Licht an")
    # Nothing persisted: the store has no sessions.
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM engine_sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM engine_messages").fetchone()[0] == 0
    conn.close()


async def test_tool_discipline_pinned_last_before_caller_prompt(db, soul):
    # Position is load-bearing (box A/B 2026-06-12): the anti-narration rule
    # must sit at the END of the engine's system block, after soul/registry,
    # so it outweighs narrative examples in the caller-supplied history.
    async def handler(args):
        return "{}"

    tool = Tool(
        name="ha_call_service",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    client, fake = _engine(
        db,
        soul,
        [ChatResult(content="Ok.", prompt_tokens=5, completion_tokens=1)],
        tools=[tool],
    )
    messages = [
        {"role": "system", "content": "Antworte kurz."},
        {"role": "user", "content": "Licht an"},
    ]
    [e async for e in client.respond(messages, uid="michael")]
    system = fake.calls[0]["messages"][0]["content"]
    assert "Sage NIEMALS nur" in system
    # Recency regression (box 2026-06-12 evening): the rule must come AFTER
    # the caller (HA) prompt — "Antworte kurz" as the last line re-broke the
    # discipline and the model narrated device actions again.
    assert system.index("Du bist Solaris.") < system.index("Antworte kurz.")
    assert system.index("Antworte kurz.") < system.index("Sage NIEMALS nur")
    assert system.rstrip().endswith("führst du ohne Rückfrage direkt aus.")


async def test_no_tool_discipline_without_tools(db, soul):
    client, fake = _engine(
        db, soul, [ChatResult(content="Hi.", prompt_tokens=5, completion_tokens=1)]
    )
    [e async for e in client.respond([{"role": "user", "content": "hi"}], uid="m")]
    assert "Sage NIEMALS nur" not in fake.calls[0]["messages"][0]["content"]


async def test_respond_runs_tool_loop(db, soul):
    seen = []

    async def handler(args):
        seen.append(args)
        return '{"ok": true}'

    tool = Tool(
        name="ha_call_service",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    client, fake = _engine(
        db,
        soul,
        [
            ChatResult(
                content="",
                tool_calls=[
                    {
                        "function": {
                            "name": "ha_call_service",
                            "arguments": {"entity_id": "light.buero"},
                        }
                    }
                ],
                prompt_tokens=10,
            ),
            ChatResult(content="Erledigt.", prompt_tokens=12, completion_tokens=2),
        ],
        tools=[tool],
    )
    events = [
        e
        async for e in client.respond(
            [{"role": "user", "content": "mach das büro licht an"}], uid="michael"
        )
    ]
    assert seen == [{"entity_id": "light.buero"}]
    kinds = [e["type"] for e in events]
    assert "tool.started" in kinds and "tool.completed" in kinds
    final = events[-1]["data"]["messages"][-1]["content"]
    assert final == "Erledigt."
    assert len(fake.calls) == 2


# -- facade routes -----------------------------------------------------------


def _app(db, soul, results, api_key="", bus=None):
    household, fake = _engine(db, soul, results, bus=bus)
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        api_key=api_key,
        bus=bus,
    )
    return app, fake


async def test_tags_lists_profiles(aiohttp_client, db, soul):
    app, _ = _app(db, soul, [])
    client = await aiohttp_client(app)
    body = await (await client.get("/ollama/api/tags")).json()
    names = [m["model"] for m in body["models"]]
    assert names == ["solaris"]


async def test_tags_requires_bearer_when_key_set(aiohttp_client, db, soul):
    app, _ = _app(db, soul, [], api_key="secret")
    client = await aiohttp_client(app)
    assert (await client.get("/ollama/api/tags")).status == 401
    ok = await client.get(
        "/ollama/api/tags", headers={"Authorization": "Bearer secret"}
    )
    assert ok.status == 200


async def test_chat_stream_ndjson(aiohttp_client, db, soul):
    app, _ = _app(
        db,
        soul,
        [ChatResult(content="Hallo zurück!", prompt_tokens=10, completion_tokens=3)],
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/ollama/api/chat",
        json={"model": "solaris", "messages": [{"role": "user", "content": "Hallo"}]},
    )
    assert resp.status == 200
    lines = [json.loads(line) for line in (await resp.text()).strip().splitlines()]
    assert lines[-1]["done"] is True
    assert lines[-1]["done_reason"] == "stop"
    content = "".join(line["message"]["content"] for line in lines)
    assert "Hallo" in content


async def test_chat_non_stream_single_json(aiohttp_client, db, soul):
    app, _ = _app(
        db,
        soul,
        [ChatResult(content="Erledigt.", prompt_tokens=10, completion_tokens=2)],
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Licht an"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["done"] is True
    assert body["message"]["content"] == "Erledigt."


# -- #566: continuous conversation — a follow-up turn ends in `?` -----------


def _offer_tool():
    from solaris_chat.engine.tools.choices import build_choice_tools

    return build_choice_tools()


def _offer_then_say(content: str):
    # Pass 1 calls offer_choices (populates the choice_sink -> quick_replies);
    # pass 2 produces the spoken answer text.
    return [
        ChatResult(
            content="",
            tool_calls=[
                {
                    "function": {
                        "name": "offer_choices",
                        "arguments": {"options": ["ja", "nein"]},
                    }
                }
            ],
            prompt_tokens=5,
        ),
        ChatResult(content=content, prompt_tokens=6, completion_tokens=2),
    ]


async def test_followup_turn_final_text_ends_with_question_mark_non_stream(
    aiohttp_client, db, soul
):
    # A turn that called offer_choices must end the spoken text in `?` so HA's
    # ollama integration sets continue_conversation and the Voice PE re-opens
    # the mic without a re-wake (#566). The model's text lacks the `?`.
    household, _ = _engine(db, soul, _offer_then_say("Soll ich das Garagentor öffnen"))
    household._profile.toolbox = Toolbox(_offer_tool())
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Garagentor öffnen"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["message"]["content"].rstrip().endswith("?")


async def test_followup_turn_final_text_ends_with_question_mark_stream(
    aiohttp_client, db, soul
):
    household, _ = _engine(db, soul, _offer_then_say("Meinst du das Büro oder das Bad"))
    household._profile.toolbox = Toolbox(_offer_tool())
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "messages": [{"role": "user", "content": "Mach das Licht an"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    lines = [json.loads(line) for line in (await resp.text()).strip().splitlines()]
    content = "".join(line["message"]["content"] for line in lines)
    assert content.rstrip().endswith("?")


async def test_followup_does_not_double_existing_question_mark(
    aiohttp_client, db, soul
):
    # The model already asked properly — don't append a second `?`.
    household, _ = _engine(db, soul, _offer_then_say("Garage öffnen?"))
    household._profile.toolbox = Toolbox(_offer_tool())
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Garagentor öffnen"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["message"]["content"].rstrip().endswith("?")
    assert not body["message"]["content"].rstrip().endswith("??")


async def test_statement_turn_does_not_get_forced_question_mark(
    aiohttp_client, db, soul
):
    # No offer_choices this turn: a normal statement must NOT be forced into a
    # question, so the Voice PE stops listening (continue_conversation=False).
    app, _ = _app(
        db,
        soul,
        [ChatResult(content="Erledigt.", prompt_tokens=5, completion_tokens=1)],
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Licht an"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["message"]["content"] == "Erledigt."
    assert not body["message"]["content"].rstrip().endswith("?")


async def test_mid_text_question_then_statement_gets_trailing_question_mark(
    aiohttp_client, db, soul
):
    # The model asks a question but APPENDS statements after it, so the `?` is
    # mid-reply and the text doesn't end in `?` (#627). A question IS pending —
    # force the trailing `?` so HA keeps the mic open for the answer.
    app, _ = _app(
        db,
        soul,
        [
            ChatResult(
                content="Welche Farbe möchtest du? Ich habe Rot und Blau parat.",
                prompt_tokens=5,
                completion_tokens=3,
            )
        ],
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Mach Licht an"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["message"]["content"].rstrip().endswith("?")


async def test_mid_text_question_then_statement_stream(aiohttp_client, db, soul):
    app, _ = _app(
        db,
        soul,
        [
            ChatResult(
                content="Soll ich das Garagentor öffnen? Es ist gerade geschlossen.",
                prompt_tokens=5,
                completion_tokens=3,
            )
        ],
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "messages": [{"role": "user", "content": "Garagentor"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    lines = [json.loads(line) for line in (await resp.text()).strip().splitlines()]
    content = "".join(line["message"]["content"] for line in lines)
    assert content.rstrip().endswith("?")


async def test_quick_replies_without_trailing_question_still_continues(
    aiohttp_client, db, soul
):
    # offer_choices fired but the spoken text neither ends nor contains a `?` —
    # a question is still pending (the chips are the answer options), so force
    # the trailing `?` to keep the mic open (#566).
    household, _ = _engine(db, soul, _offer_then_say("Wähle eine Option"))
    household._profile.toolbox = Toolbox(_offer_tool())
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Optionen"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["message"]["content"].rstrip().endswith("?")


async def test_bare_confirmation_does_not_continue(aiohttp_client, db, soul):
    # A bare confirmation with no `?` and no chips is not a question — the mic
    # must close (continue_conversation=False).
    app, _ = _app(
        db,
        soul,
        [ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1)],
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Mach das"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["message"]["content"] == "Klar."
    assert not body["message"]["content"].rstrip().endswith("?")


async def test_chat_unknown_model_404(aiohttp_client, db, soul):
    app, _ = _app(db, soul, [])
    client = await aiohttp_client(app)
    resp = await client.post(
        "/ollama/api/chat",
        json={"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status == 404


async def test_tool_pass2_sees_the_turn_uid(db, soul):
    # Regression: the SSE heartbeat runs each generator step in its own task,
    # so the contextvar set at turn start is invisible from pass 2 on — the
    # loop re-pins the uid in the dispatching task (timers/facts must never
    # be written ownerless).
    from solaris_chat.engine import client as engine_client

    seen_uids = []

    async def handler(args):
        seen_uids.append(engine_client.current_uid.get())
        return "{}"

    tool = Tool(
        name="timer_set",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    client, _ = _engine(
        db,
        soul,
        [
            ChatResult(
                content="",
                tool_calls=[{"function": {"name": "timer_set", "arguments": {}}}],
                prompt_tokens=5,
            ),
            ChatResult(content="Ok.", prompt_tokens=6, completion_tokens=1),
        ],
        tools=[tool],
    )

    async def consume_each_step_in_own_task():
        # Mirror server._heartbeat: every __anext__ in a fresh task.
        import asyncio

        gen = client.respond(
            [{"role": "user", "content": "Timer bitte"}], uid="michael"
        ).__aiter__()
        while True:
            try:
                await asyncio.ensure_future(gen.__anext__())
            except StopAsyncIteration:
                break

    await consume_each_step_in_own_task()
    assert seen_uids == ["michael"]


async def test_stream_abort_does_not_raise_foreign_context(aiohttp_client, db, soul):
    # Regression for the panel "Network error": closing the SSE stream runs
    # the generator finally in a foreign task context — the contextvar reset
    # must never ValueError through the response (chat path, not facade).
    household, _ = _engine(
        db,
        soul,
        [ChatResult(content="Hallo zurück!", prompt_tokens=5, completion_tokens=2)],
    )
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/api/chat/stream",
        json={"input": "Hallo"},
        headers={"Remote-User": "michael"},
    )
    body = await resp.text()
    assert resp.status == 200
    assert "event: done" in body
    assert "ValueError" not in body


# -- #345: durable household session for voice ------------------------------


async def test_respond_session_persists_into_durable_household_session(db, soul):
    # A voice turn now lands in the resident's durable household session (the
    # same row the browser opens) — not a stateless replay. Only the latest
    # user utterance is run; the store owns the history.
    client, _ = _engine(
        db, soul, [ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1)]
    )
    events = [e async for e in client.respond_session("Licht an", uid="michael")]
    assert events[-1]["type"] == "run.completed"
    sid = store.household_session_id("michael")
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT role, content FROM engine_messages WHERE session_id = ? ORDER BY seq",
        (sid,),
    ).fetchall()
    conn.close()
    # The session exists, owned by the resident, with the user + assistant turn.
    assert store.session_owner(db, sid) == "michael"
    assert [r[0] for r in rows] == ["user", "assistant"]
    assert rows[0][1].endswith("Licht an")
    assert rows[1][1] == "Klar."


async def test_voice_session_lists_and_is_idempotent(aiohttp_client, db, soul):
    # Two voice turns reuse ONE durable session (deterministic id), and it
    # surfaces in the resident's GET /api/sessions list.
    app, _ = _app(
        db,
        soul,
        [
            ChatResult(content="Eins.", prompt_tokens=5, completion_tokens=1),
            ChatResult(content="Zwei.", prompt_tokens=5, completion_tokens=1),
        ],
    )
    http = await aiohttp_client(app)
    for text in ("Hallo", "Und nochmal"):
        resp = await http.post(
            "/ollama/api/chat",
            json={
                "model": "solaris",
                "stream": False,
                "messages": [{"role": "user", "content": text}],
                "user": "michael",
            },
        )
        assert resp.status == 200
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM engine_sessions").fetchone()[0]
    conn.close()
    assert n == 1  # idempotent — one durable session for both turns
    listed = await (
        await http.get("/api/sessions", headers={"Remote-User": "michael"})
    ).json()
    assert store.household_session_id("michael") in [
        s["id"] for s in listed["sessions"]
    ]


# -- #405: a voice turn persists its trace into the household session --------


async def test_voice_turn_persists_session_trace_row(aiohttp_client, db, soul):
    # A durable voice turn now writes a session_traces row into the SAME
    # household session as its messages (#405) — so the "Zuhause" chat reopens
    # with the per-turn trace, not just message history. session_traces is part
    # of the shared `_SCHEMA` (test_engine), so no local CREATE is needed.
    app, _ = _app(
        db, soul, [ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1)]
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Licht an"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    sid = store.household_session_id("michael")
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT owner_uid, step_kind FROM session_traces WHERE session_id = ?",
        (sid,),
    ).fetchall()
    conn.close()
    assert rows and all(r[0] == "michael" for r in rows)
    assert any(r[1] == "llm" for r in rows)


async def test_failed_voice_turn_still_persists_its_trace(aiohttp_client, db, soul):
    # A voice turn that errors mid-loop (#562) must still write the steps the
    # recorder captured before the failure — otherwise the failure (the
    # operator's intent-failed) is invisible in the chat UI. Pass 1 runs a tool
    # (one llm + one tool step recorded under the household session), pass 2
    # raises, surfacing as an EngineError; the trace must persist anyway.
    seen = []

    async def handler(args):
        seen.append(args)
        return '{"ok": true}'

    tool = Tool(
        name="ha_call_service",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )

    class FailingOllama:
        def __init__(self):
            self.n = 0

        async def stream(self, model, messages, tools=None, think=False, options=None):
            self.n += 1
            if self.n == 1:
                yield (
                    "done",
                    ChatResult(
                        content="",
                        tool_calls=[
                            {
                                "function": {
                                    "name": "ha_call_service",
                                    "arguments": {"entity_id": "light.buero"},
                                }
                            }
                        ],
                        prompt_tokens=10,
                    ),
                )
                return
            raise OllamaError("intent-failed")
            yield  # pragma: no cover — makes this an async generator

    household = EngineClient(
        EngineProfile(
            name="household",
            model="gemma4:e2b",
            soul_path=soul,
            toolbox=Toolbox([tool]),
        ),
        db_path=db,
        ollama=FailingOllama(),
        recorder=TraceRecorder(),
        context_window=32768,
    )
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "messages": [{"role": "user", "content": "Licht an"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    lines = [json.loads(line) for line in (await resp.text()).strip().splitlines()]
    assert lines[-1]["done_reason"] == "error"
    sid = store.household_session_id("michael")
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT step_kind FROM session_traces WHERE session_id = ?",
        (sid,),
    ).fetchall()
    conn.close()
    assert rows, "a failed voice turn must still leave a visible trace"
    assert any(r[0] == "llm" for r in rows)


async def test_guest_voice_turn_persists_no_trace(aiohttp_client, db, soul):
    # The ephemeral guest path must persist nothing — neither messages nor a
    # session_traces row. session_traces comes from the shared `_SCHEMA`.
    app, _ = _app_with_guest(
        db, soul, [ChatResult(content="Gast.", prompt_tokens=5, completion_tokens=1)]
    )
    _stash(db, "Wer bist du", "guest", matched=False)
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Wer bist du"}],
            "user": "household",
        },
    )
    assert resp.status == 200
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM session_traces").fetchone()[0]
    conn.close()
    assert n == 0


# -- #344: live mirror into open browser sessions ----------------------------


async def test_voice_turn_mirrors_to_an_open_browser(aiohttp_client, db, soul):
    # An open browser SSE on the household session receives the voice turn
    # near-live: the transcript (mirror_user) then the streamed answer (delta).
    bus = SessionBus()
    app, _ = _app(
        db,
        soul,
        [ChatResult(content="Mache ich.", prompt_tokens=5, completion_tokens=2)],
        bus=bus,
    )
    http = await aiohttp_client(app)
    sid = store.ensure_household_session(db, "michael")

    async def run_voice_turn() -> None:
        await asyncio.sleep(0.05)  # let the subscriber attach first
        await http.post(
            "/ollama/api/chat",
            json={
                "model": "solaris",
                "stream": False,
                "messages": [{"role": "user", "content": "Licht an"}],
                "user": "michael",
            },
        )

    sub = await http.get(
        f"/api/sessions/{sid}/events", headers={"Remote-User": "michael"}
    )
    task = asyncio.create_task(run_voice_turn())
    body = b""
    while b"event: completed" not in body:
        chunk = await asyncio.wait_for(sub.content.read(256), timeout=5)
        if not chunk:
            break
        body += chunk
    await task
    text = body.decode()
    assert "event: mirror_user" in text
    assert "Licht an" in text  # the transcript reached the browser
    assert "event: delta" in text
    # The answer mirrored too (deltas arrive token-wise).
    answer = "".join(
        json.loads(line[len("data: ") :])["text"]
        for block in text.split("\n\n")
        if "event: delta" in block
        for line in block.splitlines()
        if line.startswith("data: ")
    )
    assert "Mache" in answer and "ich." in answer


async def test_mirror_is_per_resident_scoped(aiohttp_client, db, soul):
    # A different resident may not subscribe to someone else's session (#344
    # privacy posture): the wrong-owner subscribe is forbidden.
    bus = SessionBus()
    app, _ = _app(db, soul, [], bus=bus)
    http = await aiohttp_client(app)
    sid = store.ensure_household_session(db, "michael")
    resp = await http.get(
        f"/api/sessions/{sid}/events", headers={"Remote-User": "anna"}
    )
    assert resp.status == 403


# -- #353: guest profile — restricted toolbox + ephemeral session ------------


async def test_guest_toolbox_allows_control_and_qa_but_no_writes():
    # The guest toolbox = HA control/state only; it must NOT carry any
    # durable-write tool (notes/fact_store, timers) or admin/MCP tool.
    from solaris_chat.engine.profiles import build_engine_clients

    _, _, guest, _, _, _, _ = build_engine_clients(
        db_path=":memory:",
        ollama_url="http://x",
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
        soul_path="/nonexistent/SOUL.md",
        hass_url="http://ha",
        hass_token="t",
        notes_dir="/tmp/notes",  # household gets notes; guest must not
    )
    toolsets = await guest.list_toolsets()
    names = set(toolsets[0]["tools"])
    # Allowed: device control + state reads.
    assert {"ha_call_service", "ha_get_state"} <= names
    # The web-search path is gone (#1122) — no guest web tools either.
    assert not (names & {"web_search", "web_extract", "research"})
    # Denied: durable writes and admin.
    assert not (
        names & {"note_write", "fact_store", "timer_set", "timer_list", "timer_cancel"}
    )
    assert guest.ephemeral is True


# -- #366: household profile reads the admin-set model override --------------


async def test_household_profile_reads_persisted_model(tmp_path):
    from solaris_chat import settings_store
    from solaris_chat.engine.profiles import build_engine_clients

    db = str(tmp_path / "solaris.db")
    household, _, _, _, _, _, _ = build_engine_clients(
        db_path=db,
        ollama_url="http://x",
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
        soul_path="/nonexistent/SOUL.md",
    )
    # Unset -> the configured fast default.
    assert household._model() == "gemma4:e2b"
    # An admin selection persists and the profile picks it up on the next turn.
    settings_store.set_household_model(db, "gemma4:12b")
    assert household._model() == "gemma4:12b"


async def test_guest_facade_turn_persists_nothing(aiohttp_client, db, soul):
    # A guest turn runs the stateless `respond` path: no session row, no
    # message row — nothing about the guest survives the conversation.
    household, _ = _engine(db, soul, [])
    guest, _ = _engine(
        db,
        soul,
        [ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1)],
        name="solaris-guest",
    )
    guest._profile.ephemeral = True
    app = build_app(
        engine=household,
        engine_guest=guest,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    http = await aiohttp_client(app)
    names = [
        m["model"]
        for m in (await (await http.get("/ollama/api/tags")).json())["models"]
    ]
    assert "solaris-guest" in names
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris-guest",
            "stream": False,
            "messages": [{"role": "user", "content": "Wie spät ist es?"}],
            "user": "guest",
        },
    )
    assert resp.status == 200
    assert (await resp.json())["message"]["content"] == "Klar."
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM engine_sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM engine_messages").fetchone()[0] == 0
    conn.close()


# -- #350: transcript-keyed uid side-channel (approach b) -------------------


def _stash(
    db: str, transcript: str, uid: str, *, matched: bool, room: str = ""
) -> None:
    """Write the row the gatekeeper would write. `matched` is keyword-only and
    has no default: the recognition claim is always stated, never inherited
    from the uid (#1152). `room` is the second half of the correlation key
    (#1218) and defaults to the HA-STT path's "gatekeeper doesn't know"."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO voice_uid_stash (transcript, room, uid, matched) "
        "VALUES (?, ?, ?, ?)",
        (transcript, room, uid, 1 if matched else 0),
    )
    conn.commit()
    conn.close()


async def test_facade_resolves_uid_from_stash(aiohttp_client, db, soul):
    # The gatekeeper stashed {transcript -> anna}; the facade must attribute
    # the turn to anna even though HA sends user=household.
    from solaris_chat.engine import store

    app, _ = _app(
        db, soul, [ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1)]
    )
    _stash(db, "Licht an", "anna", matched=True)
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Licht an"}],
            "user": "household",
        },
    )
    assert resp.status == 200
    # The durable session was created under the resolved resident, not household.
    sid = store.household_session_id("anna")
    assert store.session_owner(db, sid) == "anna"


async def test_facade_falls_back_to_household_on_stash_miss(aiohttp_client, db, soul):
    from solaris_chat.engine import store

    app, _ = _app(
        db, soul, [ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1)]
    )
    # No stash row for this transcript.
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Wer bin ich"}],
            "user": "household",
        },
    )
    assert resp.status == 200
    sid = store.household_session_id("household")
    assert store.session_owner(db, sid) == "household"


async def test_facade_stash_is_consume_once(aiohttp_client, db, soul):
    # The first turn consumes the stashed uid; an identical second utterance
    # falls back to household (the row is gone).
    from solaris_chat.engine import store

    app, _ = _app(
        db,
        soul,
        [
            ChatResult(content="Eins.", prompt_tokens=5, completion_tokens=1),
            ChatResult(content="Zwei.", prompt_tokens=5, completion_tokens=1),
        ],
    )
    _stash(db, "Licht an", "anna", matched=True)
    http = await aiohttp_client(app)
    for _ in range(2):
        resp = await http.post(
            "/ollama/api/chat",
            json={
                "model": "solaris",
                "stream": False,
                "messages": [{"role": "user", "content": "Licht an"}],
                "user": "household",
            },
        )
        assert resp.status == 200
    # The stash row was deleted on first read.
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM voice_uid_stash").fetchone()[0]
    conn.close()
    assert n == 0
    # First turn went to anna; the second (consumed) fell back to household.
    assert store.session_owner(db, store.household_session_id("anna")) == "anna"
    assert (
        store.session_owner(db, store.household_session_id("household")) == "household"
    )


async def test_facade_speaker_match_is_the_stash_not_the_user_field(
    aiohttp_client, db, soul
):
    # #1146: `user` routes the conversation, the stash is the evidence. The
    # gatekeeper writes a stash row only when speaker-ID actually attributed the
    # utterance, so PERSONAL unlocks on a stash hit and stays locked when the
    # body merely names a resident — which is all an inert speaker-ID setup, or
    # a satellite that never published its match, can offer.
    from solaris_chat.engine.tools import current_speaker_matched

    seen: list[bool] = []

    async def _capture(args):
        seen.append(current_speaker_matched.get())
        return "ok"

    capture = Tool(
        name="capture",
        description="capture the speaker verdict",
        parameters={"type": "object", "properties": {}},
        handler=_capture,
        visibility=Visibility.HOUSEHOLD,
    )
    call = ChatResult(
        content="",
        tool_calls=[{"function": {"name": "capture", "arguments": {}}}],
        prompt_tokens=5,
    )
    answer = ChatResult(content="Klar.", prompt_tokens=6, completion_tokens=2)
    household, _ = _engine(db, soul, [call, answer, call, answer], tools=[capture])
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        speaker_id_enabled=True,
    )
    http = await aiohttp_client(app)

    _stash(db, "Licht an", "anna", matched=True)
    for transcript in ("Licht an", "Wer bin ich"):
        resp = await http.post(
            "/ollama/api/chat",
            json={
                "model": "solaris",
                "stream": False,
                "messages": [{"role": "user", "content": transcript}],
                "user": "anna",
            },
        )
        assert resp.status == 200
    # Stashed match -> attributed. Stash miss with the very same `user` -> not.
    assert seen == [True, False]


def test_consume_speaker_is_atomic_consume_once(db):
    # A single consume returns the stashed speaker; a second returns None
    # because the read+delete happen in one statement under the write lock, so
    # a concurrent duplicate turn can't re-read the same identity.
    from solaris_chat import voice_uid_stash

    _stash(db, "Licht an", "anna", matched=True)
    assert voice_uid_stash.consume_speaker(db, "Licht an") == (
        voice_uid_stash.StashedSpeaker("anna", matched=True)
    )
    assert voice_uid_stash.consume_speaker(db, "Licht an") is None
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM voice_uid_stash").fetchone()[0] == 0
    conn.close()


def test_consume_speaker_ignores_but_reaps_expired_row(db):
    # A row past the TTL must not be consumed (no stale identity leaks into a
    # much-later identical utterance) but is still reaped from the table.
    from solaris_chat import voice_uid_stash

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO voice_uid_stash (transcript, uid, matched, created_at) "
        "VALUES (?, ?, 1, datetime('now', ?))",
        ("Licht an", "anna", f"-{voice_uid_stash.STASH_TTL_SECONDS + 60} seconds"),
    )
    conn.commit()
    conn.close()

    assert voice_uid_stash.consume_speaker(db, "Licht an") is None
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM voice_uid_stash").fetchone()[0] == 0
    conn.close()


# -- #1218: the transcript alone can't tell two residents apart -------------


def _count(db: str) -> int:
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM voice_uid_stash").fetchone()[0]
    conn.close()
    return n


def test_consume_speaker_room_gives_each_resident_their_own_row(db):
    # Two satellites, one sentence, two rooms: the room is the second half of
    # the key, so neither turn can be handed the other resident's identity.
    from solaris_chat import voice_uid_stash

    _stash(db, "Licht an", "anna", matched=True, room="küche")
    _stash(db, "Licht an", "bob", matched=True, room="bad")

    assert voice_uid_stash.consume_speaker(db, "Licht an", "Küche") == (
        voice_uid_stash.StashedSpeaker("anna", matched=True)
    )
    # bob's row survived anna's consume-once and is still his.
    assert voice_uid_stash.consume_speaker(db, "Licht an", "bad") == (
        voice_uid_stash.StashedSpeaker("bob", matched=True)
    )
    assert _count(db) == 0


def test_consume_speaker_races_two_residents_under_one_transcript(db):
    # The race itself: two turns consume concurrently. Each must come away
    # with its own room's row — never the other resident's uid+claim.
    from solaris_chat import voice_uid_stash

    _stash(db, "Licht an", "anna", matched=True, room="küche")
    _stash(db, "Licht an", "bob", matched=True, room="bad")
    got: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def consume(room: str) -> None:
        barrier.wait()
        got[room] = voice_uid_stash.consume_speaker(db, "Licht an", room)

    threads = [threading.Thread(target=consume, args=(r,)) for r in ("küche", "bad")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert got == {
        "küche": voice_uid_stash.StashedSpeaker("anna", matched=True),
        "bad": voice_uid_stash.StashedSpeaker("bob", matched=True),
    }


def test_consume_speaker_fails_closed_when_the_room_cannot_separate_two_rows(db):
    # The HA-STT path: the facade has no room prefix, so both rows are equally
    # plausible. Neither resident's identity may be handed out — the answer is
    # the unknown speaker, and both rows are burned so the second turn can't
    # pick up the loser.
    from solaris_chat import voice_uid_stash

    _stash(db, "Licht an", "anna", matched=True, room="küche")
    _stash(db, "Licht an", "bob", matched=True, room="bad")

    speaker = voice_uid_stash.consume_speaker(db, "Licht an")
    assert speaker == voice_uid_stash.StashedSpeaker("guest", matched=False)
    assert speaker.matched is False
    assert _count(db) == 0
    assert voice_uid_stash.consume_speaker(db, "Licht an") is None


def test_consume_speaker_refuses_a_row_from_a_different_room(db):
    # Both ends named a room and they disagree: this is not that turn's row.
    # It stays put for the turn it belongs to; this one falls back to the
    # household default.
    from solaris_chat import voice_uid_stash

    _stash(db, "Licht an", "anna", matched=True, room="küche")

    assert voice_uid_stash.consume_speaker(db, "Licht an", "bad") is None
    assert _count(db) == 1
    assert voice_uid_stash.consume_speaker(db, "Licht an", "küche") == (
        voice_uid_stash.StashedSpeaker("anna", matched=True)
    )


@pytest.mark.parametrize(("stashed", "asked"), [("", "Küche"), ("küche", ""), ("", "")])
def test_consume_speaker_still_resolves_when_one_end_has_no_room(db, stashed, asked):
    # The gatekeeper's peer on the HA-STT path is HA, not the satellite, so it
    # has no room to write; HA may still inject a `[room: X]` prefix. One end
    # not knowing must not break attribution when there is only one candidate.
    from solaris_chat import voice_uid_stash

    _stash(db, "Licht an", "anna", matched=True, room=stashed)
    assert voice_uid_stash.consume_speaker(db, "Licht an", asked) == (
        voice_uid_stash.StashedSpeaker("anna", matched=True)
    )


# -- #1152: `matched` is the claim; nothing else may stand in for it ---------


@pytest.mark.parametrize("uid", ["guest", "unknown", "anna"])
def test_consume_speaker_reads_no_match_without_the_claim(db, uid):
    # The row's existence and the uid it carries say nothing about
    # recognition — only the flag does, for any uid a future gatekeeper picks.
    from solaris_chat import voice_uid_stash

    _stash(db, "Licht an", uid, matched=False)
    speaker = voice_uid_stash.consume_speaker(db, "Licht an")
    assert speaker == voice_uid_stash.StashedSpeaker(uid, matched=False)
    assert speaker.matched is False


@pytest.mark.parametrize("value", [None, 0, "true", "yes", 2, -1])
def test_consume_speaker_only_accepts_an_exact_match_flag(db, value):
    # Anything but the integer 1 is not a claim: a NULL left by an older
    # writer, a stringly-typed value, a truthy-looking number. (`"1"` is
    # absent on purpose — the column's INTEGER affinity stores it as 1, so it
    # really is the claim.)
    from solaris_chat import voice_uid_stash

    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE voice_uid_stash")
    # NOT NULL is dropped here so the NULL case is reachable at all.
    conn.execute(
        "CREATE TABLE voice_uid_stash (transcript TEXT PRIMARY KEY, uid TEXT NOT NULL,"
        " matched INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO voice_uid_stash (transcript, uid, matched) VALUES (?, ?, ?)",
        ("Licht an", "anna", value),
    )
    conn.commit()
    conn.close()

    speaker = voice_uid_stash.consume_speaker(db, "Licht an")
    assert speaker == voice_uid_stash.StashedSpeaker("anna", matched=False)


def test_consume_speaker_on_a_premigration_table_attributes_but_claims_nothing(
    tmp_path,
):
    # Independent deploys: a new engine can start against a DB the 0031
    # migration hasn't reached. The uid still routes the turn; the missing
    # column reads as "not matched" rather than raising or unlocking.
    from solaris_chat import voice_uid_stash

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE voice_uid_stash (transcript TEXT PRIMARY KEY, uid TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO voice_uid_stash (transcript, uid) VALUES (?, ?)",
        ("Licht an", "anna"),
    )
    conn.commit()
    conn.close()

    assert voice_uid_stash.consume_speaker(path, "Licht an") == (
        voice_uid_stash.StashedSpeaker("anna", matched=False)
    )
    # Still consume-once through the compat path.
    assert voice_uid_stash.consume_speaker(path, "Licht an") is None


# -- #351: unknown speaker routes to the guest profile ----------------------


def _app_with_guest(db, soul, results):
    # An app whose facade has the guest profile wired, so unknown-speaker
    # routing has a target (the guest model is ephemeral).
    household, _ = _engine(db, soul, [])
    guest, fake = _engine(db, soul, results, name="solaris-guest")
    guest._profile.ephemeral = True
    app = build_app(
        engine=household,
        engine_guest=guest,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    return app, fake


async def test_unknown_speaker_routes_to_guest_profile(aiohttp_client, db, soul):
    # Speaker-ID ran and matched no resident: the gatekeeper stashed the `guest`
    # sentinel. The turn must run the ephemeral guest profile (HA still asks for
    # model=solaris) — nothing persists, no household session is created for it.
    from solaris_chat.engine import store

    app, guest_fake = _app_with_guest(
        db, soul, [ChatResult(content="Gast.", prompt_tokens=5, completion_tokens=1)]
    )
    _stash(db, "Wer bist du", "guest", matched=False)
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Wer bist du"}],
            "user": "household",
        },
    )
    assert resp.status == 200
    assert (await resp.json())["message"]["content"] == "Gast."
    # The guest (ephemeral) client served the turn.
    assert len(guest_fake.calls) == 1
    # Nothing persisted — neither a guest nor a household session.
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM engine_sessions").fetchone()[0] == 0
    conn.close()
    assert store.session_owner(db, store.household_session_id("household")) is None


async def test_speaker_id_off_stays_household_not_guest(aiohttp_client, db, soul):
    # No stash row (speaker-ID off / not attempted): the turn falls back to
    # household — it must NOT be routed to the guest profile.
    from solaris_chat.engine import store

    app, _ = _app(
        db, soul, [ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1)]
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Licht an"}],
            "user": "household",
        },
    )
    assert resp.status == 200
    # Ran on household: the durable household session exists, owned by household.
    sid = store.household_session_id("household")
    assert store.session_owner(db, sid) == "household"


async def test_identified_resident_runs_as_their_uid_not_guest(
    aiohttp_client, db, soul
):
    # Speaker-ID identified an enrolled resident: the turn runs as their uid in
    # their durable session, not the guest profile (the guest profile is wired
    # but the resident uid must bypass it).
    from solaris_chat.engine import store

    household, _ = _engine(
        db, soul, [ChatResult(content="x", prompt_tokens=1, completion_tokens=1)]
    )
    guest, guest_fake = _engine(db, soul, [], name="solaris-guest")
    guest._profile.ephemeral = True
    app = build_app(
        engine=household,
        engine_guest=guest,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    _stash(db, "Licht an", "anna", matched=True)
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Licht an"}],
            "user": "household",
        },
    )
    assert resp.status == 200
    sid = store.household_session_id("anna")
    assert store.session_owner(db, sid) == "anna"
    # The guest profile was not used for an identified resident.
    assert guest_fake.calls == []


# -- #616: strip [[ ]] cross-links to plain text on the voice/facade path ----


def test_strip_wikilinks_renders_plain_spoken_text():
    from solaris_chat.engine.facade import _strip_wikilinks

    assert _strip_wikilinks("[[Anna]] kommt") == "Anna kommt"
    assert _strip_wikilinks("[[Buero-Licht|das Licht]] ist an") == "das Licht ist an"
    # Several links in one string, mixed plain/labelled.
    assert (
        _strip_wikilinks("[[Anna]] und [[bob|der Bob]] sind [[da]]")
        == "Anna und der Bob sind da"
    )
    # No links -> unchanged.
    assert _strip_wikilinks("Alles erledigt.") == "Alles erledigt."


def test_wikilink_stripper_handles_link_split_across_deltas():
    # A labelled link contains a space, so the streaming source splits it across
    # two deltas — the stripper must still render the whole link, never leak a
    # stray `[[`/`[`.
    from solaris_chat.engine.facade import WikilinkStripper

    s = WikilinkStripper()
    out = s.feed("[[Buero-Licht|das ") + s.feed("Licht]] an") + s.flush()
    assert out == "das Licht an"
    assert "[" not in out


async def test_facade_strips_wikilinks_non_stream(aiohttp_client, db, soul):
    # A non-stream voice turn (the gatekeeper's surface) whose reply wraps an
    # entity returns plain text for clean TTS — no brackets/pipe.
    app, _ = _app(
        db,
        soul,
        [
            ChatResult(
                content="[[Anna]] hat das [[Buero-Licht|Licht]] an gelassen.",
                prompt_tokens=5,
                completion_tokens=3,
            )
        ],
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Wer war das"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    content = (await resp.json())["message"]["content"]
    assert content == "Anna hat das Licht an gelassen."
    assert "[[" not in content


async def test_facade_strips_wikilinks_stream(aiohttp_client, db, soul):
    app, _ = _app(
        db,
        soul,
        [
            ChatResult(
                content="[[Anna]] hat das [[Buero-Licht|Licht]] an gelassen.",
                prompt_tokens=5,
                completion_tokens=3,
            )
        ],
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "messages": [{"role": "user", "content": "Wer war das"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    lines = [json.loads(line) for line in (await resp.text()).strip().splitlines()]
    content = "".join(line["message"]["content"] for line in lines)
    assert content.strip() == "Anna hat das Licht an gelassen."
    assert "[[" not in content


async def test_browser_path_keeps_wikilinks(aiohttp_client, db, soul):
    # The browser/SPA path (server.py /api/chat/stream -> client.chat_stream) is
    # a DIFFERENT route from the facade and must keep `[[ ]]` so the SPA renders
    # tap-through links — the strip is voice-only.
    household, _ = _engine(
        db,
        soul,
        [ChatResult(content="[[Anna]] kommt.", prompt_tokens=5, completion_tokens=2)],
    )
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/api/chat/stream",
        json={"input": "Wer kommt"},
        headers={"Remote-User": "michael"},
    )
    body = await resp.text()
    assert resp.status == 200
    assert "[[Anna]]" in body


async def test_chat_latest_suffix_resolves(aiohttp_client, db, soul):
    app, _ = _app(
        db, soul, [ChatResult(content="ok", prompt_tokens=1, completion_tokens=1)]
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/ollama/api/chat",
        json={
            "model": "solaris:latest",
            "stream": False,
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert resp.status == 200


# -- u99: a `[room: X]` prefix sets current_room + is stripped from the text --


def test_split_room_parses_and_strips_prefix():
    from solaris_chat.engine.facade import _split_room

    assert _split_room("[room: Küche]\nspiele Musik") == ("Küche", "spiele Musik")
    # case-insensitive marker, optional whitespace, no newline
    assert _split_room("[ROOM: Bad] Licht an") == ("Bad", "Licht an")
    # no prefix → untouched
    assert _split_room("spiele Musik") == ("", "spiele Musik")


async def test_chat_strips_room_prefix_and_sets_current_room(aiohttp_client, db, soul):
    from solaris_chat.engine.client import current_room

    seen = {}

    async def _capture(args):
        seen["room"] = current_room.get()
        return "ok"

    capture = Tool(
        name="capture",
        description="capture the current room",
        parameters={"type": "object", "properties": {}},
        handler=_capture,
        # A room lookup is household-visible; without a class the #1130 gate
        # would (correctly) withhold it on this voice route.
        visibility=Visibility.HOUSEHOLD,
    )
    # Pass 1 calls the capturing tool; pass 2 answers.
    results = [
        ChatResult(
            content="",
            tool_calls=[{"function": {"name": "capture", "arguments": {}}}],
            prompt_tokens=5,
        ),
        ChatResult(content="Läuft.", prompt_tokens=6, completion_tokens=2),
    ]
    household, fake = _engine(db, soul, results, tools=[capture])
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "[room: Küche]\nspiele Musik"}],
            "user": "household",
        },
    )
    assert resp.status == 200
    # The room was threaded to the tool via the contextvar...
    assert seen["room"] == "Küche"
    # ...and the model never saw the `[room:` marker (stripped from the user turn).
    user_msgs = [m["content"] for m in fake.calls[0]["messages"] if m["role"] == "user"]
    assert user_msgs
    assert all("[room:" not in c for c in user_msgs)
    assert any(c.rstrip().endswith("spiele Musik") for c in user_msgs)


# -- #724: a completed voice turn propagates over SSE / Web Push -------------


class _FakeNotifier:
    def __init__(self):
        self.pushes: list[tuple] = []

    async def push(self, uid, title, body, data):
        self.pushes.append((uid, title, body, data))


async def test_voice_turn_pushes_when_no_sse_client(aiohttp_client, db, soul):
    # A completed voice turn (HA Assist -> facade) with no portal open pushes
    # the reply to the resident's phone, deep-linking the household session.
    from solaris_chat.engine.notify import EventBus

    household, _ = _engine(
        db, soul, [ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1)]
    )
    bus = EventBus()  # nobody subscribed → app backgrounded
    notifier = _FakeNotifier()
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        event_bus=bus,
        notifier=notifier,
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Licht an"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    assert len(notifier.pushes) == 1
    uid, _title, body, data = notifier.pushes[0]
    assert uid == "michael"
    assert body == "Klar."
    assert data["kind"] == "chat"
    assert data["url"] == f"/#/c/{store.household_session_id('michael')}"


async def test_voice_turn_does_not_push_when_sse_client_open(aiohttp_client, db, soul):
    # An open /api/events subscriber gets the chat event live — no phone push.
    from solaris_chat.engine.notify import EventBus

    household, _ = _engine(
        db, soul, [ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1)]
    )
    bus = EventBus()
    notifier = _FakeNotifier()
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        event_bus=bus,
        notifier=notifier,
    )
    http = await aiohttp_client(app)
    sse = await http.get("/api/events", headers={"Remote-User": "michael"})
    await asyncio.sleep(0.05)  # let the subscription register
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Licht an"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    assert notifier.pushes == []  # SSE delivered it; no phone push
    sse.close()


async def test_ephemeral_guest_turn_does_not_emit(aiohttp_client, db, soul):
    # A guest (ephemeral) turn persists nothing and must fire no SSE/push — it
    # is not a resident's durable session, so there is nothing to surface.
    from solaris_chat.engine.notify import EventBus

    household, _ = _engine(db, soul, [])
    guest_fake = FakeOllama(
        [ChatResult(content="Hallo.", prompt_tokens=5, completion_tokens=1)]
    )
    guest = EngineClient(
        EngineProfile(
            name="solaris-guest",
            model="gemma4:e2b",
            soul_path=soul,
            toolbox=Toolbox([]),
            ephemeral=True,
        ),
        db_path=db,
        ollama=guest_fake,
        recorder=TraceRecorder(),
        context_window=32768,
    )
    bus = EventBus()
    notifier = _FakeNotifier()
    app = build_app(
        engine=household,
        engine_guest=guest,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        event_bus=bus,
        notifier=notifier,
    )
    http = await aiohttp_client(app)
    resp = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris-guest",
            "stream": False,
            "messages": [{"role": "user", "content": "Hallo"}],
            "user": "guest",
        },
    )
    assert resp.status == 200
    assert notifier.pushes == []


# --- conversation-scoped prompt history (#1067) ---------------------------


async def test_poisoned_history_from_an_old_conversation_is_not_replayed(db, soul):
    """The regression this change exists for. The shared Zuhause row carried a
    day of turns including 12 identity statements from failed enrolments, and
    every later voice turn preloaded them — measured on the box: ~5.6k tokens,
    and a single-message request still answered "… wurde als M-D-O-P-P
    erkannt!". Old conversations must not reach the prompt; the browser must
    still show them."""
    client, fake = _engine(
        db, soul, [ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1)]
    )
    sid = store.ensure_household_session(db, "michael")
    # Two shapes of poison: a legacy row (no conversation id at all) and one
    # stamped with an old, long-idle conversation.
    store.append_message(db, sid, "assistant", "Michael wurde als M-D-O-P-P erkannt!")
    store.append_message(db, sid, "user", "alte Frage", conversation_id="alt")
    store.append_message(db, sid, "assistant", "Alte Antwort.", conversation_id="alt")
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE engine_messages SET created_at = datetime('now', '-3 hours')"
        " WHERE session_id = ?",
        (sid,),
    )
    conn.commit()
    conn.close()

    async for _ in client.respond_session("Wie warm ist es?", uid="michael"):
        pass

    sent = fake.calls[0]["messages"]
    assert not any("erkannt" in str(m.get("content", "")) for m in sent)
    assert not any("Alte Antwort" in str(m.get("content", "")) for m in sent)
    assert [m["role"] for m in sent if m["role"] != "system"] == ["user"]
    # The browser history keeps every bubble.
    shown = [m["content"] for m in store.get_session(db, sid, "michael")["messages"]]
    assert any("erkannt" in c for c in shown)


async def test_ha_conversation_id_scopes_the_prompt(db, soul):
    client, fake = _engine(
        db,
        soul,
        [
            ChatResult(content=f"A{i}", prompt_tokens=5, completion_tokens=1)
            for i in range(3)
        ],
    )
    async for _ in client.respond_session("erste", uid="michael", conversation_id="A"):
        pass
    async for _ in client.respond_session("zweite", uid="michael", conversation_id="A"):
        pass
    async for _ in client.respond_session("dritte", uid="michael", conversation_id="B"):
        pass

    second = [str(m.get("content", "")) for m in fake.calls[1]["messages"]]
    assert any("erste" in c for c in second)  # same conversation carries over
    third = [str(m.get("content", "")) for m in fake.calls[2]["messages"]]
    assert not any("erste" in c or "zweite" in c for c in third)


async def test_absent_conversation_id_reuses_within_the_idle_gap(db, soul):
    client, fake = _engine(
        db,
        soul,
        [
            ChatResult(content=f"A{i}", prompt_tokens=5, completion_tokens=1)
            for i in range(2)
        ],
    )
    async for _ in client.respond_session("erste", uid="michael"):
        pass
    async for _ in client.respond_session("zweite", uid="michael"):
        pass

    second = [str(m.get("content", "")) for m in fake.calls[1]["messages"]]
    assert any("erste" in c for c in second)


async def test_enrollment_say_is_persisted_once_and_hidden_from_the_prompt(db, soul):
    """The say short-circuit wrote the spoken line twice and left it in the
    shared session, where it replayed into every later prompt."""

    async def _start(args):
        return json.dumps({"ok": True, "say": "Alex wurde als A - L - E - X erkannt?"})

    tool = Tool(
        name="start_wakeword_enrollment",
        description="Startet die Wakeword-Aufnahme.",
        parameters={"type": "object", "properties": {}},
        handler=_start,
    )
    client, fake = _engine(
        db,
        soul,
        [
            ChatResult(
                content="",
                tool_calls=[
                    {
                        "function": {
                            "name": "start_wakeword_enrollment",
                            "arguments": {},
                        }
                    }
                ],
                prompt_tokens=5,
                completion_tokens=1,
            ),
            ChatResult(content="Klar.", prompt_tokens=5, completion_tokens=1),
        ],
        tools=[tool],
    )
    async for _ in client.respond_session("Richte das Wakeword ein", uid="michael"):
        pass

    sid = store.household_session_id("michael")
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT role, content, in_prompt FROM engine_messages"
        " WHERE session_id = ? ORDER BY seq",
        (sid,),
    ).fetchall()
    conn.close()
    spoken = [r for r in rows if r[0] == "assistant" and "erkannt" in (r[1] or "")]
    assert len(spoken) == 1, "the say line must be persisted exactly once"
    assert spoken[0][2] == 0, "and must be hidden from later prompts"
    assert all(r[2] == 0 for r in rows if r[0] == "tool")
    # Visible in the browser, absent from the next prompt.
    shown = [m["content"] for m in store.get_session(db, sid, "michael")["messages"]]
    assert any("erkannt" in c for c in shown)

    async for _ in client.respond_session("und weiter?", uid="michael"):
        pass
    sent = [str(m.get("content", "")) for m in fake.calls[-1]["messages"]]
    assert not any("erkannt" in c for c in sent)


def test_as_question_replaces_trailing_punctuation():
    """The Voice PE mic stays open on a trailing '?', but appending one after a
    full stop produced "Bitte antworte mit Ja oder Nein.?" on the box."""
    from solaris_chat.engine.facade import _as_question

    assert _as_question("Bitte antworte mit Ja oder Nein.") == (
        "Bitte antworte mit Ja oder Nein?"
    )
    assert _as_question("Sag mir noch einen Satz!") == "Sag mir noch einen Satz?"
    assert _as_question("Und weiter...") == "Und weiter?"
    assert _as_question("Wie heisst du") == "Wie heisst du?"
    assert _as_question("Alles klar?") == "Alles klar?"  # untouched
    assert _as_question("Fertig.  ") == "Fertig?"


def test_room_markers_are_stripped_from_every_replayed_turn():
    """HA replays the whole conversation on the guest path, and each turn
    carried its own `[room: X]` marker. Only the newest one was cleaned, so the
    model saw the internal marker on all the others."""
    from solaris_chat.engine.facade import _strip_room_from_messages

    messages = [
        {"role": "user", "content": "[room: Küche] Licht an"},
        {"role": "assistant", "content": "Klar."},
        {"role": "user", "content": "[room: Bad] und hier?"},
    ]
    _strip_room_from_messages(messages)

    assert [m["content"] for m in messages] == ["Licht an", "Klar.", "und hier?"]


def test_bare_decline_is_recognised_but_a_real_answer_is_not():
    """ "nein" as a whole utterance ends the conversation; "nein" inside a
    sentence, or a yes, is ordinary content the model must still see."""
    from solaris_chat.engine.facade import _is_bare_decline

    for text in ("nein", "Nein.", "nein danke", "nö", "stop"):
        assert _is_bare_decline(text) is True, text
    for text in (
        "",
        "ja",
        "nein, mach das Licht im Wohnzimmer aus",
        "spiel keine Musik mehr ab bitte",
        "wie spät ist es",
    ):
        assert _is_bare_decline(text) is False, text


async def test_declining_a_pleasantry_closes_the_mic(aiohttp_client, db, soul):
    """HA re-opens the mic purely because a reply ended in `?`, and nothing ever
    closed it. Box-observed: "kann ich noch etwas für dich tun?" — "nein" —
    silence, then the time, because a content-free turn left the model nothing
    but the wall-clock hint."""
    app, fake = _app(
        db, soul, [ChatResult(content="19:57", prompt_tokens=5, completion_tokens=1)]
    )
    http = await aiohttp_client(app)

    r = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "nein"}],
            "user": "michael",
        },
    )
    answer = (await r.json())["message"]["content"]

    assert answer == "Alles klar."
    assert not answer.rstrip().endswith("?"), "a trailing ? re-opens the mic"
    assert fake.calls == [], "the model must not run for a bare decline"


async def test_a_held_action_still_gets_its_no(aiohttp_client, db, soul):
    """The one "nein" that must NOT be swallowed: the confirm gate is waiting on
    it. Swallowing it would leave the sensitive action stashed forever."""
    from solaris_chat.engine import confirm, store

    household, fake = _engine(
        db,
        soul,
        [
            ChatResult(
                content="Alles klar, ich lasse es.",
                prompt_tokens=5,
                completion_tokens=1,
            )
        ],
    )
    # The gate is keyed by the durable household session the voice path runs in.
    household._pending.stash(
        store.household_session_id("michael"),
        confirm.PendingAction(
            domain="cover",
            service="open_cover",
            entity_id="cover.garage",
            data=None,
            prompt="Soll ich das Garagentor öffnen?",
        ),
    )
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        api_key="",
    )
    http = await aiohttp_client(app)

    r = await http.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "nein"}],
            "user": "michael",
        },
    )
    await r.json()
    assert fake.calls != [], "the gate's pending action must still see the answer"


async def test_a_bystander_turn_is_not_swallowed_by_another_dialogs_wizard(
    aiohttp_client, db, soul
):
    """#1184: the enrolment FSM had one row for the whole box, so while Anna sat
    at its consent step Bob's unrelated turn to another satellite was read as
    HER answer — a yes-word in it consented to her biometric enrolment, and he
    got no normal service. Keyed on HA's conversation id, his turn is his own.
    """
    from solaris_chat.engine import enrollment_fsm
    from solaris_chat.engine.tools.register import build_register_tools

    household, _ = _engine(
        db,
        soul,
        [
            ChatResult(
                content="",
                tool_calls=[
                    {"function": {"name": "start_voice_enrollment", "arguments": {}}}
                ],
            ),
            ChatResult(content="Es ist 14:35."),
        ],
        tools=build_register_tools(db),
    )
    app = build_app(
        engine=household,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        api_key="",
    )
    http = await aiohttp_client(app)

    anna = await (
        await http.post(
            "/ollama/api/chat",
            json={
                "model": "solaris",
                "stream": False,
                "conversation_id": "conv-anna",
                "messages": [{"role": "user", "content": "Richte mein Profil ein"}],
            },
        )
    ).json()
    assert "Ja oder Nein" in anna["message"]["content"]
    assert enrollment_fsm.get_fsm_state(db, "conv-anna")["state"] == "consent"

    bob = await (
        await http.post(
            "/ollama/api/chat",
            json={
                "model": "solaris",
                "stream": False,
                "conversation_id": "conv-bob",
                "messages": [{"role": "user", "content": "ok, wie spät ist es"}],
            },
        )
    ).json()
    assert "14:35" in bob["message"]["content"]
    assert "Zustimmung" not in bob["message"]["content"]
    # Anna's consent step is untouched — his "ok" was never her answer.
    assert enrollment_fsm.get_fsm_state(db, "conv-anna")["state"] == "consent"


# -- #1267: a spoken turn is never silence -----------------------------------


async def test_empty_turn_is_never_spoken_as_silence(aiohttp_client, db, soul):
    """An empty model turn reached the satellite as no audio at all — the
    resident cannot tell a done command from a lost one and repeats it."""
    app, _ = _app(db, soul, [ChatResult(content="   ")])
    client = await aiohttp_client(app)
    resp = await client.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "stream": False,
            "messages": [{"role": "user", "content": "Licht an"}],
        },
    )
    body = await resp.json()
    assert body["message"]["content"] == NO_ANSWER


async def test_empty_streamed_turn_is_never_silence(aiohttp_client, db, soul):
    app, _ = _app(db, soul, [ChatResult(content="")])
    client = await aiohttp_client(app)
    resp = await client.post(
        "/ollama/api/chat",
        json={
            "model": "solaris",
            "messages": [{"role": "user", "content": "Licht an"}],
        },
    )
    lines = [json.loads(line) for line in (await resp.text()).strip().splitlines()]
    content = "".join(line["message"]["content"] for line in lines)
    assert content == NO_ANSWER
