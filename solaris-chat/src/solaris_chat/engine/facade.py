"""Ollama-compatible facade — the engine as a Home Assistant conversation agent.

HA 2026.6's core `openai_conversation` integration has no custom base_url, but
its `ollama` integration takes a free URL + optional Bearer api_key and speaks
exactly the protocol the engine already uses downstream. So the engine exposes
a minimal Ollama surface under `/ollama` on the chat port:

  GET  /ollama/api/tags     — the config-flow validation call (`client.list()`)
  GET  /ollama/api/version  — cheap liveness some ollama clients ping
  POST /ollama/api/chat     — the conversation call, NDJSON-streamed or single

"Models" are engine profiles: `solaris` (household, fast) and `solaris-deep`
(same e4b, thinks by default — the voice "Gründlich" path; 12b retired
2026-07-13). HA resends its conversation history per turn; the engine runs its
own tool loop server-side and streams only content deltas back — HA never
sees tool_calls, so its MAX_TOOL_ITERATIONS loop runs exactly once. The
voice-gatekeeper speaks the same surface (stream=false) for wyoming-satellite
hardware.
"""

from __future__ import annotations
from solaris_chat.engine import confirm, enrollment_fsm

import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from solaris_chat import trace_store
from solaris_chat.engine import store
from solaris_chat.engine.client import EngineClient, EngineError, current_room
from solaris_chat.engine.notify import EventBus, Notifier, emit_chat
from solaris_chat.engine.tools import (
    CHANNEL_VOICE,
    current_channel,
    current_speaker_matched,
)
from solaris_chat.logging import log
from solaris_chat.voice_uid_stash import consume_uid

# The gatekeeper stashes this uid for a speaker that speaker-ID heard but
# matched to no enrolled resident (an attempted-but-unknown speaker, #351).
# It is not a real resident: the turn runs the ephemeral guest profile (#353).
GUEST_UID = "guest"


# The gatekeeper (and an HA Voice PE per-device system prompt) prefixes the user
# utterance with `[room: <area>]\n` to name the originating room (#313). The
# facade parses it into the `current_room` contextvar so a device-less "spiele
# Musik" defaults to that room's media_player, then strips it so the model never
# sees the marker. The newline is optional (a per-device prompt may omit it).
_ROOM_PREFIX = re.compile(r"^\[room:\s*(?P<room>[^\]]*)\]\s*", re.IGNORECASE)


def _split_room(text: str) -> tuple[str, str]:
    """`[room: Küche]\\nspiele Musik` → ("Küche", "spiele Musik").

    Returns ("", text) when there is no `[room: …]` prefix."""
    m = _ROOM_PREFIX.match(text)
    if not m:
        return "", text
    return m.group("room").strip(), text[m.end() :]


def _model_entry(name: str) -> dict[str, Any]:
    # Enough fields for the ollama python client's pydantic ListResponse.
    return {
        "name": name,
        "model": name,
        "modified_at": "2026-01-01T00:00:00Z",
        "size": 0,
        "digest": "solaris-engine",
        "details": {"family": "solaris", "parameter_size": "", "format": ""},
    }


def _authorized(request: web.Request, api_key: str) -> bool:
    if not api_key:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {api_key}"


# HA's `ollama` conversation integration derives `continue_conversation` purely
# from whether the assistant's reply text ends in a question mark (chat_log.py
# continue_conversation ← util.py). So when THIS turn has a QUESTION PENDING the
# spoken text must end in `?` for the Voice PE to re-open the mic without a
# re-wake (#566, #627). A question is pending when offer_choices fired (the
# `quick_replies` event) OR the reply text already CONTAINS a `?` somewhere — the
# latter catches the common case where the model asks then APPENDS statements
# (so the `?` isn't last) and the confirm-gate / play need_device|no_favorite
# replies, which all end up phrasing a question (#627). A plain statement with no
# `?` and no chips is NOT a question, so the loop stops.
_QUESTION_MARKS = ("?", "？", ";")


def _question_pending(text: str, offered_choices: bool) -> bool:
    return offered_choices or any(q in text for q in _QUESTION_MARKS)


