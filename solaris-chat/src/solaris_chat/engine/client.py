"""EngineClient — the in-process replacement for a Hermes gateway.

Implements the HermesClient surface (create/list/get/delete session,
set_title, chat, chat_stream, list_toolsets) so `server.py`'s routing,
compaction and the browser SSE protocol keep working unchanged — but the
"gateway" is a profile object: a model tag, a soul, a toolbox and an optional
entity registry, all sharing one store, one Ollama connection and one trace
recorder. Three of these replace the three Hermes gateways; what used to be
a container-and-port is now a constructor call.

Events yielded by `chat_stream` mirror the Hermes SSE shapes `_normalize`
folds for the browser: `assistant.delta`, `tool.started`/`tool.completed`,
`run.completed` (with `reasoning_content` on the final assistant message).
Plus `llm.step` (model + wall_s after each Ollama pass) for the live
activity bubble (#347); `_normalize` folds it to a `step` browser event.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from solaris_chat import favorites_store
from solaris_chat.engine import confirm, remember, store
from solaris_chat.engine.bus import SessionBus
from solaris_chat.engine.ollama import OllamaChat, OllamaError
from solaris_chat.engine.registry import EntityRegistry
from solaris_chat.engine.residents import identity_block
from solaris_chat.engine.tools import Toolbox, current_channel, current_speaker_matched
from solaris_chat.engine.tools import choices as choice_tools
from solaris_chat.engine.tools import ha as ha_tools
from solaris_chat.engine.tools.favorites import PINNABLE_TOOLS
from solaris_chat.engine.trace import TraceRecorder
from solaris_chat.logging import log

# The current turn's resident — read by tools (timers, facts) that need an
# owner. A contextvar because the toolbox is built once per profile but a
# turn belongs to whoever sent it.
current_uid: contextvars.ContextVar[str] = contextvars.ContextVar(
    "engine_uid", default=""
)

# The current turn's room — the area the voice command came from, injected by
# the gatekeeper/HA as a `[room: X]` prefix (parsed in the facade). Lets a
# device-less "spiele Musik" default to the originating room's media_player.
current_room: contextvars.ContextVar[str] = contextvars.ContextVar(
    "engine_room", default=""
)

# The current turn's session id — read by pin_favorite to key the recorder's
# per-conversation step filter (#645). Like current_uid it must be set INSIDE
# the dispatch task; a set() on the enclosing coroutine does not propagate into
# the SSE-heartbeat-spawned gather task.
current_session: contextvars.ContextVar[str] = contextvars.ContextVar(
    "engine_session", default=""
)

# The acting admin's verified Authelia identity for THIS turn (#794): the
# (Remote-User, Remote-Groups) forward-auth headers of the admin driving a
# Wartung turn, so the SB-MCP toolbox can exchange them for a short-lived
# scoped token (token-from-authelia-session) instead of holding a standing
# minting credential. Empty for every non-admin turn — the token exchange is
# then never attempted, so a household/guest turn can mint nothing. Like
# current_uid it must be re-pinned INSIDE the dispatch task (the SSE heartbeat
# runs gather in a fresh task that doesn't inherit the enclosing set()).
current_admin_identity: contextvars.ContextVar[tuple[str, str]] = (
    contextvars.ContextVar("engine_admin_identity", default=("", ""))
)

# Tool-call passes per turn: enough for list->act->confirm chains plus a
# retry, small enough that a confused model can't spin.
_MAX_PASSES = 6

# Max chars kept of a PAST turn's tool result when compacting the history
# (#623): a 1-line gist that still carries the salient value so a follow-up
# ('und das Wohnzimmer?') can resolve against it — short, never empty.
_GIST_CHARS = 120

# A turn that emits many tool calls (e.g. 'alle Lichter' -> ha_get_state per
# entity) dispatches them concurrently with this bound, instead of awaiting
# each in turn. Order is still preserved (results appended in emitted order);
# the bound keeps a burst from hammering HA/MCP all at once.
_MAX_PARALLEL_TOOLS = 5


class _Unset:
    """Sentinel: `pending_key` was not passed (use session_id), distinct from an
    explicit None (no per-conversation key — do not stash)."""


_UNSET = _Unset()

_LOCAL_TZ = ZoneInfo("Europe/Berlin")


def _now_hint() -> str:
    now = datetime.now(_LOCAL_TZ)
    return f"[Aktuelle Zeit: {now.strftime('%A, %d.%m.%Y, %H:%M Uhr %Z')}]"


# Tool discipline, pinned as the LAST system block so it sits closest to the
# history. Position is load-bearing (box A/B 2026-06-12): one stochastic
# narrative reply in the history makes the model imitate it forever after —
# the same rule placed early in the soul lost 0/3 against a poisoned history,
# placed here it reliably restored tool calls. German on purpose: it must
# outweigh German narrative examples in the history.
_TOOL_DISCIPLINE = (
    "Sage NIEMALS nur, dass du etwas tust, lädst oder prüfst. Für jede"
    " Geräteaktion und jede Zustandsfrage rufst du IMMER zuerst das passende"
    " Tool auf und antwortest erst mit dem Ergebnis — auch wenn frühere"
    " Antworten im Verlauf eine Aktion nur angekündigt haben."
    " Bei sicherheitsrelevanten oder schwer umkehrbaren Aktionen, die das Haus"
    " öffnen oder sichern — lock (besonders unlock), alarm_control_panel"
    " entschärfen, und cover mit Geräteklasse garage — fragst du zuerst kurz"
    " nach ('Soll ich …?') UND rufst dazu offer_choices(['ja','nein']) auf,"
    " damit der Nutzer tippen kann. In genau diesem Zug rufst du dann KEIN"
    " Aktions-Tool (ha_call_service o. Ä.) auf — du hörst nach der Rückfrage"
    " auf und wartest auf die Antwort; die Aktion führst du erst aus, wenn der"
    " Nutzer im nächsten Zug bestätigt. Alles andere (Licht, Schalter,"
    " media_player, Klima, Ventilator, Szenen, Skripte, normale"
    " Rollos/Jalousien) führst du ohne Rückfrage direkt aus."
    " Bei einer Wissensfrage rufst du research(query) auf und antwortest"
    " DIREKT aus den gelieferten Quellen MIT Quellenangabe — ohne vorher"
    " eine Rückfrage zu stellen."
)

# A present-tense German device-state assertion ("… ist an", "… ist aus",
# "… ist eingeschaltet", "… läuft", "… ist gesperrt") OR a perfect-tense action
# claim ("habe das Licht eingeschaltet", "Das Licht wurde ausgeschaltet"). When
# the model emits one of these as its final answer WITHOUT having called a tool
# this turn, it is fabricating a result — the clarify→"Ja."→empty-tool_calls
# path (#356) that survives low-temp + the discipline rule. Detection is German
# on purpose: the hot path runs German, and the false-positive surface (a turn
# that merely quotes a state read back from a tool) is excluded by the "no tool
# ran this turn" gate, not by the text. The participle anchor (ge…schaltet) only
# fires on a *completed* action, so an infinitive question ("Soll ich das Licht
# einschalten?") or a future intent ("ich schalte gleich …") does not match.
_DEVICE_CLAIM = re.compile(
    r"\bist\s+(an|aus|ein(geschaltet)?|aus(geschaltet)?|"
    r"gesperrt|entsperrt|gestartet|gestoppt|geschlossen|geöffnet|offen|zu)\b"
    r"|\bist\s+jetzt\b|\bläuft\b"
    # perfect-tense action: habe/hat/haben … (ein|aus|an)geschaltet, with an
    # optional intervening accusative object, or the passive "wurde … geschaltet".
    r"|\b(habe|hat|haben|wurde|wurden)\b[\wäöüß ]*?\b(ein|aus|an)geschaltet\b",
    re.IGNORECASE,
)

# The corrective nudge injected once per turn when a fabricated claim is caught:
# the model asserted an action it never dispatched — force the tool pass.
_CLAIM_CORRECTION = (
    "STOPP: Du hast eine Geräteaktion als erledigt behauptet, aber kein Tool"
    " aufgerufen. Rufe JETZT das passende Tool (ha_call_service) für diese"
    " Aktion auf. Behaupte nichts ohne Tool-Ergebnis."
)

# Tools whose result carries a ready-made `say` line: the wizard scripts the
# dialog, the model only reads it out. Their turns are kept OUT of later prompts
# (#1067) — they name the speaker ("… wurde als M-D-O-P-P erkannt"), land in the
# shared household session, and the model then imitates them forever. Distinct
# from the enrolment tool *allowlist* below, which also carries a read-only tool.
_ENROLLMENT_SAY_TOOLS = frozenset(
    {
        "start_voice_enrollment",
        "register_pending_resident",
        "start_wakeword_enrollment",
        "record_wakeword_sample",
        "trigger_wakeword_training",
        "audit_wakeword_samples",
        "delete_wakeword_sample",
    }
)


def _is_fabricated_device_claim(content: str) -> bool:
    return bool(_DEVICE_CLAIM.search(content or ""))


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """The latest user turn's text — drives the state-scoped card filter (#536)."""
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def _gist(content: str) -> str:
    """A 1-line, length-capped gist of a verbose tool result (#623).

    Whitespace-collapsed and truncated to `_GIST_CHARS` so a past turn's full
    JSON shrinks to a readable summary that still carries the salient value
    (a follow-up referencing it can resolve). Never empty for non-empty input."""
    flat = " ".join((content or "").split())
    if len(flat) > _GIST_CHARS:
        return flat[:_GIST_CHARS].rstrip() + "…"
    return flat


