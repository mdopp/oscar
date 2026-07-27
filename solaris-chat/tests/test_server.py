"""Tests for uid mapping and the create-then-chat session flow."""

from __future__ import annotations

from importlib.metadata import version

import pytest

from solaris_chat import compaction, marker, personalities, skills, topics_store
from solaris_chat.engine.tools import Toolbox
from solaris_chat.engine.tools.mcp_tools import CombinedToolbox, McpToolbox
import solaris_chat.server as server_mod
from solaris_chat.server import (
    _IMAGE_PROMPT,
    _heartbeat,
    _images_from,
    _normalize,
    _reasoning_from_completed,
    _stream_phases,
    _title_from,
    _trace_from_phases,
    _version,
    build_app,
    is_admin,
    resolve_uid,
    strip_internal_hints,
)


def _assert_turns(turns, expected):
    """Assert forwarded `(session_id, text)` turns, tolerating the per-turn
    current-time line (#265) that now leads every user turn. Each expected
    text must be the tail of the forwarded turn for the same session."""
    assert len(turns) == len(expected)
    for (sid, sent), (exp_sid, exp_text) in zip(turns, expected):
        assert sid == exp_sid
        assert sent.endswith(exp_text)


class _FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def test_resolve_uid_from_header():
    req = _FakeRequest({"Remote-User": "mdopp"})
    assert resolve_uid(req, "Remote-User", "household") == "mdopp"


def test_resolve_uid_strips_whitespace():
    req = _FakeRequest({"Remote-User": "  mdopp  "})
    assert resolve_uid(req, "Remote-User", "household") == "mdopp"


def test_resolve_uid_falls_back_when_header_absent():
    req = _FakeRequest({})
    assert resolve_uid(req, "Remote-User", "household") == "household"


def test_resolve_uid_falls_back_on_empty_header():
    req = _FakeRequest({"Remote-User": "   "})
    assert resolve_uid(req, "Remote-User", "household") == "household"


class _FakeEngine:
    def __init__(self, events=None, store=None):
        self.created = []
        self.created_prompts = []
        self.maintenance = []
        self.ephemeral = []
        self.models = []
        self.turns = []
        self.titles = []
        self.create_titles = []
        self.deleted = []
        self.images = []
        self.efforts = []
        self.turn_uids = []
        self._events = events or []
        # store: list of {id, user_id, title, last_activity, messages}
        self._store = store or []

    async def create_session(
        self,
        uid,
        system_prompt=None,
        *,
        maintenance=False,
        ephemeral=False,
        model="",
        title="",
    ):
        self.created.append(uid)
        self.created_prompts.append(system_prompt or "")
        self.maintenance.append(maintenance)
        self.ephemeral.append(ephemeral)
        self.models.append(model)
        self.create_titles.append(title)
        # First create is "sess-1" (existing tests assert that); later creates
        # (e.g. a compaction continuation) get distinct ids.
        return "sess-1" if len(self.created) == 1 else f"sess-{len(self.created)}"

    async def set_title(self, session_id, uid, title):
        self.titles.append((session_id, uid, title))

    async def delete_session(self, session_id, uid):
        # Mirror the real store: owner-scoped (#438). A session the caller
        # doesn't own deletes nothing and returns False (same as missing).
        for s in self._store:
            if s["id"] != session_id:
                continue
            if not marker.has_marker(uid, s.get("title", "")):
                return False
            self.deleted.append(session_id)
            return True
        return False

    async def list_toolsets(self):
        return [
            {
                "name": "web",
                "label": "Web",
                "description": "search",
                "enabled": True,
                "configured": True,
                "tools": ["web_search"],
            }
        ]

    async def chat(
        self, session_id, text, images=None, reasoning_effort="none", turn_uid=""
    ):
        self.turns.append((session_id, text))
        self.images.append(images or [])
        self.efforts.append(reasoning_effort)
        self.turn_uids.append(turn_uid)
        return f"echo: {text}"

    async def chat_stream(
        self,
        session_id,
        text,
        images=None,
        reasoning_effort="none",
        suggest_answers=False,
        turn_uid="",
    ):
        self.turns.append((session_id, text))
        self.images.append(images or [])
        self.efforts.append(reasoning_effort)
        self.turn_uids.append(turn_uid)
        for event in self._events:
            yield event

    async def list_sessions(self, uid):
        # Mirror the real client: filter to the caller's uid marker and strip
        # it from the displayed title (#153). Store titles already carry the
        # marker (see _two_user_store).
        out = []
        for s in self._store:
            title = s.get("title", "")
            if not marker.has_marker(uid, title):
                continue
            out.append(
                {
                    "id": s["id"],
                    "title": marker.strip(title),
                    "last_activity": s.get("last_activity", ""),
                }
            )
        return out

    async def get_session(self, session_id, uid):
        # Mirror the real client: owner-scoped by the uid marker (#153). A
        # session the caller doesn't own is None (same as a missing id).
        for s in self._store:
            if s["id"] != session_id:
                continue
            title = s.get("title", "")
            if not marker.has_marker(uid, title):
                return None
            return {
                "id": s["id"],
                "title": marker.strip(title),
                "last_activity": s.get("last_activity", ""),
                "messages": s.get("messages", []),
                # engine per-session token totals (#210 compaction trigger);
                # default 0 so a store item without them never trips the cap.
                "input_tokens": s.get("input_tokens", 0),
                "output_tokens": s.get("output_tokens", 0),
            }
        return None


def test_title_from_first_message():
    assert _title_from("buy milk") == "buy milk"
    assert _title_from("  hello   there  ") == "hello there"
    long = "a" * 80
    out = _title_from(long)
    assert out.endswith("…") and len(out) <= 58
    assert _title_from("") == ""


# --- Version badge (#223) --------------------------------------------------


def test_version_prefers_env(monkeypatch):
    # The injected release version (SOLARIS_VERSION, set at image build from the
    # git tag/ref) wins over the never-bumped package version.
    monkeypatch.setenv("SOLARIS_VERSION", "0.3.0")
    assert _version() == "0.3.0"


def test_version_env_blank_falls_back_to_package(monkeypatch):
    # A blank env (local/dev build) falls through to the package metadata, so
    # the badge still shows something rather than going empty.
    monkeypatch.setenv("SOLARIS_VERSION", "   ")
    assert _version() == version("solaris-chat")


def test_version_no_env_uses_package(monkeypatch):
    monkeypatch.delenv("SOLARIS_VERSION", raising=False)
    assert _version() == version("solaris-chat")


# --- Latency trace (#225) --------------------------------------------------


def test_trace_from_phases_computes_pct_and_drops_zero():
    trace = _trace_from_phases(
        [("Prefill (TTFT)", 200.0), ("Answer", 800.0), ("noop", 0.0)],
        1000.0,
    )
    assert trace["total_seconds"] == 1.0
    assert trace["phases"] == [
        {"label": "Prefill (TTFT)", "seconds": 0.2, "pct": 20.0},
        {"label": "Answer", "seconds": 0.8, "pct": 80.0},
    ]


def test_trace_from_phases_empty_total_is_safe():
    # Total 0 must not divide-by-zero; an empty phase list yields no rows.
    assert _trace_from_phases([], 0.0) == {"total_seconds": 0.0, "phases": []}


def test_stream_phases_with_reasoning_split():
    # start=0, first token=100, </thinking>=600, end=1000, 0 tool ms.
    phases = _stream_phases(0.0, 100.0, 600.0, 1000.0, 0.0)
    assert phases == [
        ("Prefill (TTFT)", 100.0),
        ("Reasoning", 500.0),
        ("Answer", 400.0),
    ]


def test_stream_phases_no_reasoning_just_prefill_and_answer():
    phases = _stream_phases(0.0, 100.0, None, 1000.0, 0.0)
    assert phases == [("Prefill (TTFT)", 100.0), ("Answer", 900.0)]


def test_stream_phases_includes_tool_round_trip():
    phases = _stream_phases(0.0, 100.0, None, 1000.0, 250.0)
    assert ("Tool round-trip", 250.0) in phases


def test_stream_phases_no_tokens_yields_only_tool_or_empty():
    # A turn that streamed no assistant tokens (tool-only) keeps just the tool
    # span; a totally empty turn yields nothing.
    assert _stream_phases(0.0, None, None, 1000.0, 300.0) == [
        ("Tool round-trip", 300.0)
    ]
    assert _stream_phases(0.0, None, None, 1000.0, 0.0) == []


def test_normalize_assistant_delta():
    assert _normalize({"type": "assistant.delta", "data": {"delta": "hi"}}) == (
        "delta",
        {"text": "hi"},
    )


def test_normalize_delta_text_and_string_payload():
    assert _normalize({"type": "assistant.delta", "data": {"text": "yo"}}) == (
        "delta",
        {"text": "yo"},
    )
    assert _normalize({"type": "assistant.delta", "data": "raw"}) == (
        "delta",
        {"text": "raw"},
    )


def test_normalize_tool_events():
    assert _normalize({"type": "tool.started", "data": {"tool": "search"}}) == (
        "tool",
        {"name": "search", "phase": "started"},
    )
    assert _normalize({"type": "tool.completed", "data": {"name": "search"}}) == (
        "tool",
        {"name": "search", "phase": "completed"},
    )


def test_normalize_tool_completed_carries_wall_s():
    # #347: a completed tool round-trip carries its own wall_s for the live bubble.
    assert _normalize(
        {"type": "tool.completed", "data": {"tool": "ha_call_service", "wall_s": 0.4}}
    ) == ("tool", {"name": "ha_call_service", "phase": "completed", "wall_s": 0.4})


def test_normalize_llm_step():
    # #347: each completed Ollama pass folds to a `step` event (model + wall_s).
    assert _normalize(
        {"type": "llm.step", "data": {"model": "gemma4:e2b", "wall_s": 2.1}}
    ) == ("step", {"label": "gemma4:e2b", "wall_s": 2.1})


def test_normalize_completed_and_unknown():
    # run.completed with no reasoning => empty reasoning string (#231).
    assert _normalize({"type": "run.completed", "data": {}}) == (
        "completed",
        {"reasoning": "", "answer": ""},
    )
    assert _normalize({"type": "ping", "data": {}}) == ("keepalive", {})


def test_normalize_ha_cards_passes_through():
    # The read-only HA cards event (#475) forwards its card list verbatim.
    cards = [{"entity_id": "sensor.x", "domain": "sensor", "state": "21"}]
    assert _normalize({"type": "ha_cards", "data": {"cards": cards}}) == (
        "ha_cards",
        {"cards": cards},
    )
    # Missing cards key degrades to an empty list, never None.
    assert _normalize({"type": "ha_cards", "data": {}}) == ("ha_cards", {"cards": []})


def test_normalize_anchors_passes_through():
    # The auto-anchors event (#501) forwards its anchor list verbatim.
    anchors = ["@anna", "#garten-projekt"]
    assert _normalize({"type": "anchors", "data": {"anchors": anchors}}) == (
        "anchors",
        {"anchors": anchors},
    )
    assert _normalize({"type": "anchors", "data": {}}) == ("anchors", {"anchors": []})


def test_normalize_completed_surfaces_reasoning():
    # gemma4 puts the thinking text on the final message's reasoning_content
    # field of run.completed — NOT a literal <thinking> tag in the answer (#231).
    event = {
        "type": "run.completed",
        "data": {
            "messages": [
                {
                    "role": "assistant",
                    "content": "Die Antwort ist 4.",
                    "reasoning": "ignored-fallback",
                    "reasoning_content": "Erst 2+2 rechnen…",
                }
            ]
        },
    }
    assert _normalize(event) == (
        "completed",
        {"reasoning": "Erst 2+2 rechnen…", "answer": "Die Antwort ist 4."},
    )


def test_reasoning_from_completed_shapes():
    # reasoning_content preferred over reasoning; first message with text wins;
    # missing/garbage => "".
    assert _reasoning_from_completed({}) == ""
    assert _reasoning_from_completed({"messages": "nope"}) == ""
    assert _reasoning_from_completed({"messages": [{"content": "hi"}]}) == ""
    assert (
        _reasoning_from_completed({"messages": [{"reasoning": "fallback only"}]})
        == "fallback only"
    )
    assert (
        _reasoning_from_completed(
            {"messages": ["bad", {"reasoning_content": "the thoughts"}]}
        )
        == "the thoughts"
    )


# --- Tool-turn keepalive + late completed-answer relay (#319) --------------


