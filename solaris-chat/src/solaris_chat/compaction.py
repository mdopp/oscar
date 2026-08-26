"""Chat compaction: extract durable learnings, then compact (#210).

A long chat grows past the model's context window and buries durable knowledge
in transcript. Compaction frees the window *without* losing what matters, in a
strict order: **extract first, compact second** — so nothing durable is dropped
even if the later step fails.

Two triggers feed the same backend:
  - **Hard cap** — a turn finds the session's running token usage near the
    context-window cap; we compact before the next turn so it never truncates
    mid-conversation (wired in `server.py`).
  - **Overnight cron** — a Hermes job (the `chat-compactor` skill, registered in
    `solarisbay`'s post-deploy) compacts stale chats unattended.

Hermes-capability facts this design is built on (read from the Hermes source +
our client, NOT box-verified here — see the verify checklist in the unit notes):

  1. **Per-session token usage is measurable.** the engine's single-session fetch
     (`GET /api/sessions/{id}`) carries `input_tokens`/`output_tokens` running
     totals (`engine._session_summary` already surfaces them). `usage_fraction`
     reads those against the configured context window — so the hard-cap trigger
     has a real signal. (ASSUMPTION to box-verify: that these totals are the
     prompt-token running total, not just the last turn.)

  2. **There is NO in-place history truncate/replace API.** `create_session`
     accepts a `system_prompt` only at create time; `PATCH` sets only the title;
     there is no endpoint to drop or rewrite a session's messages. So compaction
     v1 is **continuation, not in-place rewrite**: we summarise the old session
     and open a *fresh* session seeded with `{summary + a pointer to the extracted
     learnings}` as its system prompt. The original session is **kept** (never
     deleted) — the transcript stays as the durable record; the resident simply
     continues in the small-context continuation.

  3. **Durable learnings go through the active memory provider's tools, not an
     HTTP store.** The holographic provider exposes `fact_store`/`fact_feedback`
     as *agent* tools (the model calls them mid-turn); there is no proxy-callable
     memory endpoint. So the extraction pass is itself an LLM turn whose prompt
     instructs the agent to call `fact_store` for each durable learning. That
     reuses the existing memory mechanism — we invent no new store.

So both passes (extract, summarise) are ordinary engine chat turns on the
*source* session, run before the continuation is opened.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from solaris_chat.logging import log

if TYPE_CHECKING:
    from solaris_chat.engine.client import EngineClient

# Default fraction of the context window at which a session is compacted. The
# issue asks for ~90-95%; 0.90 leaves headroom so a turn never truncates while
# the compaction (itself two LLM turns on the old session) runs.
DEFAULT_THRESHOLD = 0.90

# Pass 1 — extract durable learnings into long-term memory FIRST. The agent has
# the holographic `fact_store` tool; this turn tells it to use it. Phrased so a
# quiet/short chat with nothing durable simply stores nothing (no fabrication).
#
# Deliberately does NOT list "device/room/entity mappings" as a target (#250):
# those are derivable from Home Assistant's own registry and pure device-control
# turns ("schalte Bürolicht an") are not durable learnings — memorising them
# pollutes memory and duplicates state that belongs in HA. The instruction below
# names device-control/tool-call turns as explicit skip cases.
EXTRACT_PROMPT = (
    "[system: memory extraction before compaction] Review THIS conversation and "
    "store only genuinely durable, reusable knowledge worth keeping beyond this "
    "chat — facts, decisions, household preferences, people, recurring routines. "
    "Use your memory tool (fact_store) to save each one as a short standalone "
    "fact. SKIP turns that are pure device control, tool calls, or trivial "
    "confirmations (e.g. switching a light, asking which devices are on): those "
    "are not learnings. Do NOT memorise device/room/entity mappings or device "
    "state — those live in Home Assistant and must be read from it, not stored. "
    "Store nothing that is transient small-talk or already obvious; if there is "
    "nothing durable, store nothing. Do not summarise back to me — just save the "
    "facts. Tag each stored fact with a #topic and an @person mention where they "
    "apply, inline in the fact text. Reply only with how many facts you stored."
)

# The nightly Stenograph (#652) reuses the extraction pass on LIVE household
# sessions, but must not append to their durable history — so it runs the same
# instruction against a day's transcript INLINED into the prompt, in an
# ephemeral deep session owned by the source session's owner. This prefix frames
# the inlined slice; `EXTRACT_PROMPT` (minus its "THIS conversation" wording)
# supplies the store-only-durable rules that follow the transcript.
STENOGRAPH_PREFIX = (
    "[system: nightly memory extraction] Below is today's slice of a household "
    "conversation. Review it and store only genuinely durable, reusable knowledge "
    "worth keeping — facts, decisions, household preferences, people, recurring "
    "routines. Use your memory tool (fact_store) to save each one as a short "
    "standalone fact, tagged with a #topic and an @person mention where they "
    "apply. SKIP turns that are pure device control, tool calls, or trivial "
    "confirmations; do NOT memorise device/room/entity mappings or device state "
    "(those live in Home Assistant). Store nothing transient or already obvious; "
    "if there is nothing durable, store nothing. Do not summarise back to me — "
    "just save the facts. Reply only with how many facts you stored.\n\n"
    "--- today's conversation ---\n"
)

# Pass 2 — produce a compact summary of the conversation so far, to seed the
# continuation session. Kept short on purpose: the point is to free the window.
SUMMARY_PROMPT = (
    "[system: compaction summary] Summarise this entire conversation so far into "
    "a compact briefing that lets us continue seamlessly in a fresh session: the "
    "topic, what was decided or done, any open thread, and the user's current "
    "intent. Be terse and factual. Reply with only the summary."
)


def usage_fraction(session: dict[str, Any], context_window: int) -> float | None:
    """Fraction of the context window the session's running token usage occupies.

    Reads the engine's per-session `input_tokens`/`output_tokens` totals (present on
    the single-session fetch). Returns None when neither is available (e.g. a
    list item, or a Hermes build that omits them) — the caller must treat an
    unknown usage as "don't compact", never as 0 or as over-cap.
    """
    if context_window <= 0:
        return None
    inp = session.get("input_tokens")
    out = session.get("output_tokens")
    if inp is None and out is None:
        return None
    used = (inp or 0) + (out or 0)
    return used / context_window


def needs_compaction(
    session: dict[str, Any], context_window: int, threshold: float = DEFAULT_THRESHOLD
) -> bool:
    """True when the session's usage is at/over the compaction threshold.

    Unknown usage (no token totals) returns False — we never compact on a guess.
    """
    frac = usage_fraction(session, context_window)
    return frac is not None and frac >= threshold


def _continuation_prompt(base_system_prompt: str, summary: str) -> str:
    """Seed prompt for the continuation session: the original overlay (if any)
    plus the compacted summary and a pointer to the stored learnings.

    The continuation starts with a tiny context (this prompt) instead of the
    whole transcript — that is what frees the window. Durable learnings are not
    inlined here: they were already written to memory in pass 1 and are recalled
    on demand via the memory provider, so they survive compaction without
    re-inflating the prompt.
    """
    parts: list[str] = []
    if base_system_prompt.strip():
        parts.append(base_system_prompt.strip())
    parts.append(
        "[continued conversation] This continues an earlier chat that was "
        "compacted to free context. Durable facts from it were saved to your "
        "memory (recall them when relevant). Summary of the earlier chat:\n"
        + summary.strip()
    )
    return "\n\n".join(parts)


async def compact_session(
    engine: EngineClient,
    uid: str,
    session_id: str,
    *,
    base_system_prompt: str = "",
    context_window: int,
    threshold: float = DEFAULT_THRESHOLD,
    force: bool = False,
) -> str | None:
    """Extract-then-compact one session; return the continuation session id.

    Order (never lose data silently):
      1. Fetch the session; bail (return None) if it's gone or not over the
         threshold (unless `force`, used by the overnight cron which has already
         picked stale sessions).
      2. **Extract** durable learnings to memory (LLM turn calling `fact_store`).
      3. **Summarise** the conversation (LLM turn).
      4. Open a **continuation** session seeded with `{overlay + summary}` and
         return its id. The original session is left intact.

    Returns None (and changes nothing) when there is nothing to compact or the
    extract/summary turn fails — the caller keeps using the original session, so
    a failed compaction degrades to "no compaction", never to data loss.
    """
    session = await engine.get_session(session_id, uid)
    if session is None:
        return None
    if not force and not needs_compaction(session, context_window, threshold):
        return None

    # A confirmation the resident has not answered yet ("Soll ich die Haustür
    # wirklich aufschließen?") is lifted out of the gate for the duration (#1247)
    # and put back under whichever session the resident continues in. Otherwise
    # compaction's own two turns read as "not a yes" and drop it, and the
    # continuation starts with no stash — so the "ja" silently actuates nothing.
    # The deadline rides along, so this cannot outlive the window it was given.
    held = engine.detach_pending(session_id)
    resumes_in = session_id
    try:
        # Pass 1: extract durable learnings into memory BEFORE anything is dropped.
        try:
            await engine.chat(session_id, EXTRACT_PROMPT, None, "high")
        except Exception as e:  # noqa: BLE001 — a failed extract must abort compaction
            log.error(
                "chat.compaction.extract_failed", session_id=session_id, error=str(e)
            )
            return None

        # Pass 2: summarise the conversation to seed the continuation.
        try:
            summary = await engine.chat(session_id, SUMMARY_PROMPT, None, "none")
        except Exception as e:  # noqa: BLE001
            log.error(
                "chat.compaction.summary_failed", session_id=session_id, error=str(e)
            )
            return None
        if not summary.strip():
            log.warning("chat.compaction.empty_summary", session_id=session_id)
            return None

        # Open the continuation; the original session is untouched (kept as record).
        # The title carries a timestamp suffix so it never collides with an
        # abandoned bare-marker stub — the engine enforces title uniqueness and a
        # bare `[uid:...] ` marker would 400 against any stub already holding it
        # (#267).
        try:
            new_id = await engine.create_session(
                uid,
                _continuation_prompt(base_system_prompt, summary),
                title=f"Fortsetzung {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            )
        except Exception as e:  # noqa: BLE001
            log.error(
                "chat.compaction.create_failed", session_id=session_id, error=str(e)
            )
            return None

        log.info(
            "chat.compaction.compacted",
            uid=uid,
            session_id=session_id,
            continuation_id=new_id,
        )
        resumes_in = new_id
        return new_id
    finally:
        if held is not None:
            engine.attach_pending(resumes_in, held)