def compact_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact the verbose tool args/results of PRIOR turns (#623).

    The small household model carries every past turn's full MCP tool params +
    JSON results — bloat that grows the prefill. This pass keeps the user
    question and the assistant answer TEXT verbatim, but for messages BEFORE the
    current turn (everything up to the LAST user message):
      - a tool-role message's full JSON `content` becomes `[tool <name>] <gist>`
        (tool name + a 1-line, length-capped gist — salient, never empty).
    Past assistant `tool_calls` are kept INTACT (args are small; the bloat is the
    verbose RESULTS, gisted above). They MUST NOT be reduced to empty-args calls:
    a small model imitates an `arguments: {}` pattern in its context and then
    emits argument-less tool calls itself ("invalid domain or service name").
    The CURRENT turn (the last user message and everything after it — this
    turn's fresh tool calls and their results) is left FULL, since the model
    needs those to answer now. Pure: returns a new list, never mutates input."""
    boundary = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            boundary = i
            break

    # Tool names for the result rows: the store's reloaded history drops
    # `tool_name` (it persists only role/content/tool_calls), so recover the
    # name from each preceding assistant tool_calls — the i-th tool result
    # answers the i-th call in the run's emission order.
    out: list[dict[str, Any]] = []
    pending_names: list[str] = []
    for i, m in enumerate(messages):
        if i >= boundary:
            out.append(m)
            continue
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            pending_names = [
                str((tc.get("function") or {}).get("name") or "")
                for tc in m["tool_calls"]
            ]
        if role == "tool":
            name = str(m.get("tool_name") or "") or (
                pending_names.pop(0) if pending_names else ""
            )
            new = dict(m)
            label = f"[tool {name}] " if name else "[tool] "
            new["content"] = f"{label}{_gist(str(m.get('content') or ''))}"
            out.append(new)
        else:
            out.append(m)
    return out


# A trailing `FOLLOWUPS: a | b | c` line the SOUL invites the model to emit
# (#498): tappable follow-up question chips. Parsed off the answer's tail so
# the chips ride a per-turn event and the marker never shows in the bubble.
_FOLLOWUPS = re.compile(r"\n*FOLLOWUPS:[ \t]*(.+?)[ \t]*$", re.IGNORECASE)


def _split_followups(content: str) -> tuple[str, list[str]]:
    """Strip a trailing FOLLOWUPS line off `content`, returning (answer, chips).

    Up to three non-empty chips; no marker → the answer unchanged and []."""
    m = _FOLLOWUPS.search(content or "")
    if not m:
        return content, []
    chips = [c.strip() for c in m.group(1).split("|")]
    chips = [c for c in chips if c][:3]
    return content[: m.start()].rstrip(), chips


# A trailing `ANCHORS: @person | #topic | #place` line the SOUL invites the
# model to emit (#501): 0-3 salient anchors for what the turn is about. Parsed
# off the answer's tail like FOLLOWUPS so the chips ride a per-turn event and
# the marker never shows in the bubble.
_ANCHORS = re.compile(r"\n*ANCHORS:[ \t]*(.+?)[ \t]*$", re.IGNORECASE)


def _split_anchors(content: str) -> tuple[str, list[str]]:
    """Strip a trailing ANCHORS line off `content`, returning (answer, anchors).

    Each anchor keeps its `#`/`@` prefix; up to three valid ones (a bare token
    with no prefix is dropped). No marker → the answer unchanged and []."""
    m = _ANCHORS.search(content or "")
    if not m:
        return content, []
    anchors = [a.strip() for a in m.group(1).split("|")]
    anchors = [a for a in anchors if a[:1] in ("#", "@") and a[1:]][:3]
    return content[: m.start()].rstrip(), anchors


class EngineError(Exception):
    """Raised when a turn cannot run (DB/model failures). Name-compatible
    handling: server catches HermesError OR EngineError."""


@dataclass
class EngineProfile:
    """What used to be a Hermes gateway profile."""

    name: str
    model: str
    soul_path: str
    # An optional per-turn model override (#366): when set, its return value
    # (if non-empty) is the model for the next turn, so an admin can re-point
    # the household profile from the panel without a restart. `model` is the
    # static fallback (the configured default).
    model_resolver: Callable[[], str] | None = None
    extra_prompt: str = ""
    registry: EntityRegistry | None = None
    think_default: bool = False
    # The shared household uid (and HA's fallback `user`): a turn carrying this
    # uid is NOT personal, so no resident identity block is injected (#352).
    default_uid: str = "household"
    # Sampling override; None keeps the model's default. The household hot
    # path runs low temperature: at the modelfile default of 1.0 e2b
    # occasionally narrates a device action instead of calling the tool, and
    # one such reply in HA's history self-reinforces (box A/B 2026-06-12).
    temperature: float | None = None
    toolbox: Toolbox = field(default_factory=lambda: Toolbox([]))
    # Guest profile (#353): a turn runs statelessly — nothing is written to the
    # store, so no guest session, history or fact survives the conversation.
    ephemeral: bool = False


class EngineClient:
    def __init__(
        self,
        profile: EngineProfile,
        *,
        db_path: str,
        ollama: OllamaChat,
        recorder: TraceRecorder,
        context_window: int | None = None,
        bus: SessionBus | None = None,
    ):
        self._profile = profile
        self._db_path = db_path
        self._ollama = ollama
        self._recorder = recorder
        self._context_window = context_window
        self._bus = bus
        self._soul_cache: tuple[float, str] = (0.0, "")
        # Per-session stash of a sensitive action held for ja/nein confirmation
        # (#570). In-memory on the client (one per profile) — survives the turn
        # boundary for both the durable session and the stateless facade source.
        self._pending = confirm.PendingStore()

    @property
    def recorder(self) -> TraceRecorder:
        return self._recorder

    def has_pending_confirmation(self, key: str | None) -> bool:
        """True when a sensitive action is held awaiting this caller's yes/no.

        The voice path needs it to tell a decline that ENDS the conversation
        ("kann ich sonst noch helfen?" — "nein") from one that ANSWERS a held
        action ("soll ich das Licht ausschalten?" — "nein"), which must still
        reach the gate."""
        return key is not None and self._pending.peek(key) is not None

    @property
    def profile_name(self) -> str:
        return self._profile.name

    def _model(self) -> str:
        """The model for this turn: the profile's resolver override (#366) if it
        yields a non-empty tag, else the static `profile.model` default."""
        resolver = self._profile.model_resolver
        return (resolver() if resolver else "") or self._profile.model

    @property
    def ephemeral(self) -> bool:
        return self._profile.ephemeral

    # -- session surface (HermesClient-compatible) --------------------------

    async def create_session(
        self,
        uid: str,
        system_prompt: str | None = None,
        *,
        maintenance: bool = False,
        ephemeral: bool = False,
        model: str = "",
        title: str = "",
    ) -> str:
        session_id = store.create_session(
            self._db_path,
            uid,
            title=title,
            profile=self._profile.name,
            ephemeral=ephemeral,
            maintenance=maintenance,
        )
        if system_prompt:
            store.set_overlay(self._db_path, session_id, system_prompt)
        return session_id

    async def delete_session(self, session_id: str, uid: str) -> bool:
        return store.delete_session(self._db_path, session_id, uid)

    async def list_sessions(self, uid: str) -> list[dict[str, Any]]:
        return store.list_sessions(self._db_path, uid)

    async def get_session(self, session_id: str, uid: str) -> dict[str, Any] | None:
        return store.get_session(self._db_path, session_id, uid)

    async def set_title(self, session_id: str, uid: str, title: str) -> None:
        store.set_title(self._db_path, session_id, uid, title)

    async def list_toolsets(self) -> list[dict[str, Any]]:
        return [
            {
                "name": self._profile.name,
                "label": f"Solaris Engine · {self._profile.name}",
                "description": f"model={self._model()}",
                "enabled": True,
                "configured": True,
                "tools": self._profile.toolbox.names(),
            }
        ]

    async def dispatch_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one tool directly on this gateway's toolbox (the start page's
        favorite-run path, #646). Bypasses the agent loop and its confirm gate,
        so the caller must re-check confirm.is_sensitive itself."""
        await self._profile.toolbox.prepare()
        return await self._profile.toolbox.dispatch(name, arguments)

    # -- turns ---------------------------------------------------------------

    async def chat(
        self,
        session_id: str,
        text: str,
        images: list[str] | None = None,
        reasoning_effort: str = "none",
        turn_uid: str = "",
        model_override: str = "",
        conversation_id: str | None = None,
    ) -> str:
        """One turn, non-streamed: drain the stream, return the final answer.

        `model_override` runs this turn on a specific model tag instead of the
        profile's default (the document extractor uses a stronger model than the
        librarian's default, #doc)."""
        answer = ""
        async for event in self.chat_stream(
            session_id,
            text,
            images,
            reasoning_effort,
            turn_uid=turn_uid,
            model_override=model_override,
            conversation_id=conversation_id,
        ):
            if event["type"] == "run.completed":
                for msg in event["data"].get("messages", []):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        answer = str(msg["content"])
        return answer

    async def chat_stream(
        self,
        session_id: str,
        text: str,
        images: list[str] | None = None,
        reasoning_effort: str = "none",
        suggest_answers: bool = False,
        turn_uid: str = "",
        model_override: str = "",
        conversation_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        owner = store.session_owner(self._db_path, session_id)
        if owner is None:
            raise EngineError(f"unknown session: {session_id}")
        # The identity of THIS turn's caller (#649): for the shared household
        # session the owner is `household`, but a typed turn carries the real
        # resident, so timers/facts and the identity block stay theirs. Absent
        # (voice/facade) it falls back to the owner — the correct household id.
        token = current_uid.set(turn_uid or owner)
        try:
            async for event in self._run_turn(
                session_id,
                text,
                images,
                reasoning_effort,
                suggest_answers,
                turn_uid=turn_uid,
                model_override=model_override,
                conversation_id=conversation_id,
            ):
                yield event
        except OllamaError as e:
            log.error("engine.turn.failed", session_id=session_id, error=str(e))
            raise EngineError(str(e)) from e
        finally:
            # The SSE heartbeat consumes each generator step in its own task,
            # so this finally can run in a foreign context (box-observed:
            # ValueError tore down the stream as a Network error).
            try:
                current_uid.reset(token)
            except ValueError:
                pass

    async def _run_turn(
        self,
        session_id: str,
        text: str,
        images: list[str] | None,
        reasoning_effort: str,
        suggest_answers: bool = False,
        turn_uid: str = "",
        model_override: str = "",
        conversation_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        owner = store.session_owner(self._db_path, session_id) or ""
        # Resolve BEFORE the user row lands, or the idle gap is measured against
        # the row we are about to write.
        conversation = store.resolve_conversation_id(
            self._db_path, session_id, requested=conversation_id
        )
        store.append_message(
            self._db_path,
            session_id,
            "user",
            text,
            images=images or None,
            conversation_id=conversation,
        )
        # Bound the durable household chat in place: it is never forked (#419),
        # so when its history outgrows the window the oldest turns are dropped
        # (#420). Soul + device registry are the per-turn system prompt below —
        # never touched; only chat turns are cut. Other sessions are bounded by
        # continuation-compaction (server.maybe_compact), not here.
        scoped = bool(owner) and session_id == store.household_session_id(owner)
        if scoped:
            store.truncate_session_head(
                self._db_path, session_id, int((self._context_window or 32768) * 0.4)
            )
        system = await self._system_prompt(session_id)
        messages = [{"role": "system", "content": system}]
        # The shared household row carries every past conversation, so the prompt
        # takes only the current one (#1067). Forkable sessions keep full history.
        messages += (
            store.prompt_history(self._db_path, session_id, conversation)
            if scoped
            else store.history(self._db_path, session_id)
        )
        think = self._profile.think_default or reasoning_effort not in ("", "none")
        # Mirror the inbound transcript to this session's OTHER open tabs (#344)
        # before any token streams — a tab that didn't originate the turn (voice,
        # or another browser) renders the user bubble as soon as it lands.
        self._mirror(session_id, owner, "mirror_user", {"text": text})
        async for event in self._loop(
            messages,
            think=think,
            session_id=session_id,
            persist=True,
            uid=turn_uid or owner,
            suggest_answers=suggest_answers,
            model_override=model_override,
            conversation=conversation,
        ):
            self._mirror(session_id, owner, "mirror_event", event)
            yield event

    async def respond(
        self,
        messages: list[dict[str, Any]],
        *,
        uid: str = "",
        source: str = "assist",
        conversation_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stateless turn for the Ollama facade (HA Assist / gatekeeper).

        The caller owns the conversation history and resends it per turn;
        nothing persists to the store. Incoming system messages (HA's
        configurable prompt) are folded after the profile's own system block,
        and the wall-clock hint rides the last user message — same lever the
        session path uses, and prefix-cache-friendly (the stable soul+registry
        block stays byte-identical across turns).

        `conversation_id` (HA's per-turn id) keys the confirmation gate so a
        held action is confirmable only within the same conversation, never by a
        different caller on the same profile source (#570 F3). None when HA sent
        no id — the gate then re-gates every turn instead of stashing.
        """
        token = current_uid.set(uid)
        try:
            system = await self._system_prompt_stateless()
            incoming = [
                str(m.get("content") or "")
                for m in messages
                if m.get("role") == "system" and m.get("content")
            ]
            # Recency is load-bearing (box A/B): the tool-discipline rule must
            # be the LAST system content — after the caller's prompt, which
            # otherwise outweighs it again ("Antworte kurz" → narration).
            tail = [_TOOL_DISCIPLINE] if self._profile.toolbox.names() else []
            msgs: list[dict[str, Any]] = [
                {"role": "system", "content": "\n\n".join([system, *incoming, *tail])}
            ]
            msgs += [dict(m) for m in messages if m.get("role") != "system"]
            for m in reversed(msgs):
                if m.get("role") == "user":
                    m["content"] = f"{_now_hint()}\n\n{m.get('content') or ''}"
                    break
            async for event in self._loop(
                msgs,
                think=self._profile.think_default,
                session_id=source,
                persist=False,
                uid=uid,
                pending_key=conversation_id,
            ):
                yield event
        except OllamaError as e:
            log.error("engine.respond.failed", source=source, error=str(e))
            raise EngineError(str(e)) from e
        finally:
            # A client that drops the stream closes this generator from a
            # different asyncio context — the reset token is then foreign
            # (box-observed ValueError on an aborted HA turn).
            try:
                current_uid.reset(token)
            except ValueError:
                pass

    async def respond_session(
        self,
        text: str,
        *,
        uid: str,
        conversation_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """A voice turn into the resident's durable household session (#345).

        Where `respond` is stateless (HA owns the history), this persists into
        the shared household session — the same row the browser opens — so
        spoken and typed history are one conversation. HA still resends its
        full message list, but the store is now the source of truth, so only
        the latest user `text` is run; the soul/registry block is the session's
        own (the caller's per-call system prompt is dropped — the durable
        session already carries the engine's identity)."""
        session_id = store.ensure_household_session(self._db_path, uid)
        # The wall-clock hint rides the user turn (the session path has no
        # topic-hint wrapper) — same lever the browser turns get server-side.
        turn = f"{_now_hint()}\n\n{text}" if text else text
        async for event in self.chat_stream(
            session_id, turn, conversation_id=conversation_id
        ):
            yield event

    def _count_usage(self, uid: str, name: str, args: Any, output: str) -> None:
        """Increment the "häufig genutzt" counter for one executed pinnable tool
        (#645). Skips ephemeral (guest) turns, non-pinnable tools and failed
        calls; a usage-write hiccup must never break the turn."""
        if (
            self._profile.ephemeral
            or name not in PINNABLE_TOOLS
            or not isinstance(args, dict)
            or output.startswith('{"error"')
        ):
            return
        try:
            favorites_store.record_usage(
                self._db_path, uid or self._profile.default_uid, name, args
            )
        except Exception:  # noqa: BLE001 — a usage hiccup must not kill the turn
            pass

    async def _gate_sensitive(
        self,
        args: dict[str, Any],
        confirmed: set[tuple[str, str, str]],
        session_id: str | None,
        quick_replies: list[str],
    ) -> str | None:
        """Hold a sensitive, unconfirmed ha_call_service (#570).

        Returns a needs_confirmation tool-result string (and, when a per-
        conversation `session_id` is available, stashes the pending action +
        fills the ja/nein chips) when the call must be confirmed first; returns
        None to let dispatch run normally (routine, or just-confirmed). With no
        per-conversation key (ephemeral, no conversation_id) it still gates but
        does not stash — every turn re-gates rather than risk a cross-caller
        confirmation (F3)."""
        domain = str(args.get("domain") or "")
        service = str(args.get("service") or "")
        entity_id = str(args.get("entity_id") or "")
        # The gate keys on what the action would do to the REAL entity, not the
        # model's supplied domain: e4b mis-routes a garage open as the wrong
        # domain (e.g. light.open_cover) and the model's domain string can't be
        # trusted to classify sensitivity (#632). The entity_id carries the true
        # domain, so classify on that; fall back to the model's domain only when
        # no entity_id was given.
        gate_domain = entity_id.split(".", 1)[0] if "." in entity_id else domain
        # Normalise the natural verb the same way call_service does
        # (cover "open" -> "open_cover"), so the classification sees the real
        # service name — keyed on the true domain.
        service = ha_tools._SERVICE_ALIASES.get(gate_domain, {}).get(service, service)
        # A cover's danger is class-specific: resolve the entity's device_class
        # so a garage/door/gate is gated but a blind is not (F1).
        device_class: str | None = None
        if gate_domain == "cover" and self._profile.registry is not None:
            device_class = await self._profile.registry.device_class(entity_id)
        if not confirm.is_sensitive(gate_domain, service, device_class):
            return None
        if (domain, service, entity_id) in confirmed:
            return None
        data = args.get("data") if isinstance(args.get("data"), dict) else None
        prompt = confirm.confirm_prompt(domain, service, entity_id)
        # Only stash when we have a per-conversation key (F3): without one a held
        # action could be confirmed by a different caller, so re-gate next turn.
        if session_id:
            self._pending.stash(
                session_id,
                confirm.PendingAction(
                    domain=domain,
                    service=service,
                    entity_id=entity_id,
                    data=data,
                    prompt=prompt,
                ),
            )
        quick_replies.clear()
        quick_replies.extend(["ja", "nein"])
        return json.dumps(
            {"ok": False, "needs_confirmation": True, "prompt": prompt},
            ensure_ascii=False,
        )

    async def _suggest_answers(self, question: str) -> list[str]:
        """Generate 2-4 likely user answers to a question (u87 chat fallback).

        A tight, non-thinking secondary completion: the model returns a JSON
        array of short answers, most-likely first. Fail-open — any error (bad
        JSON, model down) returns []. Cleaned with the same rules as
        offer_choices so the chips render identically."""
        sys_prompt = (
            "Gib 2-4 sehr kurze, wahrscheinliche Antworten des Nutzers auf die"
            " folgende Frage als JSON-Array zurück, die wahrscheinlichste zuerst."
            " Nur das JSON."
        )
        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": question},
        ]
        try:
            result = None
            async for kind, payload in self._ollama.stream(
                self._model(),
                msgs,
                tools=None,
                think=False,
                options={"num_predict": 64, "temperature": 0.0},
            ):
                if kind == "done":
                    result = payload
            if result is None:
                return []
            raw = result.content.strip()
            start, end = raw.find("["), raw.rfind("]")
            if start == -1 or end == -1 or end < start:
                return []
            parsed = json.loads(raw[start : end + 1])
        except (OllamaError, ValueError, TypeError):
            return []
        return choice_tools._clean(parsed)

    async def _loop(
        self,
        messages: list[dict[str, Any]],
        *,
        think: bool,
        session_id: str,
        persist: bool,
        uid: str = "",
        pending_key: str | None | _Unset = _UNSET,
        suggest_answers: bool = False,
        model_override: str = "",
        conversation: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        """The agent loop: stream, dispatch tools, feed results back, repeat.

        `persist=False` runs the identical loop without store writes (the
        stateless facade path); traces record either way — session turns under
        their session id, stateless ones under the source label.

        `pending_key` keys the confirmation gate's stash per CONVERSATION, not
        per session/source (F3): the durable path leaves it unset (its session
        id is the conversation), the stateless facade path passes HA's
        conversation_id — or None when absent, which disables stashing so a
        per-profile source can never be a cross-caller confirmation key.
        """
        if isinstance(pending_key, _Unset):
            pending_key = session_id
        # The acting admin's Authelia identity (#794), captured in THIS task
        # (the server handler set it before dispatch) so it can be re-pinned in
        # the gather child task below — prepare() already runs in this task.
        admin_identity = current_admin_identity.get()
        # The turn's surface + speaker-ID outcome (#1130), captured here for the
        # same reason: the visibility gate runs inside the gather child task.
        channel = current_channel.get()
        speaker_matched = current_speaker_matched.get()
        await self._profile.toolbox.prepare()
        tools = self._profile.toolbox.definitions()

        try:
            from solaris_chat import enroll_requests_store, wakeword_requests_store

            _enrollment_active = enroll_requests_store.has_any_active_request(
                self._db_path
            ) or wakeword_requests_store.has_any_active_request(self._db_path)
            if _enrollment_active:
                _enrollment_tools = {
                    "start_voice_enrollment",
                    "register_pending_resident",
                    "start_wakeword_enrollment",
                    "record_wakeword_sample",
                    "trigger_wakeword_training",
                    "audit_wakeword_samples",
                    "list_wakeword_samples",
                    "delete_wakeword_sample",
                }
                tools = [
                    t
                    for t in tools
                    if (t.get("function") or {}).get("name") in _enrollment_tools
                ]
        except Exception:
            pass
        # Per-turn sink the HA state tools fill with read-only card-specs (#475);
        # drained into a `ha_cards` event at turn end.
        ha_cards: list[dict[str, Any]] = []
        ha_tools.card_sink.set(ha_cards)
        # Per-turn sink the offer_choices tool fills with quick-reply options
        # (#555); drained into a `quick_replies` event at turn end like ha_cards.
        quick_replies: list[str] = []
        choice_tools.choice_sink.set(quick_replies)
        options = (
            {"temperature": self._profile.temperature}
            if self._profile.temperature is not None
            else None
        )

        # Deterministic confirmation gate (#570): if this conversation holds a
        # sensitive action from a prior turn, the current user reply decides its
        # fate before the model runs. A CLEAR affirmative executes the held
        # action now (then the model reports the result from the tool message); a
        # clear negative drops it (negative wins on a tie). ANYTHING ELSE — a
        # fresh request with no yes/no — DROPS the pending (does NOT execute, F2)
        # and lets the new request process normally, re-gating if it is itself
        # sensitive. `confirmed` carries the just-confirmed target so the gate
        # below doesn't re-hold the very action we are now executing.
        confirmed: set[tuple[str, str, str]] = set()
        confirmed_executed = False
        pending = self._pending.peek(pending_key) if pending_key else None
        if pending is not None:
            reply = _last_user_text(messages)
            if confirm.is_negative(reply):
                self._pending.take(pending_key)
            elif not confirm.is_affirmative(reply):
                # A new request, not a yes — abandon the stale pending and let
                # the turn run; never execute the old action on ambiguity.
                self._pending.take(pending_key)
            else:
                self._pending.take(pending_key)
                confirmed.add((pending.domain, pending.service, pending.entity_id))
                tc = {
                    "function": {"name": "ha_call_service", "arguments": pending.args()}
                }
                yield {"type": "tool.started", "data": {"tool": "ha_call_service"}}
                if uid:
                    current_uid.set(uid)
                current_session.set(session_id)
                t0 = time.monotonic()
                output = await self._profile.toolbox.dispatch(
                    "ha_call_service", pending.args()
                )
                tool_wall_s = time.monotonic() - t0
                self._recorder.record_tool(
                    session_id=session_id,
                    profile=self._profile.name,
                    tool_name="ha_call_service",
                    wall_s=tool_wall_s,
                    arguments=pending.args(),
                    output=output,
                )
                self._count_usage(uid, "ha_call_service", pending.args(), output)
                yield {
                    "type": "tool.completed",
                    "data": {"tool": "ha_call_service", "wall_s": tool_wall_s},
                }
                if persist:
                    store.append_message(
                        self._db_path,
                        session_id,
                        "assistant",
                        "",
                        tool_calls=[tc],
                        conversation_id=conversation,
                    )
                    store.append_message(
                        self._db_path,
                        session_id,
                        "tool",
                        output,
                        conversation_id=conversation,
                    )
                messages.append(
                    {"role": "assistant", "content": "", "tool_calls": [tc]}
                )
                messages.append(
                    {"role": "tool", "content": output, "tool_name": "ha_call_service"}
                )
                confirmed_executed = True

        has_tools = bool(self._profile.toolbox.names())
        # A confirmed action already ran a tool this turn, so the model's report
        # ("Garagentor ist offen") is grounded, not a fabrication (#570/#356).
        tool_dispatched = confirmed_executed
        # Whether the model itself stored the fact this turn (#621): if the user
        # said "merk dir …" and no store tool ran, the loop enforces it at turn
        # end. e4b obeys the SOUL only sometimes, so we detect the gap and fill it.
        stored_fact = False
        corrected = False
        short_circuited = False
        final_content = ""
        final_thinking = ""
        model = model_override or self._model()
        for _ in range(_MAX_PASSES):
            result = None
            # Trim the verbose tool args/results of PRIOR turns before the model
            # sees them (#623) — this turn's own tool results stay full. Recompute
            # each pass: a freshly appended tool result is "current" until the
            # next user turn, so the boundary is always the last user message.
            sent = compact_history(messages)
            async for kind, payload in self._ollama.stream(
                model, sent, tools=tools, think=think, options=options
            ):
                if kind == "delta":
                    yield {"type": "assistant.delta", "data": {"delta": payload}}
                elif kind == "done":
                    result = payload
            assert result is not None
            self._recorder.record(
                session_id=session_id,
                profile=self._profile.name,
                model=model,
                messages=sent,
                tools=tools,
                content=result.content,
                thinking=result.thinking,
                tool_calls=result.tool_calls,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                wall_s=result.wall_s,
                context_window=self._context_window,
            )
            if persist:
                store.add_usage(
                    self._db_path,
                    session_id,
                    result.prompt_tokens,
                    result.completion_tokens,
                )
            final_thinking = result.thinking or final_thinking
            yield {
                "type": "llm.step",
                "data": {"model": model, "wall_s": result.wall_s},
            }

            if not result.tool_calls:
                # Fabrication guard (#356): the model claims a device action is
                # done but dispatched no tool this turn. Re-prompt once to force
                # the tool pass instead of accepting the fabricated success.
                if (
                    has_tools
                    and not tool_dispatched
                    and not corrected
                    and _is_fabricated_device_claim(result.content)
                ):
                    corrected = True
                    messages.append({"role": "assistant", "content": result.content})
                    messages.append({"role": "system", "content": _CLAIM_CORRECTION})
                    continue
                final_content = result.content
                break

            # Tool pass: persist the call, dispatch, feed results back. An
            # enrolment pass is hidden from later prompts as a WHOLE (call row +
            # every result row): a prompt must never hold a tool_calls row whose
            # results are missing, or a result with no preceding call.
            pass_hidden = any(
                str((tc.get("function") or {}).get("name") or "")
                in _ENROLLMENT_SAY_TOOLS
                for tc in result.tool_calls
            )
            if persist:
                store.append_message(
                    self._db_path,
                    session_id,
                    "assistant",
                    result.content,
                    tool_calls=result.tool_calls,
                    conversation_id=conversation,
                    in_prompt=not pass_hidden,
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": result.content,
                    "tool_calls": result.tool_calls,
                }
            )
            # GATE-CHECK PASS (#624/#570): classify every call FIRST, before any
            # dispatch, so a sensitive ha_call_service can never slip past the
            # gate via parallelism. A gated call is held (stash + ja/nein chips +
            # needs_confirmation result) and NOT run; the rest are dispatched
            # concurrently below. The gate runs serially here — it is cheap, and
            # serial gating is what guarantees two sensitive calls don't both
            # stash or race.
            dispatch_plan: list[dict[str, Any]] = []
            for tc in result.tool_calls:
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                tool_dispatched = True
                if name in ("fact_store", "note_write"):
                    stored_fact = True
                held: str | None = None
                if name == "ha_call_service" and isinstance(args, dict):
                    held = await self._gate_sensitive(
                        args, confirmed, pending_key, quick_replies
                    )
                dispatch_plan.append({"name": name, "args": args, "held": held})

            # Emit tool.started for every call in emitted order.
            for item in dispatch_plan:
                yield {"type": "tool.started", "data": {"tool": item["name"]}}

            # CONCURRENT DISPATCH: run the non-gated calls together under a
            # bounded semaphore (gather preserves order). Per-tool errors are
            # isolated by Toolbox.dispatch (it returns an error JSON, never
            # raises), so one failure can't abort the batch. A gated call is not
            # dispatched — its needs_confirmation result is already its output.
            sem = asyncio.Semaphore(_MAX_PARALLEL_TOOLS)

            async def _run(name: str, args: Any) -> tuple[str, float]:
                # Re-pin the turn's resident + sinks IN this dispatch task: the
                # SSE heartbeat runs each generator step (and thus gather) in a
                # fresh task that doesn't inherit the turn's set() — tools would
                # otherwise see the default uid (timers/facts written ownerless)
                # and card reads (#475) would land nowhere.
                if uid:
                    current_uid.set(uid)
                current_session.set(session_id)
                # Re-pin the acting admin's Authelia identity (#794) too: the
                # SB-MCP toolbox reads it from this task to exchange for a
                # short-lived token, so it must survive the gather task hop.
                current_admin_identity.set(admin_identity)
                current_channel.set(channel)
                current_speaker_matched.set(speaker_matched)
                ha_tools.card_sink.set(ha_cards)
                choice_tools.choice_sink.set(quick_replies)
                async with sem:
                    t0 = time.monotonic()
                    out = await self._profile.toolbox.dispatch(name, args)
                    return out, time.monotonic() - t0

            outputs = await asyncio.gather(
                *(
                    _run(item["name"], item["args"])
                    for item in dispatch_plan
                    if item["held"] is None
                )
            )

            # Stitch results back in EMITTED ORDER (gated -> its held result,
            # non-gated -> the next gathered output), emitting tool.completed
            # and appending the tool message per call in order.
            next_output = iter(outputs)
            for item in dispatch_plan:
                name = item["name"]
                if item["held"] is not None:
                    output = item["held"]
                    tool_wall_s = 0.0
                else:
                    output, tool_wall_s = next(next_output)
                    self._recorder.record_tool(
                        session_id=session_id,
                        profile=self._profile.name,
                        tool_name=name,
                        wall_s=tool_wall_s,
                        arguments=item["args"]
                        if isinstance(item["args"], dict)
                        else None,
                        output=output,
                    )
                    self._count_usage(uid, name, item["args"], output)
                yield {
                    "type": "tool.completed",
                    "data": {"tool": name, "wall_s": tool_wall_s},
                }
                if persist:
                    store.append_message(
                        self._db_path,
                        session_id,
                        "tool",
                        output,
                        conversation_id=conversation,
                        in_prompt=not pass_hidden,
                    )
                messages.append({"role": "tool", "content": output, "tool_name": name})
                # SHORT-CIRCUIT: if the tool returned a 'say' field, emit it directly
                # without a second LLM pass. This prevents 20-30s model stalls during
                # voice enrollment and wakeword dialogs (#1056).
                try:
                    import json as _json

                    _payload = _json.loads(output)
                    _say = _payload.get("say") if isinstance(_payload, dict) else None
                    if _say and name in _ENROLLMENT_SAY_TOOLS:
                        # The generic persist below writes this same text in the
                        # same position — writing it here too produced two
                        # identical assistant rows per enrolment turn.
                        final_content = _say
                        short_circuited = True
                        break
                except Exception:
                    pass
            if short_circuited:
                break
        else:
            # Pass budget exhausted mid-tool-chain: surface what we have.
            final_content = (
                final_content
                or "Entschuldige, das hat zu viele Schritte gebraucht — ich breche hier ab."
            )

        # Code-enforced remember-this (#621): the SOUL asks e4b to store a
        # "merk dir …" via fact_store/note_write, but it confirms conversationally
        # and skips the tool about as often as it obeys. When the user's turn was
        # an explicit remember directive and no store tool ran, persist the fact
        # here — scoped to the speaker via the turn's pinned uid — so the second
        # brain actually remembers. Mirrors the confirm-gate enforcement style.
        if has_tools and not stored_fact:
            fact = remember.wants_remember(_last_user_text(messages))
            if fact and "fact_store" in self._profile.toolbox.names():
                if uid:
                    current_uid.set(uid)
                await self._profile.toolbox.dispatch("fact_store", {"fact": fact})

        final_content, anchors = _split_anchors(final_content)
        final_content, suggestions = _split_followups(final_content)
        if persist:
            store.append_message(
                self._db_path,
                session_id,
                "assistant",
                final_content,
                reasoning=final_thinking,
                conversation_id=conversation,
                in_prompt=not short_circuited,
            )
        if ha_cards:
            ha_cards = ha_tools.filter_cards_by_query_state(
                ha_cards, _last_user_text(messages)
            )
        if ha_cards:
            grouped = False
            if self._profile.registry is not None:
                snap = await self._profile.registry.area_snapshot()
                grouped = ha_tools.group_cards_by_room(ha_cards, snap.entity_area)
            yield {"type": "ha_cards", "data": {"cards": ha_cards, "grouped": grouped}}
        # Reliability fallback (u87): gemma4:e4b calls offer_choices only
        # sometimes, so a question often shows no chips. When the answer is a
        # question and the model offered none, suggest 2-4 likely answers via a
        # cheap secondary call — CHAT ONLY (suggest_answers), never the voice
        # path, where it would add latency. Fail-open: any error leaves it empty.
        if (
            suggest_answers
            and not quick_replies
            and final_content.rstrip().endswith("?")
        ):
            quick_replies = await self._suggest_answers(final_content)
        if quick_replies:
            yield {"type": "quick_replies", "data": {"options": quick_replies}}
        if suggestions:
            yield {"type": "suggestions", "data": {"suggestions": suggestions}}
        if anchors:
            yield {"type": "anchors", "data": {"anchors": anchors}}
        yield {
            "type": "run.completed",
            "data": {
                "messages": [
                    {
                        "role": "assistant",
                        "content": final_content,
                        "reasoning_content": final_thinking,
                    }
                ]
            },
        }

    # -- prompt assembly -----------------------------------------------------

    async def _system_prompt(self, session_id: str) -> str:
        parts = [self._soul()]
        if self._profile.extra_prompt:
            parts.append(self._profile.extra_prompt)
        resident = identity_block(current_uid.get(), self._profile.default_uid)
        if resident:
            parts.append(resident)
        overlay = store.get_overlay(self._db_path, session_id)
        if overlay:
            parts.append(overlay)
        if self._profile.registry is not None:
            block = await self._profile.registry.prompt_block()
            if block:
                parts.append(block)
        if self._profile.toolbox.names():
            parts.append(_TOOL_DISCIPLINE)
        return "\n\n".join(p for p in parts if p.strip())

    async def _system_prompt_stateless(self) -> str:
        """Profile prompt without a session overlay (the facade path). The
        tool-discipline tail is appended by respond() AFTER the caller's
        system prompt — recency is load-bearing."""
        parts = [self._soul()]
        if self._profile.extra_prompt:
            parts.append(self._profile.extra_prompt)
        resident = identity_block(current_uid.get(), self._profile.default_uid)
        if resident:
            parts.append(resident)
        if self._profile.registry is not None:
            block = await self._profile.registry.prompt_block()
            if block:
                parts.append(block)
        return "\n\n".join(p for p in parts if p.strip())

    def _mirror(
        self, session_id: str, uid: str, kind: str, event: dict[str, Any]
    ) -> None:
        """Publish one turn event to this session's other open tabs (#344).

        No-op without a bus (offline tests) or an owner. The originating request
        keeps its own direct stream; subscribers are every OTHER open client of
        the same (session, uid)."""
        if self._bus is not None and uid:
            self._bus.publish(session_id, uid, {"kind": kind, "event": event})

    def _soul(self) -> str:
        """SOUL.md, mtime-cached — an edit lands on the next turn, no restart."""
        path = Path(self._profile.soul_path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        if mtime != self._soul_cache[0]:
            self._soul_cache = (
                mtime,
                path.read_text(encoding="utf-8", errors="replace"),
            )
        return self._soul_cache[1]