# Sentence-final punctuation a trailing "?" REPLACES rather than follows —
# appending produced "Bitte antworte mit Ja oder Nein.?" on the box. Not the
# Greek ";" — that is already a question mark and returns above.
_SENTENCE_END = ".!…:,"


def _as_question(text: str) -> str:
    stripped = text.rstrip()
    if stripped.endswith(_QUESTION_MARKS):
        return text
    return stripped.rstrip(_SENTENCE_END) + "?"


# A bare "nein" after a closing pleasantry ends the conversation. HA re-opens
# the mic purely because the reply ended in `?`, and nothing ever closed it
# deliberately — so the resident's decline arrived as an ordinary utterance with
# no content, and the model answered from the only thing left in the prompt: the
# wall-clock hint. Box-observed: "kann ich noch etwas für dich tun?" — "nein" —
# silence, then the time. Only "stop" (HA's own word) got out.
_MAX_DECLINE_WORDS = 3


def _is_bare_decline(text: str) -> bool:
    """A short, content-free refusal — not a "nein" that answers something."""
    words = re.findall(r"[\wäöüß]+", (text or "").lower())
    if not words or len(words) > _MAX_DECLINE_WORDS:
        return False
    return confirm.is_negative(text) and not confirm.is_affirmative(text)


def _chunk(model: str, content: str, done: bool, done_reason: str = "") -> bytes:
    body: dict[str, Any] = {
        "model": model,
        "created_at": datetime.now(UTC).isoformat(),
        "message": {"role": "assistant", "content": content},
        "done": done,
    }
    if done:
        body["done_reason"] = done_reason or "stop"
    return (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")


# The SOUL invites the model to wrap entities inline as `[[X]]` / `[[X|label]]`
# cross-links; the browser/SPA path renders those as tap-through links, but the
# voice/facade path hands the reply text straight to HA's TTS, which would speak
# the brackets ("klammer klammer Anna", #616). Strip the markup to its plain
# spoken form here — voice path only; the browser path (client.py / server.py)
# keeps the `[[ ]]`.
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def _strip_wikilinks(text: str) -> str:
    """`[[X]]` → `X`, `[[X|label]]` → `label`; non-link text unchanged."""
    return _WIKILINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), text)


