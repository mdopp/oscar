"""Session + message store for the Solaris Engine (solaris.db).

Ownership is a plain `owner_uid` column — the Hermes-era `[uid:...]`
title-marker workaround is gone. Ephemeral chats are real rows flagged
`ephemeral=1` (excluded from listings, deleted on close), so the engine
needs no second namespace for them.

Synchronous sqlite3 on purpose: every call is a point read/write on a
local file, the same pattern topics_store/mentions_store/trace_store use.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any


# Namespace for the deterministic per-resident household session id (#345).
_HOUSEHOLD_NS = uuid.UUID("a3f0c0de-0501-0345-0000-000000000345")

# Namespace for the deterministic shared "Wartung" (admin ops) session id (#786).
_WARTUNG_NS = uuid.UUID("a3f0c0de-0501-0786-0000-000000000786")

# How long a durable session stays "the same conversation" for callers that
# supply no conversation id of their own. The household row is never forked, so
# without this every turn would replay months of history into the prompt.
_CONVERSATION_IDLE_GAP_S = 600


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    # WAL so readers (the gatekeeper's speaker-ID lookups) and the ingest
    # writer don't block each other. Under the default rollback journal a
    # reader blocks the writer, so the OKF ingest hits "database is locked"
    # on every asset and its retry loop wedges the chat server (#586). WAL is
    # persisted in the db header; _conn runs at boot (before the ingest), so
    # this flips both existing and fresh databases. synchronous=NORMAL is the
    # safe, recommended pairing for WAL.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def create_session(
    db_path: str,
    uid: str,
    *,
    title: str = "",
    profile: str = "household",
    ephemeral: bool = False,
    maintenance: bool = False,
) -> str:
    session_id = uuid.uuid4().hex
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO engine_sessions"
            " (id, owner_uid, title, profile, ephemeral, maintenance)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, uid, title, profile, int(ephemeral), int(maintenance)),
        )
    return session_id


def household_session_id(uid: str) -> str:
    """The stable session id for a resident's durable household chat (#345).

    Deterministic from the uid so the voice facade always lands in the SAME
    session and it survives restarts (a fresh uuid each boot would orphan the
    spoken history). A namespaced uuid5 keeps it a normal 32-hex id, so it
    behaves like any other session id everywhere else.
    """
    return uuid.uuid5(_HOUSEHOLD_NS, uid).hex


def ensure_household_session(
    db_path: str, uid: str, *, profile: str = "household"
) -> str:
    """Return the resident's durable household session, creating it once (#345).

    The session voice turns persist into and the browser opens — so spoken and
    typed history are the same row. Created with the fixed `household_session_id`
    and the household primary topic so routing + the pinned "Zuhause" row pick
    it up; idempotent (INSERT OR IGNORE) so concurrent first turns can't dup it.
    """
    session_id = household_session_id(uid)
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO engine_sessions"
            " (id, owner_uid, title, profile) VALUES (?, ?, ?, ?)",
            (session_id, uid, "Zuhause", profile),
        )
    return session_id


def wartung_session_id(uid: str) -> str:
    """The stable session id for the shared "Wartung" admin ops chat (#786).

    Deterministic from `uid` — the same namespaced-uuid5 trick as
    `household_session_id`, so the pinned "Wartung" row always opens the SAME
    admin session and it survives restarts. Passed `default_uid` it yields the
    one shared session every admin opens (mirrors the shared Zuhause row)."""
    return uuid.uuid5(_WARTUNG_NS, uid).hex


def ensure_wartung_session(db_path: str, uid: str) -> str:
    """Return the shared "Wartung" admin session, creating it once (#786).

    The pinned admin ops chat (epic #784). Created on the `admin` profile so its
    turns route to the admin gateway (ops soul + SB-MCP toolset) and flagged
    `maintenance` so it renders in the admin view; idempotent (INSERT OR IGNORE)
    so concurrent first opens can't dup it."""
    session_id = wartung_session_id(uid)
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO engine_sessions"
            " (id, owner_uid, title, profile, maintenance) VALUES (?, ?, ?, ?, 1)",
            (session_id, uid, "Wartung", "admin"),
        )
    return session_id


def truncate_session_head(db_path: str, session_id: str, keep_token_budget: int) -> int:
    """Drop the OLDEST messages of a session in place, keeping the most recent
    turns within ~`keep_token_budget` tokens (~4 chars/token). Returns the count
    deleted; a no-op (0) when the history already fits.

    Bounds the durable household chat, which is never forked (#419): when it
    outgrows the window the oldest turns are simply cut. The kept window is
    advanced to start at a `user` message so no orphan tool result / assistant
    tool_calls leads the history. The soul + device registry are NOT stored here
    (they are the per-turn system prompt, rebuilt each turn), so only chat turns
    are dropped — exactly the "cut long conversations at the front" behaviour.
    """
    budget_chars = max(0, keep_token_budget) * 4
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT seq, role, created_at, length(content) AS clen"
            " FROM engine_messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        if not rows or sum((r["clen"] or 0) for r in rows) <= budget_chars:
            return 0
        acc = 0
        start = 0
        for i in range(len(rows) - 1, -1, -1):
            acc += rows[i]["clen"] or 0
            if acc > budget_chars:
                start = i + 1
                break
        while start < len(rows) and rows[start]["role"] != "user":
            start += 1
        if start >= len(rows):
            return 0  # newest turn alone exceeds budget — keep it rather than nuke
        cur = conn.execute(
            "DELETE FROM engine_messages WHERE session_id = ? AND seq < ?",
            (session_id, rows[start]["seq"]),
        )
        # Drop the LLM traces of the cut turns too, so the trace panel never
        # outlives the messages it describes (else old, now-truncated turns
        # mis-align onto the kept bubbles). Best-effort: a pre-migration db
        # without the table just keeps the messages trimmed.
        try:
            conn.execute(
                "DELETE FROM session_traces WHERE session_id = ? AND created_at < ?",
                (session_id, rows[start]["created_at"]),
            )
        except sqlite3.OperationalError:
            pass
        return cur.rowcount


def delete_session(db_path: str, session_id: str, uid: str) -> bool:
    """Owner-scoped delete: a wrong-owner id deletes nothing and returns False,
    indistinguishable from a missing one (mirrors get_session's scoping)."""
    with _conn(db_path) as conn:
        owner = conn.execute(
            "SELECT 1 FROM engine_sessions WHERE id = ? AND owner_uid = ?",
            (session_id, uid),
        ).fetchone()
        if owner is None:
            return False
        conn.execute("DELETE FROM engine_messages WHERE session_id = ?", (session_id,))
        # Purge the chat's persisted LLM traces too — otherwise they outlive the
        # session and, because the household chat's id is deterministic (#419),
        # resurface in the next same-id chat as steps from old conversations.
        # Best-effort: a pre-migration db without the table just skips this.
        try:
            conn.execute(
                "DELETE FROM session_traces WHERE session_id = ?", (session_id,)
            )
        except sqlite3.OperationalError:
            pass
        cur = conn.execute(
            "DELETE FROM engine_sessions WHERE id = ? AND owner_uid = ?",
            (session_id, uid),
        )
    return cur.rowcount > 0


def list_sessions(db_path: str, uid: str) -> list[dict[str, Any]]:
    """The caller's listable sessions, newest first.

    Maintenance sessions stay listed (they render in the admin view, same as
    the Hermes maint-marker behavior); ephemeral ones never list.
    """
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT s.*,"
            " (SELECT content FROM engine_messages m"
            "   WHERE m.session_id = s.id AND m.role = 'user'"
            "   ORDER BY m.seq LIMIT 1) AS preview"
            " FROM engine_sessions s"
            " WHERE s.owner_uid = ? AND s.ephemeral = 0"
            " ORDER BY s.last_activity DESC",
            (uid,),
        ).fetchall()
    return [_summary(r) for r in rows]


def get_session(db_path: str, session_id: str, uid: str) -> dict[str, Any] | None:
    """Summary + full message history, owner-scoped: a wrong-owner id is
    indistinguishable from a missing one."""
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM engine_sessions WHERE id = ? AND owner_uid = ?",
            (session_id, uid),
        ).fetchone()
        if row is None:
            return None
        msgs = conn.execute(
            "SELECT role, content, reasoning, tool_calls, images, created_at"
            " FROM engine_messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    summary = _summary(row)
    # created_at rides each message so the reopened bubble gets its .meta line —
    # the per-turn step-trace panel attaches there, so without it persisted
    # traces never render on a cold reload (the meta is the only anchor).
    summary["messages"] = [
        {"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
        for m in msgs
        if m["role"] in ("user", "assistant") and m["content"]
    ]
    return summary


def session_profile(db_path: str, session_id: str) -> str | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT profile FROM engine_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return row["profile"] if row else None


def session_owner(db_path: str, session_id: str) -> str | None:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT owner_uid FROM engine_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return row["owner_uid"] if row else None


def set_overlay(db_path: str, session_id: str, system_prompt: str) -> None:
    """A per-session system-prompt overlay (compaction continuations)."""
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE engine_sessions SET system_prompt = ? WHERE id = ?",
            (system_prompt, session_id),
        )


def get_overlay(db_path: str, session_id: str) -> str:
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT system_prompt FROM engine_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return (row["system_prompt"] or "") if row else ""


def set_title(db_path: str, session_id: str, uid: str, title: str) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE engine_sessions SET title = ? WHERE id = ? AND owner_uid = ?",
            (title, session_id, uid),
        )