class _CaptureResp:
    """A StreamResponse stand-in that records the SSE frames written to it."""

    def __init__(self):
        self.writes: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.writes.append(data)


async def _slow_then_completed():
    """An upstream stream like a real tool turn: a tool event, then a long
    silent gap (two prefills + the tool round-trip), then run.completed whose
    answer arrives only in the final assistant message (#258 shape)."""
    import asyncio

    yield {"type": "tool.started", "data": {"tool": "ha_list_entities"}}
    await asyncio.sleep(0.25)  # the dead-air window the browser would drop on
    yield {
        "type": "run.completed",
        "data": {"messages": [{"role": "assistant", "content": "Bürolicht ist an."}]},
    }


def _events(writes: list[bytes]) -> list[str]:
    frames = b"".join(writes).decode().split("\n\n")
    out = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("event:"):
                out.append(line[len("event:") :].strip())
    return out


def test_heartbeat_keepalives_during_silent_gap(monkeypatch):
    # A long silent stretch between the tool call and the answer must NOT leave
    # the SSE connection idle — the heartbeat fills it with keepalive frames so
    # the browser's streaming fetch is not dropped (#319).
    import asyncio

    monkeypatch.setattr(server_mod, "_HEARTBEAT_S", 0.05)
    resp = _CaptureResp()

    async def run():
        events = []
        async for ev in _heartbeat(_slow_then_completed(), resp):
            events.append(ev["type"])
        return events

    events = asyncio.run(run())
    # Both upstream events made it through, in order, with no exception escaping.
    assert events == ["tool.started", "run.completed"]
    # The gap (0.25s) at a 0.05s interval emitted at least one keepalive frame.
    assert _events(resp.writes).count("keepalive") >= 1


async def _long_prefill_then_answer():
    """A single long generation (e.g. the 12b 'Gründlich' mode): NO upstream
    event at all until the first delta — the heartbeat must fire during the bare
    prefill, not only between events (#452)."""
    import asyncio

    await asyncio.sleep(0.25)  # the whole prefill is dead air on the wire
    yield {"type": "message.delta", "data": {"text": "Eine lange Antwort."}}
    yield {"type": "run.completed", "data": {}}


def test_heartbeat_fires_during_single_long_generation(monkeypatch):
    # #452: a legit long answer (no tool call, just a slow prefill) streams
    # nothing for seconds. The heartbeat wraps each upstream pull, so a keepalive
    # is emitted on every silent interval REGARDLESS of model output — keeping a
    # mobile/NPM connection from idling out before the first token.
    import asyncio

    monkeypatch.setattr(server_mod, "_HEARTBEAT_S", 0.05)
    resp = _CaptureResp()

    async def run():
        async for _ in _heartbeat(_long_prefill_then_answer(), resp):
            pass

    asyncio.run(run())
    # 0.25s of pre-first-delta silence at a 0.05s cadence => several keepalives,
    # not just one between events — proving the cadence is steady mid-generation.
    assert _events(resp.writes).count("keepalive") >= 2


def test_heartbeat_interval_under_proxy_read_timeout():
    # The keepalive cadence must stay comfortably below a reverse proxy's read
    # timeout (NPM default proxy_read_timeout = 60s) so the SSE connection never
    # idles out during a long generation (#452). A generous safety margin.
    assert server_mod._HEARTBEAT_S <= 20.0


def test_completed_answer_surfaces_after_tool_turn():
    # The #258 late-delta logic: a tool turn streams no answer deltas, so the
    # final summary on run.completed is surfaced as a delta. _normalize lifts it
    # out of the completed messages; the relay then emits it as `delta`.
    name, data = _normalize(
        {
            "type": "run.completed",
            "data": {
                "messages": [{"role": "assistant", "content": "Bürolicht ist an."}]
            },
        }
    )
    assert name == "completed"
    assert data["answer"] == "Bürolicht ist an."


# --- Image attachments (#183) ---------------------------------------------


def test_images_from_keeps_data_url_prefix_and_caps():
    body = {
        "images": [
            "data:image/png;base64,AAAA",  # full data URL kept (#202)
            "BBBB",
            "",  # dropped (empty)
            123,  # dropped (not a string)
            "C1",
            "C2",
            "C3",  # 6th valid -> dropped by the cap of 4
        ]
    }
    assert _images_from(body) == ["data:image/png;base64,AAAA", "BBBB", "C1", "C2"]


def test_images_from_non_list():
    assert _images_from({}) == []
    assert _images_from({"images": "nope"}) == []


async def test_chat_forwards_images(aiohttp_client, tmp_path):
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat",
        json={"input": "scan this", "images": ["data:image/jpeg;base64,ZZ"]},
    )
    assert resp.status == 200
    _assert_turns(fake.turns, [("sess-1", "scan this")])
    # Full data URL reaches the engine (prefix kept, #202).
    assert fake.images == [["data:image/jpeg;base64,ZZ"]]


async def test_chat_image_only_uses_default_prompt(aiohttp_client, tmp_path):
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat", json={"images": ["QQ"]})
    assert resp.status == 200
    _assert_turns(fake.turns, [("sess-1", _IMAGE_PROMPT)])
    assert fake.images == [["QQ"]]


async def test_chat_image_only_resolves_the_hook_from_the_registry(
    aiohttp_client, tmp_path, monkeypatch
):
    # #483: an image-only turn fires the `image-upload` event; the flow point
    # resolves which hook acts on it from the registry (skills.hooks_for_event)
    # instead of a hardcoded id, so rebinding in the /hooks editor changes the
    # handler. We assert the flow point queries the registry for that event.
    calls: list[str] = []
    monkeypatch.setattr(
        server_mod.skills,
        "hooks_for_event",
        lambda skills_dir, event: calls.append(event) or [],
    )
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat", json={"images": ["QQ"]})
    assert resp.status == 200
    assert calls == ["image-upload"]
    # A typed turn (text present) is not an image-upload event — no resolution.
    calls.clear()
    resp = await client.post("/api/chat", json={"input": "hallo"})
    assert resp.status == 200
    assert calls == []


async def test_chat_defaults_to_fast_reasoning(aiohttp_client, tmp_path):
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    resp = await client.post("/api/chat", json={"input": "welche Lichter sind an"})
    assert resp.status == 200
    assert fake.efforts == ["none"]


async def test_chat_selector_overrides_to_thorough(aiohttp_client, tmp_path):
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    # The per-conversation selector (#224) wins over the fast default.
    resp = await client.post(
        "/api/chat", json={"input": "welche Lichter sind an", "reasoning": "high"}
    )
    assert resp.status == 200
    assert fake.efforts == ["high"]


async def test_fast_turn_keeps_effort_none_without_model_override(
    aiohttp_client, tmp_path
):
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
    )
    client = await aiohttp_client(app)
    # #278/#222: a default (fast) household-control turn still maps to
    # reasoning_effort "none" per turn. #293: the household profile owns the
    # base model (e2b), so the proxy no longer overrides the model at create.
    resp = await client.post("/api/chat", json={"input": "welche Lichter sind an"})
    assert resp.status == 200
    assert fake.efforts == ["none"]
    assert fake.models == [""]


async def test_thinking_turn_keeps_effort_high_without_model_override(
    aiohttp_client, tmp_path
):
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
    )
    client = await aiohttp_client(app)
    # #278/#222: the "thinking" selector still maps to reasoning_effort "high"
    # per turn. #293: no per-session model override — the profile pins the model.
    resp = await client.post("/api/chat", json={"input": "hallo", "reasoning": "high"})
    assert resp.status == 200
    assert fake.efforts == ["high"]
    assert fake.models == [""]


async def test_chat_no_routing_tags_no_model_override(aiohttp_client, tmp_path):
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    # Routing off by default → no per-session model override (the engine's default).
    resp = await client.post("/api/chat", json={"input": "hi"})
    assert resp.status == 200
    assert fake.models == [""]


async def test_chat_admin_escalates_reasoning(aiohttp_client, tmp_path):
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    # Admin/diagnose context auto-escalates (#222) when no selector is sent.
    resp = await client.post(
        "/api/chat", json={"input": "status?"}, headers={"Remote-Groups": "admins"}
    )
    assert resp.status == 200
    assert fake.efforts == ["high"]


async def test_chat_no_text_no_images_rejected(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat", json={"input": "  ", "images": []})
    assert resp.status == 400
    assert fake.turns == []


async def test_stream_forwards_images(aiohttp_client, tmp_path):
    fake = _FakeEngine(events=[{"type": "assistant.delta", "data": {"delta": "ok"}}])
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat/stream",
        json={"input": "look", "images": ["data:image/png;base64,PP"]},
    )
    assert resp.status == 200
    await resp.text()
    _assert_turns(fake.turns, [("sess-1", "look")])
    assert fake.images == [["data:image/png;base64,PP"]]


async def test_first_turn_creates_session(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat", json={"input": "hello"}, headers={"Remote-User": "mdopp"}
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["ok"] is True
    assert body["session_id"] == "sess-1"
    assert body["reply"].endswith("hello")
    assert fake.created == ["mdopp"]
    _assert_turns(fake.turns, [("sess-1", "hello")])
    # The session is born with a unique, marker-embedded title derived from the
    # first turn (passed to create_session), not the bare `[uid:...]` marker, so
    # a first turn can never 400 against an abandoned bare-marker stub (#277).
    # No separate set_title PATCH rides the turn anymore.
    assert fake.create_titles == ["hello"]
    assert fake.titles == []


async def test_first_turn_create_title_is_non_bare_marker_unique(aiohttp_client):
    # Regression for #277: two first turns for the SAME uid must not collide on
    # the bare `[uid:...]` marker title (the engine enforces title uniqueness). The
    # session must be born with a marker-embedded title (uid-attributable for
    # owner-scoping / has_marker) whose human suffix differs per first turn.
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    for text in ("erster chat", "zweiter chat"):
        resp = await client.post(
            "/api/chat", json={"input": text}, headers={"Remote-User": "mdopp"}
        )
        assert resp.status == 200

    bare = marker.marker_for("mdopp")
    embedded = [marker.embed("mdopp", t) for t in fake.create_titles]
    # Neither create used the bare marker, both stay uid-attributable, and the
    # two embedded titles differ — so they can't 400 against each other.
    assert all(t for t in fake.create_titles)
    assert all(e != bare and marker.has_marker("mdopp", e) for e in embedded)
    assert embedded[0] != embedded[1]


async def test_subsequent_turn_reuses_session(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat", json={"input": "again", "session_id": "existing"}
    )
    body = await resp.json()
    assert body["session_id"] == "existing"
    assert fake.created == []
    _assert_turns(fake.turns, [("existing", "again")])
    # Reusing a session never re-titles it.
    assert fake.titles == []


async def test_two_consecutive_turns_share_one_session(aiohttp_client):
    # The operator's #268 scenario: a 2nd turn ("welche lichter sind an", asked
    # twice) in the same chat. The browser sends back the session_id it got from
    # turn 1, so turn 2 reuses that warm engine session — one create, both turns
    # on the same id. A cold turn-2 TTFT is model eviction, not a session bug.
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    first = await (
        await client.post(
            "/api/chat",
            json={"input": "welche lichter sind an"},
            headers={"Remote-User": "mdopp"},
        )
    ).json()
    sid = first["session_id"]

    second = await (
        await client.post(
            "/api/chat",
            json={"input": "welche lichter sind an", "session_id": sid},
            headers={"Remote-User": "mdopp"},
        )
    ).json()

    assert second["session_id"] == sid
    # Exactly one session was created across both turns.
    assert fake.created == ["mdopp"]
    _assert_turns(
        fake.turns,
        [(sid, "welche lichter sind an"), (sid, "welche lichter sind an")],
    )


async def test_empty_input_rejected(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat", json={"input": "   "})
    assert resp.status == 400
    assert fake.created == []


async def test_stream_creates_session_and_restreams(aiohttp_client):
    fake = _FakeEngine(
        events=[
            {"type": "assistant.delta", "data": {"delta": "He"}},
            {"type": "tool.started", "data": {"tool": "clock"}},
            {"type": "assistant.delta", "data": {"delta": "llo"}},
            {"type": "run.completed", "data": {}},
        ]
    )
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat/stream", json={"input": "hi"}, headers={"Remote-User": "mdopp"}
    )
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/event-stream"
    body = await resp.text()

    assert "event: session" in body
    assert '"session_id": "sess-1"' in body
    assert '"text": "He"' in body and '"text": "llo"' in body
    assert '"name": "clock"' in body and '"phase": "started"' in body
    assert "event: completed" in body
    assert body.rstrip().endswith("data: {}")  # final 'done' frame
    assert fake.created == ["mdopp"]
    _assert_turns(fake.turns, [("sess-1", "hi")])
    assert fake.create_titles == ["hi"]
    assert fake.titles == []


async def test_stream_thorough_turn_emits_reasoning_event(aiohttp_client):
    # Gründlich turn: the engine returns reasoning on run.completed (no <thinking>
    # tag in the answer deltas), so the proxy surfaces it as a distinct
    # `reasoning` event the panel renders collapsibly (#231).
    fake = _FakeEngine(
        events=[
            {"type": "assistant.delta", "data": {"delta": "4"}},
            {
                "type": "run.completed",
                "data": {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "4",
                            "reasoning_content": "2 plus 2 macht 4.",
                        }
                    ]
                },
            },
        ]
    )
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat/stream",
        json={"input": "was ist 2+2", "reasoning": "high"},
        headers={"Remote-User": "mdopp"},
    )
    body = await resp.text()
    assert fake.efforts == ["high"]
    assert "event: reasoning" in body
    assert '"text": "2 plus 2 macht 4."' in body
    # The forwarded `completed` frame stays bare (reasoning lives in its own event).
    assert "event: completed\ndata: {}" in body