class WikilinkStripper:
    """Streaming-safe `_strip_wikilinks`: feed deltas, get plain-text deltas.

    Holds back any trailing fragment that could be the start of an unclosed
    `[[…` so a wikilink split across deltas is still normalized whole; `flush`
    releases whatever remains at turn end.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> str:
        self._buf += delta
        # Hold back from the last unclosed `[[` (or a lone trailing `[` that
        # could still become one): everything before it can't be mid-wikilink.
        safe_upto = len(self._buf)
        start = self._buf.rfind("[[")
        if start != -1 and "]]" not in self._buf[start:]:
            safe_upto = start
        elif self._buf.endswith("[") and not self._buf.endswith("]]"):
            safe_upto = len(self._buf) - 1
        out = _strip_wikilinks(self._buf[:safe_upto])
        self._buf = self._buf[safe_upto:]
        return out

    def flush(self) -> str:
        out = _strip_wikilinks(self._buf)
        self._buf = ""
        return out


def add_facade_routes(
    app: web.Application,
    *,
    clients: dict[str, EngineClient],
    api_key: str,
    default_uid: str,
    solaris_db_path: str,
    event_bus: EventBus | None = None,
    notifier: Notifier | None = None,
) -> None:
    async def tags(request: web.Request) -> web.Response:
        if not _authorized(request, api_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"models": [_model_entry(name) for name in clients]})

    async def version(request: web.Request) -> web.Response:
        if not _authorized(request, api_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"version": "solaris-engine"})

    async def chat(request: web.Request) -> web.StreamResponse:
        if not _authorized(request, api_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response({"error": "invalid json"}, status=400)
        model = str(body.get("model") or "")
        client = clients.get(model.removesuffix(":latest"))
        if client is None:
            return web.json_response(
                {"error": f"model '{model}' not found"}, status=404
            )
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return web.json_response({"error": "messages required"}, status=400)
        stream = body.get("stream", True)
        # The latest user utterance doubles as the lookup key for the live
        # voice path: when the gatekeeper served as HA's STT provider it
        # stashed {transcript -> resolved resident uid} (#350, approach b).
        # Resolve the speaking resident by that transcript; fall back to the
        # body's `user` (HA sends `household`) on a miss. Consume-once.
        # An originating-room prefix (`[room: X]`) injected by the gatekeeper or
        # an HA Voice PE per-device prompt is parsed out HERE: the room rides a
        # contextvar so a device-less play defaults to it, and the marker is
        # stripped from both the transcript and the replayed message so the model
        # (and the uid-stash lookup, keyed on the raw whisper transcript) never
        # sees it.
        room, transcript = _split_room(_last_user(messages))
        # Unconditionally: an older replayed turn can carry a marker even when
        # the newest one does not.
        _strip_room_from_messages(messages)
        current_room.set(room)
        matched_uid = consume_uid(solaris_db_path, transcript)
        uid = matched_uid or str(body.get("user") or default_uid)
        # Visibility gate (#1130, ADR-12 / G-6): everything on this route is
        # spoken out loud, so the tool's declared class decides whether it may
        # answer with content at all. A stash hit is a real speaker-ID match and
        # unlocks the PERSONAL class; it never unlocks CONFIDENTIAL, because
        # far-field recognition is spoofable and the failure mode is a leak
        # inside the family.
        current_channel.set(CHANNEL_VOICE)
        current_speaker_matched.set(bool(matched_uid) and matched_uid != GUEST_UID)
        # An unknown speaker (speaker-ID ran but matched no resident, #351) is
        # routed to the ephemeral guest profile, not the resident's household
        # session — only when speaker-ID actively resolved UNKNOWN, never on a
        # plain stash miss (speaker-ID off / not attempted), which stays
        # household. Falls through to the requested model if no guest profile
        # is wired (it ships as its own model first; #353).
        guest = clients.get("solaris-guest")
        if uid == GUEST_UID and guest is not None:
            model, client = "solaris-guest", guest

        # Enrollment routing (#1056): if ANY user has an active voice enrollment
        # or wakeword request, route ALL voice turns to the isolated enrollment
        # profile. This profile has no HA tools, no history, no context pollution.
        enrollment = clients.get("solaris-enrollment")
        if enrollment is not None:
            try:
                from solaris_chat import enroll_requests_store, wakeword_requests_store

                if enroll_requests_store.has_any_active_request(
                    solaris_db_path
                ) or wakeword_requests_store.has_any_active_request(solaris_db_path):
                    model, client = "solaris-enrollment", enrollment
            except Exception:
                pass
        log.info("engine.facade.turn", model=model, uid=uid, n_messages=len(messages))

        # A voice turn lands in the resident's durable household session (#345):
        # the store owns the history, so only the latest user utterance is run
        # (HA still replays its whole list — we take the tail). The same session
        # the browser opens, so spoken + typed history are one conversation and
        # the turn mirrors live into open tabs (#344) via the persisted path.
        # A guest profile (#353) is ephemeral: it runs the stateless `respond`
        # path on HA's replayed history, so nothing about the guest persists.
        text = transcript

        # A durable voice turn persists its trace into the same household
        # session (#405) so the "Zuhause" chat carries the same per-turn trace
        # rows the browser path writes — not just message history. Ephemeral
        # guest turns persist nothing.
        t0 = time.time()

        # HA's conversation id keys the confirmation gate per conversation on the
        # ephemeral path so one caller can't confirm another's held action (#570
        # F3); None when absent disables stashing (re-gate every turn).
        conversation_id = body.get("conversation_id")
        conversation_id = str(conversation_id) if conversation_id else None

        def turns() -> AsyncIterator[dict[str, Any]]:
            # A decline that ends the conversation, answered deterministically so
            # the reply carries no `?` and HA closes the mic. Skipped while the
            # wizard runs (it owns its own ja/nein) and while a sensitive action
            # is held for confirmation, where "nein" is the answer, not an exit.
            if (
                _is_bare_decline(transcript)
                and not enrollment_fsm.is_active(solaris_db_path)
                and not client.has_pending_confirmation(
                    conversation_id
                    if client.ephemeral
                    else store.household_session_id(uid)
                )
            ):

                async def _closing_turns() -> AsyncIterator[dict[str, Any]]:
                    reply = "Alles klar."
                    yield {"type": "assistant.delta", "data": {"delta": reply}}
                    yield {"type": "run.completed", "data": {"answer": reply}}

                return _closing_turns()

            if client.profile_name == "solaris-enrollment" or enrollment_fsm.is_active(
                solaris_db_path
            ):
                # Deterministic FSM Pipeline (#1056): Runs 100% deterministically in Python with 0 LLM calls.
                # Guarantees zero latency, zero hallucinations, and zero device action risk.
                async def _fsm_turns() -> AsyncIterator[dict[str, Any]]:
                    reply = enrollment_fsm.handle_turn(
                        solaris_db_path, transcript, uid_hint=uid
                    )
                    yield {"type": "assistant.delta", "data": {"delta": reply}}
                    yield {"type": "run.completed", "data": {"answer": reply}}

                return _fsm_turns()

            if client.ephemeral:
                return client.respond(
                    messages, uid=uid, source=model, conversation_id=conversation_id
                )
            return client.respond_session(
                text, uid=uid, conversation_id=conversation_id
            )

        def persist_trace() -> None:
            if client.ephemeral:
                return
            _persist_voice_trace(solaris_db_path, client, uid, t0)

        if not stream:
            try:
                answer, offered_choices = await _drain(turns())
            except EngineError:
                persist_trace()
                return web.json_response({"error": "engine unavailable"}, status=502)
            persist_trace()
            answer = _strip_wikilinks(answer)
            if answer and _question_pending(answer, offered_choices):
                answer = _as_question(answer)
            await _emit_voice_turn(event_bus, notifier, client, uid, answer)
            return web.Response(
                body=_chunk(model, answer, done=True),
                content_type="application/json",
            )

        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        streamed = ""
        offered_choices = False
        stripper = WikilinkStripper()
        try:
            async for event in turns():
                if event["type"] == "assistant.delta":
                    delta = str(event["data"].get("delta") or "")
                    if delta:
                        spoken = stripper.feed(delta)
                        if spoken:
                            streamed += spoken
                            await resp.write(_chunk(model, spoken, done=False))
                elif event["type"] == "quick_replies":
                    offered_choices = True
                elif event["type"] == "run.completed":
                    final = _final_answer(event)
                    # A tool turn can finish with no streamed deltas — surface
                    # the final answer as one late chunk (the #258 pattern).
                    if final and not streamed.strip():
                        final = _strip_wikilinks(final)
                        streamed = final
                        await resp.write(_chunk(model, final, done=False))
            tail = stripper.flush()
            if tail:
                streamed += tail
                await resp.write(_chunk(model, tail, done=False))
            # A turn with a question pending must end in `?` so HA keeps the mic
            # open for the answer without a re-wake (#566, #627). Deltas already
            # went out verbatim — append the missing `?` as a trailing chunk.
            if (
                streamed.strip()
                and _question_pending(streamed, offered_choices)
                and not streamed.rstrip().endswith(_QUESTION_MARKS)
            ):
                await resp.write(_chunk(model, "?", done=False))
        except EngineError as e:
            log.error("engine.facade.failed", model=model, error=str(e))
            # A failed voice turn still persists whatever the recorder captured
            # before the error (#562) — otherwise the failure is invisible in
            # the chat UI, the operator's exact complaint about intent-failed.
            persist_trace()
            await resp.write(_chunk(model, "", done=True, done_reason="error"))
            return resp
        persist_trace()
        await _emit_voice_turn(event_bus, notifier, client, uid, streamed)
        await resp.write(_chunk(model, "", done=True))
        return resp

    app.router.add_get("/ollama/api/tags", tags)
    app.router.add_get("/ollama/api/version", version)
    app.router.add_post("/ollama/api/chat", chat)


async def _emit_voice_turn(
    event_bus: EventBus | None,
    notifier: Notifier | None,
    client: EngineClient,
    uid: str,
    reply: str,
) -> None:
    """Propagate a completed voice turn to the resident (#715, #724).

    A voice reply is spoken on the Voice PE speaker but fires no browser turn,
    so — like the non-stream `/api/chat` caller — it may Web Push (`push=True`)
    so a backgrounded phone still surfaces the reply, while an open SSE tab is
    updated live instead of self-pushed (`emit_chat` reuses `has_subscriber`).
    Ephemeral guest turns persist nothing and stay silent."""
    if event_bus is None or client.ephemeral or not reply.strip():
        return
    session_id = store.household_session_id(uid)
    await emit_chat(event_bus, notifier, uid, session_id, reply)


def _persist_voice_trace(
    db_path: str, client: EngineClient, uid: str, t0: float
) -> None:
    """Persist a durable voice turn's trace into its household session (#405).

    Mirrors the browser path's `persist_turn_trace`: the recorder's steps for
    this session since `t0` become `session_traces` rows, so the "Zuhause" chat
    reopens with the same per-turn trace the typed path shows. Best-effort — a
    trace-write hiccup never breaks a voice turn that already replied."""
    session_id = store.household_session_id(uid)
    try:
        trace_id = uuid.uuid4().hex
        steps = []
        for order, rec in enumerate(client.recorder.for_session(session_id, t0)):
            # Persist the detail body with the step under a stable per-step key
            # so the modal resolves after a reload/restart (#451).
            detail = client.recorder.detail(rec["id"]) if "id" in rec else None
            steps.append(
                {
                    "model": rec.get("model"),
                    "profile": rec.get("profile"),
                    "wall_s": rec.get("wall_s"),
                    "prompt_tokens": rec.get("prompt_tokens"),
                    "completion_tokens": rec.get("completion_tokens"),
                    "context_free": rec.get("context_free"),
                    "finish_reason": rec.get("finish_reason"),
                    "n_tools": rec.get("n_tools"),
                    "detail_id": f"{trace_id}:{order}" if detail else None,
                    "step_kind": rec.get("step_kind"),
                    "tool_name": rec.get("tool_name"),
                    "detail_json": json.dumps(detail) if detail else None,
                }
            )
        if steps:
            trace_store.persist_trace(db_path, session_id, trace_id, uid, steps)
    except Exception as e:  # noqa: BLE001 — trace persistence is best-effort
        log.warn("engine.facade.trace_persist_error", uid=uid, error=str(e))


def _strip_room_from_messages(messages: list[Any]) -> None:
    """Strip the `[room: X]` prefix off EVERY user message in place, so the
    ephemeral `respond` path (which replays the caller's message list) never
    feeds the marker to the model.

    Every one, not just the newest: HA replays the whole conversation, so the
    turns before it still carried their own markers into the guest prompt.
    """
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("content"):
            _, stripped = _split_room(str(msg["content"]))
            msg["content"] = stripped


def _last_user(messages: list[Any]) -> str:
    """The latest user utterance in HA's replayed message list (#345). The
    durable session owns the rest of the history, so only the tail is run."""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("content"):
            return str(msg["content"])
    return ""


async def _drain(turns: AsyncIterator[dict[str, Any]]) -> tuple[str, bool]:
    answer = ""
    streamed = ""
    offered_choices = False
    async for event in turns:
        if event["type"] == "assistant.delta":
            streamed += str(event["data"].get("delta") or "")
        elif event["type"] == "quick_replies":
            offered_choices = True
        elif event["type"] == "run.completed":
            answer = _final_answer(event)
    return answer or streamed, offered_choices


def _final_answer(event: dict[str, Any]) -> str:
    for msg in event.get("data", {}).get("messages", []):
        if msg.get("role") == "assistant" and msg.get("content"):
            return str(msg["content"])
    return ""