def append_message(
    db_path: str,
    session_id: str,
    role: str,
    content: str,
    *,
    reasoning: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    images: list[str] | None = None,
    conversation_id: str = "",
    in_prompt: bool = True,
) -> None:
    """Append one row. `conversation_id` scopes what a later prompt replays;
    `in_prompt=False` keeps the row in the browser history but out of every
    future prompt (the enrolment dialog scripts)."""
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO engine_messages"
            " (session_id, seq, role, content, reasoning, tool_calls, images,"
            "  conversation_id, in_prompt)"
            " VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM engine_messages"
            "             WHERE session_id = ?), ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                session_id,
                role,
                content,
                reasoning or None,
                json.dumps(tool_calls) if tool_calls else None,
                json.dumps(images) if images else None,
                conversation_id or None,
                int(in_prompt),
            ),
        )
        conn.execute(
            "UPDATE engine_sessions SET last_activity = datetime('now') WHERE id = ?",
            (session_id,),
        )


def _to_messages(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        msg: dict[str, Any] = {"role": r["role"], "content": r["content"]}
        if r["tool_calls"]:
            msg["tool_calls"] = json.loads(r["tool_calls"])
        if r["images"]:
            # Ollama native takes raw base64 (no data: prefix) on `images`.
            msg["images"] = [_strip_data_url(i) for i in json.loads(r["images"])]
        out.append(msg)
    return out


def history(db_path: str, session_id: str) -> list[dict[str, Any]]:
    """The Ollama-shaped message history for the next call: user/assistant
    turns plus tool calls and their results, reasoning omitted (it is never
    fed back — gemma4 reasons fresh per turn)."""
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content, tool_calls, images"
            " FROM engine_messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    return _to_messages(rows)


def prompt_history(
    db_path: str, session_id: str, conversation_id: str
) -> list[dict[str, Any]]:
    """The model-visible slice of a session: one conversation, hidden rows
    dropped. The never-forked household row would otherwise replay every past
    conversation into each prompt (#1067)."""
    if not conversation_id:
        return []
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content, tool_calls, images"
            " FROM engine_messages"
            " WHERE session_id = ? AND conversation_id = ? AND in_prompt = 1"
            " ORDER BY seq",
            (session_id, conversation_id),
        ).fetchall()
    msgs = _to_messages(rows)
    # A head cut by `truncate_session_head`, or a hidden enrolment pass, can
    # leave a dangling tool result or a tool_calls row whose results are gone.
    # Ollama rejects that shape, so open on a turn that can stand alone.
    start = 0
    for i, m in enumerate(msgs):
        if m["role"] == "user" or (
            m["role"] == "assistant" and not m.get("tool_calls")
        ):
            start = i
            break
    else:
        return []
    return msgs[start:]