async def test_stream_fast_turn_suppresses_reasoning(aiohttp_client):
    # gemma4 returns reasoning even on a fast turn, but #222 fast-default must
    # show no block — the proxy gates on the per-turn effort, not on the engine.
    fake = _FakeEngine(
        events=[
            {"type": "assistant.delta", "data": {"delta": "Hallo"}},
            {
                "type": "run.completed",
                "data": {
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "Hallo",
                            "reasoning_content": "Der Nutzer grüßt.",
                        }
                    ]
                },
            },
        ]
    )
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat/stream",
        json={"input": "hallo"},
        headers={"Remote-User": "mdopp"},
    )
    body = await resp.text()
    assert fake.efforts == ["none"]
    assert "event: reasoning" not in body
    assert "Der Nutzer grüßt." not in body


async def test_stream_tool_turn_surfaces_answer_as_late_delta(aiohttp_client):
    # A HA state query is a tool-invocation turn: the engine streams no answer
    # deltas, only tool events, and delivers the summary on run.completed. The
    # proxy must surface that text as a late delta so the bubble isn't empty
    # (#258) — otherwise the UI shows "(no reply)".
    fake = _FakeEngine(
        events=[
            {"type": "tool.started", "data": {"tool": "homeassistant"}},
            {"type": "tool.completed", "data": {"tool": "homeassistant"}},
            {
                "type": "run.completed",
                "data": {
                    "messages": [
                        {"role": "user", "content": "welche lichter sind an"},
                        {"role": "tool", "content": '{"on": ["kitchen"]}'},
                        {"role": "assistant", "content": "Die Kueche-Lampe ist an."},
                    ]
                },
            },
        ]
    )
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat/stream",
        json={"input": "welche lichter sind an"},
        headers={"Remote-User": "mdopp"},
    )
    body = await resp.text()
    assert "event: delta" in body
    assert '"text": "Die Kueche-Lampe ist an."' in body
    # The forwarded completed frame stays bare (answer rode the late delta).
    assert "event: completed\ndata: {}" in body


async def test_stream_no_duplicate_delta_when_answer_already_streamed(aiohttp_client):
    # When the answer was streamed normally, run.completed must NOT re-emit it
    # as a second delta (#258 guard: only fill when answer_buf is empty).
    fake = _FakeEngine(
        events=[
            {"type": "assistant.delta", "data": {"delta": "Hallo"}},
            {
                "type": "run.completed",
                "data": {"messages": [{"role": "assistant", "content": "Hallo"}]},
            },
        ]
    )
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat/stream",
        json={"input": "hi"},
        headers={"Remote-User": "mdopp"},
    )
    body = await resp.text()
    assert body.count('"text": "Hallo"') == 1


async def test_stream_empty_input_rejected(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat/stream", json={"input": "  "})
    assert resp.status == 400
    assert fake.created == []


# --- Attachment persistence (#202) ----------------------------------------


async def test_chat_persists_attachment_and_history_reattaches(
    aiohttp_client, tmp_path
):
    # A turn with an image persists it proxy-side; opening the session later
    # re-attaches the stored data URL to the user message (#202). The image is
    # stored under the session id the turn ran against.
    store = [
        {
            "id": "sess-1",
            "user_id": "mdopp",
            "title": marker.embed("mdopp", "Photo chat"),
            "last_activity": "2026-06-07T10:00:00Z",
            "messages": [
                {"role": "user", "content": "what is this?\n[screenshot]"},
                {"role": "assistant", "content": "a cat"},
            ],
        }
    ]
    fake = _FakeEngine(store=store)
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)

    url = "data:image/png;base64,IMG1"
    resp = await client.post(
        "/api/chat",
        json={"input": "what is this?", "session_id": "sess-1", "images": [url]},
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 200

    resp = await client.get("/api/sessions/sess-1", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    msgs = body["session"]["messages"]
    # The placeholder-bearing user message regained its image; assistant didn't.
    assert msgs[0]["images"] == [url]
    assert "images" not in msgs[1]


async def test_get_session_without_stored_attachment_unchanged(
    aiohttp_client, tmp_path
):
    fake = _FakeEngine(store=_two_user_store())
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/sessions/s-mdopp", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    assert all("images" not in m for m in body["session"]["messages"])


def test_strip_internal_hints_drops_each_known_prefix():
    # Each leading internal-hint prefix the proxy/gatekeeper injects is hidden
    # on display (#309), leaving only the resident's typed text.
    cases = [
        (
            "[Aktuelle Zeit: Montag, 09.06.2026, 23:40 Uhr CEST]\n\nmach das Licht an",
            "mach das Licht an",
        ),
        (
            "[Temporary/incognito chat: this conversation is ephemeral and will be "
            "deleted on close. Do NOT save notes.]\n\nhallo",
            "hallo",
        ),
        (
            "[Active topic: Wintergarten #topic/projekt/wintergarten]\n\nwie weit sind wir?",
            "wie weit sind wir?",
        ),
        ("[Extract this to a note #topic/garten (Garten)]\n\nNotiz", "Notiz"),
        ("[room: kueche]\nist der Herd aus?", "ist der Herd aus?"),
        ("[uid:1a2b3c4d] eine frage", "eine frage"),
    ]
    for raw, expected in cases:
        assert strip_internal_hints(raw) == expected


def test_strip_internal_hints_drops_stacked_prefixes():
    raw = (
        "[Aktuelle Zeit: Montag, 09.06.2026, 23:40 Uhr CEST]\n\n"
        "[Active topic: Garten #topic/garten]\n\n"
        "Was steht heute an?"
    )
    assert strip_internal_hints(raw) == "Was steht heute an?"


def test_strip_internal_hints_keeps_resident_brackets_and_midtext():
    # A bracket the resident actually typed (not a known hint, or not leading) survives.
    assert (
        strip_internal_hints("[meine Notiz] bitte merken")
        == "[meine Notiz] bitte merken"
    )
    assert (
        strip_internal_hints("erinnere mich an [Aktuelle Zeit: ...] morgen")
        == "erinnere mich an [Aktuelle Zeit: ...] morgen"
    )


async def test_get_session_strips_internal_hints_from_user_messages(
    aiohttp_client, tmp_path
):
    # Reloading a chat shows user bubbles WITHOUT the injected hint prefixes
    # (#309); the assistant reply is untouched.
    store = [
        {
            "id": "sess-1",
            "user_id": "mdopp",
            "title": marker.embed("mdopp", "Licht"),
            "last_activity": "2026-06-09T10:00:00Z",
            "messages": [
                {
                    "role": "user",
                    "content": "[Aktuelle Zeit: Montag, 09.06.2026, 23:40 Uhr CEST]\n\n"
                    "[room: kueche]\nmach das Licht an",
                },
                {
                    "role": "assistant",
                    "content": "[Aktuelle Zeit: ...] bleibt sichtbar",
                },
            ],
        }
    ]
    fake = _FakeEngine(store=store)
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/sessions/sess-1", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    msgs = body["session"]["messages"]
    assert msgs[0]["content"] == "mach das Licht an"
    # The assistant message is never stripped.
    assert msgs[1]["content"] == "[Aktuelle Zeit: ...] bleibt sichtbar"


# --- Hard-cap compaction trigger (#210) ------------------------------------


async def test_turn_over_cap_compacts_and_switches_session(aiohttp_client, tmp_path):
    # An existing session whose token usage is over the cap is compacted before
    # the next turn: learnings are extracted (an LLM turn on the OLD session),
    # the chat continues in a fresh continuation session, and the proxy reports
    # the new id + compacted=true.
    store = [
        {
            "id": "old",
            "title": marker.embed("mdopp", "Long chat"),
            "last_activity": "2026-06-07T10:00:00Z",
            "input_tokens": 31000,  # ~0.98 of a 32768 window
            "output_tokens": 1000,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ]
    fake = _FakeEngine(store=store)
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        context_window=32768,
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat",
        json={"input": "next turn", "session_id": "old"},
        headers={"Remote-User": "mdopp"},
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["compacted"] is True
    # The turn ran against the continuation, not the over-cap original.
    assert body["session_id"] != "old"
    # Extraction happened on the OLD session BEFORE the real turn on the new one.
    texts = [t for _, t in fake.turns]
    assert texts[0] == compaction.EXTRACT_PROMPT
    assert texts[-1].endswith("next turn")
    assert fake.turns[0][0] == "old" and fake.turns[-1][0] == body["session_id"]


async def test_turn_under_cap_does_not_compact(aiohttp_client, tmp_path):
    store = [
        {
            "id": "small",
            "title": marker.embed("mdopp", "Short chat"),
            "input_tokens": 500,
            "output_tokens": 0,
            "messages": [],
        }
    ]
    fake = _FakeEngine(store=store)
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        context_window=32768,
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={"input": "hello", "session_id": "small"},
        headers={"Remote-User": "mdopp"},
    )
    body = await resp.json()
    assert body["compacted"] is False
    assert body["session_id"] == "small"
    assert fake.created == []  # no continuation created
    assert [t for _, t in fake.turns][-1].endswith("hello")


async def test_delete_session_removes_attachments(aiohttp_client, tmp_path):
    fake = _FakeEngine(store=_two_user_store())
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    # Seed an attachment for the session, then delete it.
    (tmp_path / "s-mdopp.json").write_text('{"batches": [["X"]]}', encoding="utf-8")
    resp = await client.delete(
        "/api/sessions/s-mdopp", headers={"Remote-User": "mdopp"}
    )
    assert resp.status == 200
    assert not (tmp_path / "s-mdopp.json").exists()


# --- Incognito / temporary chat (#246) -------------------------------------


_TOPICS_SCHEMA = """
CREATE TABLE topics (
  slug TEXT PRIMARY KEY, display_name TEXT NOT NULL, parent TEXT,
  scope TEXT NOT NULL DEFAULT 'resident', owner_uid TEXT,
  default_model TEXT, default_persona TEXT, color TEXT,
  archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE session_topics (
  session_id TEXT NOT NULL, topic_slug TEXT NOT NULL, role TEXT NOT NULL,
  owner_uid TEXT NOT NULL, PRIMARY KEY (session_id, topic_slug)
);
"""


def _topics_db(tmp_path):
    import sqlite3

    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_TOPICS_SCHEMA)
    conn.execute(
        "INSERT INTO topics (slug, display_name, scope, owner_uid) "
        "VALUES ('finanzen', 'Finanzen', 'shared', NULL)"
    )
    conn.commit()
    conn.close()
    return path


async def test_ephemeral_session_created_with_temp_marker(aiohttp_client, tmp_path):
    # An incognito first turn creates the session with the ephemeral flag set,
    # is NOT re-titled (re-titling would re-stamp the durable [uid:] marker and
    # surface it in the list), and carries no session_topics row.
    db = _topics_db(tmp_path)
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
        solaris_db_path=db,
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={"input": "ist das vertraulich?", "ephemeral": True},
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 200
    assert fake.ephemeral == [True]
    assert fake.titles == []  # no set_title -> [temp:] marker preserved
    # No durable topic assignment was written for the ephemeral session.
    assert topics_store.get_session_topics(db, "sess-1", "mdopp") == {
        "primary": None,
        "secondary": [],
    }


async def test_two_ephemeral_first_turns_get_distinct_noncolliding_titles(
    aiohttp_client, tmp_path
):
    # Two incognito first turns for the same resident must seed distinct
    # `create_session` titles (#286): the bare `[temp:]` marker is shared across
    # temp chats, so without a unique suffix the second collides on the engine's
    # unique-title constraint (400 -> 502). Each ephemeral create now passes
    # `title=_title_from(text)` (rides after the [temp:] marker), so two temp
    # chats with different first messages can't collide.
    db = _topics_db(tmp_path)
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
        solaris_db_path=db,
    )
    client = await aiohttp_client(app)
    for text in ("ist das vertraulich?", "und das hier auch?"):
        resp = await client.post(
            "/api/chat",
            json={"input": text, "ephemeral": True},
            headers={"Remote-User": "mdopp"},
        )
        assert resp.status == 200
    # Both creates were ephemeral and carried a non-empty, distinct title suffix.
    assert fake.ephemeral == [True, True]
    assert all(t for t in fake.create_titles)
    assert fake.create_titles[0] != fake.create_titles[1]
    # Ephemeral semantics intact: never re-titled (would surface the [uid:]
    # marker) and no durable topic row written.
    assert fake.titles == []
    assert topics_store.get_session_topics(db, "sess-1", "mdopp") == {
        "primary": None,
        "secondary": [],
    }


