"""SQLite access to persisted per-message LLM traces.

Reads/writes the `session_traces` table (migration 0007) in solaris.db. Phase 2
of the trace sidecar (#306): at turn time the server pulls the trace proxy's
llama-server calls for that turn's window, assigns them a stable per-message
`trace_id`, and persists each step here in order — so reopening a chat shows the
same trace, persistently. `detail_id` links a step back to the proxy's
`/__traces__/<id>` exact-content record.

Sync sqlite3, like `topics_store` / `mentions_store`: each op is
millisecond-cheap. If solaris.db or the table is missing (the schema-init
sidecar hasn't migrated yet), writes/reads degrade to no-op/empty rather than
erroring — the trace panel just shows nothing until the migration lands.

Scoping is per-resident (D3): every row carries the recording `owner_uid` and
every read/write is filtered by it, so a resident never sees another resident's
trace.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# The per-step columns persisted from a proxy trace record (besides the keys +
# owner_uid). Order matches the INSERT below.
_STEP_FIELDS = (
    "model",
    "profile",
    "wall_s",
    "prompt_tokens",
    "completion_tokens",
    "context_free",
    "finish_reason",
    "n_tools",
    "detail_id",
    "step_kind",
    "tool_name",
    "detail_json",
)


def step_row(
    rec: dict[str, Any], detail: dict[str, Any] | None, detail_id: str
) -> dict[str, Any]:
    """One persisted step from a recorder record + its exact-content body.

    A tool step carries no LLM body, so nothing at all was stored for it and a
    failed tool call could not be diagnosed from the trace afterwards (#1241) —
    it now persists the arguments the tool ran with and its (already capped)
    result. That body rides the SAME `detail_id`/`detail_json` channel as the LLM
    bodies, so it inherits their privacy posture unchanged: per-resident
    `owner_uid` scoping, and fetched only on demand — `list_session_trace` never
    inlines it into the panel payload.
    """
    if rec.get("step_kind") == "tool":
        detail = {
            "tool_name": rec.get("tool_name"),
            "arguments": rec.get("arguments"),
            "result": rec.get("output"),
        }
    return {
        "model": rec.get("model"),
        "profile": rec.get("profile"),
        "wall_s": rec.get("wall_s"),
        "prompt_tokens": rec.get("prompt_tokens"),
        "completion_tokens": rec.get("completion_tokens"),
        "context_free": rec.get("context_free"),
        "finish_reason": rec.get("finish_reason"),
        "n_tools": rec.get("n_tools"),
        "detail_id": detail_id if detail else None,
        "step_kind": rec.get("step_kind"),
        "tool_name": rec.get("tool_name"),
        "detail_json": json.dumps(detail) if detail else None,
    }


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout so a trace persist that overlaps a chat turn or the
    # ingest writer waits for the lock instead of raising "database is
    # locked" (#600). WAL is persisted in the db header; busy_timeout is
    # per-connection.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def persist_trace(
    db_path: str,
    session_id: str,
    trace_id: str,
    owner_uid: str,
    steps: list[dict[str, Any]],
) -> None:
    """Persist a turn's ordered LLM steps under `trace_id`.

    Each step is a proxy trace record (the `/__traces__` shape); `step_order` is
    its index in `steps`. Re-persisting the same `trace_id` replaces the prior
    rows (idempotent on a retried turn). No-op when there's nothing to record,
    the DB is missing, or the table hasn't been migrated yet.
    """
    if not steps or not Path(db_path).exists():
        return
    rows = [
        (
            session_id,
            trace_id,
            i,
            owner_uid,
            *(step.get(f) for f in _STEP_FIELDS),
        )
        for i, step in enumerate(steps)
    ]
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "DELETE FROM session_traces WHERE session_id = ? AND trace_id = ? "
                "AND owner_uid = ?",
                (session_id, trace_id, owner_uid),
            )
            conn.executemany(
                """
                INSERT INTO session_traces
                  (session_id, trace_id, step_order, owner_uid,
                   model, profile, wall_s, prompt_tokens, completion_tokens,
                   context_free, finish_reason, n_tools, detail_id,
                   step_kind, tool_name, detail_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
    except sqlite3.OperationalError:
        return


def list_session_trace(
    db_path: str, session_id: str, owner_uid: str
) -> list[dict[str, Any]]:
    """The resident's persisted trace for a chat, in turn-then-step order.

    Ordered by insertion (`rowid`): turns were persisted chronologically and each
    turn's steps in `step_order`, so the reopened trace matches the live order.
    Each item carries the keys + the persisted step fields. Empty when the
    DB/table is missing or the chat has no trace.
    """
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT trace_id, step_order, model, profile, wall_s,
                       prompt_tokens, completion_tokens, context_free,
                       finish_reason, n_tools, detail_id,
                       step_kind, tool_name, detail_json
                  FROM session_traces
                 WHERE session_id = ? AND owner_uid = ?
                 ORDER BY rowid
                """,
                (session_id, owner_uid),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        d = dict(r)
        # detail_json is the on-demand LLM request/response body (fetched via the
        # detail modal); the list endpoint only inlines it for ha_cards (#475),
        # suggestions (#498) and anchors (#501) steps, where it IS the small
        # card/chip payload, never the LLM bodies.
        if d.get("step_kind") not in ("ha_cards", "suggestions", "anchors"):
            d.pop("detail_json", None)
        out.append(d)
    return out


def detail_for(db_path: str, owner_uid: str, detail_id: str) -> str | None:
    """The persisted request/response body (#451) for one step, by `detail_id`.

    `detail_id` is the stable per-step key persisted in `persist_trace`
    (`<trace_id>:<step_order>`), so it survives a process restart unlike the
    in-process recorder ring. Per-resident scoped. Returns the raw JSON string
    (the same shape the ring's `detail()` held) or None when absent.
    """
    if not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT detail_json FROM session_traces "
                "WHERE owner_uid = ? AND detail_id = ?",
                (owner_uid, detail_id),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["detail_json"] if row and row["detail_json"] is not None else None