def resolve_conversation_id(
    db_path: str,
    session_id: str,
    *,
    requested: str | None = None,
    idle_gap_s: int = _CONVERSATION_IDLE_GAP_S,
) -> str:
    """The conversation this turn belongs to.

    Home Assistant supplies one per Assist conversation; callers without one
    (browser, direct API) continue the previous conversation while the session
    is still warm. Derived from the DB, so it survives a restart. Reads the last
    row regardless of `in_prompt` — a hidden enrolment turn must not split the
    resident's conversation in two.
    """
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT conversation_id,"
            " (julianday('now') - julianday(created_at)) * 86400.0 AS age_s"
            " FROM engine_messages WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    last = (row["conversation_id"] if row else None) or ""
    age_s = float(row["age_s"] or 0.0) if row else 0.0
    if requested:
        # A stale id resurfacing hours later starts a new conversation rather
        # than resurrecting the old one.
        if last == requested and age_s > idle_gap_s:
            return uuid.uuid4().hex
        return requested
    if last and age_s <= idle_gap_s:
        return last
    return uuid.uuid4().hex


def messages_since(
    db_path: str, session_id: str, since_utc: str
) -> list[tuple[str, str]]:
    """The user/assistant turns of `session_id` created after `since_utc` (a
    sqlite UTC `created_at` string), oldest first, as `(role, content)` pairs.

    Skips tool-call/empty rows (the stenograph only distils real conversation)
    and orders by `seq` — the durable household session is head-truncated in
    place (#466), so `created_at` filters what is new without relying on seq
    continuity. Rows marked `in_prompt = 0` are skipped too: the enrolment
    dialog scripts must not be distilled into durable facts either.
    """
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT role, content FROM engine_messages"
            " WHERE session_id = ? AND created_at > ?"
            " AND role IN ('user', 'assistant') AND content != ''"
            " AND in_prompt = 1"
            " ORDER BY seq",
            (session_id, since_utc),
        ).fetchall()
    return [(r["role"], r["content"]) for r in rows]


def add_usage(db_path: str, session_id: str, prompt: int, completion: int) -> None:
    with _conn(db_path) as conn:
        conn.execute(
            "UPDATE engine_sessions SET input_tokens = input_tokens + ?,"
            " output_tokens = output_tokens + ? WHERE id = ?",
            (prompt, completion, session_id),
        )


def _strip_data_url(url: str) -> str:
    if url.startswith("data:") and "," in url:
        return url.split(",", 1)[1]
    return url


def _summary(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "title": row["title"],
        "preview": (row["preview"] or "") if "preview" in keys else "",
        "last_activity": row["last_activity"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "message_count": None,
        "estimated_cost_usd": None,
    }