async def test_ephemeral_turn_injects_no_persist_hint(aiohttp_client, tmp_path):
    # Every ephemeral turn carries the guard hint telling the agent to persist
    # nothing (suppresses auto-ingestion / memory writes at the source).
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
        solaris_db_path=str(tmp_path / "solaris.db"),
    )
    client = await aiohttp_client(app)
    await client.post(
        "/api/chat",
        json={"input": "hallo", "ephemeral": True},
        headers={"Remote-User": "mdopp"},
    )
    turn = fake.turns[-1][1]
    assert "Temporary/incognito chat" in turn
    assert "Do NOT save notes" in turn
    assert turn.endswith("hallo")


async def test_ephemeral_existing_session_never_compacts(aiohttp_client, tmp_path):
    # A turn on an over-cap ephemeral session must NOT compact: compaction would
    # extract learnings to memory + spawn a durable [uid:] continuation, exactly
    # the persistence incognito forbids. The turn runs on the original session.
    store = [
        {
            "id": "temp-old",
            "title": marker.temp_marker("mdopp") + "secret",
            "input_tokens": 31000,
            "output_tokens": 1000,
            "messages": [],
        }
    ]
    fake = _FakeEngine(store=store)
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        context_window=32768,
        attachments_dir=str(tmp_path),
        solaris_db_path=str(tmp_path / "solaris.db"),
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={"input": "weiter", "session_id": "temp-old", "ephemeral": True},
        headers={"Remote-User": "mdopp"},
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["compacted"] is False
    assert body["session_id"] == "temp-old"
    assert fake.created == []  # no continuation session
    assert compaction.EXTRACT_PROMPT not in [t for _, t in fake.turns]


async def test_ephemeral_extract_to_topic_tags_the_note(aiohttp_client, tmp_path):
    # The escape hatch: an explicit "extract to topic finanzen" turn carries the
    # topic, so the proxy appends the #topic/<slug> stamp the ingestion skill
    # reads — that one note is the only durable output.
    db = _topics_db(tmp_path)
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
        solaris_db_path=db,
    )
    client = await aiohttp_client(app)
    await client.post(
        "/api/chat",
        json={
            "input": "Erstelle hieraus eine Notiz",
            "session_id": "temp-1",
            "ephemeral": True,
            "topic": "finanzen",
        },
        headers={"Remote-User": "mdopp"},
    )
    turn = fake.turns[-1][1]
    assert "#topic/finanzen" in turn
    assert "Finanzen" in turn  # the resolved display name labels the topic
    # Still ephemeral: the extract did NOT write a session_topics row.
    assert topics_store.get_session_topics(db, "temp-1", "mdopp") == {
        "primary": None,
        "secondary": [],
    }


async def test_non_ephemeral_chat_unaffected(aiohttp_client, tmp_path):
    # A normal chat behaves exactly as before: created without the ephemeral
    # flag, born with a title from its first turn, and never carries the
    # incognito guard hint.
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
        solaris_db_path=str(tmp_path / "solaris.db"),
    )
    client = await aiohttp_client(app)
    await client.post(
        "/api/chat",
        json={"input": "normaler chat"},
        headers={"Remote-User": "mdopp"},
    )
    assert fake.ephemeral == [False]
    assert fake.create_titles == ["normaler chat"]  # titled at create
    assert "Temporary/incognito chat" not in fake.turns[-1][1]


async def test_turn_carries_current_time_line(aiohttp_client, tmp_path):
    # #265: every user turn is prepended a fresh local wall-clock line so the
    # agent reports a correct, advancing date-time (the engine only stamps a frozen,
    # date-granular "Conversation started" line at create and runs UTC).
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
        solaris_db_path=str(tmp_path / "solaris.db"),
    )
    client = await aiohttp_client(app)
    # Normal chat: time line leads, the typed text trails.
    await client.post(
        "/api/chat",
        json={"input": "wie spät ist es?"},
        headers={"Remote-User": "mdopp"},
    )
    normal_turn = fake.turns[-1][1]
    assert normal_turn.startswith("[Aktuelle Zeit:")
    assert "Uhr" in normal_turn
    assert normal_turn.endswith("wie spät ist es?")
    # Ephemeral chat: time line composes with the incognito guard, text last.
    await client.post(
        "/api/chat",
        json={"input": "und jetzt?", "ephemeral": True},
        headers={"Remote-User": "mdopp"},
    )
    eph_turn = fake.turns[-1][1]
    assert eph_turn.startswith("[Aktuelle Zeit:")
    assert "Temporary/incognito chat" in eph_turn
    assert eph_turn.endswith("und jetzt?")


async def test_overlay_simplification_keeps_per_turn_levers(aiohttp_client, tmp_path):
    # #293: after dropping the redundant per-session model override + persona
    # overlay (the household profile owns both), a single chat turn still keeps
    # every per-turn lever the profile does NOT pin: the #265 time line leads the
    # turn text, the #278 thinking selector maps to reasoning_effort high, and
    # the session is created with no model override and no persona overlay.
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path),
        solaris_db_path=str(tmp_path / "solaris.db"),
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={
            "input": "rechne mir das vor",
            "reasoning": "high",  # #278 thinking selector
            "personality": "technical",  # no longer injects an overlay (#293)
        },
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 200
    # Per-turn levers retained:
    turn_text = fake.turns[-1][1]
    assert turn_text.startswith("[Aktuelle Zeit:")  # #265
    assert turn_text.endswith("rechne mir das vor")
    assert fake.efforts == ["high"]  # #278 → reasoning_effort
    # Redundant injection dropped (#293): profile owns model + soul.
    assert fake.models == [""]
    assert fake.created_prompts == [""]


def _two_user_store():
    # Titles carry each resident's immutable uid marker (#153); an extra
    # legacy session has no marker at all.
    return [
        {
            "id": "s-mdopp",
            "user_id": "mdopp",
            "title": marker.embed("mdopp", "Groceries"),
            "last_activity": "2026-06-05T10:00:00Z",
            "messages": [
                {"role": "user", "content": "buy milk"},
                {"role": "assistant", "content": "added"},
            ],
        },
        {
            "id": "s-lena",
            "user_id": "lena",
            "title": marker.embed("lena", "Lena private"),
            "last_activity": "2026-06-05T11:00:00Z",
            "messages": [{"role": "user", "content": "secret"}],
        },
        {
            "id": "s-legacy",
            "user_id": "",
            "title": "Untagged old session",  # no marker (pre-#153)
            "last_activity": "2026-06-04T09:00:00Z",
            "messages": [{"role": "user", "content": "old"}],
        },
    ]


async def test_list_sessions_scoped_to_caller(aiohttp_client):
    # Per-resident isolation (#153): A sees only A's sessions, never B's, and
    # never the unmarked legacy session. Marker stripped from the title.
    fake = _FakeEngine(store=_two_user_store())
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/sessions", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    assert {s["id"] for s in body["sessions"]} == {"s-mdopp"}
    # Marker stripped — the UI sees the clean human title (#155).
    assert body["sessions"][0]["title"] == "Groceries"

    # Resident B sees only B's session, not A's and not the legacy one.
    resp = await client.get("/api/sessions", headers={"Remote-User": "lena"})
    body = await resp.json()
    assert {s["id"] for s in body["sessions"]} == {"s-lena"}


async def test_delete_session(aiohttp_client):
    fake = _FakeEngine(store=_two_user_store())
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.delete(
        "/api/sessions/s-mdopp", headers={"Remote-User": "mdopp"}
    )
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True}
    assert fake.deleted == ["s-mdopp"]


async def test_delete_session_rejects_cross_resident(aiohttp_client):
    # #438: a resident cannot delete another resident's session. A wrong-owner
    # id is indistinguishable from a missing one (404) and deletes nothing.
    fake = _FakeEngine(store=_two_user_store())
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.delete("/api/sessions/s-lena", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 404
    assert body == {"ok": False, "reason": "not_found"}
    assert fake.deleted == []


async def test_whoami_reports_version(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/api/whoami")).json()
    assert "version" in body  # may be '' offline, but the key is always present


async def test_create_session_returns_id(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/sessions", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    assert body["session_id"] == "sess-1"
    assert fake.created == ["mdopp"]


async def test_get_own_session_returns_history(aiohttp_client):
    fake = _FakeEngine(store=_two_user_store())
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/sessions/s-mdopp", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    assert body["session"]["id"] == "s-mdopp"
    assert body["session"]["messages"][0]["content"] == "buy milk"
    assert body["session"]["title"] == "Groceries"  # marker stripped for the UI


async def test_get_other_residents_session_is_404(aiohttp_client):
    # Per-resident isolation (#153): mdopp cannot open lena's session by id —
    # the missing marker makes it indistinguishable from a non-existent id.
    fake = _FakeEngine(store=_two_user_store())
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/sessions/s-lena", headers={"Remote-User": "mdopp"})
    assert resp.status == 404

    # The unmarked legacy session is hidden from everyone (privacy-safe).
    resp = await client.get("/api/sessions/s-legacy", headers={"Remote-User": "mdopp"})
    assert resp.status == 404

    # An unknown id still 404s.
    resp = await client.get("/api/sessions/nope", headers={"Remote-User": "mdopp"})
    assert resp.status == 404


# --- Admin gate -----------------------------------------------------------


def test_is_admin_membership():
    req = _FakeRequest({"Remote-Groups": "family,admins"})
    assert is_admin(req, "Remote-Groups", "admins") is True


def test_is_admin_absent_or_other_group():
    assert is_admin(_FakeRequest({}), "Remote-Groups", "admins") is False
    assert (
        is_admin(_FakeRequest({"Remote-Groups": "family"}), "Remote-Groups", "admins")
        is False
    )
    # Substring of another group must not match (set membership, not `in`).
    assert (
        is_admin(
            _FakeRequest({"Remote-Groups": "superadmins"}), "Remote-Groups", "admins"
        )
        is False
    )


async def test_whoami_reports_uid_and_admin(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.get(
        "/api/whoami", headers={"Remote-User": "mdopp", "Remote-Groups": "admins"}
    )
    body = await resp.json()
    assert body["ok"] is True and body["uid"] == "mdopp" and body["is_admin"] is True

    resp = await client.get(
        "/api/whoami", headers={"Remote-User": "cdopp", "Remote-Groups": "family"}
    )
    body = await resp.json()
    assert body["uid"] == "cdopp" and body["is_admin"] is False


# --- Personalities --------------------------------------------------------


def test_personalities_catalog_hides_prompts():
    cat = personalities.catalog()
    assert any(p["id"] == "solaris" for p in cat)
    for p in cat:
        assert set(p) == {"id", "label", "description"}  # no system_prompt leaked


def test_system_prompt_for():
    assert personalities.system_prompt_for("solaris") == ""  # default = no overlay
    assert personalities.system_prompt_for(None) == ""
    assert personalities.system_prompt_for("concise")  # non-empty overlay
    assert personalities.system_prompt_for("nope") == ""  # unknown = default


async def test_list_personalities_endpoint(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/personalities")
    body = await resp.json()
    assert resp.status == 200
    assert {p["id"] for p in body["personalities"]} >= {"solaris", "concise"}


async def test_chat_does_not_inject_persona_overlay(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    # #293: the household gateway's profile owns the soul/persona, so the proxy
    # no longer injects a per-session persona overlay that would fight it — the
    # session is created with an empty system_prompt even for a named persona.
    resp = await client.post(
        "/api/chat", json={"input": "hi", "personality": "concise"}
    )
    assert resp.status == 200
    assert fake.created_prompts == [""]


async def test_chat_default_personality_no_overlay(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat", json={"input": "hi"})
    assert resp.status == 200
    assert fake.created_prompts == [""]


async def test_create_session_ignores_persona_overlay(aiohttp_client):
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    # #293: /api/sessions also defers the soul to the profile — a named persona
    # in the body no longer injects an overlay at create.
    resp = await client.post("/api/sessions", json={"personality": "teacher"})
    assert resp.status == 200
    assert fake.created_prompts == [""]


# --- Skills (filesystem) --------------------------------------------------


def _write_skill(root, dir_name, name, description, body):
    d = root / dir_name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_skills_list_and_read_from_disk(tmp_path):
    _write_skill(
        tmp_path, "status", "solaris-status", "Health at a glance", "# Status\nok"
    )
    _write_skill(tmp_path, "notes", "solaris-notes", "Search notes", "# Notes\nbody")
    (tmp_path / "README.md").write_text("not a skill", encoding="utf-8")  # ignored

    listed = skills.list_skills(tmp_path)
    assert [s["id"] for s in listed] == ["notes", "status"]  # sorted by name
    assert listed[1] == {
        "id": "status",
        "name": "solaris-status",
        "description": "Health at a glance",
    }

    one = skills.read_skill(tmp_path, "status")
    assert one["name"] == "solaris-status"
    assert one["body"].strip() == "# Status\nok"


def test_skills_missing_dir_and_traversal(tmp_path):
    assert skills.list_skills(tmp_path / "nope") == []
    assert skills.read_skill(tmp_path, "../etc") is None
    assert skills.read_skill(tmp_path, "nope") is None


def _write_def(root, dir_name, *, name, kind=None, scope=None, extra="", body="x"):
    d = root / dir_name
    d.mkdir()
    fm = [f"name: {name}", "description: d", "version: 1"]
    if kind is not None:
        fm.append(f"kind: {kind}")
    if scope is not None:
        fm.append(f"scope: {scope}")
    if extra:
        fm.append(extra)
    head = "\n".join(fm)
    (d / "SKILL.md").write_text(f"---\n{head}\n---\n\n{body}\n", encoding="utf-8")


def test_list_defs_filters_by_kind_default_is_skill(tmp_path):
    _write_def(tmp_path, "status", name="solaris-status")  # no kind => skill
    _write_def(tmp_path, "debug-set", name="debug-set", kind="command")
    _write_def(tmp_path, "chronicle", name="chronicle", kind="scheduler")
    _write_def(tmp_path, "media", name="media", kind="hook")

    assert [d["id"] for d in skills.list_defs(tmp_path, "skill")] == ["status"]
    assert [d["id"] for d in skills.list_defs(tmp_path, "command")] == ["debug-set"]
    assert [d["id"] for d in skills.list_defs(tmp_path, "scheduler")] == ["chronicle"]
    assert [d["id"] for d in skills.list_defs(tmp_path, "hook")] == ["media"]
    # list_skills stays the skill-kind view (back-compat).
    assert [d["id"] for d in skills.list_skills(tmp_path)] == ["status"]
    # kind + scope surface on the registry rows (scope defaults to household).
    skill = skills.list_defs(tmp_path, "skill")[0]
    assert skill["kind"] == "skill" and skill["scope"] == "household"


def test_list_defs_surfaces_the_scheduler_schedule(tmp_path):
    # #485: the /scheduler card's cron-time picker needs each entry's `schedule:`
    # cron on the registry row to render + prefill the picker.
    _write_def(
        tmp_path,
        "chronicle",
        name="chronicle",
        kind="scheduler",
        extra="schedule: 59 23 * * *",
    )
    _write_def(tmp_path, "status", name="solaris-status")  # skill: no schedule
    sched = skills.list_defs(tmp_path, "scheduler")[0]
    assert sched["schedule"] == "59 23 * * *"
    assert skills.list_defs(tmp_path, "skill")[0]["schedule"] == ""


def test_list_defs_surfaces_the_hook_event(tmp_path):
    # #483: the /hooks card's event selector needs each entry's bound `event:`
    # on the registry row to render the binding + prefill the selector.
    _write_def(
        tmp_path,
        "media",
        name="media",
        kind="hook",
        extra="event: image-upload",
    )
    _write_def(tmp_path, "status", name="solaris-status")  # skill: no event
    hook = skills.list_defs(tmp_path, "hook")[0]
    assert hook["event"] == "image-upload"
    assert skills.list_defs(tmp_path, "skill")[0]["event"] == ""


def test_list_tool_defs_surfaces_the_declarative_plugin_surface(tmp_path):
    # #1004 (ADR 0011): a kind:tool def carries its dot-command, api path, the
    # action ids the server auto-registers, and a cell-schema — parsed off the
    # flat frontmatter (actions comma-split, cell-schema one-line JSON).
    _write_def(
        tmp_path,
        "task-tool",
        name="solaris-task-tool",
        kind="tool",
        extra=(
            "tool-id: task\n"
            "tool-label: Aufgabe\n"
            "command: .task\n"
            "tool-api-path: /api/portal/tasks?done=1\n"
            "tool-actions: task.set_status, task.add, task.update\n"
            'tool-cell-schema: {"title": "title", "meta": ["due"]}'
        ),
    )
    _write_def(tmp_path, "status", name="solaris-status")  # skill: not a tool
    tools = skills.list_tool_defs(tmp_path)
    assert [t["id"] for t in tools] == ["task-tool"]
    tool = tools[0]
    assert tool["kind"] == "tool"
    assert tool["tool-id"] == "task"
    assert tool["command"] == ".task"
    assert tool["tool-api-path"] == "/api/portal/tasks?done=1"
    assert tool["tool-actions"] == ["task.set_status", "task.add", "task.update"]
    assert tool["tool-cell-schema"] == {"title": "title", "meta": ["due"]}


def test_cell_schema_lint_accepts_role_field_mappings():
    # #1022: a renderer-agnostic schema maps semantic roles to bare field names.
    assert skills.cell_schema_violations({"title": "title", "meta": ["due"]}) == []
    assert (
        skills.cell_schema_violations(
            {
                "title": "name",
                "subtitle": "role",
                "meta": ["phone", "email"],
                "badge": "state",
                "actions": ["contact.add"],
            }
        )
        == []
    )


def test_cell_schema_lint_rejects_browser_only_leakage():
    # #1022: HTML/CSS/handlers, unknown roles, and shape mismatches all fail —
    # they would break a non-browser (RemoteViews) consumer.
    assert skills.cell_schema_violations({"title": "<b>{{name}}</b>"})
    assert skills.cell_schema_violations({"title": ".task-title"})  # CSS class
    assert skills.cell_schema_violations({"onClick": "doThing()"})  # unknown role
    assert skills.cell_schema_violations({"title": ["a", "b"]})  # role wants one field
    assert skills.cell_schema_violations({"meta": "phone"})  # role wants a list
    assert skills.cell_schema_violations([])  # not an object


def test_shipped_pack_groups_into_the_four_kinds():
    # The #484 reorg sorts the household pack by frontmatter kind; admin-soul
    # (admin-act/diagnose/logs) is a sibling pack, so it isn't walked here.
    from pathlib import Path

    pack = (
        Path(__file__).resolve().parents[2]
        / "templates"
        / "solaris"
        / "skills"
        / "household"
    )
    by_kind = {
        kind: {d["id"] for d in skills.list_defs(pack, kind)} for kind in skills.KINDS
    }
    assert by_kind["scheduler"] == {
        "chat-compactor",
        "daily-chronicle",
        "problem-summarizer",
    }
    assert by_kind["hook"] == {
        "media-ingestion-multimodal",
        "topic-suggester",
        "room-enrollment",
        "enrollment",
    }
    assert by_kind["skill"] == {
        "status",
        "notes-search",
        "audit-query",
        "dynamic-skills",
        "media",
    }
    assert by_kind["command"] == {"debug-set"}
    # #1006: every existing .tool now ships as a declarative kind:tool plugin
    # (task is the reference; the rest joined in slice 4).
    assert by_kind["tool"] == {
        "task-tool",
        "note-tool",
        "doc-tool",
        "contacts-tool",
        "photo-tool",
        "home-tool",
        "energy-tool",
    }
    # list_skills stays the skill-kind view — no scheduler/hook/command leaks in.
    assert {d["id"] for d in skills.list_skills(pack)} == by_kind["skill"]


def test_read_def_404s_on_wrong_kind(tmp_path):
    _write_def(tmp_path, "status", name="solaris-status")  # skill
    assert skills.read_def(tmp_path, "skill", "status") is not None
    assert skills.read_def(tmp_path, "scheduler", "status") is None


def test_write_def_creates_and_rejects_kind_mismatch(tmp_path):
    # Create a new command def via the CRUD writer.
    res = skills.write_def(
        tmp_path,
        "command",
        "greet",
        "---\nname: greet\nkind: command\n---\n\nSag Hallo.\n",
    )
    assert res == {"id": "greet", "created": True, "frontmatter_changed": True}
    assert skills.read_def(tmp_path, "command", "greet")["body"].strip() == "Sag Hallo."

    # A PUT to /skill with command content is rejected (kind mismatch).
    assert (
        skills.write_def(tmp_path, "skill", "greet2", "---\nkind: command\n---\n\nx\n")
        is None
    )
    # Updating an existing def to a different kind is rejected.
    assert (
        skills.write_def(tmp_path, "skill", "greet", "---\nkind: skill\n---\n\ny\n")
        is None
    )


def test_delete_def_only_matching_kind(tmp_path):
    _write_def(tmp_path, "greet", name="greet", kind="command")
    assert skills.delete_def(tmp_path, "scheduler", "greet") is False  # wrong kind
    assert (tmp_path / "greet").exists()
    assert skills.delete_def(tmp_path, "command", "greet") is True
    assert not (tmp_path / "greet").exists()


def test_hooks_for_event(tmp_path):
    _write_def(
        tmp_path, "media", name="media", kind="hook", extra="event: image-upload"
    )
    _write_def(tmp_path, "guest", name="guest", kind="hook", extra="event: guest-start")
    _write_def(
        tmp_path, "media2", name="media2", kind="hook", extra="event: image-upload"
    )
    _write_def(tmp_path, "status", name="status")  # skill, ignored
    assert skills.hooks_for_event(tmp_path, "image-upload") == ["media", "media2"]
    assert skills.hooks_for_event(tmp_path, "guest-start") == ["guest"]
    assert skills.hooks_for_event(tmp_path, "nope") == []


async def test_defs_endpoints(aiohttp_client, tmp_path):
    _write_def(tmp_path, "status", name="solaris-status")  # skill
    _write_def(tmp_path, "debug-set", name="debug-set", kind="command")
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        skills_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/defs/skill")
    body = await resp.json()
    assert resp.status == 200
    assert [d["id"] for d in body["defs"]] == ["status"]

    resp = await client.get("/api/defs/command")
    assert [d["id"] for d in (await resp.json())["defs"]] == ["debug-set"]

    resp = await client.get("/api/defs/bogus")
    assert resp.status == 404

    resp = await client.get("/api/defs/skill/status")
    assert (await resp.json())["def"]["kind"] == "skill"

    # Wrong kind for an existing id 404s.
    resp = await client.get("/api/defs/scheduler/status")
    assert resp.status == 404

    # Mutate is admin-gated.
    resp = await client.put(
        "/api/defs/command/greet", json={"content": "---\nkind: command\n---\n\nhi\n"}
    )
    assert resp.status == 403

    admin = {"Remote-Groups": "admins"}
    resp = await client.put(
        "/api/defs/command/greet",
        json={"content": "---\nkind: command\n---\n\nhi\n"},
        headers=admin,
    )
    body = await resp.json()
    assert resp.status == 200 and body["created"] is True
    assert (await client.get("/api/defs/command/greet")).status == 200

    # PUT with kind contradicting the route is a 400.
    resp = await client.put(
        "/api/defs/skill/greet2",
        json={"content": "---\nkind: command\n---\n\nx\n"},
        headers=admin,
    )
    assert resp.status == 400

    resp = await client.delete("/api/defs/command/greet", headers=admin)
    assert resp.status == 200
    assert (await client.get("/api/defs/command/greet")).status == 404


async def test_defs_tool_endpoint_serves_the_declarative_surface(
    aiohttp_client, tmp_path
):
    # #1004 (ADR 0011): /api/defs/tool mirrors /api/defs/command, but each row
    # carries the plugin surface (dot-command, api path, actions, cell-schema).
    _write_def(
        tmp_path,
        "task-tool",
        name="solaris-task-tool",
        kind="tool",
        extra=(
            "tool-id: task\n"
            "command: .task\n"
            "tool-api-path: /api/portal/tasks?done=1\n"
            "tool-actions: task.set_status, task.add\n"
            'tool-cell-schema: {"title": "title", "meta": ["due"]}'
        ),
    )
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        skills_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/defs/tool")
    body = await resp.json()
    assert resp.status == 200 and body["kind"] == "tool"
    assert [d["id"] for d in body["defs"]] == ["task-tool"]
    tool = body["defs"][0]
    assert tool["command"] == ".task"
    assert tool["tool-actions"] == ["task.set_status", "task.add"]
    assert tool["tool-cell-schema"] == {"title": "title", "meta": ["due"]}


async def test_build_app_auto_registers_a_tool_defs_actions(tmp_path):
    # #1004: build_app auto-registers a tool def's declared actions from the
    # handler pool — the same registry the callback endpoint dispatches over.
    from solaris_chat.engine import action_cards

    _write_def(
        tmp_path,
        "task-tool",
        name="solaris-task-tool",
        kind="tool",
        extra="command: .task\ntool-actions: task.set_status, task.add, task.update",
    )
    build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        skills_dir=str(tmp_path),
    )
    for action_id in ("task.set_status", "task.add", "task.update"):
        assert action_cards.get(action_id) is not None


async def test_skills_endpoints(aiohttp_client, tmp_path):
    _write_skill(
        tmp_path, "status", "solaris-status", "Health", "# Status\nrendered me"
    )
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        skills_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/skills")
    body = await resp.json()
    assert resp.status == 200
    assert body["skills"][0]["name"] == "solaris-status"

    resp = await client.get("/api/skills/status")
    body = await resp.json()
    assert "rendered me" in body["skill"]["body"]

    resp = await client.get("/api/skills/missing")
    assert resp.status == 404


# --- Soul -----------------------------------------------------------------


async def test_soul_endpoint_reads_file(aiohttp_client, tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("# Solaris\nI am the soul.", encoding="utf-8")
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        soul_path=str(soul),
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/soul")
    body = await resp.json()
    assert resp.status == 200
    assert body["soul"]["content"] == "# Solaris\nI am the soul."


async def test_soul_endpoint_missing_file_is_502(aiohttp_client, tmp_path):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        soul_path=str(tmp_path / "missing" / "SOUL.md"),
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/soul")
    assert resp.status == 502


async def test_toolsets_endpoint(aiohttp_client):
    app = build_app(
        engine=_FakeEngine(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/api/toolsets")).json()
    assert body["ok"] is True
    assert body["toolsets"][0]["name"] == "web"


async def test_whoami_reports_context_window(aiohttp_client):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        context_window=4096,
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/api/whoami")).json()
    assert body["context_window"] == 4096


async def test_whoami_reports_shared_household_session_id(aiohttp_client):
    # The pinned "Zuhause" row opens the ONE SHARED household session every
    # resident opens (#649): /api/whoami exposes the default_uid-owned id (the
    # same row voice persists to), NOT a per-resident one, so spoken and typed
    # history are the same conversation for everyone logged in.
    from solaris_chat.engine import store

    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
    )
    client = await aiohttp_client(app)
    body = await (
        await client.get("/api/whoami", headers={"Remote-User": "cdopp"})
    ).json()
    assert body["household_session_id"] == store.household_session_id("household")


# --- Soul edit (admin, direct file write on the chat-owned volume) --------


async def test_put_soul_admin_writes_file(aiohttp_client, tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("# Solaris\nold", encoding="utf-8")
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        soul_path=str(soul),
    )
    client = await aiohttp_client(app)

    resp = await client.put(
        "/api/soul",
        json={"content": "# Solaris\nnew"},
        headers={"Remote-Groups": "admins"},
    )
    assert resp.status == 200
    assert soul.read_text(encoding="utf-8") == "# Solaris\nnew"


async def test_put_soul_non_admin_forbidden_no_write(aiohttp_client, tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("# Solaris\nold", encoding="utf-8")
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        soul_path=str(soul),
    )
    client = await aiohttp_client(app)

    resp = await client.put(
        "/api/soul", json={"content": "x"}, headers={"Remote-Groups": "family"}
    )
    assert resp.status == 403
    assert soul.read_text(encoding="utf-8") == "# Solaris\nold"  # untouched


async def test_put_soul_empty_rejected(aiohttp_client):
    app = build_app(
        engine=_FakeEngine(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.put(
        "/api/soul", json={"content": "  "}, headers={"Remote-Groups": "admins"}
    )
    assert resp.status == 400


async def test_put_soul_unwritable_path_is_502(aiohttp_client, tmp_path):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        soul_path=str(tmp_path / "missing" / "SOUL.md"),
    )
    client = await aiohttp_client(app)
    resp = await client.put(
        "/api/soul", json={"content": "x"}, headers={"Remote-Groups": "admins"}
    )
    assert resp.status == 502


# --- Everyday-chat model toggle (admin; fast/thorough routing pref) -------


def _model_app(tmp_path):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
        solaris_db_path=str(tmp_path / "solaris.db"),
        attachments_dir=str(tmp_path / "att"),
    )


async def test_get_model_admin(aiohttp_client, tmp_path):
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.get("/api/model", headers={"Remote-Groups": "admins"})
    body = await resp.json()
    assert resp.status == 200
    assert body["current"] == "fast"  # default (#809: fresh chats start fast)
    assert body["options"] == [
        {"value": "fast", "label": "Schnell", "model": "gemma4:e2b"},
        {"value": "thorough", "label": "Gründlich", "model": "gemma4:12b"},
    ]


async def test_get_model_non_admin_forbidden(aiohttp_client, tmp_path):
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.get("/api/model", headers={"Remote-Groups": "family"})
    assert resp.status == 403


async def test_put_model_admin_persists(aiohttp_client, tmp_path):
    from solaris_chat import settings_store

    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.put(
        "/api/model", json={"value": "fast"}, headers={"Remote-Groups": "admins"}
    )
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True, "current": "fast"}
    # Persisted to the JSON sidecar so it survives a restart.
    assert settings_store.get_other_model_pref(str(tmp_path / "solaris.db")) == "fast"


async def test_put_model_non_admin_forbidden(aiohttp_client, tmp_path):
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.put(
        "/api/model", json={"value": "fast"}, headers={"Remote-Groups": "family"}
    )
    assert resp.status == 403


async def test_put_model_rejects_unknown_value(aiohttp_client, tmp_path):
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.put(
        "/api/model", json={"value": "gemma4:12b"}, headers={"Remote-Groups": "admins"}
    )
    assert resp.status == 400


# --- Household-profile model picker (#366) --------------------------------


async def test_get_model_surfaces_household(aiohttp_client, tmp_path):
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.get("/api/model", headers={"Remote-Groups": "admins"})
    body = await resp.json()
    assert resp.status == 200
    # Default (unset) household model is the configured fast model.
    assert body["household_current"] == "gemma4:e2b"
    assert body["household_default"] == "gemma4:e2b"
    assert body["household_options"] == [
        {"value": "gemma4:e2b", "model": "gemma4:e2b"},
        {"value": "gemma4:12b", "model": "gemma4:12b"},
    ]


async def test_put_household_model_admin_persists(aiohttp_client, tmp_path):
    from solaris_chat import settings_store

    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.put(
        "/api/model",
        json={"household_model": "gemma4:12b"},
        headers={"Remote-Groups": "admins"},
    )
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True, "household_current": "gemma4:12b"}
    assert (
        settings_store.get_household_model(str(tmp_path / "solaris.db")) == "gemma4:12b"
    )
    # The everyday-chat pref is untouched by a household-model write.
    assert settings_store.get_other_model_pref(str(tmp_path / "solaris.db")) == "fast"


async def test_put_household_model_non_admin_forbidden(aiohttp_client, tmp_path):
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.put(
        "/api/model",
        json={"household_model": "gemma4:12b"},
        headers={"Remote-Groups": "family"},
    )
    assert resp.status == 403


async def test_put_household_model_rejects_unoffered_tag(aiohttp_client, tmp_path):
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.put(
        "/api/model",
        json={"household_model": "llama3:70b"},
        headers={"Remote-Groups": "admins"},
    )
    assert resp.status == 400


# --- Global TTS voice picker (#368; admin) --------------------------------


def _voice_app(tmp_path, tts_voices="martin,anna"):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        tts_voices=tts_voices,
        solaris_db_path=str(tmp_path / "solaris.db"),
        attachments_dir=str(tmp_path / "att"),
    )


async def test_get_voice_admin_default_martin(aiohttp_client, tmp_path):
    client = await aiohttp_client(_voice_app(tmp_path))
    resp = await client.get("/api/voice", headers={"Remote-Groups": "admins"})
    body = await resp.json()
    assert resp.status == 200
    # Unset selection => the first offered voice (the baked-in Martin default).
    assert body["current"] == "martin"
    assert body["default"] == "martin"
    assert body["options"] == ["martin", "anna"]


async def test_get_voice_non_admin_forbidden(aiohttp_client, tmp_path):
    client = await aiohttp_client(_voice_app(tmp_path))
    resp = await client.get("/api/voice", headers={"Remote-Groups": "family"})
    assert resp.status == 403


async def test_put_voice_admin_persists(aiohttp_client, tmp_path):
    from solaris_chat import settings_store

    client = await aiohttp_client(_voice_app(tmp_path))
    resp = await client.put(
        "/api/voice", json={"value": "anna"}, headers={"Remote-Groups": "admins"}
    )
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True, "current": "anna"}
    assert settings_store.get_tts_voice(str(tmp_path / "solaris.db")) == "anna"
    # The round-trip now reports the persisted selection, not the default.
    resp = await client.get("/api/voice", headers={"Remote-Groups": "admins"})
    assert (await resp.json())["current"] == "anna"


async def test_put_voice_non_admin_forbidden(aiohttp_client, tmp_path):
    client = await aiohttp_client(_voice_app(tmp_path))
    resp = await client.put(
        "/api/voice", json={"value": "anna"}, headers={"Remote-Groups": "family"}
    )
    assert resp.status == 403


async def test_put_voice_rejects_unoffered(aiohttp_client, tmp_path):
    client = await aiohttp_client(_voice_app(tmp_path))
    resp = await client.put(
        "/api/voice", json={"value": "ryan"}, headers={"Remote-Groups": "admins"}
    )
    assert resp.status == 400


# --- Model pull + VRAM headroom estimate (#367; admin) --------------------


class _FakeOllama:
    """Stand-in for OllamaChat with canned tags/ps and a pull stream."""

    tags_models: list = []
    ps_models: list = []
    pull_chunks: list = [{"status": "success"}]

    def __init__(self, *a, **k):
        pass

    async def tags(self):
        return _FakeOllama.tags_models

    async def ps(self):
        return _FakeOllama.ps_models

    async def pull(self, model):
        _FakeOllama.pulled = model
        for c in _FakeOllama.pull_chunks:
            yield c


_GIB = 1024 * 1024 * 1024


async def test_vram_admin_combines_and_flags_over_budget(
    aiohttp_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(server_mod, "OllamaChat", _FakeOllama)
    monkeypatch.setenv("GPU_TOTAL_VRAM", str(4 * _GIB))
    # Selected = fast (gemma4:e2b, on disk 6 GiB) + thorough (gemma4:12b, 6 GiB).
    _FakeOllama.tags_models = [
        {"name": "gemma4:e2b", "size": 6 * _GIB},
        {"name": "gemma4:12b", "size": 6 * _GIB},
    ]
    _FakeOllama.ps_models = []
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.get("/api/vram", headers={"Remote-Groups": "admins"})
    body = await resp.json()
    assert resp.status == 200
    assert body["estimate"] is True
    assert set(body["selected"]) == {"gemma4:e2b", "gemma4:12b"}
    assert body["available_bytes"] == 4 * _GIB
    assert body["combined_bytes"] > body["available_bytes"]
    assert body["over_budget"] is True


async def test_vram_unknown_available_does_not_flag(
    aiohttp_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(server_mod, "OllamaChat", _FakeOllama)
    monkeypatch.delenv("GPU_TOTAL_VRAM", raising=False)
    monkeypatch.setattr(server_mod.vram.shutil, "which", lambda _: None)
    _FakeOllama.tags_models = [{"name": "gemma4:e2b", "size": 6 * _GIB}]
    _FakeOllama.ps_models = []
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.get("/api/vram", headers={"Remote-Groups": "admins"})
    body = await resp.json()
    assert body["available_bytes"] is None
    assert body["over_budget"] is False  # can't judge fit => never flag


async def test_vram_uses_servicebay_gpu_total_and_used(
    aiohttp_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(server_mod, "OllamaChat", _FakeOllama)
    _FakeOllama.tags_models = [{"name": "gemma4:e2b", "size": 3 * _GIB}]
    _FakeOllama.ps_models = []

    async def fake_gpu(url, token_path):
        return (16 * _GIB, 10 * _GIB)

    monkeypatch.setattr(server_mod.vram, "servicebay_gpu", fake_gpu)
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.get("/api/vram", headers={"Remote-Groups": "admins"})
    body = await resp.json()
    assert body["gpu_total_bytes"] == 16 * _GIB
    assert body["gpu_used_bytes"] == 10 * _GIB
    # available = real total - real used, straight from ServiceBay.
    assert body["available_bytes"] == 6 * _GIB


async def test_vram_non_admin_forbidden(aiohttp_client, tmp_path):
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.get("/api/vram", headers={"Remote-Groups": "family"})
    assert resp.status == 403


async def test_pull_admin_streams_and_forwards_tag(
    aiohttp_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(server_mod, "OllamaChat", _FakeOllama)
    _FakeOllama.pull_chunks = [{"status": "downloading"}, {"status": "success"}]
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.post(
        "/api/model/pull",
        json={"model": "hf.co/owner/repo:Q4_K_M"},
        headers={"Remote-Groups": "admins"},
    )
    assert resp.status == 200
    text = await resp.text()
    assert _FakeOllama.pulled == "hf.co/owner/repo:Q4_K_M"
    assert '"status": "success"' in text


async def test_pull_non_admin_forbidden(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "OllamaChat", _FakeOllama)
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.post(
        "/api/model/pull",
        json={"model": "gemma4:12b"},
        headers={"Remote-Groups": "family"},
    )
    assert resp.status == 403


async def test_pull_rejects_empty_model(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "OllamaChat", _FakeOllama)
    client = await aiohttp_client(_model_app(tmp_path))
    resp = await client.post(
        "/api/model/pull", json={"model": "  "}, headers={"Remote-Groups": "admins"}
    )
    assert resp.status == 400


# --- Skill edit (admin) ---------------------------------------------------


def test_read_skill_exposes_raw(tmp_path):
    _write_skill(tmp_path, "status", "solaris-status", "Health", "# Status\nok")
    one = skills.read_skill(tmp_path, "status")
    # raw is the full file (frontmatter + body) the editor loads.
    assert one["raw"].startswith("---\nname: solaris-status")
    assert "# Status" in one["raw"]


def test_write_skill_body_only_no_restart(tmp_path):
    _write_skill(tmp_path, "status", "solaris-status", "Health", "# Status\nold")
    new = "---\nname: solaris-status\ndescription: Health\nversion: 1\n---\n\n# Status\nnew\n"
    result = skills.write_skill(tmp_path, "status", new)
    assert result == {"id": "status", "frontmatter_changed": False}
    assert skills.read_skill(tmp_path, "status")["body"].strip() == "# Status\nnew"


def test_write_skill_frontmatter_change_flags_restart(tmp_path):
    _write_skill(tmp_path, "status", "solaris-status", "Health", "# Status\nok")
    new = "---\nname: solaris-status\ndescription: Changed\nversion: 1\n---\n\n# Status\nok\n"
    result = skills.write_skill(tmp_path, "status", new)
    assert result["frontmatter_changed"] is True
    assert skills.read_skill(tmp_path, "status")["description"] == "Changed"


def test_write_skill_rejects_missing_and_traversal(tmp_path):
    _write_skill(tmp_path, "status", "solaris-status", "Health", "# Status\nok")
    assert skills.write_skill(tmp_path, "nope", "x") is None
    assert skills.write_skill(tmp_path, "../etc", "x") is None


def _skill_app(fake, tmp_path):
    return build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        skills_dir=str(tmp_path),
    )


async def test_put_skill_admin_saves(aiohttp_client, tmp_path):
    _write_skill(tmp_path, "status", "solaris-status", "Health", "# Status\nold")
    client = await aiohttp_client(_skill_app(_FakeEngine(), tmp_path))
    new = "---\nname: solaris-status\ndescription: Health\nversion: 1\n---\n\n# Status\nnew\n"

    resp = await client.put(
        "/api/skills/status",
        json={"content": new},
        headers={"Remote-User": "mdopp", "Remote-Groups": "admins"},
    )
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True, "restart_needed": False}
    assert skills.read_skill(tmp_path, "status")["body"].strip() == "# Status\nnew"


async def test_put_skill_frontmatter_change_signals_restart(aiohttp_client, tmp_path):
    _write_skill(tmp_path, "status", "solaris-status", "Health", "# Status\nok")
    client = await aiohttp_client(_skill_app(_FakeEngine(), tmp_path))
    new = (
        "---\nname: solaris-status\ndescription: New\nversion: 1\n---\n\n# Status\nok\n"
    )

    resp = await client.put(
        "/api/skills/status",
        json={"content": new},
        headers={"Remote-Groups": "admins"},
    )
    body = await resp.json()
    assert body["restart_needed"] is True


async def test_put_skill_non_admin_forbidden_and_unchanged(aiohttp_client, tmp_path):
    _write_skill(tmp_path, "status", "solaris-status", "Health", "# Status\nkeep")
    client = await aiohttp_client(_skill_app(_FakeEngine(), tmp_path))

    resp = await client.put(
        "/api/skills/status",
        json={"content": "---\nname: x\n---\nhacked"},
        headers={"Remote-User": "cdopp", "Remote-Groups": "family"},
    )
    assert resp.status == 403
    # The file must be untouched by a rejected write.
    assert skills.read_skill(tmp_path, "status")["body"].strip() == "# Status\nkeep"


async def test_put_skill_missing_and_empty(aiohttp_client, tmp_path):
    _write_skill(tmp_path, "status", "solaris-status", "Health", "# Status\nok")
    client = await aiohttp_client(_skill_app(_FakeEngine(), tmp_path))
    admin = {"Remote-Groups": "admins"}

    resp = await client.put("/api/skills/missing", json={"content": "x"}, headers=admin)
    assert resp.status == 404

    resp = await client.put("/api/skills/status", json={"content": "  "}, headers=admin)
    assert resp.status == 400


# --- MCP servers endpoint (the admin profile's servicebay_admin toolbox) ---


class _FakeMcpToolbox(McpToolbox):
    def __init__(self):
        super().__init__("http://127.0.0.1:5888/mcp", "/tmp/nope")
        self.prepared = 0
        self.dispatched = []

    async def prepare(self):
        self.prepared += 1
        self._names = ["restart_service", "get_services"]

    async def dispatch(self, name, arguments):
        self.dispatched.append((name, arguments))
        return '{"out": "ok"}'


class _FakeProfile:
    def __init__(self, toolbox):
        self.toolbox = toolbox


class _FakeAdminEngine(_FakeEngine):
    def __init__(self, toolbox):
        super().__init__()
        self._profile = _FakeProfile(toolbox)


def _mcp_app(toolbox):
    return build_app(
        engine=_FakeEngine(),
        engine_admin=_FakeAdminEngine(toolbox),
        remote_user_header="Remote-User",
        default_uid="household",
    )


async def test_mcp_endpoint_reports_admin_toolbox(aiohttp_client):
    toolbox = _FakeMcpToolbox()
    client = await aiohttp_client(_mcp_app(toolbox))
    body = await (await client.get("/api/mcp")).json()
    assert body["ok"] is True
    assert body["servers"][0]["name"] == "servicebay_admin"
    assert body["servers"][0]["reachable"] is True
    assert "restart_service" in body["servers"][0]["tools"]
    assert "token" not in body["servers"][0]
    assert toolbox.prepared == 1


async def test_mcp_endpoint_finds_toolbox_inside_combined(aiohttp_client):
    # Since #386 the admin profile carries a CombinedToolbox wrapping the
    # McpToolbox plus the local onboarding tools — _admin_mcp() must unwrap it.
    mcp = _FakeMcpToolbox()
    combined = CombinedToolbox(mcp, Toolbox([]))
    client = await aiohttp_client(_mcp_app(combined))
    body = await (await client.get("/api/mcp")).json()
    assert body["ok"] is True
    assert body["servers"][0]["name"] == "servicebay_admin"
    assert body["servers"][0]["reachable"] is True
    assert "restart_service" in body["servers"][0]["tools"]
    assert mcp.prepared == 1


async def test_mcp_endpoint_empty_without_toolbox(aiohttp_client):
    app = build_app(
        engine=_FakeEngine(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/api/mcp")).json()
    assert body == {"ok": True, "servers": []}


async def test_test_mcp_admin_dispatches_toolbox(aiohttp_client):
    toolbox = _FakeMcpToolbox()
    client = await aiohttp_client(_mcp_app(toolbox))
    resp = await client.post(
        "/api/mcp/servicebay_admin/test",
        json={"tool": "restart_service", "arguments": {"name": "solaris"}},
        headers={"Remote-Groups": "admins"},
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["ok"] is True
    assert toolbox.dispatched == [("restart_service", {"name": "solaris"})]


async def test_test_mcp_non_admin_forbidden_no_call(aiohttp_client):
    toolbox = _FakeMcpToolbox()
    client = await aiohttp_client(_mcp_app(toolbox))
    resp = await client.post(
        "/api/mcp/servicebay_admin/test",
        json={"tool": "restart_service", "arguments": {}},
        headers={"Remote-Groups": "family"},
    )
    assert resp.status == 403
    assert toolbox.dispatched == []


async def test_test_mcp_unknown_server_rejected(aiohttp_client):
    toolbox = _FakeMcpToolbox()
    client = await aiohttp_client(_mcp_app(toolbox))
    resp = await client.post(
        "/api/mcp/other-server/test",
        json={"tool": "x", "arguments": {}},
        headers={"Remote-Groups": "admins"},
    )
    body = await resp.json()
    assert body["ok"] is False
    assert toolbox.dispatched == []


async def test_test_mcp_empty_tool_rejected(aiohttp_client):
    toolbox = _FakeMcpToolbox()
    client = await aiohttp_client(_mcp_app(toolbox))
    resp = await client.post(
        "/api/mcp/servicebay_admin/test",
        json={"tool": "  ", "arguments": {}},
        headers={"Remote-Groups": "admins"},
    )
    assert resp.status == 400


async def test_cancel_unknown_session_is_noop(aiohttp_client):
    app = build_app(
        engine=_FakeEngine(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.post("/api/chat/cancel", json={"session_id": "nope"})
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True, "cancelled": False}


async def test_cancel_interrupts_active_stream(aiohttp_client):
    # A stream that yields forever until cancelled; the cancel endpoint must
    # break the loop and emit a `cancelled` frame (#192).
    import asyncio

    class _SlowEngine(_FakeEngine):
        async def chat_stream(
            self,
            session_id,
            text,
            images=None,
            reasoning_effort="none",
            suggest_answers=False,
            turn_uid="",
        ):
            self.turns.append((session_id, text))
            while True:
                yield {"type": "assistant.delta", "data": {"delta": "x"}}
                await asyncio.sleep(0.01)

    app = build_app(
        engine=_SlowEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat/stream", json={"input": "hi"})
    assert resp.status == 200

    # Read a couple of frames so the stream is registered, then cancel it.
    await resp.content.readuntil(b"event: session")
    await resp.content.readuntil(b"event: delta")
    cancel = await client.post("/api/chat/cancel", json={"session_id": "sess-1"})
    cbody = await cancel.json()
    assert cbody == {"ok": True, "cancelled": True}

    body = await resp.text()
    assert "event: cancelled" in body
    assert body.rstrip().endswith("data: {}")  # final 'done' frame


# --- ServiceBay-maintenance persona lock (#229) ---------------------------


async def test_maint_persona_admin_locked_to_admin_profile(aiohttp_client):
    # An admin creating a session via ?persona=servicebay-maintenance lands on
    # the admin engine profile (which OWNS the operator soul — no per-session
    # prompt), tagged maintenance; any body `personality` is ignored.
    household = _FakeEngine()
    admin = _FakeEngine()
    app = build_app(
        engine=household,
        engine_admin=admin,
        remote_user_header="Remote-User",
        default_uid="household",
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/sessions?persona=servicebay-maintenance",
        json={"personality": "concise"},  # ignored under the lock
        headers={"Remote-User": "mdopp", "Remote-Groups": "admins"},
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["session_id"] == "sess-1"
    # Created on the ADMIN profile with no per-session overlay — the profile
    # supplies the operator soul + skill pack.
    assert admin.created == ["mdopp"]
    assert admin.created_prompts == [""]
    assert admin.maintenance == [True]
    assert household.created == []


async def test_maint_persona_non_admin_forbidden_no_create(aiohttp_client):
    # A non-admin requesting the maintenance persona is refused (403) and no
    # session is created — the Authelia admin gate is enforced server-side.
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/sessions?persona=servicebay-maintenance",
        headers={"Remote-User": "cdopp", "Remote-Groups": "family"},
    )
    assert resp.status == 403
    assert fake.created == []


async def test_household_create_unaffected_by_query_persona(aiohttp_client):
    # A normal (non-maintenance) create stays on the household gateway: no soul
    # fetch, no maintenance marker, and (#293) no per-session persona overlay —
    # the profile supplies the soul. An unrelated query string is inert.
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/sessions?persona=solaris",
        json={"personality": "teacher"},
        headers={"Remote-User": "mdopp", "Remote-Groups": "family"},
    )
    assert resp.status == 200
    assert fake.created_prompts == [""]
    assert fake.maintenance == [False]


async def test_maint_persona_cannot_escalate_mid_session(aiohttp_client):
    # Once a session exists, per-turn `personality` is ignored — a maintenance
    # session's locked prompt can't be switched to the household Solaris persona by
    # any client-supplied field on a follow-up turn.
    fake = _FakeEngine()
    app = build_app(
        engine=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat",
        json={"input": "status", "session_id": "maint-1", "personality": "solaris"},
    )
    assert resp.status == 200
    # Reusing an existing session never re-creates it, so no new system prompt
    # is applied — the create-time lock holds for the session's whole life.
    assert fake.created == []
    _assert_turns(fake.turns, [("maint-1", "status")])


# --- Solaris Gründlich = e4b + reasoning (12b retired, #809) ------------------


async def test_chat_never_routes_to_deep_gateway(aiohttp_client, tmp_path):
    # 12b retired 2026-07-13: no chat persona/pref routes a turn to the deep
    # gateway anymore — "Gründlich" is the reasoning knob on the household model
    # (covered in test_gateway_routing), so the chat path never touches deep.
    from solaris_chat import settings_store

    household = _FakeEngine()
    deep = _FakeEngine()
    db = str(tmp_path / "solaris.db")
    settings_store.set_other_model_pref(db, "thorough")
    app = build_app(
        engine=household,
        engine_deep=deep,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )
    client = await aiohttp_client(app)

    # A once-"solaris-deep" persona is now just an unknown overlay id: it does not
    # switch gateways — the turn stays on household, reasoning driven by the pref.
    resp = await client.post(
        "/api/chat",
        json={"input": "denk gründlich nach", "personality": "solaris-deep"},
        headers={"Remote-User": "cdopp", "Remote-Groups": "family"},
    )
    assert resp.status == 200
    assert household.created == ["cdopp"]
    assert deep.created == [] and deep.turns == []


async def test_default_persona_with_fast_pref_stays_on_household(
    aiohttp_client, tmp_path
):
    # A normal turn follows the everyday-chat pref: with "fast" it stays on the
    # household (e4b) gateway and runs no reasoning. (The "thorough" path — same
    # gateway + reasoning — is covered in test_gateway_routing.)
    from solaris_chat import settings_store

    household = _FakeEngine()
    deep = _FakeEngine()
    db = str(tmp_path / "solaris.db")
    settings_store.set_other_model_pref(db, "fast")
    app = build_app(
        engine=household,
        engine_deep=deep,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat",
        json={"input": "wie spät", "personality": "solaris"},
        headers={"Remote-User": "cdopp", "Remote-Groups": "family"},
    )
    assert resp.status == 200
    assert household.created == ["cdopp"]
    assert household.efforts == ["none"]
    assert deep.created == []
    assert deep.turns == []


async def test_csp_header_from_default_frame_ancestors(aiohttp_client):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
    )
    client = await aiohttp_client(app)

    resp = await client.get("/health")
    assert resp.headers["Content-Security-Policy"] == "frame-ancestors 'self'"
    assert "X-Frame-Options" not in resp.headers


async def test_csp_header_uses_configured_frame_ancestors(aiohttp_client):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        frame_ancestors="'self' https://admin.dopp.cloud",
    )
    client = await aiohttp_client(app)

    resp = await client.get("/health")
    assert (
        resp.headers["Content-Security-Policy"]
        == "frame-ancestors 'self' https://admin.dopp.cloud"
    )


async def test_ha_call_runs_scoped_service_and_returns_state(
    aiohttp_client, monkeypatch
):
    seen = {}

    async def _fake_call(hass_url, hass_token, entity_id, service, data=None):
        seen.update(
            url=hass_url, token=hass_token, entity=entity_id, service=service, data=data
        )
        return {"ok": True, "state": "on"}

    monkeypatch.setattr(server_mod, "call_service_scoped", _fake_call)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        hass_url="http://ha",
        hass_token="tok",
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/ha/call",
        json={"entity_id": "light.kitchen", "service": "light.toggle"},
    )
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True, "state": "on"}
    # the configured household HA creds are used, never a client-supplied token
    assert seen == {
        "url": "http://ha",
        "token": "tok",
        "entity": "light.kitchen",
        "service": "light.toggle",
        "data": None,
    }


async def test_ha_call_rejects_unsupported_domain(aiohttp_client, monkeypatch):
    called = False

    async def _fake_call(*a, **k):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(server_mod, "call_service_scoped", _fake_call)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        hass_url="http://ha",
        hass_token="tok",
    )
    client = await aiohttp_client(app)

    # phases 2+3 cover light/switch/cover/climate; a media_player/shell domain is refused
    resp = await client.post(
        "/api/ha/call",
        json={"entity_id": "media_player.tv", "service": "media_player.toggle"},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "unsupported_domain"
    assert called is False  # never reaches the HA helper


@pytest.mark.parametrize(
    "entity_id,service,data",
    [
        ("cover.blind", "cover.set_cover_position", {"position": 40}),
        ("climate.living", "climate.set_temperature", {"temperature": 22}),
    ],
)
async def test_ha_call_allows_cover_and_climate_controls(
    aiohttp_client, monkeypatch, entity_id, service, data
):
    # Phase 3 (#477): the cover-position and climate-setpoint controls reach the
    # scoped helper with the configured creds (mocked HA — never a real device).
    seen = {}

    async def _fake_call(hass_url, hass_token, eid, svc, d=None):
        seen.update(entity=eid, service=svc, data=d)
        return {"ok": True, "state": "ok"}

    # The #702 confirm-gate resolves a cover's device_class before running: an
    # ordinary blind is non-sensitive → the tap proceeds without a confirm.
    async def _fake_card(url, token, eid):
        return {"domain": "cover", "device_class": "blind", "entity_id": eid}

    monkeypatch.setattr(server_mod, "call_service_scoped", _fake_call)
    monkeypatch.setattr(server_mod, "fetch_card", _fake_card)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        hass_url="http://ha",
        hass_token="tok",
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/ha/call",
        json={"entity_id": entity_id, "service": service, "data": data},
    )
    assert resp.status == 200
    assert seen == {"entity": entity_id, "service": service, "data": data}


async def test_ha_call_refuses_unconfirmed_sensitive_cover(aiohttp_client, monkeypatch):
    # #702: a garage/door/gate cover open is confirm-gated. A bare card tap
    # (no confirmed flag) must be refused server-side and never reach HA — the
    # server gate is authoritative, not the client dialog. Fails against the
    # pre-#702 code where /api/ha/call ran any allowed-domain service unchecked.
    called = False

    async def _fake_call(*a, **k):
        nonlocal called
        called = True
        return {"ok": True, "state": "open"}

    async def _fake_card(url, token, eid):
        return {"domain": "cover", "device_class": "garage", "entity_id": eid}

    monkeypatch.setattr(server_mod, "call_service_scoped", _fake_call)
    monkeypatch.setattr(server_mod, "fetch_card", _fake_card)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        hass_url="http://ha",
        hass_token="tok",
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/ha/call",
        json={"entity_id": "cover.garage", "service": "cover.open_cover"},
    )
    assert resp.status == 403
    assert (await resp.json())["error"] == "sensitive_action"
    assert called is False  # never reaches the HA helper


async def test_ha_call_runs_confirmed_sensitive_cover(aiohttp_client, monkeypatch):
    # #702: the same garage open proceeds once the client sends confirmed=true
    # (after its explicit confirm dialog); the server re-checked and let it pass.
    seen = {}

    async def _fake_call(hass_url, hass_token, eid, svc, d=None):
        seen.update(entity=eid, service=svc)
        return {"ok": True, "state": "open"}

    async def _fake_card(url, token, eid):
        return {"domain": "cover", "device_class": "garage", "entity_id": eid}

    monkeypatch.setattr(server_mod, "call_service_scoped", _fake_call)
    monkeypatch.setattr(server_mod, "fetch_card", _fake_card)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        hass_url="http://ha",
        hass_token="tok",
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/ha/call",
        json={
            "entity_id": "cover.garage",
            "service": "cover.open_cover",
            "confirmed": True,
        },
    )
    assert resp.status == 200
    assert seen == {"entity": "cover.garage", "service": "cover.open_cover"}


async def test_ha_call_503_when_ha_not_configured(aiohttp_client):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/ha/call",
        json={"entity_id": "light.kitchen", "service": "light.toggle"},
    )
    assert resp.status == 503
    assert (await resp.json())["error"] == "ha_not_configured"


async def test_portal_energy_history_503_when_ha_not_configured(aiohttp_client):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/portal/energy/history")
    assert resp.status == 503
    assert (await resp.json())["error"] == "ha_unconfigured"


async def test_portal_energy_history_maps_range_and_returns_series(
    aiohttp_client, monkeypatch
):
    seen = {}

    async def fake_history(hass_url, hass_token, hours):
        seen["hours"] = hours
        return {
            "hours": hours,
            "series": [
                {
                    "entity_id": "sensor.hausverbrauch",
                    "label": "Hausverbrauch",
                    "unit": "W",
                    "points": [{"t": "2026-07-07T10:00:00+00:00", "v": 500.0}],
                }
            ],
        }

    monkeypatch.setattr(server_mod, "fetch_energy_history", fake_history)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        hass_url="http://ha.local",
        hass_token="tok",
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/portal/energy/history?range=7d")
    assert resp.status == 200
    body = await resp.json()
    assert seen["hours"] == 168
    assert body["ok"] is True
    assert body["history"]["series"][0]["label"] == "Hausverbrauch"

    resp = await client.get("/api/portal/energy/history?range=24h")
    assert resp.status == 200
    assert seen["hours"] == 24

    # No range → defaults to 24h.
    resp = await client.get("/api/portal/energy/history")
    assert resp.status == 200
    assert seen["hours"] == 24


async def test_portal_energy_history_502_when_ha_unavailable(
    aiohttp_client, monkeypatch
):
    async def fake_history(hass_url, hass_token, hours):
        return None

    monkeypatch.setattr(server_mod, "fetch_energy_history", fake_history)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        hass_url="http://ha.local",
        hass_token="tok",
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/portal/energy/history?range=24h")
    assert resp.status == 502
    assert (await resp.json())["error"] == "ha_unavailable"
