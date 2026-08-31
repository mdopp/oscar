"""aiohttp app: the chat surface over the in-process Solaris Engine.

The browser keeps the current session id and sends it back with each turn;
on the first turn (no id) the server creates a session bound to the SSO
identity and returns the id. Chat/session state lives in solaris.db via the
engine's store; the server itself stays a thin routing layer.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import time
import uuid
import wave
import zipfile
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiohttp.typedefs import Handler

from solaris_chat import (
    compaction,
    db_health,
    device_token_store,
    documents_portal_db,
    favorites_store,
    ha_notify,
    mentions_store,
    model_lease,
    notes_portal_db,
    notes_search,
    personalities,
    push_store,
    reasoning,
    settings_store,
    skills,
    topics_store,
    trace_store,
    wakeword_requests_store,
    wakeword_samples_store,
    wakeword_training_store,
)
from solaris_chat.attachments import AttachmentStore, attach_to_messages
from solaris_chat.context import STATIC_DEFAULT, ContextWindow
from solaris_chat.engine import (
    action_cards,
    approvals,
    confirm,
    escalation,
    store,
    tasks as tasks_svc,
    updates,
)
from solaris_chat.engine import sb_companion as sb_companion_module
from solaris_chat.engine.document_deadlines_sync import cascade_task_event_configured
from solaris_chat.engine.importers.google_takeout import orchestrator as import_flow
from solaris_chat.engine.client import (
    NO_ANSWER,
    EngineClient,
    EngineError,
    current_admin_identity,
    current_uid,
)
from solaris_chat.engine import vram
from solaris_chat.engine.ingest.immich_client import RestImmichClient
from solaris_chat.engine.ingest.upload_extract import extract_into_companion
from solaris_chat.engine.facade import add_facade_routes
from solaris_chat.engine.notify import emit_chat, inject
from solaris_chat.engine.ollama import OllamaChat, OllamaError
from solaris_chat.engine.knowledge import okf, projection, safe_slug
from solaris_chat.engine.knowledge.records import ConceptRecord
from solaris_chat.engine.knowledge.writer import write_concept
from solaris_chat.engine.tools.favorites import PINNABLE_TOOLS
from solaris_chat.engine.areas import AreaRegistry
from solaris_chat.engine.tools.ha import (
    _ENTITY_HISTORY_RANGES,
    _ENTITY_RE,
    _SERVICE_ALIASES,
    call_service_scoped,
    fetch_addable_cards,
    fetch_addable_runnables,
    fetch_camera_snapshot,
    fetch_cameras,
    fetch_card,
    fetch_energy,
    fetch_energy_history,
    fetch_entity_history,
    fetch_entity_names,
)
from solaris_chat.engine.tools.mcp_tools import McpToolbox, exchange_sb_token
from solaris_chat.engine.tools.notes import build_notes_tools
from solaris_chat.logging import log

STATIC_DIR = Path(__file__).parent / "static"

# Stable "always the latest signed build" link for the companion app (#…): the
# Android CI publishes a GitHub release per tag with `app-release.apk`, and
# GitHub's `releases/latest/download/<asset>` redirect always points at the
# newest one — so `www.dopp.cloud/download` never needs bumping on a new release.
ANDROID_APK_URL = (
    "https://github.com/mdopp/solaris-android/releases/latest/download/app-release.apk"
)

# Native-API prefix (#757). Authelia BYPASSES this prefix for the Android widgets,
# which authenticate with a `sol_device_` device-token bearer alone. Because it is
# proxy-bypassed, the `/napi/*` routes are device-token-ONLY and fail-closed: no
# valid token ⇒ 401, never the household `default_uid` and never a Remote-User
# header (untrusted here). Minting stays on the interactive-Authelia `/api/` path.
NATIVE_PREFIX = "/napi/"

# Matches the healthcheck interval in templates/solaris/template.yml: a client
# that waits this long retries just after the next probe would have gone green.
DB_RETRY_AFTER_S = 30

# Widget-usage forwarding (#1026). The Android widgets tag their deep-link opens
# and `/napi/` calls `?src=widget.<tool>.<zone>`; Solaris is a thin forwarder —
# it turns one tagged request into ONE aggregate increment on the household
# usage-metrics service and keeps no counter of its own. Solaris runs
# `effectiveHostNetwork: true`, so that isolated-netns service is reached on the
# host loopback (127.0.0.1:8095), NOT `host.containers.internal`.
USAGE_METRICS_URL = "http://127.0.0.1:8095/ingest"
USAGE_METRICS_TIMEOUT_S = 2.0
# `widget.<tool>.<zone>` and nothing else — a tag is attacker-supplied query
# text, and this is what stops anything but two short slugs becoming an event
# name on a shared service.
_WIDGET_SRC_RE = re.compile(r"\Awidget\.[a-z0-9_-]{1,32}\.[a-z0-9_-]{1,32}\Z")
# Strong refs to the in-flight fire-and-forget posts (asyncio only holds weak
# ones, so a task can otherwise be garbage-collected mid-flight).
_usage_metrics_tasks: set[asyncio.Task[None]] = set()
# Same, for the fire-and-forget FAST_MODEL re-warm at a lease end (#1260).
_rewarm_tasks: set[asyncio.Task[bool]] = set()
# Same, for the fire-and-forget Web Push fan-out of an HA notice (#1276) — the
# caller is answered before a single push leaves the box.
_ha_notify_tasks: set[asyncio.Task[None]] = set()


def is_loopback_caller(request: web.Request) -> bool:
    """True only for a request that came straight off the host loopback.

    The model lease (#1260) carries no token and no shared secret by agreement
    with foundry-chronicle, so reachability IS the authorisation. NPM runs
    hostNetwork on this same box, so its peer address is loopback too — a
    forwarding header therefore means the request came through the proxy from
    outside and is not a neighbour on the box.
    """
    if request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP"):
        return False
    peer = request.remote or ""
    return peer == "::1" or peer.startswith("127.")


def widget_event(src: str | None) -> str | None:
    """The `widget.<tool>.<zone>` event for a `?src=` tag, or None (#1026)."""
    tag = (src or "").strip()
    return tag if _WIDGET_SRC_RE.match(tag) else None


def usage_metrics_payload(event: str) -> dict[str, str | int]:
    """The ENTIRE body that leaves the house: `{app, event, day, count}` (#1026).

    Those four fields are what the service's `/ingest` requires and all it
    accepts — it rejects unknown fields and a `count` below 1 with 400
    (mdopp/usage-metrics `ingest.go`). No uid, no room, no entity id, no
    request body, no content — the counted fact is "someone in this house
    used this widget zone today". Test `test_widget_usage_metrics.py` asserts
    this key set against that contract, so a field can't be added here by
    accident and a missing one can't ship green (#1252)."""
    return {
        "app": "solaris",
        "event": event,
        "day": datetime.now(timezone.utc).date().isoformat(),
        "count": 1,
    }


# A broken metrics service must not spam the log, but "sends nothing, forever"
# must not be silent either — that is how #1252 stayed invisible for a release.
# The first failure of this process is logged; the rest stay quiet.
_usage_metrics_failure_logged = False


def _log_usage_metrics_failure(event: str, **args: Any) -> None:
    global _usage_metrics_failure_logged
    if _usage_metrics_failure_logged:
        return
    _usage_metrics_failure_logged = True
    log.warn("chat.usage_metrics.post_failed", event=event, **args)


async def _post_usage_metric(event: str) -> None:
    """POST one increment; swallow everything (#1026 — fail-open, always).

    Fail-open never means fail-silent: a non-2xx or an unreachable service is
    logged once per process (#1252)."""
    token = os.environ.get("USAGE_METRICS_TOKEN", "").strip()
    if not token:
        return
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=USAGE_METRICS_TIMEOUT_S)
        ) as session:
            async with session.post(
                USAGE_METRICS_URL,
                json=usage_metrics_payload(event),
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if not 200 <= response.status < 300:
                    detail = (await response.text())[:200]
                    _log_usage_metrics_failure(
                        event, status=response.status, detail=detail
                    )
    except Exception as exc:  # noqa: BLE001 — a metrics hiccup must never reach a resident.
        _log_usage_metrics_failure(event, error=str(exc))


def count_widget_hit(request: web.Request) -> None:
    """Fire-and-forget one increment for a `?src=widget.*` request (#1026)."""
    event = widget_event(request.query.get("src"))
    if event is None:
        return
    task = asyncio.create_task(_post_usage_metric(event))
    _usage_metrics_tasks.add(task)
    task.add_done_callback(_usage_metrics_tasks.discard)


@web.middleware
async def widget_usage(request: web.Request, handler: Any) -> web.StreamResponse:
    # Counting happens BEFORE the handler runs and is never awaited, so the
    # resident's response is not delayed by the metrics service (#1026).
    count_widget_hit(request)
    return await handler(request)


# Idle-keepalive interval for the portal SSE stream (#1093). Two things drop an
# idle stream, and the shorter one sets this: (a) the NPM proxy in front of chat,
# `proxy_read_timeout 600s` (read off the box's proxy_host conf on 2026-07-28 —
# it lives in mdopp/servicebay, not here, so treat it as observed, not owned);
# (b) mobile-carrier NAT, which expires idle TCP bindings after a few minutes and
# kills the stream SILENTLY — no error, just no more events. (b) is unmeasurable
# from this repo, so this stays a conservative fraction of the commonly-cited
# ~2 min carrier floor rather than anything near the proxy's 600s. The native
# app holds this connection open 24/7 from its foreground service, so every ping
# is a CPU + radio wake-up: 90s is 40 wake-ups/hour instead of 240.
_SSE_HEARTBEAT_S = 90

# Self-contained confirm page for /pair-device (#751). Inline HTML/CSS in the
# server.py style of the other simple routes. The `{devices}` slot is the
# server-rendered paired-devices list; the confirm form POSTs same-origin (the
# mint happens only on that explicit click, never on GET).
_PAIR_DEVICE_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gerät koppeln</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 1.5rem;
         background: #101014; color: #f0f0f4; }}
  main {{ max-width: 28rem; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; }}
  p {{ color: #b8b8c4; line-height: 1.5; }}
  input[type=text] {{ width: 100%; box-sizing: border-box; padding: .6rem;
         border-radius: .5rem; border: 1px solid #33333c; background: #1a1a20;
         color: #f0f0f4; font-size: 1rem; }}
  button.primary {{ margin-top: 1rem; width: 100%; padding: .8rem;
         border: none; border-radius: .5rem; background: #5b8cff; color: #fff;
         font-size: 1rem; font-weight: 600; cursor: pointer; }}
  section.paired {{ margin-top: 2rem; border-top: 1px solid #26262e;
         padding-top: 1rem; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ display: flex; justify-content: space-between; align-items: center;
        padding: .5rem 0; border-bottom: 1px solid #1e1e26; }}
  li.empty {{ color: #7a7a88; }}
  button.revoke {{ background: none; border: 1px solid #55323a; color: #ff8b9c;
        border-radius: .4rem; padding: .3rem .6rem; cursor: pointer; }}
</style>
</head>
<body>
<main>
  <h1>Dieses Gerät koppeln</h1>
  <p>Erzeugt einen Zugangs-Token für die Solaris-App auf diesem Gerät.
     Der Token wird nur an die App übergeben und ist deinem Konto zugeordnet.</p>
  <form method="post" action="/pair-device">
    <label for="label">Gerätename (optional)</label>
    <input type="text" id="label" name="label" placeholder="z. B. Mein Pixel"
           autocomplete="off">
    <button type="submit" class="primary">Dieses Gerät koppeln</button>
  </form>
  <section class="paired">
    <h2>Gekoppelte Geräte</h2>
    <ul>{devices}</ul>
  </section>
</main>
<script>
document.querySelectorAll("button.revoke").forEach(function (b) {{
  b.addEventListener("click", async function () {{
    await fetch("/api/device-tokens/" + b.dataset.id, {{ method: "DELETE" }});
    location.reload();
  }});
}});
</script>
</body>
</html>"""


def _read_okf(notes_dir: str, okf_path: str) -> dict[str, str]:
    """Read an OKF concept file's description + body for the concept page (#502).

    `okf_path` is the projection-stored vault-relative path; resolve it under
    `notes_dir`, refusing any path that escapes the vault. Empty on any error so
    the page degrades to the projected facts/events.
    """
    root = Path(notes_dir).resolve()
    try:
        target = (root / okf_path).resolve()
        target.relative_to(root)
        return okf.read_concept(target.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {"description": "", "body": ""}


# ---- Notes portal (#696): read-only aggregators over the vault ----------------

_NOTES_TITLE_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")
_NOTES_FM_RE = re.compile(r"(?s)\A---\n(.*?)\n---\s*\n?")
# The topic tag in either written form (mirrors notes_search._topic_pattern) —
# used to bucket notes by #Thema for the browse view.
_TOPIC_TAG_RE = re.compile(r"(?<![\w/])#?topic/([\w/-]+)")


def _note_title(text: str, fallback: str) -> str:
    """A note's first `# ` heading, or the filename stem as a fallback."""
    m = _NOTES_TITLE_RE.search(text)
    return m.group(1).strip() if m else fallback


def _note_frontmatter(text: str) -> dict[str, str]:
    """The note's leading `--- … ---` block parsed as flat key/value pairs.

    A deliberately small, deterministic subset (no PyYAML in the engine, mirroring
    `okf`): each `key: value` line at the top of the block. Empty when there is no
    frontmatter fence."""
    m = _NOTES_FM_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() and not k.startswith(" "):
            out[k.strip()] = v.strip().strip("'\"")
    return out


def _notes_inbox_count(notes_dir: str, uid: str) -> int:
    """Unconsolidated fact files older than the Bibliothekar's stale threshold.

    The same signal the nightly librarian queues (#653): fact files whose
    `YYYY-MM-DD-` name prefix is older than `_BIBLIOTHEKAR_STALE_DAYS` and that
    carry no `consolidated: true` stamp — the household inbox that still needs
    curation. Scoped to the caller ∪ shared pool (`facts/` shared,
    `users/<uid>/facts/` the caller's own)."""
    from datetime import timedelta, timezone

    from solaris_chat.engine.crons import _BIBLIOTHEKAR_STALE_DAYS

    root = Path(notes_dir)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_BIBLIOTHEKAR_STALE_DAYS)
    ).strftime("%Y-%m-%d")
    dirs = [root / "facts"]
    if uid != notes_search.SHARED_UID:
        dirs.append(root / "users" / uid / "facts")
    count = 0
    for facts_dir in dirs:
        if not facts_dir.is_dir():
            continue
        for path in facts_dir.glob("*.md"):
            if path.name[:10] >= cutoff:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "consolidated: true" not in text:
                count += 1
    return count


def _notes_visible_files(notes_dir: str, uid: str):
    """Yield `(relpath, text, path)` for every vault `.md` the caller may see.

    Owner-scoped via `is_visible(owner_of(...))` (caller ∪ shared, default-deny);
    skips pathological files, matching the notes_search readers. Walks via the
    prune-aware, bounded `iter_vault_md` so a Syncthing vault's `.stversions/`
    history (tens of thousands of copies) can't wedge the scan (#705)."""
    root = Path(notes_dir)
    for path in sorted(notes_search.iter_vault_md(root)):
        try:
            if path.stat().st_size > notes_search._MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root))
        if notes_search.is_visible(notes_search.owner_of(rel, text), uid):
            yield rel, text, path


def _dedup_note_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse note-list rows that resolve to the same note (#709 safety net).

    Two collapse keys, first row (by input order) wins and later duplicates drop:
    a canonical journal date (`journal/…<YYYY-MM-DD>…` under any convention → one
    entry per day), and — when a row carries its `text` — a `title`+content-hash
    identity so a hand-copied stray shows once. Rows are expected pre-sorted
    (newest first), so the surviving row is the freshest. Purely presentational —
    the underlying files are untouched (the librarian owns consolidation)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        date = notes_search.journal_date(str(row.get("path") or ""))
        if date is not None:
            key = f"journal:{date}"
        elif row.get("text") is not None:
            digest = hashlib.sha1(str(row["text"]).encode("utf-8")).hexdigest()
            key = f"note:{row.get('title', '')}:{digest}"
        else:
            out.append(row)
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _notes_recent(notes_dir: str, uid: str, limit: int = 10) -> list[dict[str, Any]]:
    """The most recently modified notes the caller may see: `[{path, mtime, title}]`."""
    rows: list[dict[str, Any]] = []
    for rel, text, path in _notes_visible_files(notes_dir, uid):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        rows.append(
            {
                "path": rel,
                "mtime": mtime,
                "title": _note_title(text, path.stem),
                "text": text,
            }
        )
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    deduped = _dedup_note_rows(rows)[:limit]
    for r in deduped:
        r.pop("text", None)
    return deduped


# TTL for the notes-portal overview (#705): a single prune-bounded vault walk is
# cheap, but repeated portal opens shouldn't re-scan on every request. Short
# enough that a fresh note shows up within a minute.
_NOTES_OVERVIEW_TTL = 60.0
_notes_overview_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _notes_overview_scan(notes_dir: str, uid: str) -> dict[str, Any]:
    """Compute the portal overview in one prune-bounded walk (#705).

    Blocking (walks the vault, reads files) — callers run it off the event loop.
    Returns the counts, the recent list, and a `truncated` flag set when the walk
    hit `iter_vault_md`'s file budget (so the UI can show "≥N"). The librarian
    trail is a bounded tail read, kept out of the walk."""
    root = Path(notes_dir)
    total = 0
    facts = 0
    md_seen = 0
    rows: list[dict[str, Any]] = []
    for path in sorted(notes_search.iter_vault_md(root)):
        md_seen += 1
        try:
            if path.stat().st_size > notes_search._MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            mtime = path.stat().st_mtime
        except OSError:
            continue
        rel = str(path.relative_to(root))
        if not notes_search.is_visible(notes_search.owner_of(rel, text), uid):
            continue
        total += 1
        norm = rel.replace("\\", "/")
        if norm.split("/", 1)[0] == "facts" or "/facts/" in norm:
            facts += 1
        rows.append(
            {
                "path": rel,
                "mtime": mtime,
                "title": _note_title(text, path.stem),
                "text": text,
            }
        )
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    recent = _dedup_note_rows(rows)[:10]
    for r in recent:
        r.pop("text", None)
    return {
        "ok": True,
        "counts": {
            "notes": total,
            "facts": facts,
            "inbox": _notes_inbox_count(notes_dir, uid),
        },
        "truncated": md_seen >= notes_search._VAULT_WALK_BUDGET,
        "librarian": _notes_last_librarian(notes_dir),
        "recent": recent,
    }


def _notes_last_librarian(notes_dir: str, lines: int = 8) -> list[str]:
    """The last N lines of `okf/log.md` — the Bibliothekar's run trail (#653)."""
    log_path = Path(notes_dir).resolve() / "okf" / "log.md"
    try:
        log_path.relative_to(Path(notes_dir).resolve())
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    return [ln for ln in text.splitlines() if ln.strip()][-lines:]


def _notes_overview_payload(notes_dir: str, db_path: str, uid: str) -> dict[str, Any]:
    """The overview from `solaris.db` (perf: no vault walk), or the vault scan.

    Serves note/fact counts + recent from the FTS index and OKF projection; the
    inbox is the existing bounded fact scan and the librarian trail a bounded tail
    read (both cheap). Falls back to the full `_notes_overview_scan` only when the
    projection is missing (fresh install / unmigrated db), so nothing breaks."""
    payload = notes_portal_db.overview(
        db_path,
        uid,
        inbox_count=_notes_inbox_count(notes_dir, uid),
        librarian=_notes_last_librarian(notes_dir),
    )
    if payload is None:
        return _notes_overview_scan(notes_dir, uid)
    return payload


# ---- Notes portal V2 (#697): inbox curation workbench -------------------------
# The inbox is the same signal the nightly Bibliothekar curates (#653): stale,
# unconsolidated fact files, scoped to the caller ∪ shared pool. Assign folds a
# fact into a topic/person note and stamps the source `consolidated: true`;
# archive moves it under `archive/` — both honour the never-delete contract
# (#653) and log every move to `okf/log.md`.

_ARCHIVE_DIR = "archive"


def _notes_inbox_dirs(notes_dir: str, uid: str) -> list[Path]:
    """The fact directories the caller's inbox draws from: the shared `facts/`
    pool plus, for a real resident, their own `users/<uid>/facts/` (mirrors the
    Bibliothekar's per-scope candidate dirs)."""
    root = Path(notes_dir)
    dirs = [root / "facts"]
    if uid != notes_search.SHARED_UID:
        dirs.append(root / "users" / uid / "facts")
    return dirs


def _notes_inbox_list(
    notes_dir: str, uid: str, limit: int = 200
) -> list[dict[str, Any]]:
    """The unconsolidated fact files older than the stale threshold (#697).

    The same query the librarian queues (#653): a `YYYY-MM-DD-` name prefix older
    than `_BIBLIOTHEKAR_STALE_DAYS` and no `consolidated: true` stamp. Owner-scoped
    to the caller ∪ shared pool; bounded to `limit` files so a large vault can't
    wedge the request. Returns `[{path, title, date, snippet}]`."""
    from datetime import timedelta, timezone

    from solaris_chat.engine.crons import _BIBLIOTHEKAR_STALE_DAYS

    root = Path(notes_dir)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_BIBLIOTHEKAR_STALE_DAYS)
    ).strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    for facts_dir in _notes_inbox_dirs(notes_dir, uid):
        if not facts_dir.is_dir():
            continue
        for path in sorted(facts_dir.glob("*.md")):
            if path.name[:10] >= cutoff:
                continue
            try:
                if path.stat().st_size > notes_search._MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "consolidated: true" in text:
                continue
            body = _NOTES_FM_RE.sub("", text).strip()
            rows.append(
                {
                    "path": str(path.relative_to(root)),
                    "title": _note_title(text, path.stem),
                    "date": path.name[:10],
                    "snippet": body[:200],
                }
            )
            if len(rows) >= limit:
                return rows
    return rows


def _notes_resolve_owned(notes_dir: str, rel: str, uid: str) -> Path | None:
    """Path-jail + owner-scope a caller-supplied vault-relative fact path (#697).

    Resolves `rel` under the vault, rejects a `..`-escape, and returns the path
    only when the caller may see it (own subtree or shared pool, default-deny) and
    it is a real file. None on any failure — the caller maps that to 400/404."""
    root = Path(notes_dir).resolve()
    try:
        path = (root / rel).resolve()
        path.relative_to(root)
    except (ValueError, OSError):
        return None
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    canon = str(path.relative_to(root))
    if not notes_search.is_visible(notes_search.owner_of(canon, text), uid):
        return None
    return path


def _notes_log_append(notes_dir: str, line: str) -> None:
    """Append one dated line to `okf/log.md` — the Bibliothekar's run trail (#653).

    Every portal curation writes here too, so the "Bibliothekar" report block
    shows hand-curations alongside the nightly runs. Path-jailed to the vault."""
    root = Path(notes_dir).resolve()
    log_path = root / "okf" / "log.md"
    from datetime import timezone

    try:
        log_path.relative_to(root)
    except ValueError:
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"{day} {line}\n")


def _notes_stamp_consolidated(text: str) -> str:
    """Add `consolidated: true` to a fact file's frontmatter, verbatim otherwise.

    Mirrors the Bibliothekar's stamp (#653) and the verbatim-frontmatter rule
    (#657): insert the key into the existing `--- … ---` block without rewriting
    the rest; synthesize a block when the file has none. Idempotent."""
    if "consolidated: true" in text:
        return text
    m = _NOTES_FM_RE.match(text)
    if m:
        fm = m.group(1)
        rest = text[m.end() :]
        return f"---\n{fm}\nconsolidated: true\n---\n{rest}"
    return f"---\nconsolidated: true\n---\n\n{text}"


def _notes_assign_fact(
    notes_dir: str, src: Path, target: str, name: str, uid: str
) -> dict[str, Any]:
    """Fold a fact into its topic/person note, then stamp the source (#697).

    The Bibliothekar's merge convention for a fact (#653): append the fact's body
    under the target note (a `#topic/<slug>` note for `topic`, a `@person` note
    for `person`), keep the source file, and write `consolidated: true` into its
    frontmatter. Writes are atomic (tmp+replace) and owner-scoped: a resident's
    target lands in their `users/<uid>/` subtree, the shared pool at the root. The
    source is never deleted (never-delete contract)."""
    root = Path(notes_dir).resolve()
    slug = re.sub(r"[^a-z0-9äöüß/-]+", "-", name.lower()).strip("-/")
    if not slug:
        return {"ok": False, "error": "bad_name"}
    src_text = src.read_text(encoding="utf-8", errors="replace")
    body = _NOTES_FM_RE.sub("", src_text).strip()
    rel_src = str(src.relative_to(root))

    base = root if uid == notes_search.SHARED_UID else (root / "users" / uid)
    sub = "topics" if target == "topic" else "people"
    tgt = (base / sub / f"{slug}.md").resolve()
    try:
        tgt.relative_to(base.resolve())
    except ValueError:
        return {"ok": False, "error": "bad_target"}
    anchor = f"#topic/{slug}" if target == "topic" else f"@{slug}"
    block = f"\n\n<!-- aus {rel_src} -->\n{anchor} {body}\n"
    tgt.parent.mkdir(parents=True, exist_ok=True)
    if tgt.is_file():
        with tgt.open("a", encoding="utf-8") as f:
            f.write(block)
    else:
        header = (
            "added_by: household"
            if uid == notes_search.SHARED_UID
            else f"added_by: {uid}"
        )
        tmp = tgt.with_suffix(".md.tmp")
        tmp.write_text(f"---\n{header}\n---\n\n# {name}{block}", encoding="utf-8")
        tmp.replace(tgt)

    stamped = _notes_stamp_consolidated(src_text)
    tmp = src.with_suffix(".md.tmp")
    tmp.write_text(stamped, encoding="utf-8")
    tmp.replace(src)
    rel_tgt = str(tgt.relative_to(root))
    _notes_log_append(notes_dir, f"assign {rel_src} → {rel_tgt} (portal, {uid})")
    return {"ok": True, "target_path": rel_tgt, "source": rel_src}


def _notes_archive_fact(notes_dir: str, src: Path, uid: str) -> dict[str, Any]:
    """Move a fact under `archive/` — never delete (#697, mirrors #653).

    Preserves the source subtree under the archive folder so a resident's private
    fact stays in `archive/users/<uid>/…`. If the destination already exists a
    numeric suffix is added rather than clobbering. Logs to `okf/log.md`."""
    root = Path(notes_dir).resolve()
    rel_src = str(src.relative_to(root))
    dest = (root / _ARCHIVE_DIR / rel_src).resolve()
    try:
        dest.relative_to(root / _ARCHIVE_DIR)
    except ValueError:
        return {"ok": False, "error": "bad_path"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        n = 1
        while dest.with_name(f"{dest.stem}-{n}.md").exists():
            n += 1
        dest = dest.with_name(f"{dest.stem}-{n}.md")
    src.replace(dest)
    rel_dest = str(dest.relative_to(root))
    _notes_log_append(notes_dir, f"archive {rel_src} → {rel_dest} (portal, {uid})")
    return {"ok": True, "archived": rel_dest, "source": rel_src}


# ---- Notes portal V3 (#698): inline note editor -------------------------------
# The viewer's Edit toggle PUTs the full source (frontmatter verbatim, #657). A
# content hash from the GET rides the PUT so a concurrent edit is a 409 instead
# of a silent overwrite; the write is atomic (tmp+replace) and owner-scoped.


def _note_hash(text: str) -> str:
    """A short content hash for the optimistic-concurrency guard (#698)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _notes_resolve_note(notes_dir: str, rel: str, uid: str) -> Path | None:
    """Path-jail + owner-scope a caller-supplied vault note path (#698).

    Like `_notes_resolve_owned` but for any visible note (not just facts): resolve
    under the vault, reject a `..`-escape, and return the path only when the caller
    may see it (own subtree or shared pool, default-deny) and it is a real file."""
    return _notes_resolve_owned(notes_dir, rel, uid)


def _notes_write_note(
    notes_dir: str, path: Path, content: str, prev_hash: str
) -> dict[str, Any]:
    """Overwrite a note's source verbatim, guarded by a content hash (#698).

    Rejects (409-mapped) when the on-disk file changed since the caller's GET
    (its hash no longer matches `prev_hash`) — no silent overwrite. The write is
    atomic (tmp+replace); frontmatter and body are written exactly as supplied."""
    root = Path(notes_dir).resolve()
    try:
        current = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"ok": False, "error": "not_found", "status": 404}
    if _note_hash(current) != prev_hash:
        return {
            "ok": False,
            "error": "stale",
            "status": 409,
            "hash": _note_hash(current),
        }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return {
        "ok": True,
        "path": str(path.relative_to(root)),
        "hash": _note_hash(content),
    }


# ---- Notes portal statistics (#699) -------------------------------------------
# A "Statistik" section over the vault: frequent #tags/topics and @persons (by
# note count), notes per folder/OKF domain, notes-created per month (~12 months),
# and most-[[..]]-linked entities. Computed off-loop in one prune-bounded walk
# (#705), owner-scoped like the rest, and TTL-cached (the vault is small).

_NOTES_STATS_TTL = 600.0
_notes_stats_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_HASHTAG_RE = re.compile(r"(?<![\w/])#([\w/-]+)")
_PERSON_RE = re.compile(r"(?<![\w@])@([\wäöüß-]+)", re.IGNORECASE)
_CREATED_RE = re.compile(r"(?mi)^created:\s*(\d{4}-\d{2})")


def _note_month(rel: str, text: str) -> str | None:
    """The `YYYY-MM` a note was created: `created:` frontmatter first, else a
    leading `YYYY-MM-DD` in the filename (journal/fact naming). None when neither."""
    m = _CREATED_RE.search(text)
    if m:
        return m.group(1)
    stem = rel.replace("\\", "/").rsplit("/", 1)[-1]
    return stem[:7] if re.match(r"\d{4}-\d{2}-\d{2}", stem) else None


def _note_category(rel: str) -> str:
    """The folder/OKF domain a note belongs to for the category breakdown.

    An `okf/<domain>/…` note is its OKF domain; anything else is its top-level
    folder, or `(Wurzel)` at the vault root."""
    parts = rel.replace("\\", "/").split("/")
    if parts[0] == "okf":
        return f"okf/{parts[1]}" if len(parts) > 2 else "okf"
    return parts[0] if len(parts) > 1 else "(Wurzel)"


def _notes_stats_scan(notes_dir: str, uid: str, top_n: int = 12) -> dict[str, Any]:
    """Compute the notes-statistics payload in one prune-bounded walk (#699/#705).

    Blocking (walks the vault, reads files) — callers run it off the event loop.
    Every count is owner-scoped (caller ∪ shared, default-deny). Tags/persons are
    counted by the number of notes that mention them; `[[..]]` link targets by the
    number of notes linking them (backlink counts). `truncated` is set when the
    walk hit `iter_vault_md`'s file budget."""
    root = Path(notes_dir)
    tags: dict[str, int] = {}
    persons: dict[str, int] = {}
    categories: dict[str, int] = {}
    months: dict[str, int] = {}
    links: dict[str, int] = {}
    total = 0
    md_seen = 0
    for path in sorted(notes_search.iter_vault_md(root)):
        md_seen += 1
        try:
            if path.stat().st_size > notes_search._MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(root))
        if not notes_search.is_visible(notes_search.owner_of(rel, text), uid):
            continue
        total += 1
        categories[_note_category(rel)] = categories.get(_note_category(rel), 0) + 1
        month = _note_month(rel, text)
        if month:
            months[month] = months.get(month, 0) + 1
        # Each tag/person/link counted once per note (a note that mentions #x
        # twice still counts as one note for #x).
        for tag in {t.lower() for t in _HASHTAG_RE.findall(text)}:
            tags[tag] = tags.get(tag, 0) + 1
        for person in {p.lower() for p in _PERSON_RE.findall(text)}:
            persons[person] = persons.get(person, 0) + 1
        for m in notes_search._WIKILINK_RE.findall(text):
            target = notes_search._wikilink_target(m).strip()
            if target:
                links[target] = links.get(target, 0) + 1

    def _top(counts: dict[str, int]) -> list[dict[str, Any]]:
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"value": k, "count": v} for k, v in ranked[:top_n]]

    # A dense, gap-free last-12-months series for the growth bars.
    now = datetime.now(timezone.utc)
    series: list[dict[str, Any]] = []
    for i in range(11, -1, -1):
        y, m = divmod((now.year * 12 + now.month - 1) - i, 12)
        key = f"{y:04d}-{m + 1:02d}"
        series.append({"month": key, "count": months.get(key, 0)})

    return {
        "ok": True,
        "counts": {"notes": total},
        "truncated": md_seen >= notes_search._VAULT_WALK_BUDGET,
        "tags": _top(tags),
        "persons": _top(persons),
        "categories": _top(categories),
        "months": series,
        "linked": _top(links),
    }


def _notes_stats_payload(notes_dir: str, db_path: str, uid: str) -> dict[str, Any]:
    """The Statistik payload from `solaris.db` (perf: no vault walk), or the scan.

    Derives tags/persons (inline `mentions`), categories + growth (`concepts`),
    and most-linked (`event_entities` edges) from indexed queries. Falls back to
    the full `_notes_stats_scan` only when the projection is missing."""
    payload = notes_portal_db.stats(db_path, uid)
    if payload is None:
        return _notes_stats_scan(notes_dir, uid)
    return payload


# Readable German labels for the common HA services a favorite can carry (#741),
# so "Häufig genutzt" reads "Bürolicht — Aus" instead of "dimmer 2 — turn_off".
_SERVICE_LABELS = {
    "turn_on": "An",
    "turn_off": "Aus",
    "toggle": "Umschalten",
    "open_cover": "Öffnen",
    "close_cover": "Schließen",
    "stop_cover": "Stopp",
    "set_temperature": "Temperatur",
    "set_hvac_mode": "Modus",
    "set_percentage": "Stufe",
    "set_value": "Wert",
    "set_brightness": "Helligkeit",
}

# Readable labels for the known non-HA tools a favorite can carry (#741).
_TOOL_LABELS = {
    "play_radio": "Radio abspielen",
    "play_music": "Musik abspielen",
    "play_playlist": "Playlist abspielen",
    "stop_playback": "Wiedergabe stoppen",
}


def _humanize(token: str) -> str:
    """Turn a raw slug/service/tool identifier into a readable label: strip a
    leading `domain.` / `_`-word, replace `_` with spaces, title-case."""
    tail = token.split(".", 1)[1] if "." in token else token
    return tail.replace("_", " ").strip().title()


def _service_label(service: str) -> str:
    return _SERVICE_LABELS.get(service, _humanize(service)) if service else ""


def _favorite_label(
    payload: dict[str, Any], names: dict[str, str] | None = None
) -> str:
    """A short label for a usage-counted action (#646) — a readable name +
    action, else the tool's most telling argument, else the tool name (#741).
    `payload` is `{tool, args}` from the counter; `names` is an optional
    entity_id → friendly_name map (from HA) used to resolve device names — when
    absent the entity_id slug is humanized instead so this stays pure."""
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    tool = str(payload.get("tool") or "")
    if tool == "ha_call_service":
        entity_id = str(args.get("entity_id") or "")
        name = (names or {}).get(entity_id) or _humanize(entity_id)
        action = _service_label(str(args.get("service") or ""))
        return f"{name} — {action}".strip(" —") or tool
    if tool in _TOOL_LABELS:
        return _TOOL_LABELS[tool]
    for key in ("query", "station", "title", "name"):
        val = args.get(key)
        if val:
            return str(val)
    return _humanize(tool) if tool else "Aktion"


# Default prompt for an image-only turn (attachment with no typed text), so the
# media-ingestion hook has a turn to trigger on. Mirrors the German tone the
# hook itself uses with residents.
_IMAGE_PROMPT = "Bitte sieh dir dieses Bild an und verarbeite es."
# The lifecycle event an image-only turn fires; the hook that acts on it is
# resolved from the registry (not a hardcoded id) so rebinding it in the
# `/hooks` editor changes which definition handles the upload.
_IMAGE_UPLOAD_EVENT = "image-upload"
# Cap attachments per turn — a small guard against an oversized payload, not a
# product limit (the panel sends at most a couple of camera/upload images).
_MAX_IMAGES = 4

# Native camera upload (#826): the companion app captures 1 image / a series /
# a multi-page PDF and POSTs it to `/napi/upload`. Uploads land in the resident's
# notes vault so they are `notes_search`-visible and PWA-visible — NOT Immich, NOT
# a separate inbox. Allowed types map to their canonical extension; anything else
# is 415. Per-file and per-request caps fail cleanly (413 / 400).
_UPLOAD_MIME_EXT = {"image/jpeg": ".jpg", "application/pdf": ".pdf"}
_UPLOAD_MAX_BYTES = 25 * 1024 * 1024
_UPLOAD_MAX_FILES = 20
# A Google-Takeout `.zip` is an archive, not a vault file: it is stored under the
# resident's uploads dir and handed to the durable import job (never processed
# inline), so it gets its own far-larger cap (a full export is hundreds of MB).
_ARCHIVE_MIME = {"application/zip", "application/x-zip-compressed"}
_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024 * 1024
_ARCHIVE_SUBDIR = "imports"


def _ensure_takeout_zip(data: bytes, filename: str) -> bytes:
    """Wrap a bare Takeout `.json` (e.g. `Wiedergabeverlauf.json` on its own) into
    a single-entry zip so the whole zip-based import pipeline handles it unchanged.
    Bytes that are already a zip (`PK\\x03\\x04`) pass through untouched."""
    if data[:4] == b"PK\x03\x04":
        return data
    name = filename.replace("\\", "/").rsplit("/", 1)[-1] or "Wiedergabeverlauf.json"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, data)
    return buf.getvalue()


# Room for `_UPLOAD_MAX_FILES` max-size parts plus multipart framing, so a legit
# series upload isn't rejected by aiohttp's whole-body `client_max_size` before
# the handler's own per-file guard runs.
_UPLOAD_REQUEST_MAX_BYTES = _UPLOAD_MAX_FILES * _UPLOAD_MAX_BYTES + 8 * 1024 * 1024
# Vault subfolder the raw upload bytes are stored under (the companion note lives
# beside it and embeds it, so `notes_search` — which indexes markdown, not raw
# binaries — can surface the upload).
_UPLOAD_SUBDIR = "uploads"
_UPLOAD_UNSAFE_RE = re.compile(r"[^A-Za-z0-9äöüÄÖÜß._ -]+")

# Browser wake-word recorder (#1081). The page captures raw PCM, downsamples to
# 16 kHz mono int16 and encodes the WAV itself — the chat image is
# `python:3.12-slim` with no ffmpeg, so nothing here can transcode, and this is
# exactly the format the trainer and the gatekeeper's own samples use.
_WAKEWORD_RATE = 16000
_WAKEWORD_MIN_FRAMES = _WAKEWORD_RATE // 4  # 0.25 s — shorter is a mis-tap
_WAKEWORD_MAX_FRAMES = _WAKEWORD_RATE * 4  # 4 s — one word, not a sentence
_WAKEWORD_MAX_BYTES = _WAKEWORD_MAX_FRAMES * 2 + 4096


def _wakeword_pcm_frames(data: bytes) -> int | None:
    """Frame count of a 16 kHz mono 16-bit WAV, or None for anything else."""
    try:
        with wave.open(io.BytesIO(data)) as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (
                1,
                2,
                _WAKEWORD_RATE,
            ):
                return None
            return wav.getnframes()
    except (wave.Error, EOFError):
        return None


def _wakeword_run_view(run: dict | None) -> dict | None:
    """One training run as the card reads it. The timestamps get their `Z`:
    SQLite's `datetime('now')` is UTC without a marker, and the browser would
    otherwise render it as local time and be hours off."""
    if run is None:
        return None

    def stamp(ts: str | None) -> str:
        return ts.replace(" ", "T") + "Z" if ts else ""

    return {
        "status": run["status"],
        "requested_at": stamp(run["requested_at"]),
        "started_at": stamp(run["started_at"]),
        "finished_at": stamp(run["finished_at"]),
        "result": run["result"] or "",
    }


def _sanitize_upload_name(filename: str, mime: str) -> str:
    """A safe vault filename for an uploaded file, extension forced to `mime`.

    Neutralises any path (`../`, separators) by keeping only the basename, maps
    unsafe chars to `_`, and forces the extension to the one implied by the MIME
    so a `.jpg` labelled `application/pdf` (or an extension-less name) is stored
    consistently. Falls back to `upload` when nothing usable remains."""
    ext = _UPLOAD_MIME_EXT[mime]
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    stem = base[: -len(Path(base).suffix)] if Path(base).suffix else base
    stem = _UPLOAD_UNSAFE_RE.sub("_", stem).strip(" ._-")
    return f"{stem or 'upload'}{ext}"


def _unique_upload_path(directory: Path, name: str) -> Path:
    """A collision-free path in `directory` for `name` (append `_1`, `_2`, …)."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, ext = Path(name).stem, Path(name).suffix
    for i in range(1, 1000):
        candidate = directory / f"{stem}_{i}{ext}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{uuid.uuid4().hex}{ext}"


def _write_upload_note(notes_dir: str, uid: str, dest: Path, mime: str) -> str:
    """Write a markdown note beside `dest` that embeds the upload (#826).

    `notes_search` indexes markdown, not raw JPEG/PDF bytes, so an uploaded file
    is only discoverable through a note that names and references it. The note
    sits next to the file in the same `uploads/` folder, titled with the file's
    stem, embeds it via an Obsidian `![[...]]` link, and carries the capture date
    plus `added_by:` so the vault's per-owner scope (#576) treats it as the
    resident's private note. Returns the note's vault-relative path."""
    root = Path(notes_dir).resolve()
    note_path = dest.with_suffix(".md")
    if note_path.exists():
        note_path = _unique_upload_path(dest.parent, f"{dest.stem}_note.md")
    title = dest.stem
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    label = "PDF" if mime == "application/pdf" else "Bild"
    body = (
        f"---\nadded_by: {uid}\ndate: {day}\nkind: upload\n---\n\n"
        f"# {title}\n\n"
        f"{label} hochgeladen am {day}.\n\n"
        f"![[{dest.name}]]\n"
    )
    note_path.write_text(body, encoding="utf-8")
    return str(note_path.relative_to(root))


# Ephemeral/incognito chats (#246): the proxy prepends this to every turn so the
# agent knows nothing is durable — it must NOT auto-ingest notes, write memory
# facts, or otherwise persist anything unless the resident explicitly asks to
# extract a note (which carries a topic and routes through `topic_turn_text`).
_EPHEMERAL_HINT = (
    "[Temporary/incognito chat: this conversation is ephemeral and will be "
    "deleted on close. Do NOT save notes, store memory facts, or persist "
    "anything from it. Persist ONLY if the resident explicitly asks to extract "
    "a note (e.g. 'erstelle hieraus eine Notiz im Topic X').]"
)


_LOCAL_TZ = ZoneInfo("Europe/Berlin")

# The built-in household topic slug (mirrors the frontend `HOUSEHOLD_TOPIC`). A
# chat whose primary topic is this is the pinned "Zuhause" household chat: it is
# pinned to the fast e2b household gateway and never offers thinking, regardless
# of the everyday-chat model preference.
HOUSEHOLD_TOPIC = "household"


def _now_hint() -> str:
    """A fresh local wall-clock line prepended to every user turn.

    the engine stamps the session with a frozen, date-granular "Conversation
    started" line at create time and replays it verbatim; the container also
    runs UTC. Without a per-turn line the agent reports a wrong/frozen
    date-time. the engine binds system_prompt at create and rejects per-turn
    updates, so injecting this into the user turn is the only lever.
    """
    now = datetime.now(_LOCAL_TZ)
    return f"[Aktuelle Zeit: {now.strftime('%A, %d.%m.%Y, %H:%M Uhr %Z')}]"


# Leading internal-hint prefixes the proxy/gatekeeper inject into a user turn so
# the agent reads context the resident never typed (#309). They must NOT appear
# in the rendered history; this matches what's actually injected:
#   server.topic_turn_text  -> "[Aktuelle Zeit: ...]", "[Active topic: ... #topic/<slug>]",
#                              the "[Temporary/incognito ...]" ephemeral guard,
#                              "[Extract this to a note #topic/<slug> (...)]"
#   voice gatekeeper.engine -> "[room: <location>]" (#312/#313)
# Each rides as a leading bracketed block; topic_turn_text joins them with "\n\n",
# the voice room hint with "\n". `[uid:...]` lives on the title (marker.py), but a
# leading one is stripped too for safety. Only LEADING hints are removed so a hint
# the resident actually typed mid-message survives.
_HINT_PREFIX_RE = re.compile(
    r"^\[(?:Aktuelle Zeit:|Temporary/incognito|Active topic:|Extract this to a note|room:|uid:)[^\]]*\]\s*",
    re.IGNORECASE,
)


def strip_internal_hints(content: str) -> str:
    """Drop leading internal-hint prefixes from a user message for DISPLAY (#309).

    Display-only: what was sent to the engine is unchanged — this runs on the way out
    of the messages API. Strips each consecutive leading bracketed hint block,
    then the whitespace it was joined with, leaving the resident's actual text.
    """
    prev = None
    while content != prev:
        prev = content
        content = _HINT_PREFIX_RE.sub("", content, count=1)
    return content


def _version() -> str:
    """The Solaris release version, for the sidebar footer. '' if unavailable.

    Prefers the `SOLARIS_VERSION` env injected at image build (the release
    git tag/ref, see build-images.yml) — the package version in pyproject.toml
    is never bumped (releases are git tags, no release-please), so it would
    always read "0.1.0". Falls back to the package metadata for local/dev
    builds where the env is unset, so the badge still shows something.
    """
    import os

    env = os.environ.get("SOLARIS_VERSION", "").strip()
    if env:
        return env
    try:
        from importlib.metadata import version

        return version("solaris-chat")
    except Exception:  # noqa: BLE001 — metadata absent in some run contexts
        return ""


VERSION = _version()


# Inline mention tokens (#279): a word-boundary `#`/`@` followed by a run of
# tag-safe characters (letters/digits/_-, plus the topic-hierarchy `/`). The
# negative lookbehind keeps mid-word `#`/`@` (e.g. an email's `@`, a `C#`)
# from matching; the captured group excludes the marker char.
_TAG_RE = re.compile(r"(?<![\w/])#([\w/-]+)", re.UNICODE)
_PERSON_RE = re.compile(r"(?<![\w/])@([\w/-]+)", re.UNICODE)


def parse_mentions(text: str) -> tuple[list[str], list[str]]:
    """Split `#tag` / `@person` tokens out of a turn's text.

    Returns `(tags, persons)`, each de-duplicated, lower-cased, order-preserved.
    The leading `#`/`@` is dropped; the bare value is what's stored/suggested.
    """
    tags = _dedup(m.lower() for m in _TAG_RE.findall(text))
    persons = _dedup(m.lower() for m in _PERSON_RE.findall(text))
    return tags, persons


def _dedup(values: Any) -> list[str]:
    seen: dict[str, None] = {}
    for v in values:
        if v:
            seen.setdefault(v, None)
    return list(seen)


# Manual person seed for `@person` autosuggest before a resident has used any
# name in chat (#279). Residents/uids contribute the rest at runtime. CardDAV
# enrichment (#207, parked behind gbrain) is a future source that appends here —
# `seeded_persons()` is the single seam it plugs into. Keep this list small.
_MANUAL_PERSONS = ["mdopp", "anna", "lena"]


def seeded_persons(residents: Any) -> list[str]:
    """The person-suggestion seed: known residents/uids + a manual list.

    De-duplicated, lower-cased. A CardDAV source (#207) would extend this by
    unioning its contact names in here — the autosuggest endpoint reads only
    this function, so adding a source is a one-place change.
    """
    return _dedup(p.lower() for p in [*(residents or []), *_MANUAL_PERSONS])


def _title_from(text: str) -> str:
    """Derive a short session title from the first user message.

    the engine leaves chat-created sessions title-null; we PATCH this in so the
    list shows a meaningful label instead of a placeholder for every row.
    """
    snippet = " ".join(text.split())
    return snippet[:57].rstrip() + "…" if len(snippet) > 60 else snippet


_CONTACT_EMAIL_RE = re.compile(r"[^\s,]+@[^\s,]+\.[^\s,]+")
_CONTACT_PHONE_RE = re.compile(r"\+?[\d][\d\s/()\-]{4,}\d")


def _parse_contact_input(raw: str) -> tuple[str, str, str]:
    """Split a raw `.contacts` blob into (name, email, phone).

    Pulls the first email- and phone-shaped run out of the line (German
    phone/email heuristics) and treats whatever text is left as the name, so a
    mixed input like `michael dopp 01775524222 mdopp@web.de` lands as clean
    structured fields instead of dumping the whole string as the name.
    """
    rest = " ".join(raw.split())
    email = ""
    m = _CONTACT_EMAIL_RE.search(rest)
    if m:
        email = m.group(0)
        rest = (rest[: m.start()] + " " + rest[m.end() :]).strip()
    phone = ""
    m = _CONTACT_PHONE_RE.search(rest)
    if m:
        phone = m.group(0).strip()
        rest = (rest[: m.start()] + " " + rest[m.end() :]).strip()
    name = " ".join(rest.split())
    return (name, email, phone)


def resolve_uid(
    request: web.Request,
    header: str,
    default_uid: str,
    solaris_db_path: str | None = None,
) -> str:
    """Map the Authelia trusted-proxy identity header to an engine uid.

    Precedence (unchanged for the existing paths):

    1. A `sol_device_`-prefixed `Authorization: Bearer` — a native-client
       device token (#717). Resolved via `device_token_store` to its owner_uid,
       so the token authenticates as the resident that minted it. FAIL-CLOSED: an
       unknown/revoked/malformed device token resolves to no uid (empty string),
       NOT `default_uid` — an invalid token must never inherit the loopback
       identity. Only taken when `solaris_db_path` is threaded in; a bearer that
       is NOT `sol_device_`-prefixed (e.g. the SOLARIS_API_KEY service key) is
       left untouched and falls through to the header path below exactly as
       today.
    2. NPM sets `Remote-User` after Authelia authenticates; we fold that into
       the engine uid so there is no second login.
    3. Absent header (e.g. direct loopback access for offline testing) falls
       back to `default_uid`.

    The `/napi/` native prefix (#757) is proxy-BYPASSED by Authelia, so neither
    the `Remote-User` header nor the loopback `default_uid` fallback can be
    trusted there — an unauthenticated internet caller must never inherit the
    household identity. On that prefix resolution is device-token-ONLY and
    fail-closed: no valid `sol_device_` bearer ⇒ empty uid (the native wrapper
    turns that into a 401). See `native_uid`.
    """
    if solaris_db_path is not None:
        native = request.path.startswith(NATIVE_PREFIX)
        auth = request.headers.get("Authorization", "").strip()
        if auth.startswith("Bearer "):
            token = auth[len("Bearer ") :].strip()
            if token.startswith(device_token_store.TOKEN_PREFIX):
                # Fail closed: an invalid/revoked device token resolves to no
                # uid, never the default — it must not inherit loopback privilege.
                return device_token_store.resolve(solaris_db_path, token) or ""
        if native:
            # Fail-closed because proxy-bypassed: no device token ⇒ no uid, never
            # Remote-User (untrusted here) and never default_uid.
            return ""
    value = request.headers.get(header, "").strip()
    return value or default_uid


def native_uid(request: web.Request, solaris_db_path: str | None) -> str | None:
    """The resident behind a valid `sol_device_` bearer on the `/napi/` prefix,
    or None (#757).

    Native (Android-widget) requests reach solaris-chat through an Authelia
    BYPASS, so this path is authenticated by the device token ALONE — no
    `Remote-User` header trust, no `default_uid` fallback. A missing / malformed /
    unknown / revoked token ⇒ None, and the native routes turn that into a 401.
    This is the whole security point: because the prefix is proxy-bypassed, an
    unauthenticated internet caller must NOT get the household identity.
    """
    if solaris_db_path is None:
        return None
    auth = request.headers.get("Authorization", "").strip()
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer ") :].strip()
    if not token.startswith(device_token_store.TOKEN_PREFIX):
        return None
    return device_token_store.resolve(solaris_db_path, token) or None


def is_admin(request: web.Request, header: str, admin_group: str) -> bool:
    """True when the Authelia groups header lists `admin_group`.

    Authelia forwards `Remote-Groups` as a comma-separated list through the
    trusted proxy. Panel writes (phase 2) gate on this; phase-1 reads use it
    only to tell the browser which controls to surface.
    """
    raw = request.headers.get(header, "")
    groups = {g.strip() for g in raw.split(",") if g.strip()}
    return admin_group in groups


def build_app(
    *,
    engine: EngineClient | Any,
    engine_admin: EngineClient | Any = None,
    engine_guest: EngineClient | Any = None,
    engine_enrollment: EngineClient | Any = None,
    remote_user_header: str,
    default_uid: str,
    remote_groups_header: str = "Remote-Groups",
    admin_group: str = "admins",
    skills_dir: str = "/data/skills",
    soul_path: str = "/data/SOUL.md",
    logout_url: str = "",
    context_window: ContextWindow | int = STATIC_DEFAULT,
    compaction_threshold: float = compaction.DEFAULT_THRESHOLD,
    attachments_dir: str = "/data/attachments",
    frame_ancestors: str = "'self'",
    fast_model: str = "",
    thorough_model: str = "",
    tts_voices: str = "martin",
    solaris_db_path: str = "/var/lib/solaris/solaris.db",
    notes_dir: str = "/opt/data/notes",
    ollama_url: str = "http://127.0.0.1:11434",
    trace_recorder: Any = None,
    residents: list[str] | None = None,
    api_key: str = "",
    bus: Any = None,
    event_bus: Any = None,
    notifier: Any = None,
    sb_mcp_url: str = "",
    sb_mcp_token_path: str = "",
    sb_api_url: str = "",
    hass_url: str = "",
    hass_token: str = "",
    crons: Any = None,
    vapid_public_key: str = "",
    android_package: str = "cloud.dopp.solaris",
    android_cert_fingerprints: tuple[str, ...] = (),
    ha_watcher: Any = None,
    native_watch: Any = None,
    sb_companion: Any = None,
    import_jobs: Any = None,
    caldav_url: str = "",
    caldav_username: str = "",
    caldav_password: str = "",
    carddav_url: str = "",
    carddav_username: str = "",
    carddav_password: str = "",
    music_dir: str = "/opt/data/music",
    import_data_dir: str = "/data/imports",
    immich_base_url: str = "",
    immich_api_key: str = "",
    paperless_ui_url: str = "",
    speaker_id_enabled: bool = False,
    model_lease_enabled: bool = True,
) -> web.Application:
    # Known resident uids feeding the `@person` autosuggest seed (#279), beyond
    # the manual list in seeded_persons. The caller's own uid is always folded
    # in at the endpoint, so this is the *other* residents.
    resident_uids = list(residents or [])
    if isinstance(context_window, int):
        context_window = ContextWindow.static(context_window)
    # the engine drops inbound images (persists a `[screenshot]` placeholder, no
    # attachment API), so the proxy persists the sent data URLs itself and
    # re-attaches them on history load (#202) — the one stateful exception.
    attachments = AttachmentStore(attachments_dir)

    # Active streaming turns, keyed by session id (#192). Each entry is an
    # asyncio.Event the stream loop polls; POST /api/chat/cancel sets it, which
    # breaks the loop and closes the upstream engine connection (closing that
    # connection is what actually interrupts the model's generation).
    cancels: dict[str, asyncio.Event] = {}

    # Profile routing: household sessions ride `engine` (the engine's
    # household profile); admin/servicebay-maintenance sessions ride the
    # admin profile. A session created on the admin profile is recorded here
    # so its follow-up turns route back to the same profile. When no admin
    # client is configured both fall back to `engine` (offline-test topology).
    household_gw = engine
    admin_gw = engine_admin or engine
    enrollment_gw = engine_enrollment or engine
    admin_sessions: set[str] = set()
    # Sessions pinned to the household (e4b) gateway — the pinned "Zuhause"
    # chat. Populated at create; the persisted primary topic is the
    # restart-survival source of truth, this set is just the fast path.
    household_sessions: set[str] = set()

    # The everyday-chat reasoning preference (#332-followup / #809): "fast" (no
    # reasoning) or "thorough" (reasoning/thought). Sets the per-turn effort
    # default when the UI selector is absent; both run the same e4b model. Cached
    # in memory; the JSON sidecar beside solaris.db survives restarts. Household
    # chats ignore it (always fast).
    other_model_pref = settings_store.get_other_model_pref(solaris_db_path)

    # The one shared "Zuhause" every resident opens (#649): with speaker-ID off
    # all voice is anonymous-household, so spoken and typed turns are one family
    # conversation in a single deterministic row (owner_uid = default_uid).
    shared_household_id = store.household_session_id(default_uid)

    # The one shared "Wartung" admin ops chat (#786): a deterministic session
    # (like Zuhause) owned by default_uid so every admin opens the same ops
    # conversation. Routed to the admin gateway (ops soul + SB-MCP toolset) and
    # exposed admin-only in /api/whoami; a household user never learns the id.
    shared_wartung_id = store.wartung_session_id(default_uid)

    def effective_uid(uid: str, session_id: str, *, admin: bool = False) -> str:
        """Admit any authenticated resident to the shared household row (#649).

        Owner-scoped reads/writes filter on `owner_uid`; the shared Zuhause is
        owned by `default_uid`, so a resident's real uid would 403/404 against
        it. Map their uid to the owner for that one session, leave every other
        session per-resident (privacy posture unchanged). The shared Wartung row
        (#786) is likewise owned by default_uid — but only for an ADMIN caller
        (#1169): its id is a fixed uuid5 anyone can compute, so mapping every
        resident onto its owner handed them the admin ops conversation. `admin`
        defaults to False so a call site that forgets it fails closed."""
        if session_id == shared_household_id:
            return default_uid
        if session_id == shared_wartung_id and admin:
            return default_uid
        return uid

    def owns_session(uid: str, session_id: str) -> bool:
        """May this caller act in `session_id`? (#1168)

        The chat endpoints were the outlier: `/api/sessions/<id>/events` and the
        trace store already filter on `owner_uid`, but `/api/chat` took the
        body's `session_id` on trust. A row that does not exist yet is nobody's
        — the first turn into a not-yet-materialized shared row lands there — so
        only a *different* owner is refused."""
        owner = store.session_owner(solaris_db_path, session_id)
        return owner is None or owner == uid

    def is_household_chat(uid: str, session_id: str, topic_slug: str) -> bool:
        """True when this turn belongs to the pinned household chat — by the
        first-turn topic, the fast-path set, or the persisted primary topic."""
        if topic_slug == HOUSEHOLD_TOPIC:
            return True
        if session_id and session_id == store.household_session_id(uid):
            # The durable voice/household session (#345) — fast e2b, no think.
            return True
        if session_id and session_id in household_sessions:
            return True
        if session_id:
            assigned = topics_store.get_session_topics(solaris_db_path, session_id, uid)
            return assigned.get("primary") == HOUSEHOLD_TOPIC
        return False

    def gateway_for(
        request: web.Request,
        session_id: str,
        persona: object = None,
        *,
        uid: str = "",
        topic_slug: str = "",
    ) -> EngineClient:
        """Pick the engine gateway for a turn (#293/#332/#809).

        Every household/everyday chat rides the one e4b household gateway; the
        fast/thorough distinction is the reasoning knob on that same model
        (chosen in `choose_effort`), not a separate 12b gateway — 12b was retired
        2026-07-13 (does not fit the 16GB GPU).

        Admin gateway only when the caller is an Authelia admin AND either the
        session was created on the admin gateway (recorded at create) or this
        request explicitly selects the admin/maintenance persona. A non-admin
        caller is ALWAYS routed off the admin gateway — even if it presents a
        known admin session_id — so the #209/#229 gate holds at the routing
        layer too.
        """
        try:
            from solaris_chat import enroll_requests_store, wakeword_requests_store

            if enroll_requests_store.has_any_active_request(
                solaris_db_path
            ) or wakeword_requests_store.has_any_active_request(solaris_db_path):
                return enrollment_gw
        except Exception:
            pass
        if is_household_chat(uid, session_id, topic_slug):
            return household_gw
        sel = request.rel_url.query.get("persona") or persona
        if is_admin(request, remote_groups_header, admin_group):
            # The pinned "Wartung" ops chat (#786) is always the admin gateway
            # for an admin — its ops soul + SB-MCP (read+lifecycle+mutate) toolset.
            if session_id == shared_wartung_id:
                return admin_gw
            if session_id and session_id in admin_sessions:
                return admin_gw
            if sel == personalities.MAINTENANCE_ID:
                return admin_gw
        return household_gw

    def ensure_wartung_row(request: web.Request, session_id: str) -> None:
        """Create the durable Wartung row on an admin's first turn into it (#786).

        The pinned admin ops chat is opened by id (the frontend gets it from
        whoami), so unlike a fresh chat there is no create step — the row is
        materialized lazily here, admin-gated, before the first turn runs."""
        if session_id != shared_wartung_id:
            return
        if not is_admin(request, remote_groups_header, admin_group):
            return
        store.ensure_wartung_session(solaris_db_path, default_uid)

    def pin_admin_identity(request: web.Request) -> None:
        """Bind THIS turn's verified Authelia admin identity for the SB-MCP
        toolbox (#794). Only an admin's forward-auth identity is forwarded — the
        engine exchanges it (Remote-User/Remote-Groups → token-from-authelia-
        session) for a short-lived scoped SB-MCP token, so a non-admin turn
        carries the empty identity and the exchange is never attempted. Set on
        the request-handling task; EngineClient re-pins it inside the dispatch
        task (like current_uid). The headers themselves are trustworthy only
        because NPM's forward-auth chain overwrites any client-supplied copy."""
        if is_admin(request, remote_groups_header, admin_group):
            current_admin_identity.set(
                (
                    request.headers.get(remote_user_header, ""),
                    request.headers.get(remote_groups_header, ""),
                )
            )
        else:
            current_admin_identity.set(("", ""))

    async def maybe_compact(
        uid: str, session_id: str, client: EngineClient
    ) -> tuple[str, bool]:
        """Hard-cap trigger (#210): if an existing session's running token usage
        is near the context-window cap, extract durable learnings to memory and
        compact into a continuation session *before* the next turn runs.

        Returns `(session_id, compacted)` — the continuation id when compaction
        happened, else the original id unchanged. Failure to compact degrades to
        "use the original session" (compact_session returns None), so a turn is
        never lost or blocked by compaction.

        No base_system_prompt is passed (#293): the gateway's profile supplies
        the soul, so the continuation session inherits it without a per-session
        overlay (default `base_system_prompt=""`).

        The durable household session (#345) is never forked: a `Fortsetzung`
        continuation would surface as a second "Zuhause" row, defeating the one
        durable session (#419). It stays in-place — its history grows; the
        overnight compactor still extracts learnings without continuation.
        """
        if session_id == store.household_session_id(uid):
            return session_id, False
        try:
            new_id = await compaction.compact_session(
                client,
                uid,
                session_id,
                context_window=context_window.value,
                threshold=compaction_threshold,
            )
        except EngineError:
            return session_id, False
        if new_id:
            return new_id, True
        return session_id, False

    def new_session_topic(topic_slug: str) -> str | None:
        """The primary topic to persist for a new session, or None (#241/#242).

        The household gateway's profile (#293) now OWNS the soul and the base
        model, so a session no longer carries a per-session persona overlay or a
        model override at create — those would fight the profile. What survives
        is the topic binding: a chat started under a topic is tagged with it (the
        picker selects one before the first message, or a pinned topic-chat #237
        starts pre-assigned) so its turns get the #243 topic context hint and its
        ingested notes are stamped `#topic/<slug>`. Returns the slug to persist
        as primary, or None when no topic was supplied.
        """
        return topic_slug or None

    async def create_turn_session(
        uid: str,
        topic_slug: str,
        text: str,
        ephemeral: bool,
        client: EngineClient,
    ) -> str:
        """Create the session for a first turn; return its id.

        Ephemeral (#246): an incognito chat is created with the `[temp:]` marker
        (kept out of the durable list, deleted on close) plus a unique title
        suffix after the marker (#286 — so two temp chats can't collide on
        the engine's unique-title constraint), is NOT bound to a topic, NOT re-titled
        (re-titling would re-stamp the `[uid:]` marker and surface it), and never
        has a `session_topics` row — it carries no durable state. Normal chats
        bind a primary topic and persist the auto-title.

        No system_prompt overlay or model override is passed (#293): the
        gateway's profile supplies the soul + the base model, so an empty create
        lets the profile decide instead of fighting it.

        `client` is the gateway the caller routed to (#293): household for a
        resident chat (the common case), or the admin gateway when an admin
        selected the admin persona. An admin-gateway create is recorded in
        `admin_sessions` so the session's follow-up turns route back to it.
        """
        if ephemeral:
            # A unique suffix rides after the `[temp:]` marker so a second temp
            # chat can't 400 against the first's bare-marker title (#286, same
            # collision #267/#277 fixed). The marker prefix is preserved, so the
            # chat stays incognito (not-persisted / not-listed).
            session_id = await client.create_session(
                uid,
                ephemeral=True,
                title=_title_from(text),
            )
            log.info(
                "chat.session.created", uid=uid, session_id=session_id, ephemeral=True
            )
            return session_id
        primary = new_session_topic(topic_slug)
        if primary == HOUSEHOLD_TOPIC:
            # The pinned "Zuhause" first turn lands in the ONE shared household
            # session (#345/#419/#649) — the same row voice turns use — so it
            # never mints a fresh session per click/first-turn, and every
            # resident opens the same conversation. Owned by default_uid.
            session_id = store.ensure_household_session(solaris_db_path, default_uid)
            household_sessions.add(session_id)
            # Stamp the household primary topic so the list chip + pinned-row
            # highlight (primary_topic == household) light up for this row (#241).
            topics_store.set_primary(
                solaris_db_path, session_id, HOUSEHOLD_TOPIC, default_uid
            )
            log.info(
                "chat.session.created",
                uid=uid,
                session_id=session_id,
                topic=HOUSEHOLD_TOPIC,
            )
            return session_id
        # Born with a unique marker-embedded title (not the bare `[uid:...]`
        # marker), so a first turn can never 400 against an abandoned
        # bare-marker stub already holding it — the same collision #267 fixed
        # for the compaction path, here on the main first-turn path (#277).
        session_id = await client.create_session(uid, title=_title_from(text))
        if client is admin_gw and client is not household_gw:
            admin_sessions.add(session_id)
        log.info(
            "chat.session.created",
            uid=uid,
            session_id=session_id,
            topic=primary or "",
        )
        if primary:
            topics_store.set_primary(solaris_db_path, session_id, primary, uid)
        return session_id

    def topic_turn_text(
        text: str, uid: str, session_id: str, *, ephemeral: bool, extract_topic: str
    ) -> str:
        """Prepend the active-topic / ephemeral context hint to a turn.

        Normal chat: data ingested from a topic-T chat must be stamped
        `#topic/<slug>` so it is retrievable by topic (#243). The proxy surfaces
        the chat's primary topic as a leading system-context line; any ingestion
        skill in the turn reads it and tags its note. Non-topic chats are
        untouched (no hint).

        Ephemeral chat (#246): the session is incognito, so the proxy does NOT
        consult `session_topics` (an ephemeral chat keeps no durable assignment)
        and instead injects the ephemeral guard hint that tells the agent to
        persist nothing. The one durable escape hatch is an explicit extract:
        when the turn carries `extract_topic`, the topic stamp is appended so the
        single note the agent writes is tagged `#topic/<slug>` — that note is the
        only durable output of the whole conversation.
        """
        if ephemeral:
            parts = [_now_hint(), _EPHEMERAL_HINT]
            if extract_topic:
                display = topics_store.display_name(solaris_db_path, extract_topic)
                label = display or extract_topic
                parts.append(
                    f"[Extract this to a note #topic/{extract_topic} ({label})]"
                )
            parts.append(text)
            return "\n\n".join(parts)
        hint = topics_store.topic_context_hint(solaris_db_path, session_id, uid)
        parts = [_now_hint()]
        if hint:
            parts.append(hint)
        parts.append(text)
        return "\n\n".join(parts)

    def persist_mentions(
        uid: str, session_id: str, text: str, *, ephemeral: bool
    ) -> None:
        """Parse + store the turn's `#tag`/`@person` mentions (#279).

        Skipped for ephemeral chats (they keep no durable state, like topics).
        Degrades to no-op when the DB/table is absent (mentions_store handles it).
        """
        if ephemeral:
            return
        tags, persons = parse_mentions(text)
        persons = _resolve_persons(uid, persons)
        mentions_store.record_mentions(solaris_db_path, session_id, uid, tags, persons)

    def _resolve_persons(uid: str, persons: list[str]) -> list[str]:
        """Resolve each parsed `@token` to a `person` entity (ADR 0010).

        A token matching a person's canonical_name or an alias (case-insensitive)
        is recorded under the entity's canonical name — so `@mike` and `@Michael`
        collapse to the one `Michael` entity. Unmatched tokens keep their free-text
        value (the mentions_store fallback), and no entity is auto-created."""
        directory = documents_portal_db.person_directory(solaris_db_path, uid) or []
        if not directory:
            return persons
        by_name: dict[str, str] = {}
        for p in directory:
            for label in [p["name"], *p["aliases"]]:
                by_name.setdefault(label.lower(), p["name"])
        return _dedup(by_name.get(v.lower(), v) for v in persons)

    def record_anchors(
        uid: str,
        session_id: str,
        anchors: list[str],
        user_text: str,
        *,
        ephemeral: bool,
    ) -> None:
        """Record the agent's auto-surfaced #/@ anchors as turn mentions (#501).

        Anchors keep their `#`/`@` prefix; split into tags/persons and dedup
        against the tokens the user already typed this turn (those are recorded
        by persist_mentions). Skipped for ephemeral chats; no-op when empty.
        """
        if ephemeral or not anchors:
            return
        typed_tags, typed_persons = parse_mentions(user_text)
        typed = {("#", t) for t in typed_tags} | {("@", p) for p in typed_persons}
        tags, persons = [], []
        for a in anchors:
            prefix, value = a[:1], a[1:].strip().lower()
            if not value or (prefix, value) in typed:
                continue
            (persons if prefix == "@" else tags).append(value)
        mentions_store.record_mentions(solaris_db_path, session_id, uid, tags, persons)

    async def persist_turn_trace(
        uid: str,
        session_id: str,
        t0: float,
        *,
        ephemeral: bool,
        ha_cards: list[dict[str, Any]] | None = None,
        suggestions: list[str] | None = None,
        anchors: list[str] | None = None,
    ) -> None:
        """Persist this turn's engine trace steps under a fresh trace_id.

        Native engine tracing: records carry the session id, so the turn's
        steps are an exact filter (`t0` bounds them to this turn) — no
        time-window guessing. Skipped for ephemeral chats (no durable state);
        fail-open — a DB hiccup never breaks the turn that already produced a
        reply.
        """
        if ephemeral or trace_recorder is None:
            return
        try:
            trace_id = uuid.uuid4().hex
            steps = []
            for order, rec in enumerate(trace_recorder.for_session(session_id, t0)):
                # Persist the detail body WITH the step (#451) under a stable
                # per-step key, so the modal still resolves after a reload /
                # engine restart — the recorder's ring id restarts at 0 per
                # process and would otherwise 404. A tool step has no LLM body;
                # `step_row` gives it its arguments + result instead (#1241).
                detail = trace_recorder.detail(rec["id"]) if "id" in rec else None
                steps.append(trace_store.step_row(rec, detail, f"{trace_id}:{order}"))
            # The turn's read-only HA cards (#475) ride the same trace_id as a
            # synthetic step, so the frontend re-attaches them to this turn's
            # bubble on reload alongside the step trace. detail_json carries the
            # card-specs; step_kind tags it so reload can pick it out.
            if ha_cards:
                steps.append(
                    {
                        "step_kind": "ha_cards",
                        "detail_json": json.dumps(ha_cards),
                    }
                )
            # Follow-up chips (#498) ride the same trace_id as a synthetic step,
            # so reload re-attaches them under the turn's bubble (like ha_cards).
            if suggestions:
                steps.append(
                    {
                        "step_kind": "suggestions",
                        "detail_json": json.dumps(suggestions),
                    }
                )
            # Auto-surfaced #/@ anchors (#501) ride the same trace_id as a
            # synthetic step, so reload re-attaches the chips under the bubble.
            if anchors:
                steps.append(
                    {
                        "step_kind": "anchors",
                        "detail_json": json.dumps(anchors),
                    }
                )
            if steps:
                trace_store.persist_trace(
                    solaris_db_path, session_id, trace_id, uid, steps
                )
        except Exception as e:  # noqa: BLE001 — trace persistence is best-effort
            log.warn("chat.trace.persist_error", session_id=session_id, error=str(e))

    async def session_trace(request: web.Request) -> web.Response:
        # The persisted per-turn LLM trace for one chat (#306): the ordered steps
        # the proxy captured, each with model/wall_s/tokens/detail_id, so the
        # panel renders the same trace on reopen. Per-resident scope.
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        session_id = request.match_info["session_id"]
        steps = trace_store.list_session_trace(
            solaris_db_path,
            session_id,
            effective_uid(
                uid,
                session_id,
                admin=is_admin(request, remote_groups_header, admin_group),
            ),
        )
        return web.json_response({"ok": True, "steps": steps})

    async def session_events(request: web.Request) -> web.StreamResponse:
        # The live mirror (#344): a browser opens this for the session it's
        # showing and receives turns that originate elsewhere — voice via the
        # facade, or another tab of the same person — near-live. Per-resident
        # scope: only the session's owner uid sees its turns (privacy posture,
        # like trace_store D3), so a wrong-owner subscribe gets a silent empty
        # stream. The originating request keeps its own /api/chat/stream; this
        # only carries the OTHER clients' view.
        uid = effective_uid(
            resolve_uid(request, remote_user_header, default_uid, solaris_db_path),
            request.match_info["session_id"],
            admin=is_admin(request, remote_groups_header, admin_group),
        )
        session_id = request.match_info["session_id"]
        if store.session_owner(solaris_db_path, session_id) != uid:
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)
        if bus is None:
            await _send_event(resp, "done", {})
            return resp
        streamed = False
        try:
            async for item in bus.subscribe(session_id, uid):
                kind = item.get("kind")
                if kind == "mirror_user":
                    text = strip_internal_hints(str(item["event"].get("text") or ""))
                    await _send_event(resp, "mirror_user", {"text": text})
                    streamed = False
                elif kind == "card":
                    # A server-injected action card (#787) mirrored into the open
                    # chat so the button row renders live.
                    await _send_event(resp, "card", item.get("event") or {})
                elif kind == "mirror_event":
                    name, data = _normalize(item["event"])
                    if name == "delta" and data.get("text"):
                        streamed = True
                    elif name == "completed":
                        # A tool-only turn streams no deltas — surface the final
                        # answer once (the #258 late-delta pattern), but don't
                        # double it when the answer already streamed.
                        answer = data.pop("answer", "")
                        if answer and not streamed:
                            await _send_event(resp, "delta", {"text": answer})
                    await _send_event(resp, name, data)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def portal_events(request: web.Request) -> web.StreamResponse:
        # Live status propagation (#714): an open /p/start client subscribes to
        # its uid's event bus and receives `card_state` (and later `chat`)
        # events the HA-WS watcher publishes, so a pinned entity's card updates
        # within ~1s instead of on the 12s poll. Owner-scoped like the session
        # mirror: a client only sees its own uid's events plus the shared
        # `household` stream, never another resident's — the second subscription
        # carries the household-pinned entities everyone shares.
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)
        if event_bus is None:
            await _send_event(resp, "done", {})
            return resp

        async def _pump(stream) -> None:
            async for event in stream:
                kind = event.get("kind")
                if kind in ("card_state", "chat", "servicebay", "ha"):
                    await _send_event(resp, kind, event.get("data") or {})

        async def _heartbeat_ping() -> None:
            # Without traffic this stream sits idle and something between us and
            # the client eventually drops it, losing external HA changes until a
            # reconnect. An SSE comment (comments aren't events, so clients ignore
            # them) keeps it alive — see _SSE_HEARTBEAT_S for what sets the
            # interval. One interval serves both clients: the browser reconnects
            # freely and falls back to its 12s poll, so the mobile NAT window is
            # the binding constraint and the browser is covered by it. A failed
            # write means the client is gone: raise so gather() unwinds and the
            # finally cancels the pumps.
            while True:
                await asyncio.sleep(_SSE_HEARTBEAT_S)
                await resp.write(b": ping\n\n")

        own = asyncio.ensure_future(_pump(event_bus.subscribe(uid)))
        shared = asyncio.ensure_future(
            _pump(event_bus.subscribe(favorites_store.HOUSEHOLD))
        )
        ping = asyncio.ensure_future(_heartbeat_ping())
        try:
            await asyncio.gather(own, shared, ping)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            own.cancel()
            shared.cancel()
            ping.cancel()
        return resp

    async def portal_watch(request: web.Request) -> web.Response:
        """Store/REPLACE this device's native watch-set (#810).

        Body `{entity_ids:[...]}`. A native widget can watch HA entities the
        resident has NOT web-favorited; those aren't in `pinned_entity_owners`, so
        `ha_watch` would publish nothing for them. This records the entities the
        device's widgets want, keyed by the device (not just its uid — one resident
        may pair several devices) with a TTL, and `ha_watch` unions it into its
        owner map so a state change publishes `card_state` to the device's uid over
        the existing `/napi/portal/events` SSE. No favorites row, nothing in the web
        portal; the app re-POSTs to refresh while widgets exist.

        Only reached via `native(...)` on `/napi/`: device-token-only, fail-closed,
        owner-scoped."""
        if native_watch is None:
            return web.json_response(
                {"ok": False, "error": "watch_unavailable"}, status=503
            )
        auth = request.headers.get("Authorization", "").strip()
        token = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""
        resolved = device_token_store.resolve_device(solaris_db_path, token)
        if resolved is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        device_id, uid = resolved
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        entity_ids = {
            str(e) for e in (body.get("entity_ids") or []) if isinstance(e, str) and e
        }
        native_watch.set(device_id, uid, entity_ids)
        return web.json_response({"ok": True})

    async def servicebay_read(request: web.Request) -> web.Response:
        """Aggregate one ServiceBay companion read for the app (BFF, #811).

        Solaris is the BFF/hub (ADR 0010): the app talks only to Solaris, never to
        ServiceBay. This re-serves ServiceBay's `/napi/{home,approvals,services,
        upgrades}` (servicebay#2252) — consumed server-to-server via the read-scoped
        SB-MCP token — under Solaris's OWN `/napi/servicebay/*`, so the app gets
        ServiceBay data over its one Solaris `/napi` without knowing ServiceBay.

        Only reached via `native(...)` on `/napi/`: device-token-only, fail-closed,
        read-only. Returns ServiceBay's body verbatim; a 502 when SB is unreachable,
        a 503 when no SB companion is configured."""
        key = request.match_info["key"]
        if key not in sb_companion_module.READ_PATHS:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        if sb_companion is None or not sb_companion.enabled:
            return web.json_response(
                {"ok": False, "error": "servicebay_unconfigured"}, status=503
            )
        body = await sb_companion.read(key)
        if body is None:
            return web.json_response(
                {"ok": False, "error": "servicebay_unavailable"}, status=502
            )
        return web.json_response(body)

    async def servicebay_operate(request: web.Request) -> web.Response:
        """Run a lifecycle action on a ServiceBay service for the app
        (BFF, ADR 0010, #827 operate half).

        Solaris is the BFF/hub: the app asks Solaris to start/stop/restart a
        service and Solaris forwards it to SB's lifecycle-scoped `POST
        /napi/services/:name/operate` (servicebay#2264) via the SB-MCP token —
        the app never talks to ServiceBay. Body `{action: start|stop|restart}`.

        Only reached via `native(...)` on `/napi/`: device-token-only,
        fail-closed (401 without a valid `sol_device_` bearer). An action outside
        start/stop/restart is 400; SB unreachable / non-2xx is 502; no SB
        companion configured is 503. Client-side confirmation (#44) gates the
        button; the route itself is not confirm-gated. Infra mutation — exposed
        under the device-token native surface, re-checked against SB's lifecycle
        scope on the ServiceBay side."""
        name = request.match_info["name"]
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        action = body.get("action")
        if action not in sb_companion_module.OPERATE_ACTIONS:
            return web.json_response({"ok": False, "error": "bad_action"}, status=400)
        if sb_companion is None or not sb_companion.enabled:
            return web.json_response(
                {"ok": False, "error": "servicebay_unconfigured"}, status=503
            )
        ok, detail = await sb_companion.operate(name, action)
        if not ok:
            return web.json_response(
                {
                    "ok": False,
                    "error": "servicebay_unavailable",
                    "detail": detail[:2000],
                },
                status=502,
            )
        return web.json_response({"ok": True, "name": name, "action": action})

    async def servicebay_approval_verdict(request: web.Request) -> web.Response:
        """Proxy an admin's Approve/Reject verdict on a ServiceBay approval
        (BFF, ADR 0010, #811 part 2).

        Served on the Authelia-gated `/api/` surface — the caller's Remote-User/
        Remote-Groups are TRUSTED here (NPM forward-auth injected them), so
        `is_admin()` genuinely gates admins, unlike the proxy-bypassed `/napi/`
        prefix. The app's notification button deep-links to open the app (its
        Authelia session), then hits this route. The verdict itself runs under a
        per-action, single-use `X-SB-Delegated-Admin` assertion minted from THIS
        admin's session (servicebay#2276) — no standing delegation key in the pod.

        Admin-gated (403 for a non-admin), then `pin_admin_identity` binds the
        forward-auth identity. The companion mints the delegated-admin assertion
        by forwarding THIS admin's `authelia_session` cookie to SB's www portal
        mint (servicebay#2285) — the cookie is `dopp.cloud`-scoped, so the browser
        already sent it to this subdomain."""
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        verb = request.match_info["verb"]
        if verb not in ("approve", "reject"):
            return web.json_response({"ok": False, "reason": "bad_verb"}, status=404)
        approval_id = request.match_info["id"]
        if sb_companion is None or not sb_companion.enabled:
            return web.json_response(
                {"ok": False, "reason": "servicebay_unconfigured"}, status=503
            )
        pin_admin_identity(request)
        authelia_cookie = request.cookies.get("authelia_session", "")
        ok, detail = await sb_companion.submit_verdict(
            approval_id, verb, authelia_cookie
        )
        status = 200 if ok else 502
        return web.json_response(
            {"ok": ok, "approval_id": approval_id, "detail": detail[:2000]},
            status=status,
        )

    async def servicebay_approval_detail(request: web.Request) -> web.Response:
        """One pending ServiceBay approval, for the verdict page (#1085).

        The companion cannot decide an approval with its device token
        (servicebay#2249), so it opens `#/p/servicebay/approvals/<id>` in a
        Custom Tab, which carries the Authelia cookies. That page has to show
        WHAT is being approved before it offers the buttons — this serves it.

        Same admin gate as the verdict route: whoever may not decide has no
        business reading the request body either.

        A missing id answers `gone`, not `not_found`. SB's companion feed lists
        only pending requests, so the overwhelmingly likely reason is that the
        approval was already decided — on another device or in the ServiceBay
        UI. Calling that a broken link would send the admin looking for a bug
        that isn't there.
        """
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        if sb_companion is None or not sb_companion.enabled:
            return web.json_response(
                {"ok": False, "reason": "servicebay_unconfigured"}, status=503
            )
        approval_id = request.match_info["id"]
        body = await sb_companion.read("approvals")
        if body is None:
            return web.json_response(
                {"ok": False, "reason": "servicebay_unavailable"}, status=502
            )
        entries = body.get("approvals") if isinstance(body, dict) else None
        match = next(
            (
                a
                for a in (entries or [])
                if isinstance(a, dict) and a.get("id") == approval_id
            ),
            None,
        )
        if match is None:
            return web.json_response({"ok": False, "reason": "gone"}, status=404)
        # Only the fields the page renders. SB's `payload`/`on_approve` carry
        # the mechanics of the pending operation (token-request ids, tool args);
        # the page shows none of it, so it does not travel to the browser.
        return web.json_response(
            {
                "ok": True,
                "approval": {
                    "id": approval_id,
                    "service": str(match.get("service") or ""),
                    "title": str(match.get("title") or ""),
                    "description": str(match.get("description") or ""),
                    "node": str(match.get("node") or ""),
                    "created_at": str(match.get("created_at") or ""),
                },
            }
        )

    async def inject_message(request: web.Request) -> web.Response:
        # Server-initiated turn into a resident's chat (Wartung P1a, #785): the
        # Wartung chat (#784) needs the server to speak first. Admin-gated — it
        # writes into a resident's durable history and pushes their phone. The
        # target session is the resident's household chat unless one is given.
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        target_uid = body.get("uid")
        text = body.get("text")
        card = body.get("card")
        if not isinstance(target_uid, str) or not target_uid.strip():
            return web.json_response({"ok": False, "reason": "no_uid"}, status=400)
        if not isinstance(text, str) or not text.strip():
            return web.json_response({"ok": False, "reason": "no_text"}, status=400)
        if card is not None and not isinstance(card, dict):
            return web.json_response({"ok": False, "reason": "bad_card"}, status=400)
        if event_bus is None:
            return web.json_response({"ok": False, "reason": "no_bus"}, status=503)
        session_id = body.get("session_id") or store.ensure_household_session(
            solaris_db_path, target_uid
        )
        await inject(
            solaris_db_path,
            event_bus,
            notifier,
            session_id,
            target_uid,
            text,
            card=card,
        )
        # An action card (#787) also mirrors onto the SessionBus so an open chat
        # renders its button row live (the EventBus path drives push/start-page).
        if bus is not None and isinstance(card, dict) and card.get("kind") == "action":
            bus.publish(
                session_id, target_uid, {"kind": "card", "event": {"card": card}}
            )
        return web.json_response({"ok": True, "session_id": session_id})

    async def action_callback(request: web.Request) -> web.Response:
        # Action-card button press (Wartung P2a, #787): map `action_id` to its
        # server-side handler and run it. A destructive action is confirm-gated
        # (#702 pattern): it must not fire on a bare tap, so an unconfirmed one
        # is 403 and the client re-sends with `confirmed=true`.
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        action_id = body.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            return web.json_response(
                {"ok": False, "reason": "no_action_id"}, status=400
            )
        handler = action_cards.get(action_id)
        if handler is None:
            return web.json_response(
                {"ok": False, "reason": "unknown_action"}, status=404
            )
        # Stamp the caller's verified uid onto the body so a per-resident handler
        # (the import callbacks, #869) scopes to the acting resident rather than a
        # client-supplied value. Overwrites any body-provided `uid`.
        body["uid"] = resolve_uid(
            request, remote_user_header, default_uid, solaris_db_path
        )
        # An admin-only action must not fire for a non-admin caller — checked
        # before the confirm-gate so a resident can't self-supply confirmed=true
        # to reach a privileged handler (#788/#789 SB-MCP deploy/exec).
        # On the proxy-bypassed `/napi/` prefix (#757) `Remote-Groups` is
        # client-supplied — NPM's forward-auth chain, the only thing that
        # overwrites it, does not run there — so it can never prove admin and an
        # admin action is refused outright (#1170). Non-admin actions (the import
        # callbacks) keep working for the device-token holder.
        if handler.admin and (
            request.path.startswith(NATIVE_PREFIX)
            or not is_admin(request, remote_groups_header, admin_group)
        ):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        if handler.destructive and not body.get("confirmed"):
            return web.json_response(
                {"ok": False, "reason": "confirm_required"}, status=403
            )
        # Pin the acting admin's verified Authelia identity so an admin handler's
        # SB-MCP call mints its token from THIS admin's session (#794), not a
        # standing credential (#788 [Deploy]).
        if handler.admin:
            pin_admin_identity(request)
        result = await handler.run(body)
        return web.json_response(result)

    async def trace_detail(request: web.Request) -> web.Response:
        # Exact per-call content for one trace step (#307 panel → #305 detail).
        # A persisted turn carries a stable `<trace_id>:<order>` detail_id whose
        # body lives in the trace store (#451), so it survives a reload/restart;
        # the in-flight turn still carries the recorder's bare integer ring id,
        # served live. Try the persisted body first, then the ring.
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        detail_id = request.match_info["detail_id"]
        body = trace_store.detail_for(solaris_db_path, uid, detail_id)
        if body is not None:
            return web.Response(body=body, content_type="application/json")
        detail = (
            trace_recorder.detail(int(detail_id))
            if trace_recorder is not None and detail_id.isdigit()
            else None
        )
        # The ring is process-wide and its ids are small integers, so an unscoped
        # read let any resident walk other residents' prompt/response bodies
        # (#1171). Scope it like the persisted path: the caller must own the
        # session the call ran in; an unknown session is nobody's and stays 404.
        if detail is not None:
            ring_session = str(detail.get("session_id") or "")
            ring_owner = store.session_owner(solaris_db_path, ring_session)
            if ring_owner is None or ring_owner != effective_uid(
                uid,
                ring_session,
                admin=is_admin(request, remote_groups_header, admin_group),
            ):
                detail = None
        if detail is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(detail)

    async def index(_request: web.Request) -> web.Response:
        # `no-cache` = the browser may cache but MUST revalidate (ETag/
        # Last-Modified, set by FileResponse) before serving. Without it the
        # HTML shell — which carries all the inline JS/CSS — is heuristically
        # cached, so mobile keeps showing a stale UI and deploys don't land.
        return web.FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    async def service_worker(_request: web.Request) -> web.Response:
        """Serve the push service worker at ROOT scope (#713).

        A service worker's control scope is capped at its own URL path, so the
        push SW must be served from `/` — not `/static/` — to control the whole
        PWA. `Service-Worker-Allowed: /` widens the allowed scope and `no-cache`
        makes an updated SW land on the next deploy."""
        return web.FileResponse(
            STATIC_DIR / "sw.js",
            headers={
                "Content-Type": "application/javascript",
                "Service-Worker-Allowed": "/",
                "Cache-Control": "no-cache",
            },
        )

    async def assetlinks(_request: web.Request) -> web.Response:
        """Serve the Digital Asset Links statement at ROOT well-known (#716).

        Binds the Android TWA to this domain so the app runs without a URL bar.
        Google's verifier fetches this UNAUTHENTICATED, so — like /sw.js — it is
        a dedicated public route, not behind the panel's identity header. It is
        only publicly reachable once ServiceBay adds a forwardAuth exception for
        `/.well-known/*` on the chat.dopp.cloud proxy (tracked separately); the
        payload is correct regardless. Empty fingerprints ⇒ `[]` (valid — Google
        just won't verify until the android repo's signing key exists)."""
        statement = (
            [
                {
                    "relation": ["delegate_permission/common.handle_all_urls"],
                    "target": {
                        "namespace": "android_app",
                        "package_name": android_package,
                        "sha256_cert_fingerprints": list(android_cert_fingerprints),
                    },
                }
            ]
            if android_cert_fingerprints
            else []
        )
        return web.json_response(statement)

    async def download(_request: web.Request) -> web.Response:
        """Redirect `www.dopp.cloud/download` to the latest signed companion APK.

        A stable, shareable install/update link: it 302s to GitHub's
        `releases/latest/download/app-release.apk`, so it always resolves to the
        newest CI-published build without ever touching this route. For a truly
        public (pre-login) install, ServiceBay must add a forwardAuth exception
        for `/download` on the proxy — same as `/.well-known/*`; logged-in users
        (the update case) reach it regardless."""
        raise web.HTTPFound(ANDROID_APK_URL)

    async def health(_request: web.Request) -> web.Response:
        """Readiness — can this service do its job? (#1273)

        The old handler returned `{"ok": True}` unconditionally, so it proved
        only that aiohttp accepts requests. Through the whole #1271 outage the
        ServiceBay tile stayed green while `/napi/*` threw 500 on every request
        and the scheduler, cron and HA watchers logged errors every minute; it
        surfaced after more than a day, because a resident picked up their phone.

        So the probe opens the one dependency this service owns and is useless
        without — `solaris.db` — read-only and reads its schema. That is exactly
        the fault that broke: `Path.exists()` raised `PermissionError`.

        NOT checked, deliberately: Ollama, Home Assistant, Radicale, Paperless.
        A probe that folds in neighbours goes red whenever one of them coughs,
        which makes the tile worthless again, only in the other direction —
        Solaris without Ollama is still a working chat server with its history.
        An overview of the neighbours belongs in its own field, never in the
        status code ServiceBay's tile hangs on.

        Stays unauthenticated, and answers the same 503 the `/napi/` gate does
        for the same state — one state, one answer.
        """
        reason = await asyncio.to_thread(db_health.probe, solaris_db_path)
        if reason:
            log.error("health.db_unavailable", db=solaris_db_path, reason=reason)
            return web.json_response(
                {"ok": False, "error": "database_unavailable", "reason": reason},
                status=503,
            )
        return web.json_response({"ok": True})

    async def ha_call(request: web.Request) -> web.Response:
        """Card-action endpoint (#476): run a scoped HA service on one entity.

        Phase 2 surfaced toggles on light/switch; phase 3 (#477) adds the slider/
        colour/climate controls, so cover and climate cards may act too; #1212
        adds `lock` so a lock card can lock/unlock. The
        helper applies the same allowlist as the `ha_call_service` tool (blocked
        domains, name regex, domain==entity). Owner-scoped: any authenticated
        resident, no client-side HA token. Returns the new state to confirm.
        """
        if not hass_url or not hass_token:
            return web.json_response(
                {"ok": False, "error": "ha_not_configured"}, status=503
            )
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        entity_id = str(body.get("entity_id") or "")
        service = str(body.get("service") or "")
        # `button` stays OFF this list by explicit decision, not oversight (#1212):
        # a door-opener's `button.press` unlatches the door and no SENSITIVE_*
        # rule covers it, so a mistap would let someone in unconfirmed.
        if entity_id.split(".", 1)[0] not in (
            "light",
            "switch",
            "cover",
            "climate",
            "lock",
        ):
            return web.json_response(
                {"ok": False, "error": "unsupported_domain"}, status=400
            )
        # Confirm gate for a card tap (#702): a garage/door/gate cover open is
        # sensitive and must not fire on a bare tap. The client shows an explicit
        # confirm dialog and re-sends with `confirmed=true`; the server re-checks
        # here so the gate is authoritative — an unconfirmed sensitive tap is 403.
        if await _ha_call_is_sensitive(entity_id, service) and not body.get(
            "confirmed"
        ):
            return web.json_response(
                {"ok": False, "error": "sensitive_action"}, status=403
            )
        data = body.get("data")
        result = await call_service_scoped(
            hass_url,
            hass_token,
            entity_id,
            service,
            data if isinstance(data, dict) else None,
        )
        return web.json_response(result, status=200 if result.get("ok") else 400)

    async def whoami(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        admin = is_admin(request, remote_groups_header, admin_group)
        # The pinned "Wartung" ops chat (#786) is admin-only: its deterministic
        # session id is handed to the browser ONLY for an admin, so a household
        # user's UI never learns it and thus can never render or open the row.
        # The durable row is created lazily on the first admin turn (like the
        # #345 household session), so whoami stays a pure read.
        wartung_session_id = shared_wartung_id if admin else ""
        return web.json_response(
            {
                "ok": True,
                "uid": uid,
                "is_admin": admin,
                "version": VERSION,
                "logout_url": logout_url,
                "context_window": context_window.value,
                # The ONE shared household session every resident opens (#649):
                # the pinned "Zuhause" row opens this id so spoken (anonymous-
                # household) and typed turns are the same conversation, visible
                # to every logged-in resident instead of split per-uid.
                "household_session_id": shared_household_id,
                # The shared admin ops chat id (#786), admin-only (empty for a
                # household user) — the browser pins a "Wartung" row from it.
                "wartung_session_id": wartung_session_id,
                # The Web Push VAPID public key (#713) the browser needs to
                # subscribe. Empty ⇒ Web Push is unconfigured, so the UI hides
                # the notification bell.
                "vapid_public_key": vapid_public_key,
                # The public paperless Web-UI URL (#1043): the read-only
                # Dokumente portal links here for full-text search + corrections.
                # Empty ⇒ the portal hides the outbound link (no dead link).
                "paperless_ui_url": paperless_ui_url,
            }
        )

    async def list_toolsets(_request: web.Request) -> web.Response:
        try:
            toolsets = await engine.list_toolsets()
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "toolsets": toolsets})

    def _admin_mcp() -> McpToolbox | None:
        """The admin profile's ServiceBay MCP toolbox, when one is wired.

        Since #386 the admin toolbox may be a `CombinedToolbox` wrapping the
        McpToolbox alongside local onboarding tools — find the McpToolbox in
        either shape."""
        toolbox = getattr(getattr(admin_gw, "_profile", None), "toolbox", None)
        if isinstance(toolbox, McpToolbox):
            return toolbox
        for box in getattr(toolbox, "_boxes", []):
            if isinstance(box, McpToolbox):
                return box
        return None

    async def _deploy_update(body: dict[str, Any]) -> dict[str, Any]:
        """[Deploy] on a Wartung update-card (#788): install the service's newest
        template via SB-MCP, then report the outcome back into the Wartung chat.

        Registered admin=True AND destructive=True, so the endpoint refuses a
        non-admin caller and requires `confirmed=true` before this runs; the
        acting admin's identity is already pinned (action_callback), so the
        toolbox mints its token from that session (#794). Fail-open: no toolbox
        or an SB-MCP error is reported into the chat, never raised at the button.
        """
        params = body.get("params")
        service = params.get("service") if isinstance(params, dict) else None
        if not isinstance(service, str) or not service.strip():
            return {"ok": False, "reason": "no_service"}
        mcp = _admin_mcp()
        if mcp is None:
            return {"ok": False, "reason": "no_mcp"}
        await mcp.prepare()
        result = await mcp.dispatch(
            "install_template", {"names": [service], "wipeMode": "install"}
        )
        if event_bus is not None:
            uid = default_uid
            await inject(
                solaris_db_path,
                event_bus,
                notifier,
                store.wartung_session_id(uid),
                uid,
                f"Deploy von „{service}“ gestartet: {result[:400]}",
            )
        return {"ok": True, "service": service, "result": result[:2000]}

    action_cards.register(
        updates.DEPLOY_ACTION, _deploy_update, admin=True, destructive=True
    )

    async def _approval_verdict(body: dict[str, Any], approve: bool) -> dict[str, Any]:
        """Deliver an [Approve]/[Deny] verdict on a Wartung approval-card (#790):
        POST the operator's decision to ServiceBay's verdict endpoint, then report
        the outcome back into the Wartung chat.

        The verdict runs under a LIVE admin's session-exchanged mutate-scope token
        (#794) — action_callback pinned the acting admin's identity, so the token
        is minted from THAT session, never a standing credential. Fail-open at the
        button: an unreachable SB or a non-2xx is reported into the chat, never
        raised. `approve` is registered destructive=True (it runs the request's
        declared side effect), so a bare tap can't fire it; `deny` needs no
        confirm."""
        params = body.get("params")
        approval_id = params.get("approval_id") if isinstance(params, dict) else None
        if not isinstance(approval_id, str) or not approval_id.strip():
            return {"ok": False, "reason": "no_approval_id"}
        if not sb_api_url:
            return {"ok": False, "reason": "no_sb_api"}
        token = await exchange_sb_token(sb_api_url)
        if not token:
            return {"ok": False, "reason": "no_token"}
        ok, detail = await approvals.submit_verdict(
            sb_api_url, token, approval_id, approve
        )
        verb = "genehmigt" if approve else "abgelehnt"
        if event_bus is not None:
            outcome = f"Freigabe {verb}" if ok else f"Freigabe fehlgeschlagen ({verb})"
            await inject(
                solaris_db_path,
                event_bus,
                notifier,
                store.wartung_session_id(default_uid),
                default_uid,
                f"{outcome}: {detail[:400]}",
            )
        return {"ok": ok, "approval_id": approval_id, "detail": detail[:2000]}

    async def _approve_request(body: dict[str, Any]) -> dict[str, Any]:
        return await _approval_verdict(body, approve=True)

    async def _deny_request(body: dict[str, Any]) -> dict[str, Any]:
        return await _approval_verdict(body, approve=False)

    action_cards.register(
        approvals.APPROVE_ACTION, _approve_request, admin=True, destructive=True
    )
    action_cards.register(approvals.DENY_ACTION, _deny_request, admin=True)

    async def _escalation_sink(
        op: dict[str, Any], request_id: str, _approval_id: str | None
    ) -> None:
        """Inject the one-shot delete/exec approval-card into the Wartung chat
        (#789). Called from the admin turn's SB-MCP dispatch when a destroy/exec
        call was refused and parked as a one-shot request; the card's [Approve]
        runs it once with the owner-minted token. Fail-open: no bus ⇒ no card
        (the model still saw the "pending approval" tool result)."""
        if event_bus is None:
            return
        await inject(
            solaris_db_path,
            event_bus,
            notifier,
            store.wartung_session_id(default_uid),
            default_uid,
            f"Freigabe angefragt: {escalation.op_label(op.get('tool_name'), op.get('service'))}.",
            card=escalation.card(op, request_id),
        )

    mcp_box = _admin_mcp()
    if mcp_box is not None:
        mcp_box._on_escalation = _escalation_sink

    async def _run_approved_op(body: dict[str, Any]) -> dict[str, Any]:
        """[Approve] on a one-shot delete/exec card (#789): collect the owner-
        minted single-use token and run the bound op ONCE, then report into the
        Wartung chat.

        Registered admin=True AND destructive=True — the endpoint refuses a
        non-admin and requires `confirmed=true`. The one-shot token is used on a
        fresh connection and never stored on the ambient toolbox, so the ambient
        SB-MCP token stays read+lifecycle+mutate (no standing elevation)."""
        params = body.get("params")
        if not isinstance(params, dict):
            return {"ok": False, "reason": "no_params"}
        request_id = params.get("request_id")
        tool_name = params.get("tool_name")
        arguments = params.get("arguments") or {}
        if not isinstance(request_id, str) or not request_id.strip():
            return {"ok": False, "reason": "no_request_id"}
        if not isinstance(tool_name, str) or not tool_name.strip():
            return {"ok": False, "reason": "no_tool"}
        mcp = _admin_mcp()
        if mcp is None:
            return {"ok": False, "reason": "no_mcp"}
        await mcp.prepare()
        ok, detail = await mcp.run_one_shot(tool_name, arguments, request_id)
        if event_bus is not None:
            outcome = "ausgeführt" if ok else "fehlgeschlagen"
            await inject(
                solaris_db_path,
                event_bus,
                notifier,
                store.wartung_session_id(default_uid),
                default_uid,
                f"{tool_name} {outcome}: {detail[:400]}",
            )
        return {"ok": ok, "tool_name": tool_name, "detail": detail[:2000]}

    async def _deny_op(body: dict[str, Any]) -> dict[str, Any]:
        """[Deny] on a one-shot delete/exec card (#789): nothing runs. The parked
        one-shot request is left for the owner's SB Approvals view to reject or
        lapse; the ambient token never gained delete/exec, so denying is a no-op
        beyond acknowledging the card."""
        params = body.get("params")
        tool_name = params.get("tool_name") if isinstance(params, dict) else None
        return {"ok": True, "denied": True, "tool_name": tool_name}

    action_cards.register(
        escalation.RUN_ACTION, _run_approved_op, admin=True, destructive=True
    )
    action_cards.register(escalation.DENY_ACTION, _deny_op, admin=True)

    async def list_mcp(request: web.Request) -> web.Response:
        # The engine's MCP surface is the admin profile's servicebay_admin
        # toolbox — report it (name/url/reachable/tools, no tokens).
        mcp = _admin_mcp()
        if mcp is None:
            return web.json_response({"ok": True, "servers": []})
        # Carry the admin's Authelia identity into the probe so a stale token
        # can be re-exchanged here too (#794); a non-admin caller pins nothing.
        pin_admin_identity(request)
        await mcp.prepare()
        names = mcp.names()
        return web.json_response(
            {
                "ok": True,
                "servers": [
                    {
                        "name": "servicebay_admin",
                        "url": mcp.url,
                        "reachable": bool(names),
                        "tools": names,
                    }
                ],
            }
        )

    async def test_mcp(request: web.Request) -> web.Response:
        # Interactive Tools-panel tester (#191): run one MCP tool with operator
        # args. Admin-gated — invoking a tool can mutate (e.g. restart_service),
        # so it carries the same gate as the other write controls.
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        tool = body.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            return web.json_response({"ok": False, "reason": "empty_tool"}, status=400)
        arguments = body.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return web.json_response(
                {"ok": False, "reason": "invalid_arguments"}, status=400
            )
        mcp = _admin_mcp()
        if mcp is None or request.match_info["server"] != "servicebay_admin":
            return web.json_response({"ok": False, "error": "Unknown MCP server"})
        pin_admin_identity(request)
        await mcp.prepare()
        output = await mcp.dispatch(tool.strip(), arguments)
        log.info(
            "chat.mcp.test",
            uid=resolve_uid(request, remote_user_header, default_uid, solaris_db_path),
            server=request.match_info["server"],
            tool=tool.strip(),
        )
        return web.json_response({"ok": True, "result": output})

    async def cancel_chat(request: web.Request) -> web.Response:
        # Interrupt an in-flight stream for a session (#192). Sets the cancel
        # event the stream loop polls; the loop then stops reading from the engine
        # and closes that connection, releasing the model run.
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            body = {}
        session_id = str((body or {}).get("session_id") or "")
        event = cancels.get(session_id) if session_id else None
        if event is None:
            return web.json_response({"ok": True, "cancelled": False})
        event.set()
        log.info(
            "chat.stream.cancelled",
            uid=resolve_uid(request, remote_user_header, default_uid, solaris_db_path),
            session_id=session_id,
        )
        return web.json_response({"ok": True, "cancelled": True})

    async def list_personalities(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "personalities": personalities.catalog()})

    async def list_skills(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "skills": skills.list_skills(skills_dir)})

    async def get_skill(request: web.Request) -> web.Response:
        skill = skills.read_skill(skills_dir, request.match_info["skill_id"])
        if skill is None:
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        return web.json_response({"ok": True, "skill": skill})

    async def put_skill(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        skill_id = request.match_info["skill_id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return web.json_response(
                {"ok": False, "reason": "empty_content"}, status=400
            )
        try:
            result = skills.write_skill(skills_dir, skill_id, content)
        except OSError:
            return web.json_response(
                {"ok": False, "reason": "write_failed"}, status=500
            )
        if result is None:
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        log.info(
            "chat.skill.edited",
            uid=resolve_uid(request, remote_user_header, default_uid, solaris_db_path),
            skill=skill_id,
            frontmatter_changed=result["frontmatter_changed"],
        )
        return web.json_response(
            {"ok": True, "restart_needed": result["frontmatter_changed"]}
        )

    def _valid_kind(request: web.Request) -> str | None:
        kind = request.match_info["kind"]
        return kind if kind in skills.KINDS else None

    async def list_defs(request: web.Request) -> web.Response:
        kind = _valid_kind(request)
        if kind is None:
            return web.json_response(
                {"ok": False, "reason": "unknown_kind"}, status=404
            )
        # A tool-kind def carries its declarative plugin surface (dot-command,
        # cell-schema, action ids) — serve the richer row (#1004, ADR 0011).
        defs = (
            skills.list_tool_defs(skills_dir)
            if kind == "tool"
            else skills.list_defs(skills_dir, kind)
        )
        return web.json_response({"ok": True, "kind": kind, "defs": defs})

    async def napi_tool_defs(request: web.Request) -> web.Response:
        """The tool catalog for native/device-token consumers (#1021, ADR 0011).

        Same payload `GET /api/defs/tool` serves — `tool-id`, `tool-label`,
        `tool-api-path`, `tool-search-path`, `tool-compose-path`,
        `tool-item-id-field`, `tool-actions`, `tool-cell-schema`,
        `tool-action-params` —
        but on the proxy-bypassed `/napi/` surface (device-token-ONLY, wrapped in
        `native(...)`), so an Android home-screen widget can consume a new `.tool`
        with zero native code. Only the tool kind is mirrored here: the native
        widget surface consumes tools, not the authoring kinds (command/hook/…)."""
        return web.json_response(
            {"ok": True, "kind": "tool", "defs": skills.list_tool_defs(skills_dir)}
        )

    async def get_def(request: web.Request) -> web.Response:
        kind = _valid_kind(request)
        if kind is None:
            return web.json_response(
                {"ok": False, "reason": "unknown_kind"}, status=404
            )
        one = skills.read_def(skills_dir, kind, request.match_info["def_id"])
        if one is None:
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        return web.json_response({"ok": True, "def": one})

    async def put_def(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        kind = _valid_kind(request)
        if kind is None:
            return web.json_response(
                {"ok": False, "reason": "unknown_kind"}, status=404
            )
        def_id = request.match_info["def_id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return web.json_response(
                {"ok": False, "reason": "empty_content"}, status=400
            )
        try:
            result = skills.write_def(skills_dir, kind, def_id, content)
        except OSError:
            return web.json_response(
                {"ok": False, "reason": "write_failed"}, status=500
            )
        if result is None:
            # Bad id, or the content's kind contradicts the registry.
            return web.json_response(
                {"ok": False, "reason": "kind_mismatch"}, status=400
            )
        log.info(
            "chat.def.edited",
            uid=resolve_uid(request, remote_user_header, default_uid, solaris_db_path),
            kind=kind,
            def_id=def_id,
            created=result["created"],
            frontmatter_changed=result["frontmatter_changed"],
        )
        return web.json_response(
            {
                "ok": True,
                "created": result["created"],
                "restart_needed": result["frontmatter_changed"],
            }
        )

    async def delete_def_route(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        kind = _valid_kind(request)
        if kind is None:
            return web.json_response(
                {"ok": False, "reason": "unknown_kind"}, status=404
            )
        def_id = request.match_info["def_id"]
        if not skills.delete_def(skills_dir, kind, def_id):
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        log.info(
            "chat.def.deleted",
            uid=resolve_uid(request, remote_user_header, default_uid, solaris_db_path),
            kind=kind,
            def_id=def_id,
        )
        return web.json_response({"ok": True})

    async def get_soul(_request: web.Request) -> web.Response:
        # The soul lives on the chat-owned data volume now (Solaris Engine reads
        # it per turn), so the panel reads the file directly — the Hermes-era
        # config-sidecar hop is gone.
        try:
            content = Path(soul_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return web.json_response(
                {"ok": False, "reason": "soul_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "soul": {"content": content}})

    async def put_soul(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return web.json_response(
                {"ok": False, "reason": "empty_content"}, status=400
            )
        # Atomic write on the chat-owned volume: the engine's mtime cache
        # picks the edit up on the next turn, so it is live without restart.
        try:
            tmp = Path(soul_path).with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(soul_path)
        except OSError as e:
            log.error("chat.soul.write_failed", error=str(e))
            return web.json_response(
                {"ok": False, "reason": "soul_unavailable"}, status=502
            )
        log.info(
            "chat.soul.edited",
            uid=resolve_uid(request, remote_user_header, default_uid, solaris_db_path),
        )
        return web.json_response({"ok": True})

    # The household model the picker offers (#366): the configured FAST_MODEL
    # default plus the thorough model, so an admin can put the bigger model on
    # the household hot path. The persisted override ("" = use the default) is
    # read here so GET reflects the live selection.
    def household_model_options() -> list[dict[str, str]]:
        opts = [{"value": fast_model, "model": fast_model}]
        if thorough_model and thorough_model != fast_model:
            opts.append({"value": thorough_model, "model": thorough_model})
        return opts

    def current_household_model() -> str:
        return settings_store.get_household_model(solaris_db_path) or fast_model

    async def get_model(request: web.Request) -> web.Response:
        # Admin-only: the everyday-chat model toggle is an admin control.
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        return web.json_response(
            {
                "ok": True,
                "current": other_model_pref,
                "options": [
                    {"value": "fast", "label": "Schnell", "model": fast_model},
                    {
                        "value": "thorough",
                        "label": "Gründlich",
                        "model": thorough_model,
                    },
                ],
                "household_current": current_household_model(),
                "household_default": fast_model,
                "household_options": household_model_options(),
            }
        )

    async def put_model(request: web.Request) -> web.Response:
        nonlocal other_model_pref
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        # The household-profile model override (#366): a separate field so this
        # one endpoint sets either the everyday-chat routing toggle or the
        # household model. The selection must be one of the offered tags (the
        # picker only shows those). Persists; the household profile reads it on
        # the next turn — no restart.
        household_value = body.get("household_model")
        if household_value is not None:
            valid = {o["value"] for o in household_model_options()}
            if household_value not in valid:
                return web.json_response(
                    {"ok": False, "reason": "invalid_value"}, status=400
                )
            settings_store.set_household_model(solaris_db_path, household_value)
            log.info(
                "chat.model.household.set",
                uid=resolve_uid(
                    request, remote_user_header, default_uid, solaris_db_path
                ),
                model=household_value,
            )
            return web.json_response({"ok": True, "household_current": household_value})
        value = body.get("value")
        if value not in ("fast", "thorough"):
            return web.json_response(
                {"ok": False, "reason": "invalid_value"}, status=400
            )
        # A reasoning toggle, not an engine config rewrite: every everyday chat
        # runs e4b; "fast" sets no-reasoning, "thorough" turns reasoning/thought
        # on (the effort default). Takes effect on the next turn — no restart.
        other_model_pref = value
        settings_store.set_other_model_pref(solaris_db_path, value)
        log.info(
            "chat.model.set",
            uid=resolve_uid(request, remote_user_header, default_uid, solaris_db_path),
            pref=value,
        )
        return web.json_response({"ok": True, "current": value})

    # The global TTS voice picker (#368): one Kokoro voice for all spoken
    # output, mirroring the household-model picker. The offered voices come from
    # TTS_VOICES (the box's solaris-tts image declares which it ships); the
    # first is the default. The persisted "" means "use the default", so an
    # untouched install keeps the baked-in Martin voice. The post-deploy reads
    # the persisted value and converges the Assist pipeline's tts_voice.
    def voice_options() -> list[str]:
        return [v.strip() for v in tts_voices.split(",") if v.strip()]

    def default_voice() -> str:
        opts = voice_options()
        return opts[0] if opts else ""

    def current_voice() -> str:
        return settings_store.get_tts_voice(solaris_db_path) or default_voice()

    async def get_voice(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        return web.json_response(
            {
                "ok": True,
                "current": current_voice(),
                "default": default_voice(),
                "options": voice_options(),
            }
        )

    async def put_voice(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        value = body.get("value")
        if value not in voice_options():
            return web.json_response(
                {"ok": False, "reason": "invalid_value"}, status=400
            )
        settings_store.set_tts_voice(solaris_db_path, value)
        log.info(
            "chat.voice.set",
            uid=resolve_uid(request, remote_user_header, default_uid, solaris_db_path),
            voice=value,
        )
        return web.json_response({"ok": True, "current": value})

    # The model tags whose combined VRAM footprint the headroom estimate sums:
    # the household model (selected or fast default), the thorough model the
    # admin/librarian profiles run, and the embedding model — i.e. what's
    # actually meant to be co-resident on the box. fast/thorough collapse to one
    # e4b tag now; combined_selected_bytes dedups, so it's counted once.
    def selected_models() -> list[str]:
        tags = [current_household_model(), thorough_model]
        embed = os.environ.get("EMBED_MODEL", "").strip()
        if embed:
            tags.append(embed)
        return [t for t in tags if t]

    async def get_vram(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        client = OllamaChat(ollama_url)
        try:
            tags, ps = await client.tags(), await client.ps()
        except Exception:  # noqa: BLE001 — Ollama down => no estimate, not a 500
            tags, ps = [], []
        selected = selected_models()
        combined = vram.combined_selected_bytes(selected, tags, ps)
        # Real total/used from ServiceBay's node agent (its nvidia-smi sees the
        # whole GPU, overhead included) — falls back to env/in-container smi.
        gpu = await vram.servicebay_gpu(sb_mcp_url, sb_mcp_token_path)
        if gpu is not None:
            gpu_total, gpu_used = gpu
            available: int | None = max(gpu_total - gpu_used, 0)
        else:
            gpu_total = gpu_used = None
            available = vram.available_bytes(ps)
        return web.json_response(
            {
                "ok": True,
                "estimate": True,
                "selected": selected,
                "combined_bytes": combined,
                "available_bytes": available,
                "gpu_total_bytes": gpu_total,
                "gpu_used_bytes": gpu_used,
                # available unknown => we can't judge fit, so don't flag.
                "over_budget": available is not None and combined > available,
            }
        )

    async def pull_model(request: web.Request) -> web.StreamResponse:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        model = (body.get("model") or "").strip()
        if not model:
            return web.json_response({"ok": False, "reason": "no_model"}, status=400)
        log.info(
            "chat.model.pull",
            uid=resolve_uid(request, remote_user_header, default_uid, solaris_db_path),
            model=model,
        )
        resp = web.StreamResponse()
        resp.content_type = "application/x-ndjson"
        await resp.prepare(request)
        client = OllamaChat(ollama_url)
        try:
            async for chunk in client.pull(model):
                await resp.write((json.dumps(chunk) + "\n").encode())
        except OllamaError as e:
            await resp.write((json.dumps({"error": str(e)}) + "\n").encode())
        await resp.write_eof()
        return resp

    # -- the neighbour-service model lease (#1260) --------------------------
    # foundry-chronicle declares the window in which it holds the big model on
    # this box; for its duration Solaris answers with that model rather than
    # forcing a ~56 s swap each way. Loopback-only, no token, no shared secret,
    # payload exactly {model, ttl_s} — see solaris_chat.model_lease.

    def rewarm_fast_model() -> None:
        """Pull FAST_MODEL back into VRAM without making anyone wait for it."""
        if not fast_model:
            return
        task = asyncio.create_task(OllamaChat(ollama_url).warm(fast_model))
        _rewarm_tasks.add(task)
        task.add_done_callback(_rewarm_tasks.discard)

    async def set_model_lease(request: web.Request) -> web.Response:
        if not model_lease_enabled:
            return web.json_response({"ok": False, "reason": "disabled"}, status=503)
        if not is_loopback_caller(request):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        try:
            model, ttl = model_lease.parse_payload(body)
        except ValueError as e:
            return web.json_response({"ok": False, "reason": str(e)}, status=400)
        expires_at = model_lease.grant(solaris_db_path, model, ttl)
        log.info("chat.model_lease.set", model=model, ttl=ttl)
        # The renewal cadence is answered, not assumed, so both sides read one
        # number from one constant.
        return web.json_response(
            {
                "ok": True,
                "model": model,
                "ttl_s": ttl,
                "renew_after": model_lease.RENEW_INTERVAL_SECONDS,
                "expires_at": expires_at,
            }
        )

    async def clear_model_lease(request: web.Request) -> web.Response:
        if not model_lease_enabled:
            return web.json_response({"ok": False, "reason": "disabled"}, status=503)
        if not is_loopback_caller(request):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        was_live = bool(model_lease.active_model(solaris_db_path))
        model_lease.clear(solaris_db_path)
        if was_live:
            log.info("chat.model_lease.cleared", model=fast_model)
            rewarm_fast_model()
        return web.json_response({"ok": True})

    # -- the household notice Home Assistant posts (#1276) ------------------
    #
    # THIS IS NOT AN ALARM CHANNEL, and that holds for every `category` on it —
    # the fired timers and reminders that moved onto this event kind (#1280)
    # included. Delivery is best effort: an open client gets the notice over the
    # `/napi/portal/events` SSE stream, a backgrounded PWA through Web Push, and
    # an unreachable phone gets it when it comes back or not at all. Nothing
    # here retries, queues, escalates or rings, and no `urgency` value and no
    # category changes that — see `solaris_chat.ha_notify`. A smoke detector, an
    # intrusion or anything that must wake somebody needs a real alerting
    # transport, which does not exist yet. A timer someone depends on is rung by
    # the speaker announce in `engine/scheduler.py`, which stays its primary
    # path; the event is the copy for a phone out of earshot.
    #
    # Auth is the model lease's loopback pattern (#1260), deliberately not a
    # third scheme and deliberately NOT under `/napi/` (Authelia-bypassed,
    # device-token-only, fail-closed — an unauthenticated route there would
    # punch a hole in that prefix's one invariant). HA is a hostNetwork pod on
    # this box, so it reaches `127.0.0.1:<CHAT_PORT>` as a loopback neighbour
    # exactly like foundry-chronicle does.
    #
    # Fail-open for the caller: everything that can block — the Web Push
    # round-trips — happens in a background task AFTER the response is written,
    # so an unreachable phone can neither error nor delay the automation.
    # Target resolution stays synchronous because it is a local SQLite read and
    # because a silent mis-delivery is the failure this endpoint exists to
    # prevent: an unknown target is refused, never broadcast.

    async def _fan_out_ha_notice(
        uids: list[str], title: str, body: str, data: dict[str, Any]
    ) -> None:
        if notifier is None or not notifier.enabled:
            return
        for uid in uids:
            # An open client already received it on the SSE stream (the #715
            # selective-push rule) — pushing again would double-notify.
            if event_bus is not None and event_bus.has_subscriber(uid):
                continue
            try:
                await notifier.push(uid, title, body, data)
            except Exception as e:  # noqa: BLE001 — best effort, per resident
                log.error("chat.ha_notify.push_failed", uid=uid, error=str(e))

    async def _notice_cover_classes(actions: list[dict[str, Any]]) -> dict[str, str]:
        """`entity_id -> device_class` for every `cover` a notice action names.

        The only thing that may lower an action's `confirm` from the fail-closed
        default (#1283). Reuses the read-only `/api/states/<id>` read the card
        path and the `/napi/ha/call` confirm gate already do — no second HA
        client — for at most `MAX_ACTIONS` entities. An entity that does not
        resolve, because HA is unreachable or unconfigured or the id is unknown,
        is simply absent, and `ha_notify` then keeps `confirm: true`.
        """
        classes: dict[str, str] = {}
        if not hass_url or not hass_token:
            return classes
        cover_ids = sorted(
            {a["entity_id"] for a in actions if a["entity_id"].startswith("cover.")}
        )
        for entity_id in cover_ids:
            card = await fetch_card(hass_url, hass_token, entity_id)
            device_class = (card or {}).get("device_class")
            if device_class:
                classes[entity_id] = str(device_class)
        return classes

    async def ha_notify_post(request: web.Request) -> web.Response:
        if not is_loopback_caller(request):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        try:
            notice = ha_notify.parse_payload(body)
        except ValueError as e:
            return web.json_response({"ok": False, "reason": str(e)}, status=400)
        # A `cover` is a garage door or a blind and the domain cannot tell them
        # apart, so the parse above already confirmed every one of them. Reading
        # the real device_class is the only thing that lowers that, and the raw
        # body is re-parsed with it so the fail-closed default cannot stick.
        device_classes = await _notice_cover_classes(notice["actions"])
        if device_classes:
            notice = ha_notify.parse_payload(body, device_classes)
        resolved = ha_notify.resolve(
            solaris_db_path, notice["target"], household_uid=default_uid
        )
        if resolved is None:
            # A typo'd or unpaired target delivers to NOBODY. Loud in the log,
            # silent on every phone — the alternative is one resident's notice
            # read by the whole house.
            log.warn("chat.ha_notify.unknown_target", target=notice["target"])
            return web.json_response(
                {"ok": False, "reason": "unknown_target"}, status=404
            )
        bus_uid, push_uids = resolved
        data = ha_notify.event_data(
            bus_uid,
            notice["title"],
            notice["body"],
            urgency=notice["urgency"],
            actions=notice["actions"],
            category=notice["category"],
            device_classes=device_classes,
        )
        if event_bus is not None:
            event_bus.publish(bus_uid, ha_notify.EVENT_KIND, data)
        task = asyncio.create_task(
            _fan_out_ha_notice(push_uids, notice["title"], notice["body"], data)
        )
        _ha_notify_tasks.add(task)
        task.add_done_callback(_ha_notify_tasks.discard)
        log.info(
            "chat.ha_notify.accepted",
            target=bus_uid,
            residents=len(push_uids),
            urgency=notice["urgency"],
            category=notice["category"],
        )
        return web.json_response(
            {
                "ok": True,
                "target": bus_uid,
                "residents": push_uids,
                "delivery": ha_notify.NOT_AN_ALARM,
            },
            status=202,
        )

    async def list_sessions(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        try:
            sessions = await engine.list_sessions(uid)
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        # Annotate each session with its primary topic so the list can render a
        # chip (#241). Per-resident scope (D3): only the caller's assignments.
        ids = [str(s.get("id")) for s in sessions if s.get("id")]
        primaries = topics_store.primary_topics_for(solaris_db_path, ids, uid)
        for s in sessions:
            s["primary_topic"] = primaries.get(str(s.get("id")))
        return web.json_response({"ok": True, "sessions": sessions})

    async def create_session(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)

        # The ServiceBay-maintenance lock (#229) is keyed off the URL QUERY
        # STRING, not the POST body: the iframe `src` is set by ServiceBay and
        # in-frame JS cannot rewrite it, so a request body can never forge the
        # maintenance persona — and, conversely, can never escape the lock once
        # ServiceBay has set the query.
        if request.rel_url.query.get("persona") == personalities.MAINTENANCE_ID:
            if not is_admin(request, remote_groups_header, admin_group):
                return web.json_response(
                    {"ok": False, "reason": "forbidden"}, status=403
                )
            # The admin profile OWNS the operator soul + skill pack (prompt
            # assembly, Phase 3) — an empty create lets the profile supply it.
            # Any `personality` in the body is ignored; the lock cannot be
            # overridden by the client.
            try:
                session_id = await admin_gw.create_session(uid, maintenance=True)
            except EngineError:
                return web.json_response(
                    {"ok": False, "reason": "engine_unavailable"}, status=502
                )
            # Pin this session to the admin gateway so its follow-up turns route
            # back to the same instance (engine session state is per-gateway).
            admin_sessions.add(session_id)
            log.info(
                "chat.session.created",
                uid=uid,
                session_id=session_id,
                personality=personalities.MAINTENANCE_ID,
            )
            return web.json_response({"ok": True, "session_id": session_id})

        # No system_prompt overlay (#293): the household gateway's profile owns
        # the soul, so an empty create lets the profile supply it instead of a
        # per-session persona overlay that would fight it.
        try:
            session_id = await engine.create_session(uid)
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        log.info("chat.session.created", uid=uid, session_id=session_id)
        return web.json_response({"ok": True, "session_id": session_id})

    async def get_session(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        session_id = request.match_info["session_id"]
        try:
            session = await engine.get_session(
                session_id,
                effective_uid(
                    uid,
                    session_id,
                    admin=is_admin(request, remote_groups_header, admin_group),
                ),
            )
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        if session is None:
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        messages = session.get("messages") or []
        attach_to_messages(messages, attachments.batches(session_id))
        # Hide the internal-hint prefixes the proxy injected into each user turn
        # so history shows what the resident actually typed (#309).
        for m in messages:
            if m.get("role") == "user":
                m["content"] = strip_internal_hints(m.get("content") or "")
        return web.json_response({"ok": True, "session": session})

    async def delete_session(request: web.Request) -> web.Response:
        # Owner-scoped (#438): a caller can only delete their own session. A
        # wrong-owner id is indistinguishable from a missing one (like
        # get_session), so a cross-resident delete leaks nothing.
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        session_id = request.match_info["session_id"]
        try:
            ok = await engine.delete_session(session_id, uid)
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        if not ok:
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        attachments.delete(session_id)
        log.info("chat.session.deleted", uid=uid, session_id=session_id)
        return web.json_response({"ok": True})

    async def list_topics(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        return web.json_response(
            {"ok": True, "topics": topics_store.list_topics(solaris_db_path, uid)}
        )

    async def create_topic(request: web.Request) -> web.Response:
        # Create a resident-scoped topic from a confirmed suggestion (D4, #245).
        # The topic-suggester skill POSTs here only after the resident says yes;
        # the proxy never auto-creates. Idempotent on slug (see topics_store).
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        slug = str(body.get("slug") or "").strip().strip("/")
        display_name = str(body.get("display_name") or "").strip()
        if not slug or not display_name:
            return web.json_response(
                {"ok": False, "reason": "slug_and_display_name_required"}, status=400
            )
        color = body.get("color")
        color = color.strip() if isinstance(color, str) and color.strip() else None
        topics_store.create_topic(solaris_db_path, slug, display_name, uid, color)
        log.info("chat.topic.create", uid=uid, slug=slug)
        return web.json_response({"ok": True, "slug": slug})

    async def get_session_topics(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        session_id = request.match_info["session_id"]
        assigned = topics_store.get_session_topics(solaris_db_path, session_id, uid)
        return web.json_response({"ok": True, **assigned})

    async def set_session_topics(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        session_id = request.match_info["session_id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        action = body.get("action")
        slug = body.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            return web.json_response({"ok": False, "reason": "empty_slug"}, status=400)
        slug = slug.strip()
        if action == "primary":
            topics_store.set_primary(solaris_db_path, session_id, slug, uid)
        elif action == "add_secondary":
            topics_store.add_secondary(solaris_db_path, session_id, slug, uid)
        elif action == "remove":
            topics_store.remove_topic(solaris_db_path, session_id, slug, uid)
        else:
            return web.json_response(
                {"ok": False, "reason": "invalid_action"}, status=400
            )
        log.info(
            "chat.session.topic",
            uid=uid,
            session_id=session_id,
            action=action,
            slug=slug,
        )
        assigned = topics_store.get_session_topics(solaris_db_path, session_id, uid)
        return web.json_response({"ok": True, **assigned})

    async def topic_items(request: web.Request) -> web.Response:
        # The topic dashboard's per-topic note list (#244): the notes tagged
        # `#topic/<slug>` in the vault (stamped by ingestion, #243). Per-resident
        # scope (D3): only the caller's own (or unowned/shared) notes. The slug
        # may be hierarchical (projekt/wintergarten), so the route captures the
        # rest of the path into `slug`.
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        slug = request.match_info["slug"].strip("/")
        items = notes_search.notes_for_topic(notes_dir, slug, uid)
        return web.json_response({"ok": True, "slug": slug, "items": items})

    async def concept_view(request: web.Request) -> web.Response:
        """Aggregate one entity/concept into the #502 page (phase 1).

        Composes, owner-scoped, what the household already stores for `<id>`:
        live HA state (when the id is an HA entity), the OKF concept's
        description/facts/events, the source OKF document + notes that mention
        it, and chat/note backlinks. `<id>` resolves via `entity_aliases`/
        `entities` (migration 0016) or is taken as an HA entity id directly.
        """
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        ref = request.match_info["id"].strip()
        view: dict[str, Any] = {
            "id": ref,
            "title": ref,
            "type": None,
            "ha_card": None,
            "description": "",
            "body": "",
            "facts": [],
            "events": [],
            "source_docs": [],
            "backlinks": [],
        }
        names = [ref]
        if Path(solaris_db_path).exists():
            conn = projection.open_conn(solaris_db_path)
            try:
                entity_id = projection.resolve_entity_id(conn, ref, uid)
                if entity_id is not None:
                    ent = projection.entity_row(conn, entity_id) or {}
                    view["id"] = entity_id
                    view["title"] = ent.get("canonical_name") or entity_id
                    view["type"] = ent.get("type")
                    names = [view["title"], *projection.entity_aliases(conn, entity_id)]
                    view["facts"] = projection.entity_facts(conn, entity_id, uid)
                    view["events"] = projection.entity_events(conn, entity_id, uid)
                    okf_path = projection.entity_okf_path(conn, entity_id)
                    if okf_path:
                        view["source_docs"].append(
                            {"path": okf_path, "title": view["title"], "kind": "okf"}
                        )
                        parsed = _read_okf(notes_dir, okf_path)
                        view["description"] = parsed["description"]
                        view["body"] = parsed["body"]
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        # Live state when the page id (or the resolved entity) is an HA entity.
        if hass_url and hass_token:
            card = await fetch_card(hass_url, hass_token, ref)
            if card is not None:
                view["ha_card"] = card
                if view["title"] == ref:
                    view["title"] = card.get("name") or ref
        for note in notes_search.notes_mentioning(notes_dir, names, uid):
            view["source_docs"].append({**note, "kind": "note"})
        # Backlinks span both surfaces (#505): chat turns that referenced the
        # concept and vault notes whose `[[ ]]` link targets its okf/ file.
        view["backlinks"] = mentions_store.backlinks_for(solaris_db_path, uid, names)
        okf_path = next(
            (d["path"] for d in view["source_docs"] if d["kind"] == "okf"), None
        )
        for note in notes_search.notes_wikilinking(notes_dir, names, okf_path, uid):
            view["backlinks"].append({**note, "kind": "note"})
        return web.json_response({"ok": True, "concept": view})

    async def concept_page(_request: web.Request) -> web.Response:
        # Bookmarkable deep-link to a concept: serve the SPA shell; the client
        # router reads the `/c/<id>` path and renders the page from the API.
        return web.FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    async def portal_energy(_request: web.Request) -> web.Response:
        """Aggregate the home-energy picture for the `#/p/energy` page (#503).

        Same pattern as `/api/concept/<id>`: a read-only `/api` aggregator behind
        the existing Authelia gate, rendered by an SPA view. 503 when HA is not
        configured for this household.
        """
        if not hass_url or not hass_token:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        energy = await fetch_energy(hass_url, hass_token)
        if energy is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "energy": energy})

    async def portal_energy_history(request: web.Request) -> web.Response:
        """Power history for the energy page's 24h/7d trend chart (#689).

        Same read-only Authelia-gated pattern as `portal_energy`; `range=24h|7d`
        selects the window (default 24h). 503 when HA is unconfigured, 502 when
        it is unavailable.
        """
        if not hass_url or not hass_token:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        hours = 168 if request.query.get("range") == "7d" else 24
        history = await fetch_energy_history(hass_url, hass_token, hours)
        if history is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "history": history})

    async def portal_entity_history(request: web.Request) -> web.Response:
        """State history for one entity — the native widget's sparkline (#755).

        Owner-scoped via `resolve_uid` (accepts the `sol_device_` device-token
        bearer from #748), read-only. `entity_id` is validated with the same
        `_ENTITY_RE` the tools use; `range` is a preset (24h/48h/7d, default 24h).
        Mirrors `portal_energy_history`: 503 when HA is unconfigured, 400 on bad
        input, 502 when HA is unavailable.
        """
        if not hass_url or not hass_token:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        # Owner-scoped read; resolving establishes the caller's identity even
        # though the HA read itself is household-wide.
        resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        entity_id = request.query.get("entity_id", "")
        if not _ENTITY_RE.match(entity_id):
            return web.json_response(
                {"ok": False, "error": "invalid entity_id"}, status=400
            )
        rng = request.query.get("range", "24h")
        if rng not in _ENTITY_HISTORY_RANGES:
            return web.json_response(
                {"ok": False, "error": "invalid range"}, status=400
            )
        history = await fetch_entity_history(hass_url, hass_token, entity_id, rng)
        if history is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "history": history})

    async def portal_camera_snapshot(request: web.Request) -> web.Response:
        """Current still image for a `camera.*` entity — the Android camera widget
        (#770). Owner-scoped via `resolve_uid`; on the proxy-bypassed `/napi/`
        prefix the `native(...)` wrapper has already enforced a `sol_device_`
        device-token bearer, so this live-camera read is never served to an
        unauthenticated caller (fail-closed, privacy-sensitive). `fetch_camera_snapshot`
        enforces the `camera` domain, so a non-camera `entity_id` returns 400.
        503 when HA is unconfigured, 502 when HA is unavailable.
        """
        if not hass_url or not hass_token:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        entity_id = request.match_info["entity_id"]
        if not _ENTITY_RE.match(entity_id) or not entity_id.startswith("camera."):
            return web.json_response(
                {"ok": False, "error": "invalid entity_id"}, status=400
            )
        snapshot = await fetch_camera_snapshot(hass_url, hass_token, entity_id)
        if snapshot is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        image, content_type = snapshot
        # The widget polls periodically; a short cache lets a burst of refreshes
        # share one HA read without letting the image go stale.
        return web.Response(
            body=image,
            content_type=content_type,
            headers={"Cache-Control": "max-age=5"},
        )

    async def portal_state(request: web.Request) -> web.Response:
        """Lean card-spec for one arbitrary entity — the universal device widget's
        current-state read (#762). Reuses the read-only `fetch_card` path (the
        #754-enriched shape), owner-scoped. Same guards as the other portal
        endpoints: 503 when HA is unconfigured, 400 on bad `entity_id`.
        """
        if not hass_url or not hass_token:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        # Owner-scoped read; resolving establishes the caller's identity even
        # though the HA read itself is household-wide.
        resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        entity_id = request.query.get("entity_id", "")
        if not _ENTITY_RE.match(entity_id):
            return web.json_response(
                {"ok": False, "error": "invalid entity_id"}, status=400
            )
        card = await fetch_card(hass_url, hass_token, entity_id)
        if card is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "card": card})

    async def portal_page(_request: web.Request) -> web.Response:
        # Bookmarkable deep-link to a household page: serve the SPA shell; the
        # client router reads the `/p/<type>` path and renders from the API.
        return web.FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    # Room data for the start-page picker (#669): a long-lived registry so the
    # WS snapshot is TTL-cached across picker opens (same source as the engine's
    # `list_rooms`). Only built when HA is configured.
    area_registry = (
        AreaRegistry(hass_url, hass_token) if hass_url and hass_token else None
    )

    def _action_is_sensitive(payload: dict[str, Any]) -> bool:
        """Re-check whether a pinned action would be confirm-gated (#646).

        The run path bypasses the agent-loop gate, so it must re-classify here.
        Only `ha_call_service` can be sensitive; the server holds no
        EntityRegistry, so a cover passes device_class=None and `is_sensitive`
        fails SAFE (gated) — such a favorite could never be pinned by #645
        anyway, so this is pure defense-in-depth."""
        if payload.get("tool") != "ha_call_service":
            return False
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        domain = str(args.get("domain") or "")
        service = str(args.get("service") or "")
        service = _SERVICE_ALIASES.get(domain, {}).get(service, service)
        return confirm.is_sensitive(domain, service, None)

    def _entity_card_sensitive(card: dict[str, Any]) -> bool:
        """True when a one-tap action on this entity card is confirm-gated (#702).

        A garage/door/gate cover's toggle is sensitive, and every `lock` action is
        (#1212 — the whole domain is in SENSITIVE_DOMAINS); such a card is added
        with a lock badge and its tap requires an explicit confirm the server
        re-checks (`/api/ha/call` with `confirmed=true`). Asking `is_sensitive`
        with the neutral `toggle` service keeps this one rule for both: a
        light/switch/climate/media_player toggle stays ungated."""
        return confirm.is_sensitive(
            str(card.get("domain") or ""), "toggle", card.get("device_class")
        )

    async def _ha_call_is_sensitive(entity_id: str, service: str) -> bool:
        """Authoritative confirm-gate re-check for a card tap (#702).

        `service` is dotted (`cover.open_cover`). Only a cover open is class-
        specific, so resolve the entity's live device_class (one read-only fetch)
        and let `is_sensitive` decide — an unresolved class fails SAFE (gated)."""
        domain, _, action = service.partition(".")
        if domain != "cover" or action not in confirm.COVER_OPEN_SERVICES:
            return confirm.is_sensitive(domain, action, None)
        card = await fetch_card(hass_url, hass_token, entity_id)
        device_class = card.get("device_class") if card else None
        return confirm.is_sensitive(domain, action, device_class)

    async def portal_start(request: web.Request) -> web.Response:
        """Aggregate the resident's start page (#646): their pins + the shared
        household ones + their most-used actions. Entity favorites are enriched
        with live HA state via the read-only `fetch_card` path."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        favorites = favorites_store.list_favorites(solaris_db_path, uid)

        configured = bool(hass_url and hass_token)

        def _ha_health(any_entity: bool, any_card: bool) -> str:
            """HA health for the client banner (#729): the WS watcher's live state
            is authoritative when wired in; otherwise infer from configuration +
            whether any card enrich succeeded. `unconfigured` = no url/token,
            `unreachable` = configured but the WS is down / every fetch failed,
            `ok` otherwise."""
            if not configured:
                return "unconfigured"
            if ha_watcher is not None:
                status = ha_watcher.status
                if status == "disabled":
                    return "unconfigured"
                return "ok" if status == "connected" else "unreachable"
            # No watcher: a configured HA with pinned entities where every fetch
            # returned None means HA is unreachable; no entities means no signal.
            if any_entity and not any_card:
                return "unreachable"
            return "ok"

        # Live-refresh tick (#711): the client polls the pinned entities' state
        # while #/p/start is the active view. `state_only=1` re-fetches just the
        # HA card state for the entity favorites — no usage/frequent queries — so
        # each card updates in place without re-rendering the whole page.
        if request.query.get("state_only") in ("1", "true") and configured:
            entity_ids = [
                str(f["payload"].get("entity_id") or "")
                for f in favorites
                if f["kind"] == "entity" and f["payload"].get("entity_id")
            ]
            cards = await asyncio.gather(
                *(fetch_card(hass_url, hass_token, eid) for eid in entity_ids)
            )
            states: dict[str, dict[str, Any]] = {}
            unavailable: list[str] = []
            for eid, card in zip(entity_ids, cards):
                if card is not None:
                    card["sensitive"] = _entity_card_sensitive(card)
                    states[eid] = card
                else:
                    unavailable.append(eid)
            return web.json_response(
                {
                    "ok": True,
                    "states": states,
                    "unavailable": unavailable,
                    "ha": _ha_health(bool(entity_ids), bool(states)),
                }
            )

        personal: list[dict[str, Any]] = []
        household: list[dict[str, Any]] = []

        async def enrich(fav: dict[str, Any]) -> dict[str, Any]:
            item = {
                "id": fav["id"],
                "kind": fav["kind"],
                "label": fav["label"],
                "payload": fav["payload"],
                "position": fav["position"],
                "scope": (
                    "household"
                    if fav["owner_uid"] == favorites_store.HOUSEHOLD
                    else "personal"
                ),
            }
            if fav["kind"] == "entity" and configured:
                entity_id = str(fav["payload"].get("entity_id") or "")
                card = (
                    await fetch_card(hass_url, hass_token, entity_id)
                    if entity_id
                    else None
                )
                if card is not None:
                    card["sensitive"] = _entity_card_sensitive(card)
                    item["card"] = card
                elif entity_id:
                    # Configured HA but no card (fetch failed / HA down) — flag it
                    # so the client renders an explicit "nicht verfügbar" state
                    # instead of a bare name (#729).
                    item["card_unavailable"] = True
            return item

        enriched = await asyncio.gather(*(enrich(f) for f in favorites))
        any_entity = False
        any_card = False
        for item in enriched:
            (household if item["scope"] == "household" else personal).append(item)
            if item["kind"] == "entity":
                any_entity = True
                if item.get("card") is not None:
                    any_card = True

        # Collapse (#745): when the acting uid IS `household`, the personal and
        # household buckets are the same owner — render ONE "Favoriten" section
        # and hide the scope choice/move (both are meaningless). A distinct
        # resident gets two sections as before.
        single_scope = uid == favorites_store.HOUSEHOLD

        # Cross-scope display dedup (#745): a device MAY be pinned in BOTH scopes.
        # If the same entity_id appears in both lists, render it ONCE — keep the
        # PERSONAL copy ("Meine Favoriten") and drop the household duplicate.
        if not single_scope:
            personal_entities = {
                str(item["payload"].get("entity_id") or "")
                for item in personal
                if item["kind"] == "entity" and item["payload"].get("entity_id")
            }
            household = [
                item
                for item in household
                if not (
                    item["kind"] == "entity"
                    and str(item["payload"].get("entity_id") or "") in personal_entities
                )
            ]

        # The ☆ reflects "already pinned in EITHER scope" (#745): expose every
        # pinned entity_id the caller can see (own ∪ household) so the client can
        # render the ☆ filled for it.
        owners = favorites_store.pinned_entity_owners(solaris_db_path)
        pinned_entities = sorted(
            eid
            for eid, uids in owners.items()
            if uid in uids or favorites_store.HOUSEHOLD in uids
        )

        top = favorites_store.top_usage(solaris_db_path, uid, 6)
        # Resolve entity_id → friendly_name once for the whole frequent list so
        # HA-service rows read "Bürolicht — Aus" not "dimmer 2 — turn_off" (#741).
        # One bulk /api/states beats N per-item fetch_card round-trips; None on
        # HA error / unconfigured falls back to the humanized slug.
        names: dict[str, str] = {}
        wants_names = configured and any(
            isinstance(r["payload"], dict)
            and r["payload"].get("tool") == "ha_call_service"
            for r in top
        )
        if wants_names:
            entity_names = await fetch_entity_names(hass_url, hass_token)
            if entity_names:
                names = {e["entity_id"]: e["name"] for e in entity_names}
        frequent: list[dict[str, Any]] = []
        for row in top:
            frequent.append(
                {
                    "kind": row["kind"],
                    "label": _favorite_label(row["payload"], names),
                    **row,
                }
            )
        return web.json_response(
            {
                "ok": True,
                "personal": personal,
                "household": household,
                "frequent": frequent,
                "single_scope": single_scope,
                "pinned_entities": pinned_entities,
                "ha": _ha_health(any_entity, any_card),
            }
        )

    async def portal_start_addable(request: web.Request) -> web.Response:
        """Addable entity cards for the start-page picker, grouped by room (#669).

        The resident opens the picker in edit mode and ticks cards to pin. This
        lists the house's controllable actuators (reusing the room-card domain
        set + live state), grouped by room, marking those already pinned
        (personal or household) so they aren't offered twice. A cover whose
        one-tap toggle would be confirm-gated (garage/door/gate) is marked
        `sensitive` — the client keeps such a card selectable but renders a lock
        badge and guards its tap with an explicit confirm the server re-checks
        (#702). Scenes/scripts/automations are offered as a separate "Automationen"
        group of pinnable ACTION cards."""
        if not hass_url or not hass_token or area_registry is None:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        snap = await area_registry.snapshot()
        cards = await fetch_addable_cards(hass_url, hass_token, snap.entity_area)
        if cards is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        favorites = favorites_store.list_favorites(solaris_db_path, uid)
        pinned = {
            str(f["payload"].get("entity_id") or "")
            for f in favorites
            if f["kind"] == "entity"
        }
        rooms: dict[str, list[dict[str, Any]]] = {}
        for card in cards:
            eid = str(card.get("entity_id") or "")
            card["pinned"] = eid in pinned
            card["sensitive"] = _entity_card_sensitive(card)
            rooms.setdefault(card.pop("room") or "", []).append(card)
        grouped = [
            {"room": room, "cards": rooms[room]}
            for room in sorted(rooms, key=lambda r: (r == "", r.lower()))
        ]
        # Scenes/scripts/automations as pinnable ACTION cards (#702). A pinned
        # entity_id already in `ha_run_scene_script` favorites is marked so it
        # isn't offered twice.
        pinned_runnables = {
            str(
                (f["payload"].get("args") or {}).get("entity")
                or (f["payload"].get("args") or {}).get("entity_id")
                or ""
            )
            for f in favorites
            if f["kind"] == "action"
            and f["payload"].get("tool") == "ha_run_scene_script"
        }
        runnables = await fetch_addable_runnables(hass_url, hass_token)
        automations = []
        for r in runnables or []:
            eid = str(r.get("entity_id") or "")
            automations.append(
                {
                    "entity_id": eid,
                    "name": r.get("name") or eid,
                    "domain": r.get("domain"),
                    "kind": "action",
                    "tool": "ha_run_scene_script",
                    "args": {"entity": eid},
                    "pinned": eid in pinned_runnables,
                    "sensitive": False,
                }
            )
        return web.json_response(
            {"ok": True, "rooms": grouped, "automations": automations}
        )

    async def portal_active(request: web.Request) -> web.Response:
        """Currently-active controllable entities, for the Android active-devices
        widget (#773). The widget used to N+1: list the addable actuators, then
        read each one's state. So this reuses `fetch_addable_cards` — a SINGLE
        bulk `/api/states` read that already carries every actuator's live state
        — and filters to the on/open ones here, so the widget makes one request.

        Only reached via `native(...)` on `/napi/`: device-token-only, fail-closed,
        owner-scoped, read-only. Returns a flat `active` list of the SAME card
        dicts the other portal routes return, so the widget gets the control
        attributes (`brightness`, `current_position`, colour) it renders from
        (#1233); `room` is the area friendly name from the same area snapshot
        the picker uses."""
        if not hass_url or not hass_token or area_registry is None:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        snap = await area_registry.snapshot()
        cards = await fetch_addable_cards(hass_url, hass_token, snap.entity_area)
        if cards is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        active = []
        for card in cards:
            if str(card.get("state") or "").lower() not in ("on", "open"):
                continue
            card["room"] = card.get("room") or ""
            active.append(card)
        return web.json_response({"ok": True, "active": active})

    async def portal_cameras(request: web.Request) -> web.Response:
        """Camera entities for the Android camera-widget picker (#779). The
        addable actuator list only carries controllable actuators, so the picker
        would show "no cameras". This does the ONE bulk `/api/states` read,
        filters to `camera.*`, and returns each camera's `entity_id`, friendly
        `name`, and `room` (area friendly name from the same area snapshot the
        actuator picker uses).

        Only reached via `native(...)` on `/napi/`: device-token-only, fail-closed,
        owner-scoped, read-only."""
        if not hass_url or not hass_token or area_registry is None:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        snap = await area_registry.snapshot()
        cameras = await fetch_cameras(hass_url, hass_token, snap.entity_area)
        if cameras is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "cameras": cameras})

    async def portal_notes(request: web.Request) -> web.Response:
        """Notes-portal overview for `#/p/notes` (#696): counts, the last
        Bibliothekar run, and the recently modified notes — read-only, owner-scoped
        (caller ∪ shared, default-deny), all reads path-jailed to `notes_dir`.

        The vault is a Syncthing folder; its scan runs off the event loop and is
        prune-bounded + TTL-cached so a slow/huge vault can never wedge the request
        (#705)."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        now = time.monotonic()
        cached = _notes_overview_cache.get(uid)
        if cached and now - cached[0] < _NOTES_OVERVIEW_TTL:
            return web.json_response(cached[1])
        payload = await asyncio.to_thread(
            _notes_overview_payload, notes_dir, solaris_db_path, uid
        )
        _notes_overview_cache[uid] = (now, payload)
        return web.json_response(payload)

    def _browse_groups(uid: str, by: str) -> dict[str, list[dict[str, Any]]] | None:
        """Build the Durchstöbern groups for `by` — blocking (walks the vault via
        the prune-bounded `iter_vault_md`), so the coroutine runs it off the event
        loop (#705). None signals a bad `by`."""
        groups: dict[str, list[dict[str, Any]]] = {}
        if by == "topic":
            for rel, text, path in _notes_visible_files(notes_dir, uid):
                for slug in dict.fromkeys(_TOPIC_TAG_RE.findall(text)):
                    groups.setdefault(slug, []).append(
                        {"path": rel, "title": _note_title(text, path.stem)}
                    )
        elif by == "okf":
            for rel, text, path in _notes_visible_files(notes_dir, uid):
                parts = rel.replace("\\", "/").split("/")
                if parts[0] != "okf" or len(parts) < 2:
                    continue
                domain = parts[1] if len(parts) > 2 else "okf"
                if domain == "log.md":
                    continue
                groups.setdefault(domain, []).append(
                    {"path": rel, "title": _note_title(text, path.stem)}
                )
        elif by == "journal":
            # One entry per journal DAY: same-day variants under the three path
            # conventions (#709) collapse; the canonical `journal/<YYYY>/<date>.md`
            # wins when present, else the first-seen variant represents the day.
            by_date: dict[str, dict[str, Any]] = {}
            for rel, text, path in _notes_visible_files(notes_dir, uid):
                norm = rel.replace("\\", "/")
                date = notes_search.journal_date(norm)
                if date is None:
                    continue
                item = {"path": rel, "title": _note_title(text, path.stem)}
                is_canon = norm == notes_search.canonical_journal_path(norm)
                if date not in by_date or is_canon:
                    by_date[date] = item
            for date, item in by_date.items():
                groups.setdefault(f"journal/{date[:4]}", []).append(item)
        elif by == "folder":
            for rel, text, path in _notes_visible_files(notes_dir, uid):
                norm = rel.replace("\\", "/")
                folder = norm.rsplit("/", 1)[0] if "/" in norm else "(Wurzel)"
                groups.setdefault(folder, []).append(
                    {"path": rel, "title": _note_title(text, path.stem)}
                )
        elif by == "person":
            # ADR 0010: union the `person` entities (own ∪ shared, canonical name
            # + aliases) with the chat-mention names, de-duped case-insensitively.
            # The entity's canonical spelling wins; a `.contacts` person with no
            # chat mentions still appears (count 0).
            directory = documents_portal_db.person_directory(solaris_db_path, uid) or []
            seen: dict[str, tuple[str, list[str]]] = {}
            for p in directory:
                seen[p["name"].lower()] = (p["name"], p["aliases"])
            for name in mentions_store.known_persons_for(solaris_db_path, uid):
                seen.setdefault(name.lower(), (name, []))
            for name, aliases in seen.values():
                hits = notes_search.notes_mentioning(notes_dir, [name, *aliases], uid)
                groups[name] = hits
        else:
            return None
        return groups

    async def portal_notes_browse(request: web.Request) -> web.Response:
        """Grouped vault listing for the Durchstöbern chips (#696).

        `by=topic|person|journal|okf|folder` — reusing the notes_search readers and
        the vault tree. Every result is owner-scoped (caller ∪ shared). The vault
        walk runs off the event loop so a huge Syncthing vault can't wedge the
        request (#705)."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        by = request.query.get("by", "topic")
        groups = await asyncio.to_thread(_browse_groups, uid, by)
        if groups is None:
            return web.json_response({"ok": False, "error": "bad_by"}, status=400)
        out = [
            {"group": g, "items": groups[g]}
            for g in sorted(groups, key=lambda s: s.lower())
        ]
        return web.json_response({"ok": True, "by": by, "groups": out})

    async def portal_notes_note(request: web.Request) -> web.Response:
        """One note for the viewer (#696): raw markdown + parsed frontmatter.

        Path-jail: resolve under `notes_dir` and reject anything escaping it (a
        `..` traversal). Owner-scoped via `is_visible` — a caller may only read
        their own or shared notes, never another resident's private one."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        rel = request.query.get("path", "")
        root = Path(notes_dir).resolve()
        try:
            path = (root / rel).resolve()
            path.relative_to(root)
        except (ValueError, OSError):
            return web.json_response({"ok": False, "error": "bad_path"}, status=400)
        if not path.is_file():
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        text = path.read_text(encoding="utf-8", errors="replace")
        canon = str(path.relative_to(root))
        if not notes_search.is_visible(notes_search.owner_of(canon, text), uid):
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        return web.json_response(
            {
                "ok": True,
                "path": canon,
                "title": _note_title(text, path.stem),
                "frontmatter": _note_frontmatter(text),
                "content": text,
                "hash": _note_hash(text),
            }
        )

    async def portal_notes_note_put(request: web.Request) -> web.Response:
        """Save an edited note back to the vault (#698).

        `?path=…` + `{content, hash}`. Path-jailed to `notes_dir` and owner-scoped
        (a resident edits only own+household notes). The `hash` is the one the GET
        returned; a mismatch means the file changed since and the write is refused
        with 409 (no silent overwrite of a concurrent edit). The write is atomic
        (tmp+replace) and the frontmatter is stored verbatim (#657). Off the event
        loop so a slow disk can't wedge the request."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        rel = request.query.get("path", "")
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        content = body.get("content")
        prev_hash = str(body.get("hash") or "")
        if not isinstance(content, str) or not prev_hash:
            return web.json_response({"ok": False, "error": "bad_args"}, status=400)
        path = _notes_resolve_note(notes_dir, rel, uid)
        if path is None:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        result = await asyncio.to_thread(
            _notes_write_note, notes_dir, path, content, prev_hash
        )
        status = result.pop("status", 200 if result.get("ok") else 400)
        _notes_overview_cache.pop(uid, None)
        _notes_stats_cache.pop(uid, None)
        return web.json_response(result, status=status)

    async def portal_notes_stats(request: web.Request) -> web.Response:
        """Notes statistics for the `#/p/notes` Statistik section (#699).

        Frequent `#tags`/topics and `@persons` (by note count), notes per
        folder/OKF category, notes created per month (~12 months), and the
        most-`[[..]]`-linked entities — owner-scoped, computed off the event loop
        in one prune-bounded vault walk (#705) and TTL-cached (the vault is
        small)."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        now = time.monotonic()
        cached = _notes_stats_cache.get(uid)
        if cached and now - cached[0] < _NOTES_STATS_TTL:
            return web.json_response(cached[1])
        payload = await asyncio.to_thread(
            _notes_stats_payload, notes_dir, solaris_db_path, uid
        )
        _notes_stats_cache[uid] = (now, payload)
        return web.json_response(payload)

    async def portal_notes_search(request: web.Request) -> web.Response:
        """Search over the unified notes_search machinery (#696): a thin GET
        wrapper around the engine `notes_search` tool (fuzzy + alias + #topic /
        @person + semantic fallback), owner-scoped to the caller."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        query = request.query.get("q", "").strip()
        if not query:
            return web.json_response({"ok": True, "hits": []})
        tools = build_notes_tools(
            notes_dir, lambda: uid, solaris_db_path, OllamaChat(ollama_url)
        )
        search = next(t.handler for t in tools if t.name == "notes_search")
        hits = json.loads(await search({"query": query}))
        return web.json_response({"ok": True, "hits": hits})

    async def portal_notes_inbox(request: web.Request) -> web.Response:
        """The inbox curation list for `#/p/notes` V2 (#697): the unconsolidated
        fact files older than the Bibliothekar's stale threshold — the same query
        the nightly librarian queues (#653) — owner-scoped, bounded. The fact scan
        runs off the event loop so a huge vault can't wedge the request (#705)."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        items = await asyncio.to_thread(_notes_inbox_list, notes_dir, uid)
        return web.json_response({"ok": True, "items": items})

    async def portal_notes_assign(request: web.Request) -> web.Response:
        """Fold an inbox fact into a topic/person note (#697).

        `{path, target:"topic"|"person", name}`. Path-jailed to the vault and
        owner-scoped (a caller may only act on their own or shared facts). Applies
        the Bibliothekar's merge convention (#653): append into the target note,
        stamp the source `consolidated: true`, log to `okf/log.md` — never delete.
        The file writes run off the event loop."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        rel = str(body.get("path") or "")
        target = str(body.get("target") or "")
        name = str(body.get("name") or "").strip()
        if target not in ("topic", "person") or not name:
            return web.json_response({"ok": False, "error": "bad_args"}, status=400)
        src = _notes_resolve_owned(notes_dir, rel, uid)
        if src is None:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        result = await asyncio.to_thread(
            _notes_assign_fact, notes_dir, src, target, name, uid
        )
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def portal_notes_archive(request: web.Request) -> web.Response:
        """Move an inbox fact under the vault's `archive/` folder (#697).

        `{path}`. Path-jailed + owner-scoped like assign. Never deletes — the file
        is relocated (source subtree preserved) and the move logged to
        `okf/log.md`. Runs off the event loop."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        rel = str(body.get("path") or "")
        src = _notes_resolve_owned(notes_dir, rel, uid)
        if src is None:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        result = await asyncio.to_thread(_notes_archive_fact, notes_dir, src, uid)
        status = 200 if result.get("ok") else 400
        return web.json_response(result, status=status)

    async def portal_notes_curate(request: web.Request) -> web.Response:
        """Trigger a targeted Bibliothekar run for a scope (#697).

        `{scope}` — the shared household pool or the caller's own uid; any other
        scope is coerced to the caller's (a resident may only curate their own or
        shared facts, default-deny). Reuses the #653 librarian machinery bounded to
        the one scope (`CronRunner.curate_scope`), off-loop, and returns the run's
        summary. 503 when no librarian client is wired (offline-test topology)."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        if crons is None:
            return web.json_response({"ok": False, "error": "no_librarian"}, status=503)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        scope = str(body.get("scope") or "").strip()
        if scope != notes_search.SHARED_UID:
            scope = uid
        result = await crons.curate_scope(notes_dir, scope)
        _notes_overview_cache.pop(uid, None)
        status = 200 if result.get("ok") else 503
        return web.json_response(result, status=status)

    async def portal_documents(request: web.Request) -> web.Response:
        """Category doorways for the documents page (#doc): `{category: count}`,
        owner-scoped (caller ∪ shared household)."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        cats = await asyncio.to_thread(
            documents_portal_db.categories, solaris_db_path, uid
        )
        return web.json_response({"ok": True, "categories": cats or {}})

    async def portal_documents_category(request: web.Request) -> web.Response:
        """The table rows for one document category — each document's title +
        fact map (value + confidence per predicate), owner-scoped."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        category = request.match_info["category"]
        rows = await asyncio.to_thread(
            documents_portal_db.category_view, solaris_db_path, uid, category
        )
        return web.json_response({"ok": True, "category": category, "rows": rows or []})

    async def portal_documents_search(request: web.Request) -> web.Response:
        """Documents matching `?q=` (title/category LIKE), owner-scoped, for the
        `.doc` filter. Absent/empty `q` → the full owner-scoped list."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        q = request.query.get("q", "")
        rows = await asyncio.to_thread(
            documents_portal_db.search, solaris_db_path, uid, q
        )
        return web.json_response({"ok": True, "documents": rows or []})

    async def portal_contacts(request: web.Request) -> web.Response:
        """The phone-book (#doc-graph): every provider organization with its
        contact facts and the documents grouped under it, owner-scoped."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        rows = await asyncio.to_thread(
            documents_portal_db.contacts, solaris_db_path, uid
        )
        return web.json_response({"ok": True, "contacts": rows or []})

    async def portal_tasks(request: web.Request) -> web.Response:
        """The Aufgaben (to-do) doorway (#todo): the caller's open tasks (own ∪
        shared household); `?done=1` also returns resolved ones."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        include_done = request.query.get("done") == "1"
        rows = await asyncio.to_thread(
            tasks_svc.list_tasks, solaris_db_path, uid, include_done=include_done
        )
        return web.json_response({"ok": True, "tasks": rows})

    async def _task_set_status(body: dict[str, Any]) -> dict[str, Any]:
        """Card callback: mark a task done / dismissed (params: entity_id, status)."""
        uid = body.get("uid") or default_uid
        params = body.get("params") or {}
        entity_id = params.get("entity_id")
        status = params.get("status") or "done"
        if not isinstance(entity_id, str) or status not in (
            "done",
            "dismissed",
            "open",
        ):
            return {"ok": False, "reason": "bad_params"}
        ok = await asyncio.to_thread(
            tasks_svc.set_status,
            db_path=solaris_db_path,
            uid=uid,
            entity_id=entity_id,
            status=status,
        )
        if ok:
            await cascade_task_event_configured(solaris_db_path, entity_id)
        return {"ok": ok}

    async def _task_add(body: dict[str, Any]) -> dict[str, Any]:
        """Card callback: add a task from the doorway (params: title, due?)."""
        uid = body.get("uid") or default_uid
        params = body.get("params") or {}
        try:
            tid = await asyncio.to_thread(
                tasks_svc.create_task,
                db_path=solaris_db_path,
                notes_dir=notes_dir,
                uid=uid,
                title=str(params.get("title") or ""),
                due=str(params.get("due") or ""),
                task_source="manual",
            )
        except ValueError:
            return {"ok": False, "reason": "bad_title"}
        await cascade_task_event_configured(solaris_db_path, tid)
        return {"ok": True, "id": tid}

    async def _task_update(body: dict[str, Any]) -> dict[str, Any]:
        """Card callback: correct a task's title/due (params: entity_id, title, due?)."""
        uid = body.get("uid") or default_uid
        params = body.get("params") or {}
        entity_id = params.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            return {"ok": False, "reason": "bad_params"}
        try:
            ok = await asyncio.to_thread(
                tasks_svc.update,
                db_path=solaris_db_path,
                uid=uid,
                entity_id=entity_id,
                title=str(params.get("title") or ""),
                due=str(params.get("due") or ""),
            )
        except ValueError:
            return {"ok": False, "reason": "bad_title"}
        if ok:
            await cascade_task_event_configured(solaris_db_path, entity_id)
        return {"ok": ok}

    def _write_quick_note(uid: str, text: str) -> str:
        """A quick vault note from the composer `.note` command: a dated markdown
        file under the resident's `notes/` that the nightly ingest projects +
        `notes_search` finds. Returns the vault-relative path."""
        from datetime import datetime, timezone

        root = Path(notes_dir).resolve()
        base = root if uid == notes_search.SHARED_UID else root / "users" / uid
        d = base / "notes"
        d.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower())[:40].strip("-") or "notiz"
        path = d / f"{now:%Y-%m-%d-%H%M%S}-{slug}.md"
        path.write_text(
            f"---\nadded_by: {uid}\ndate: {now:%Y-%m-%d}\ntype: note\n---\n\n{text}\n",
            encoding="utf-8",
        )
        return str(path.relative_to(root))

    async def _note_add(body: dict[str, Any]) -> dict[str, Any]:
        """Card callback: file a quick note from the `.note` command (params: text)."""
        uid = body.get("uid") or default_uid
        text = str((body.get("params") or {}).get("text") or "").strip()
        if not text:
            return {"ok": False, "reason": "empty"}
        rel = await asyncio.to_thread(_write_quick_note, uid, text)
        return {"ok": True, "path": rel}

    def _create_document(
        uid: str, upload_rel: str, category: str, tags: list[str]
    ) -> str:
        """Sort a just-uploaded file into a `document` entity with a category +
        keyword facts, so it shows under Dokumente immediately and is searchable;
        the nightly extractor later fills the detail fields on the same entity
        (identity_key = the upload path)."""
        src = _notes_resolve_owned(notes_dir, upload_rel, uid)
        title = (
            src.stem
            if src is not None
            else (upload_rel.rsplit("/", 1)[-1] or "Dokument")
        )
        facts: list[tuple[str, str, float | None]] = [
            ("category", category or "other", 1.0)
        ]
        facts += [("keyword", t, 1.0) for t in tags[:12]]
        rec = ConceptRecord(
            type="document",
            title=title,
            source="upload",
            external_id=f"upload:{upload_rel}",
            identity_key=upload_rel,
            resident=uid,
            facts=facts,
        )
        return write_concept(
            rec, db_path=solaris_db_path, notes_dir=notes_dir, ingesting_uid=uid
        ).ref_id

    async def _doc_classify(body: dict[str, Any]) -> dict[str, Any]:
        """Card callback: sort a `.doc` upload (params: upload, category, tags)."""
        uid = body.get("uid") or default_uid
        p = body.get("params") or {}
        upload_rel = str(p.get("upload") or "")
        if not upload_rel:
            return {"ok": False, "reason": "bad_params"}
        category = (str(p.get("category") or "other").strip()) or "other"
        tags = [
            t.strip().lstrip("#")
            for t in re.split(r"[,\s]+", str(p.get("tags") or ""))
            if t.strip()
        ]
        eid = await asyncio.to_thread(_create_document, uid, upload_rel, category, tags)
        return {"ok": True, "id": eid, "category": category, "tags": tags}

    def _existing_contact_facts(uid: str, title: str) -> dict[str, str]:
        """The `contact`-source email/phone already on this resident's person of
        that name, or `{}`. A write replaces a source's facts wholesale (ADR
        0003), so a second `.contacts` create that converges onto an existing
        person must carry these forward or it silently drops them. Owner-gated by
        `resolve_entity` (same `resident_uid`), like the create itself."""
        conn = projection.open_conn(solaris_db_path)
        try:
            entity_id = projection.resolve_entity(
                conn,
                type="person",
                canonical_name=title,
                resident_uid=uid,
                aliases=[],
            )
            if entity_id is None:
                return {}
            return {
                r["predicate"]: r["value"]
                for r in conn.execute(
                    "SELECT predicate, value FROM facts"
                    " WHERE subject_entity_id = ? AND source = 'contact'"
                    " AND predicate IN ('email', 'phone')",
                    (entity_id,),
                ).fetchall()
            }
        finally:
            conn.close()

    def _create_contact(uid: str, name: str, email: str, phone: str) -> str:
        title = name or email or phone or "Kontakt"
        # Identity is derived from the resident + the name, NOT a fresh uuid per
        # create: a random key was the largest duplicate source in the ADR 0010
        # audit — creating "Anna Meyer" twice minted two people, and neither
        # carried the phone/email a later dedup pass could have matched on. With
        # a stable key the second create converges on the same entity (writer,
        # ADR 0010 §1) and an unchanged repeat short-circuits on `ingest_log`.
        key = f"contact:{uid}:{safe_slug(title)}"
        prior = _existing_contact_facts(uid, title)
        email = email or prior.get("email", "")
        phone = phone or prior.get("phone", "")
        facts: list[tuple[str, str, float | None]] = []
        if email:
            facts.append(("email", email, 1.0))
        if phone:
            facts.append(("phone", phone, 1.0))
        rec = ConceptRecord(
            type="person",
            title=title,
            source="contact",
            external_id=key,
            identity_key=key,
            resident=uid,
            facts=facts,
        )
        return write_concept(
            rec, db_path=solaris_db_path, notes_dir=notes_dir, ingesting_uid=uid
        ).ref_id

    async def _contact_add(body: dict[str, Any]) -> dict[str, Any]:
        """Card callback: create a personal contact from `.contacts` (params: value,
        or explicit name/email/phone)."""
        uid = body.get("uid") or default_uid
        p = body.get("params") or {}
        name = str(p.get("name") or "").strip()
        email = str(p.get("email") or "").strip()
        phone = str(p.get("phone") or "").strip()
        value = str(p.get("value") or "").strip()
        if value and not (name or email or phone):
            name, email, phone = _parse_contact_input(value)
        if not (name or email or phone):
            return {"ok": False, "reason": "empty"}
        eid = await asyncio.to_thread(_create_contact, uid, name, email, phone)
        return {"ok": True, "id": eid, "name": name, "email": email, "phone": phone}

    def _update_contact(
        uid: str, entity_id: str, name: str, email: str, phone: str
    ) -> bool:
        """Correct a personal contact's name/email/phone. Writes the email/phone
        facts under source `contact` — the same source `.contacts` creates under —
        so `replace_facts` overwrites the created values (the correction wins), and
        renames the entity for the name. Owner-gated: the caller must own or share
        the person."""
        conn = projection.open_conn(solaris_db_path)
        try:
            ent = conn.execute(
                "SELECT resident_uid FROM entities WHERE id = ? AND type = 'person'",
                (entity_id,),
            ).fetchone()
            if ent is None or ent["resident_uid"] not in (uid, notes_search.SHARED_UID):
                return False
            facts: list[tuple[str, str, float | None]] = []
            if email:
                facts.append(("email", email, 1.0))
            if phone:
                facts.append(("phone", phone, 1.0))
            projection.replace_facts(
                conn,
                subject_entity_id=entity_id,
                resident_uid=ent["resident_uid"],
                source="contact",
                facts=facts,
            )
            if name:
                conn.execute(
                    "UPDATE entities SET canonical_name = ? WHERE id = ?",
                    (name, entity_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()

    async def _person_update(body: dict[str, Any]) -> dict[str, Any]:
        """Card callback: correct a `.contacts` person (params: entity_id, name,
        email, phone) — the edit leg of the create-and-find pattern (#967)."""
        uid = body.get("uid") or default_uid
        p = body.get("params") or {}
        entity_id = str(p.get("entity_id") or "").strip()
        name = str(p.get("name") or "").strip()
        email = str(p.get("email") or "").strip()
        phone = str(p.get("phone") or "").strip()
        if not entity_id:
            return {"ok": False, "reason": "bad_params"}
        if not (name or email or phone):
            return {"ok": False, "reason": "empty"}
        ok = await asyncio.to_thread(
            _update_contact, uid, entity_id, name, email, phone
        )
        return {"ok": ok, "name": name, "email": email, "phone": phone}

    async def portal_persons(request: web.Request) -> web.Response:
        """Personal contacts for the `.contacts` filter (own ∪ shared household)."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        rows = await asyncio.to_thread(
            documents_portal_db.person_contacts, solaris_db_path, uid
        )
        return web.json_response({"ok": True, "contacts": rows or []})

    def _document_confirm_fact(
        uid: str, entity_id: str, predicate: str, value: str
    ) -> bool:
        """Write a human-confirmed document fact (confidence 1.0) under source
        `documents:confirmed` — coexists with and outranks the agent-extracted
        0.6 fact (ADR 0003), and survives the source-scoped re-ingest. Owner-
        gated: the caller must own or share the document."""
        conn = projection.open_conn(solaris_db_path)
        try:
            ent = conn.execute(
                "SELECT resident_uid FROM entities WHERE id = ? AND type = 'document'",
                (entity_id,),
            ).fetchone()
            if ent is None or ent["resident_uid"] not in (uid, notes_search.SHARED_UID):
                return False
            projection.upsert_fact(
                conn,
                subject_entity_id=entity_id,
                resident_uid=ent["resident_uid"],
                source="documents:confirmed",
                predicate=predicate,
                value=value,
                confidence=1.0,
            )
            conn.commit()
            return True
        finally:
            conn.close()

    async def portal_documents_correct(request: web.Request) -> web.Response:
        """Confirm/correct one document field. POST `{entity_id, predicate,
        value}` → writes it at confidence 1.0 (the „Korrigieren" action)."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a malformed body is a 400, not a 500.
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        entity_id = str(body.get("entity_id") or "").strip()
        predicate = str(body.get("predicate") or "").strip()
        value = str(body.get("value") or "")
        if not entity_id or not predicate:
            return web.json_response(
                {"ok": False, "error": "entity_id + predicate required"}, status=400
            )
        ok = await asyncio.to_thread(
            _document_confirm_fact, uid, entity_id, predicate, value
        )
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    def _interactive_uid(request: web.Request) -> str | None:
        """The resident behind a real Authelia session, or None (#717).

        Minting/listing/revoking a device token is only allowed from an
        interactive session — the trusted-proxy `Remote-User` header set by
        Authelia. A device-token bearer or the SOLARIS_API_KEY service key does
        NOT satisfy this: a device token must not be usable to mint another
        device token, and the service key is not a resident. So this reads the
        header directly and does NOT go through `resolve_uid` (which would honour
        the bearer path and the loopback `default_uid` fallback).

        On the proxy-bypassed `/napi/` prefix there is no `Remote-User`; the
        `native(...)` wrapper has already validated the device-token bearer and
        stashed its owner, so use that (a device token CAN manage its own owner's
        paired devices, but still cannot mint — minting isn't on `/napi/`)."""
        native = request.get("native_uid")
        if native is not None:
            return native
        value = request.headers.get(remote_user_header, "").strip()
        return value or None

    def native(handler: Handler) -> Handler:
        """Gate a shared `/api/` handler for the proxy-bypassed `/napi/` prefix.

        Fail-closed because proxy-bypassed (#757): a `/napi/*` request must carry
        a valid `sol_device_` device-token bearer or it is 401 — it must NEVER
        fall through to the household `default_uid` (an internet caller could then
        control the house) nor trust a `Remote-User` header. On success the
        wrapped handler runs unchanged; since `resolve_uid` is native-prefix aware
        it resolves the SAME device-token owner_uid, so the endpoint's behaviour
        is identical to `/api/` for the authenticated resident."""

        @functools.wraps(handler)
        async def wrapper(request: web.Request) -> web.StreamResponse:
            try:
                uid = native_uid(request, solaris_db_path)
            except db_health.Unavailable as exc:
                # An unreadable solaris.db is OUR fault, not the caller's (#1272).
                # A 500 tells the client "try again, maybe it works" and the app
                # duly retried every minute for twenty minutes in #1271; 503 +
                # Retry-After tells it to back off until the box is fixed, and it
                # is the same answer /health gives for the same state.
                log.error("napi.db_unavailable", path=request.path, reason=str(exc))
                return web.json_response(
                    {"ok": False, "error": "database_unavailable", "reason": str(exc)},
                    status=503,
                    headers={"Retry-After": str(DB_RETRY_AFTER_S)},
                )
            if uid is None:
                return web.json_response(
                    {"ok": False, "error": "unauthorized"}, status=401
                )
            # Owner for the device-token-management routes (`_interactive_uid`),
            # which key off Remote-User on `/api/` but must key off the validated
            # device-token owner here since the proxy is bypassed.
            request["native_uid"] = uid
            return await handler(request)

        return wrapper

    async def device_token_create(request: web.Request) -> web.Response:
        """Mint a long-lived device token for a native client (#717).

        Body `{label}`. Returns `{id, token}` — the plaintext is shown ONCE and
        never recoverable. Requires an interactive Authelia session; a device
        token or the service key can NOT mint another token."""
        owner = _interactive_uid(request)
        if owner is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        label = str(body.get("label") or "")[:128]
        token_id, token = device_token_store.create(solaris_db_path, owner, label)
        return web.json_response({"ok": True, "id": token_id, "token": token})

    async def device_token_list(request: web.Request) -> web.Response:
        """The caller's device tokens — metadata only, no hash/plaintext (#717)."""
        owner = _interactive_uid(request)
        if owner is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return web.json_response(
            {
                "ok": True,
                "tokens": device_token_store.list_for_uid(solaris_db_path, owner),
            }
        )

    async def device_token_revoke(request: web.Request) -> web.Response:
        """Revoke one of the caller's device tokens (owner-checked, #717)."""
        owner = _interactive_uid(request)
        if owner is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        token_id = request.match_info["id"]
        ok = device_token_store.revoke(solaris_db_path, owner, token_id)
        return web.json_response({"ok": ok}, status=200 if ok else 404)

    # ---- Browser wake-word recorder (#1081) --------------------------------
    # The Voice PE detects "Solaris" on-device and consumes it as an activation,
    # so the wake word can never reach the server as content. The chat surface
    # (browser AND the TWA app, which is the same surface) has a microphone and
    # no wake-word gate, so the samples are recorded here instead. uid ALWAYS
    # comes from the Authelia session — never from the request — so a resident
    # can only ever write into their own set.

    def _wakeword_target(uid: str) -> int:
        req = wakeword_requests_store.get_request(solaris_db_path, uid) or {}
        return int(
            req.get("target_count") or wakeword_requests_store.WAKEWORD_TARGET_SAMPLES
        )

    def _wakeword_stored(uid: str, target: int) -> list[int]:
        """The sample indices that really exist on disk — the counter follows
        these files, so a re-record or a replayed upload can't double-count."""
        return [
            i
            for i in range(1, target + 1)
            if os.path.exists(
                wakeword_samples_store.sample_path(solaris_db_path, uid, i)
            )
        ]

    async def wakeword_samples_state(request: web.Request) -> web.Response:
        """How far the caller's own "Solaris" recordings have got (#1081)."""
        uid = _interactive_uid(request)
        if uid is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        target = _wakeword_target(uid)
        stored = _wakeword_stored(uid, target)
        return web.json_response(
            {"ok": True, "target": target, "collected": len(stored), "samples": stored}
        )

    async def wakeword_sample_upload(request: web.Request) -> web.Response:
        """Store one browser-recorded "Solaris" sample (#1081).

        `multipart/form-data` with an `index` field (1..target, sent BEFORE the
        file) and a `file` part holding a 16 kHz mono 16-bit WAV. Writing the
        same index again replaces that recording — that is how "neu aufnehmen"
        works — and the counter is recomputed from the files on disk, so a
        re-record or a retried upload never counts twice (#1080). This endpoint
        is the single writer of `collected_count` on this path.
        """
        uid = _interactive_uid(request)
        if uid is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        if not request.content_type.startswith("multipart/"):
            return web.json_response(
                {"ok": False, "error": "Die Aufnahme kam nicht richtig an."}, status=400
            )
        try:
            reader = await request.multipart()
        except (ValueError, AssertionError):
            return web.json_response(
                {"ok": False, "error": "Die Aufnahme kam nicht richtig an."}, status=400
            )
        target = _wakeword_target(uid)
        index = 0
        data = bytearray()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "index":
                raw = (await part.text()).strip()
                index = int(raw) if raw.isdigit() else 0
                continue
            if part.name != "file":
                await part.read()
                continue
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _WAKEWORD_MAX_BYTES:
                    return web.json_response(
                        {
                            "ok": False,
                            "error": "Die Aufnahme ist zu lang. Sag bitte nur das Wort „Solaris“.",
                        },
                        status=413,
                    )
        if not 1 <= index <= target:
            return web.json_response(
                {"ok": False, "error": "Diese Aufnahme-Nummer gibt es nicht."},
                status=400,
            )
        frames = _wakeword_pcm_frames(bytes(data))
        if frames is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Diese Aufnahme hat das falsche Format und wurde nicht gespeichert.",
                },
                status=415,
            )
        if frames < _WAKEWORD_MIN_FRAMES:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Die Aufnahme war zu kurz. Sag bitte deutlich „Solaris“.",
                },
                status=400,
            )
        if frames > _WAKEWORD_MAX_FRAMES:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Die Aufnahme ist zu lang. Sag bitte nur das Wort „Solaris“.",
                },
                status=413,
            )
        dest = Path(wakeword_samples_store.sample_path(solaris_db_path, uid, index))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(bytes(data))
        wakeword_samples_store.add_sample(
            solaris_db_path,
            sample_id=f"sample_{uid}_{index}",
            filename=str(dest),
            wakeword_id="solaris",
            resident_uid=uid,
            intended_phrase="Solaris",
            stt_transcript="",
            is_valid=True,
        )
        collected = len(_wakeword_stored(uid, target))
        wakeword_requests_store.set_browser_progress(
            solaris_db_path, uid, collected, target
        )
        log.info("wakeword.browser.stored", uid=uid, index=index, collected=collected)
        return web.json_response(
            {
                "ok": True,
                "index": index,
                "target": target,
                "collected": collected,
                "done": collected >= target,
            }
        )

    async def wakeword_sample_audio(request: web.Request) -> web.Response:
        """Play back one of the caller's OWN recordings (#1081)."""
        uid = _interactive_uid(request)
        if uid is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        raw = request.match_info["index"]
        target = _wakeword_target(uid)
        if not raw.isdigit() or not 1 <= int(raw) <= target:
            raise web.HTTPNotFound()
        path = Path(wakeword_samples_store.sample_path(solaris_db_path, uid, int(raw)))
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "audio/wav"})

    async def wakeword_training_state(request: web.Request) -> web.Response:
        """What the household's newest training run is doing (#1088).

        A run takes hours, so the card cannot learn its state from the tap that
        started it — it reads this on every load, after a reload and on a fresh
        visit, and polls it while something is in flight.
        """
        if _interactive_uid(request) is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        run = wakeword_training_store.latest_run(solaris_db_path)
        return web.json_response({"ok": True, "run": _wakeword_run_view(run)})

    async def wakeword_training_enqueue(request: web.Request) -> web.Response:
        """Queue one wake-word training run — on an explicit tap only (#1088).

        uid comes from the SSO identity, never from the body, like the
        recorder's own endpoints. Check-then-enqueue needs no lock: both store
        calls are sync sqlite and nothing is awaited between them, so a second
        request cannot slip in and queue hours of duplicate GPU work.
        """
        uid = _interactive_uid(request)
        if uid is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        run = wakeword_training_store.pending_run(solaris_db_path)
        if run:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Es läuft schon ein Training — mehr als eines auf einmal geht nicht.",
                    "run": _wakeword_run_view(run),
                },
                status=409,
            )
        run_id = wakeword_training_store.enqueue_run(solaris_db_path, uid)
        if run_id is None:
            return web.json_response(
                {
                    "ok": False,
                    "error": "Das Training lässt sich gerade nicht einplanen. Versuche es später noch einmal.",
                },
                status=503,
            )
        log.info("wakeword.training.enqueued", uid=uid, run_id=run_id)
        started = wakeword_training_store.latest_run(solaris_db_path)
        return web.json_response({"ok": True, "run": _wakeword_run_view(started)})

    def _import_llm_classifier():
        """A sync ``folder -> label`` backed by the household LLM for the classify
        step (fail-open to ""). Runs inside `asyncio.to_thread`, so `asyncio.run`
        is safe (a worker thread has no running loop) — mirroring the music
        importer's classifier bridge."""
        client = OllamaChat(ollama_url)

        async def _ask(folder: str) -> str:
            msgs = [
                {"role": "system", "content": import_flow._CLASSIFY_SYS},
                {"role": "user", "content": f"Ordner: {folder}"},
            ]
            out = None
            async for kind, payload in client.stream(
                current_household_model(),
                msgs,
                tools=None,
                think=False,
                options={"num_predict": 8, "temperature": 0.0},
            ):
                if kind == "done":
                    out = payload
            return out.content.strip() if out is not None else ""

        def classify(folder: str) -> str:
            try:
                return asyncio.run(_ask(folder))
            except (OllamaError, OSError, RuntimeError, ValueError):
                return ""

        return classify

    def _import_job_cfg(owner: str) -> dict[str, Any]:
        """The per-run config the durable `import` job needs to drive each importer
        (the write targets it must reach). Owner-scoped."""
        return {
            "owner_uid": owner,
            "db_path": solaris_db_path,
            "notes_dir": notes_dir,
            "ollama_url": ollama_url,
            "model": current_household_model(),
            "caldav_url": caldav_url,
            "caldav_username": caldav_username,
            "caldav_password": caldav_password,
            "carddav_url": carddav_url,
            "carddav_username": carddav_username,
            "carddav_password": carddav_password,
            "music_dir": music_dir,
            "data_dir": import_data_dir,
        }

    def _publish_import_card(uid: str, session_id: str, card: dict[str, Any]) -> None:
        """Push a live card update to an open chat (SessionBus mirror + event bus),
        the same fan-out `inject_message` uses for an action card."""
        if event_bus is not None:
            event_bus.publish(
                uid,
                "chat",
                {"session_id": session_id, "preview": card["title"], "card": card},
            )
        if bus is not None:
            bus.publish(session_id, uid, {"kind": "card", "event": {"card": card}})

    async def _stream_import_progress(
        uid: str, session_id: str, jid: str, categories: list[str]
    ) -> None:
        """Poll the durable job and mirror its progress into the plan card, then
        inject a final result summary card (Posteingang note already written by the
        job). Runs as a background task so the callback returns immediately."""
        last: str = ""
        for _ in range(3600):  # ~1h ceiling at 1s cadence — a job is long but bounded
            snap = import_jobs.get(jid, uid)
            if snap is None:
                return
            prog = snap.get("progress") or {}
            marker = f"{prog.get('stage')}:{prog.get('pct')}"
            if marker != last and snap["status"] == "running":
                last = marker
                _publish_import_card(
                    uid,
                    session_id,
                    {
                        "kind": "action",
                        "title": "Import läuft …",
                        "body": prog.get("message", "…"),
                        "buttons": [],
                    },
                )
            if snap["status"] in ("done", "failed", "interrupted"):
                break
            await asyncio.sleep(1)
        snap = import_jobs.get(jid, uid) or {}
        if snap.get("status") == "done" and snap.get("result"):
            card = import_flow.build_result_card(snap["result"])
            per = snap["result"].get("per_category", {})
            text = card["body"]
        else:
            card = {
                "kind": "action",
                "title": "Import fehlgeschlagen",
                "body": snap.get("error")
                or "Der Import konnte nicht abgeschlossen werden.",
                "buttons": [],
            }
            text = card["body"]
            per = {}
        if event_bus is not None:
            await inject(
                solaris_db_path, event_bus, notifier, session_id, uid, text, card=card
            )
        log.info("import.done", uid=uid, job=jid, per_category=per)

    # A pending import plan the resident hasn't confirmed yet. The plan card is
    # otherwise only a transient live chat inject, so an upload from the Notizen
    # section — or a reload before pressing Importieren — loses the actionable
    # card, leaving only the "archive received" text. Persist it beside the
    # archive so `import_status` can re-attach it; Importieren/Abbrechen consumes it.
    _PLAN_SUFFIX = ".plan.json"

    def _pending_plan_path(archive_path: Path) -> Path:
        return archive_path.parent / (archive_path.name + _PLAN_SUFFIX)

    def _write_pending_plan(
        archive_path: Path, archive_id: str, card: dict[str, Any]
    ) -> None:
        try:
            _pending_plan_path(archive_path).write_text(
                json.dumps({"archive_id": archive_id, "card": card}), encoding="utf-8"
            )
        except OSError as e:  # noqa: BLE001 — persistence is best-effort.
            log.warn("import.plan_persist_failed", error=str(e))

    def _clear_pending_plan(uid: str, archive_id: str) -> None:
        p = _notes_resolve_owned(notes_dir, archive_id, uid) if archive_id else None
        if p is not None:
            try:
                _pending_plan_path(p).unlink()
            except OSError:
                pass

    def _clear_all_pending_plans(uid: str) -> None:
        """Drop every pending plan for the resident — used on confirm so an earlier
        abandoned upload's plan can't linger and mask the live import (it made
        Importieren look dead)."""
        root = Path(notes_dir).resolve()
        base = root if uid == notes_search.SHARED_UID else root / "users" / uid
        d = base / _ARCHIVE_SUBDIR
        if not d.is_dir():
            return
        for p in d.glob(f"*{_PLAN_SUFFIX}"):
            try:
                p.unlink()
            except OSError:
                pass

    def _latest_pending_plan(uid: str) -> dict[str, Any] | None:
        root = Path(notes_dir).resolve()
        base = root if uid == notes_search.SHARED_UID else root / "users" / uid
        d = base / _ARCHIVE_SUBDIR
        if not d.is_dir():
            return None
        for p in sorted(
            d.glob(f"*{_PLAN_SUFFIX}"), key=lambda f: f.stat().st_mtime, reverse=True
        ):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
        return None

    async def _handle_takeout_archive(
        request: web.Request, uid: str, part: Any, base: Path
    ) -> web.Response:
        """Store an uploaded Takeout `.zip`, classify it, and inject a plan card.

        The archive is stored under `<owner>/imports/` and NOT processed inline:
        classify inspects its manifest (mechanical structure + LLM for an ambiguous
        folder), and the plan action-card (findings + choices) is injected into the
        resident's household chat. Nothing is imported until they press Importieren
        (the callback enqueues the durable job). Idempotent: the archive is named
        by its content hash, so a re-upload reuses the same stored file."""
        if import_jobs is None:
            return web.json_response(
                {"ok": False, "error": "import unavailable"}, status=503
            )
        data = bytearray()
        while True:
            chunk = await part.read_chunk()
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _ARCHIVE_MAX_BYTES:
                return web.json_response(
                    {"ok": False, "error": "archive too large"}, status=413
                )
        if not data:
            return web.json_response({"ok": False, "error": "empty file"}, status=400)
        # A bare Takeout `.json` (dropped without zipping) is wrapped into a
        # single-entry zip so the rest of the pipeline stays zip-only.
        zip_bytes = _ensure_takeout_zip(bytes(data), part.filename or "")
        archive_hash = import_flow.content_hash(zip_bytes)
        import_dir = base / _ARCHIVE_SUBDIR
        import_dir.mkdir(parents=True, exist_ok=True)
        archive_path = import_dir / f"takeout-{archive_hash}.zip"
        archive_path.write_bytes(zip_bytes)  # idempotent: hash-named, re-upload no-ops
        try:
            classification = await asyncio.to_thread(
                import_flow.classify_archive,
                zip_bytes,
                llm=_import_llm_classifier(),
            )
        except Exception as exc:  # noqa: BLE001 — a malformed zip is a 400, not a 500
            log.warn("import.classify_failed", uid=uid, error=str(exc))
            return web.json_response(
                {"ok": False, "error": "unreadable archive"}, status=400
            )
        if not classification["claims"]:
            return web.json_response(
                {"ok": False, "error": "no importable data in archive"}, status=400
            )
        archive_id = str(archive_path.relative_to(Path(notes_dir).resolve()))
        card = import_flow.build_plan_card(classification, archive_id)
        # The plan is persisted (reload re-attaches it) and returned in the
        # response — the client renders the single actionable card inline where
        # the upload happened. No household-session SSE inject: it duplicated the
        # inline card and only surfaced when that exact session was open.
        _write_pending_plan(archive_path, archive_id, card)
        log.info(
            "import.uploaded",
            uid=uid,
            hash=archive_hash,
            claims=[c["category"] for c in classification["claims"]],
        )
        return web.json_response(
            {
                "ok": True,
                "kind": "import",
                "archive_id": archive_id,
                "classification": classification,
                "card": card,
            }
        )

    async def _import_confirm(body: dict[str, Any]) -> dict[str, Any]:
        """Action-card callback: enqueue the durable import job for the selected
        categories and start streaming progress into the card (#869)."""
        if import_jobs is None or event_bus is None:
            return {"ok": False, "reason": "import_unavailable"}
        params = body.get("params") or {}
        uid = body.get("uid") or default_uid  # stamped by action_callback
        archive_id = params.get("archive_id")
        categories = [
            c
            for c in (params.get("categories") or [])
            if c in import_flow._CATEGORY_RUNNERS
        ]
        if not isinstance(archive_id, str) or not categories:
            return {"ok": False, "reason": "bad_params"}
        archive_path = _notes_resolve_owned(notes_dir, archive_id, uid)
        if archive_path is None:
            return {"ok": False, "reason": "archive_not_found"}
        payload = {
            **_import_job_cfg(uid),
            "archive_path": str(archive_path),
            "categories": categories,
            "hash": params.get("hash", ""),
        }
        job_id = import_jobs.start(uid, "import", payload)
        _clear_all_pending_plans(uid)  # confirmed → drop this + any abandoned plans
        session_id = store.ensure_household_session(solaris_db_path, uid)
        asyncio.ensure_future(
            _stream_import_progress(uid, session_id, job_id, categories)
        )
        return {"ok": True, "jobId": job_id, "categories": categories}

    async def _import_cancel(body: dict[str, Any]) -> dict[str, Any]:
        """Action-card callback: dismiss the plan without importing (#869)."""
        params = body.get("params") or {}
        uid = body.get("uid") or default_uid
        _clear_pending_plan(uid, str(params.get("archive_id") or ""))
        return {"ok": True, "detail": "abgebrochen"}

    async def import_status(request: web.Request) -> web.Response:
        """The caller's latest import job — live status/progress for the Notizen
        "Google-Daten importieren" section so a long import stays visible and a
        reload re-attaches (#869 P4b). Read-only, owner-scoped via `latest_for`."""
        uid = _interactive_uid(request)
        if uid is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        # A RUNNING job's progress trumps any pending plan: a stale, never-confirmed
        # plan from an earlier abandoned upload must not mask the live import (that
        # left "Importieren" looking dead — the job ran, but status kept returning
        # the old plan). Otherwise a pending plan re-attaches its actionable card on
        # reload (it exists only between upload and Importieren/Abbrechen).
        job = import_jobs.latest_for(uid) if import_jobs is not None else None
        if job is not None and job.get("status") == "running":
            return web.json_response({"ok": True, "job": job})
        plan = _latest_pending_plan(uid)
        if plan is not None:
            return web.json_response({"ok": True, "plan": plan})
        return web.json_response({"ok": True, "job": job})

    # The import plan-card buttons (#869). Confirm WRITES (calendar/contacts into
    # Radicale, notes into the vault, wishlist facts onto album entities), so it is
    # confirm-gated via the same mechanism the destructive Wartung cards use.
    # Not destructive: "Importieren" is itself the explicit intent, and the import
    # is additive + idempotent + lands in the Posteingang for review — a second
    # generic browser confirm() is just friction.
    action_cards.register(import_flow.CONFIRM_ACTION, _import_confirm)
    action_cards.register(import_flow.CANCEL_ACTION, _import_cancel)
    # Every `.tool` action handler, keyed by its action id — the pool a `kind:
    # tool` def's `tool-actions` auto-registers from (#1004, ADR 0011), so a new
    # tool wires its actions by naming them in one SKILL.md, not by hand here.
    # None of these is destructive (a task/note/contact is the resident's own,
    # additive + reversible).
    _TOOL_ACTION_HANDLERS: dict[str, action_cards.Handler] = {
        "task.set_status": _task_set_status,
        "task.add": _task_add,
        "task.update": _task_update,
        "note.add": _note_add,
        "doc.classify": _doc_classify,
        "contact.add": _contact_add,
        "person.update": _person_update,
    }

    # Auto-register the actions each tool def declares, from the pool above.
    # A def naming an unknown action id is skipped (the handler may not have
    # shipped yet); its own endpoint stays a separate concern.
    _registered_by_tool: set[str] = set()
    for _tool in skills.list_tool_defs(skills_dir):
        for _action_id in _tool["tool-actions"]:
            handler = _TOOL_ACTION_HANDLERS.get(_action_id)
            if handler is None:
                continue
            action_cards.register(_action_id, handler)
            _registered_by_tool.add(_action_id)

    # The existing `.tools` not yet migrated to a def (#1006) keep their inline
    # wiring — register any handler a tool def didn't already claim.
    for _action_id, handler in _TOOL_ACTION_HANDLERS.items():
        if _action_id not in _registered_by_tool:
            action_cards.register(_action_id, handler)

    async def napi_upload(request: web.Request) -> web.Response:
        """Store a camera capture / PDF / Takeout `.zip` into the vault (#826/#869).

        Reachable from BOTH surfaces on the same handler (no parallel endpoint,
        ADR 0007): `/napi/upload` (device-token, fail-closed via `native(...)`)
        and `/api/upload` (an interactive browser session, uid from `Remote-User`).
        The uid is resolved by `_interactive_uid`, which honours whichever proved
        the caller; a request that proves neither is 401.

        `multipart/form-data` with N `file` parts. An image/PDF part is written into
        the resident's notes vault under `<owner>/uploads/` with an embedding note
        (`notes_search`-visible). A Google-Takeout `.zip` part is instead stored
        under `<owner>/imports/` and handed to the interactive import flow (#869):
        it is classified, a plan action-card is injected into the resident's chat,
        and nothing is imported until they confirm — the archive is never processed
        inline. Owner-scoped. Fails cleanly: 415 unsupported type, 413 too large,
        400 bad request."""
        uid = _interactive_uid(request)
        if uid is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        if not request.content_type.startswith("multipart/"):
            return web.json_response(
                {"ok": False, "error": "expected multipart/form-data"}, status=400
            )
        base = (
            Path(notes_dir)
            if uid == notes_search.SHARED_UID
            else Path(notes_dir) / "users" / uid
        )
        upload_dir = base / _UPLOAD_SUBDIR
        chosen_name = ""
        results: list[dict[str, Any]] = []
        try:
            reader = await request.multipart()
        except (ValueError, AssertionError):
            return web.json_response(
                {"ok": False, "error": "malformed multipart"}, status=400
            )
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "filename":
                chosen_name = (await part.text()).strip()
                continue
            if part.name in ("kind",):
                await part.text()  # drained, advisory only
                continue
            if part.name != "file":
                await part.read()  # drain an unexpected part
                continue
            mime = (part.headers.get("Content-Type") or "").split(";")[0].strip()
            part_name = part.filename or ""
            if (
                mime in _ARCHIVE_MIME
                or part_name.lower().endswith((".zip", ".json"))
                or mime == "application/json"
            ):
                return await _handle_takeout_archive(request, uid, part, base)
            if mime not in _UPLOAD_MIME_EXT:
                return web.json_response(
                    {"ok": False, "error": f"unsupported type: {mime or 'unknown'}"},
                    status=415,
                )
            if len(results) >= _UPLOAD_MAX_FILES:
                return web.json_response(
                    {"ok": False, "error": "too many files"}, status=400
                )
            data = bytearray()
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > _UPLOAD_MAX_BYTES:
                    return web.json_response(
                        {"ok": False, "error": "file too large"}, status=413
                    )
            if not data:
                return web.json_response(
                    {"ok": False, "error": "empty file"}, status=400
                )
            raw_name = chosen_name or part.filename or "upload"
            safe = _sanitize_upload_name(raw_name, mime)
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_upload_path(upload_dir, safe)
            dest.write_bytes(bytes(data))
            rel_file = str(dest.relative_to(Path(notes_dir).resolve()))
            note_rel = _write_upload_note(notes_dir, uid, dest, mime)
            # Extract the document's text into the companion body off the request
            # path: pdftotext/OCR is slow, so run it in a thread and don't
            # await it — the HTTP response returns immediately; the nightly ingest
            # re-runs it idempotently if this one is lost to a restart. The
            # paperless push is deliberately NOT fired here — it's lazy-via-cron
            # by design (#1042); see engine/ingest/paperless.py.
            companion = Path(notes_dir).resolve() / note_rel
            asyncio.get_event_loop().run_in_executor(
                None, extract_into_companion, companion
            )
            results.append({"ok": True, "id": rel_file, "url": f"/notes/{rel_file}"})
            log.info(
                "napi.upload.stored", uid=uid, file=rel_file, note=note_rel, mime=mime
            )
        if not results:
            return web.json_response({"ok": False, "error": "no file part"}, status=400)
        if len(results) == 1:
            return web.json_response(results[0])
        return web.json_response({"ok": True, "files": results})

    def _immich_client() -> RestImmichClient | None:
        """The Immich REST client, or None when Immich is unconfigured — the
        `.photo` handlers degrade to a clear message instead of a 500."""
        if not (immich_base_url and immich_api_key):
            return None
        return RestImmichClient(immich_base_url, immich_api_key)

    async def photo_upload(request: web.Request) -> web.Response:
        """`.photo` dropzone: upload an image part to Immich (#961). Interactive
        session only (Remote-User); a request that proves no resident is 401."""
        uid = _interactive_uid(request)
        if uid is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        client = _immich_client()
        if client is None:
            return web.json_response(
                {"ok": False, "error": "Immich ist nicht konfiguriert."}, status=503
            )
        if not request.content_type.startswith("multipart/"):
            return web.json_response(
                {"ok": False, "error": "expected multipart/form-data"}, status=400
            )
        try:
            reader = await request.multipart()
        except (ValueError, AssertionError):
            return web.json_response(
                {"ok": False, "error": "malformed multipart"}, status=400
            )
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name != "file":
                await part.read()
                continue
            data = bytearray()
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                data.extend(chunk)
            if not data:
                return web.json_response(
                    {"ok": False, "error": "empty file"}, status=400
                )
            mime = (part.headers.get("Content-Type") or "").split(";")[0].strip()
            name = part.filename or "foto.jpg"
            try:
                asset_id = await client.upload_asset(
                    bytes(data),
                    name,
                    content_type=mime or "application/octet-stream",
                )
            except (aiohttp.ClientError, TimeoutError) as e:
                log.error("photo.upload.failed", uid=uid, error=str(e))
                return web.json_response(
                    {"ok": False, "error": "Upload fehlgeschlagen."}, status=502
                )
            log.info("photo.upload.stored", uid=uid, asset=asset_id, name=name)
            return web.json_response({"ok": True, "id": asset_id, "name": name})
        return web.json_response({"ok": False, "error": "no file part"}, status=400)

    async def photo_search(request: web.Request) -> web.Response:
        """`.photo <text>` filter: matching Immich photos by tagged person /
        caption / filename (#961). Reuses the read-only metadata search; degrades
        to a clear message when Immich is unconfigured."""
        uid = _interactive_uid(request)
        if uid is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        client = _immich_client()
        if client is None:
            return web.json_response(
                {"ok": False, "error": "Immich ist nicht konfiguriert."}, status=503
            )
        q = request.query.get("q", "").strip().lower()
        photos: list[dict[str, Any]] = []
        try:
            async for asset in client.iter_assets():
                people = [p.name for p in asset.people]
                if q and not (
                    q in asset.file_name.lower()
                    or any(q in name.lower() for name in people)
                ):
                    continue
                photos.append(
                    {
                        "id": asset.id,
                        "name": asset.file_name,
                        "when": asset.when,
                        "people": people,
                        "url": client.asset_uri(asset.id),
                    }
                )
                if len(photos) >= 40:
                    break
        except (aiohttp.ClientError, TimeoutError) as e:
            log.error("photo.search.failed", uid=uid, error=str(e))
            return web.json_response(
                {"ok": False, "error": "Suche fehlgeschlagen."}, status=502
            )
        return web.json_response({"ok": True, "photos": photos})

    async def upload_download(request: web.Request) -> web.StreamResponse:
        """Serve the caller's own uploaded original for the `📎 Original öffnen`
        link. Owner-scoped: only the current resident's `uploads/` folder,
        with a path-jail so `..` / a cross-resident target is rejected."""
        uid = _interactive_uid(request)
        if uid is None:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        base = (
            Path(notes_dir)
            if uid == notes_search.SHARED_UID
            else Path(notes_dir) / "users" / uid
        )
        uploads_dir = (base / _UPLOAD_SUBDIR).resolve()
        resolved = (uploads_dir / request.match_info["path"]).resolve()
        if (
            not str(resolved).startswith(str(uploads_dir) + os.sep)
            or not resolved.is_file()
        ):
            return web.json_response({"ok": False, "error": "not found"}, status=404)
        return web.FileResponse(resolved)

    async def pair_device_page(request: web.Request) -> web.Response:
        """Authelia-gated confirm page for pairing a native Android client (#751).

        Opened by the app in a Chrome Custom Tab (where the browser's Authelia
        cookies exist). Renders a confirm form and lists the resident's paired
        devices — it does NOT mint a token on load: minting only happens on the
        explicit POST below, so a drive-by/prefetched GET can't create a token
        (CSRF protection). Requires an interactive session; a device-token
        bearer or the service key is NOT a resident and gets 401."""
        owner = _interactive_uid(request)
        if owner is None:
            return web.Response(status=401, text="Nur für angemeldete Bewohner.")
        tokens = device_token_store.list_for_uid(solaris_db_path, owner)
        rows = "".join(
            "<li>{label} <button class='revoke' data-id='{id}'>"
            "Entfernen</button></li>".format(
                label=html.escape(t.get("label") or "Unbenanntes Gerät"),
                id=html.escape(t["id"]),
            )
            for t in tokens
        )
        if not rows:
            rows = "<li class='empty'>Noch keine Geräte gekoppelt.</li>"
        page = _PAIR_DEVICE_HTML.format(devices=rows)
        return web.Response(
            text=page,
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def pair_device_confirm(request: web.Request) -> web.Response:
        """Mint a device token on explicit confirm and hand it to the app (#751).

        Guarded by the SAME interactive-session check as POST /api/device-tokens
        (fail-closed): a device-token/service-key caller can't mint. On success
        it 302-redirects the browser to the app's deep link, carrying the token
        in the URL FRAGMENT (`#token=…`) — the fragment is never sent to the
        server/proxy, keeping the plaintext out of the redirect hop's logs. The
        scheme MUST equal the app's packageId (twa-manifest / assetlinks)."""
        owner = _interactive_uid(request)
        if owner is None:
            return web.Response(status=401, text="Nur für angemeldete Bewohner.")
        form = await request.post()
        label = str(form.get("label") or "").strip()[:128] or "Android-Gerät"
        token_id, token = device_token_store.create(solaris_db_path, owner, label)
        deep_link = f"{android_package}://pair#token={token}&id={token_id}"
        # Set Location directly rather than via HTTPFound, which normalises the
        # URL and would inject a slash before the fragment (`pair/#…`); the app's
        # intent-filter matches the exact `…://pair` path.
        return web.Response(status=302, headers={"Location": deep_link})

    async def push_subscribe(request: web.Request) -> web.Response:
        """Register a browser PushSubscription for the caller (#713).

        Body is the `PushSubscription.toJSON()` shape: `endpoint` + `keys.p256dh`
        / `keys.auth`. Owner-scoped to the Authelia identity; the endpoint URL is
        the unique key, so a re-subscribe upserts."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        endpoint = str(body.get("endpoint") or "")
        keys = body.get("keys") if isinstance(body.get("keys"), dict) else {}
        p256dh = str(keys.get("p256dh") or "")
        auth = str(keys.get("auth") or "")
        if not (endpoint and p256dh and auth):
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        user_agent = request.headers.get("User-Agent", "")[:256]
        push_store.upsert(solaris_db_path, uid, endpoint, p256dh, auth, user_agent)
        return web.json_response({"ok": True})

    async def push_unsubscribe(request: web.Request) -> web.Response:
        """Drop the caller's subscription for one endpoint (owner-scoped, #713)."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        endpoint = str(body.get("endpoint") or "")
        if not endpoint:
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        # Owner-scope: only remove an endpoint that belongs to the caller, so a
        # resident can't drop another's device by guessing its endpoint.
        owned = {s["endpoint"] for s in push_store.list_for_uid(solaris_db_path, uid)}
        if endpoint in owned:
            push_store.remove_by_endpoint(solaris_db_path, endpoint)
        return web.json_response({"ok": True})

    async def favorites_create(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        kind = str(body.get("kind") or "")
        label = str(body.get("label") or "")
        payload = body.get("payload")
        if kind not in ("entity", "action", "link") or not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        owner = favorites_store.HOUSEHOLD if body.get("scope") == "household" else uid
        if kind == "action":
            if payload.get("tool") not in PINNABLE_TOOLS:
                return web.json_response(
                    {"ok": False, "error": "tool_not_pinnable"}, status=403
                )
            if _action_is_sensitive(payload):
                return web.json_response(
                    {"ok": False, "error": "sensitive_action"}, status=403
                )
        fav_id = favorites_store.add_favorite(
            solaris_db_path, owner, kind, label, payload
        )
        return web.json_response({"ok": True, "id": fav_id})

    def _owner_for(request: web.Request, fav_id: str) -> str | None:
        """The owner_uid a resident may mutate this favorite under: their own
        uid or `household`, whichever holds the row. None → not found/forbidden."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        for fav in favorites_store.list_favorites(solaris_db_path, uid):
            if fav["id"] == fav_id:
                return fav["owner_uid"]
        return None

    async def favorites_delete(request: web.Request) -> web.Response:
        fav_id = request.match_info["fav_id"]
        owner = _owner_for(request, fav_id)
        if owner is None:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        favorites_store.remove_favorite(solaris_db_path, owner, fav_id)
        return web.json_response({"ok": True})

    async def favorites_reorder(request: web.Request) -> web.Response:
        fav_id = request.match_info["fav_id"]
        owner = _owner_for(request, fav_id)
        if owner is None:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        try:
            position = int(body.get("position"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        favorites_store.set_position(solaris_db_path, owner, fav_id, position)
        return web.json_response({"ok": True})

    async def favorites_scope(request: web.Request) -> web.Response:
        """Move a favorite between the resident's personal scope and household
        (#745). Owner-checked like delete/reorder — the row must already belong
        to the caller (own uid or `household`) before it can be re-owned."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        fav_id = request.match_info["fav_id"]
        owner = _owner_for(request, fav_id)
        if owner is None:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        to_owner = (
            favorites_store.HOUSEHOLD if body.get("scope") == "household" else uid
        )
        favorites_store.move_scope(solaris_db_path, owner, fav_id, to_owner)
        return web.json_response({"ok": True})

    async def favorites_run(request: web.Request) -> web.Response:
        """Execute a pinned action favorite verbatim (#646). Re-checks the
        confirm policy — a one-tap start-page bypass of the gate must not
        exist — then dispatches on the household gateway toolbox."""
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        fav_id = request.match_info["fav_id"]
        row = next(
            (
                f
                for f in favorites_store.list_favorites(solaris_db_path, uid)
                if f["id"] == fav_id
            ),
            None,
        )
        if row is None:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        if row["kind"] != "action":
            return web.json_response({"ok": False, "error": "not_action"}, status=400)
        payload = row["payload"]
        tool = str(payload.get("tool") or "")
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        if tool not in PINNABLE_TOOLS:
            return web.json_response(
                {"ok": False, "error": "tool_not_pinnable"}, status=403
            )
        if _action_is_sensitive(payload):
            return web.json_response(
                {"ok": False, "error": "sensitive_action"}, status=403
            )
        current_uid.set(uid)
        output = await engine.dispatch_tool(tool, args)
        favorites_store.record_usage(solaris_db_path, row["owner_uid"], tool, args)
        try:
            result = json.loads(output)
        except (TypeError, ValueError):
            result = {"ok": True, "raw": output}
        return web.json_response({"ok": True, "result": result})

    async def anchors_resolve(request: web.Request) -> web.Response:
        """Resolve auto-anchors (#501) to OKF entity ids (#506).

        Owner-scoped: each `#`/`@` anchor's bare value is matched against
        `entity_aliases`/`entities` (migration 0016) via the same resolver the
        concept aggregator uses. A token without a `#`/`@` prefix is resolved
        whole — this is the `[[X]]` cross-link path (#504), which shares this
        resolver. Returns `{resolved: {token: entity_id}}` for the ones that hit
        a known entity; unresolved tokens are absent so the client keeps phase
        1's `/search` filter chip (anchors) or plain text (`[[ ]]`).
        """
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        body = await request.json()
        anchors = [str(a) for a in (body.get("anchors") or [])]
        resolved: dict[str, str] = {}
        if anchors and Path(solaris_db_path).exists():
            conn = projection.open_conn(solaris_db_path)
            try:
                for anchor in anchors:
                    value = (
                        anchor[1:].strip()
                        if anchor[:1] in ("#", "@")
                        else anchor.strip()
                    )
                    if not value:
                        continue
                    entity_id = projection.resolve_entity_id(conn, value, uid)
                    if entity_id is not None:
                        resolved[anchor] = entity_id
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        return web.json_response({"ok": True, "resolved": resolved})

    # Auto-linkify alias index (#694): a bounded, server-cached list the client
    # uses to upgrade known references in a rendered reply into concept-page
    # links, code-enforced (the small model rarely emits [[..]]/ANCHORS). Two
    # sources, reusing the existing accessors: HA entities (friendly_name +
    # entity_id → the raw entity_id, which /c/<id> takes directly) and OKF
    # entities (canonical_name + aliases → the entity id). Short aliases (≤2
    # chars) and an over-cap payload are dropped so the single client-side regex
    # stays cheap.
    _ALIAS_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
    _ALIAS_TTL = 300.0
    _ALIAS_CAP = 400

    async def anchors_aliases(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        cached = _ALIAS_CACHE.get(uid)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _ALIAS_TTL:
            return web.json_response({"ok": True, "aliases": cached[1]})

        seen: set[str] = set()
        pairs: list[tuple[str, str]] = []

        def add(alias: str, target: str) -> None:
            alias = (alias or "").strip()
            key = alias.lower()
            if len(alias) <= 2 or not target or key in seen:
                return
            seen.add(key)
            pairs.append((alias, target))

        if hass_url and hass_token:
            ha_names = await fetch_entity_names(hass_url, hass_token)
            for row in ha_names or []:
                eid = row["entity_id"]
                add(row["name"], eid)
                add(eid, eid)
        if Path(solaris_db_path).exists():
            conn = projection.open_conn(solaris_db_path)
            try:
                for alias, entity_id in projection.linkable_aliases(conn, uid):
                    add(alias, entity_id)
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()

        # Longest alias first so the client prefers the most specific match;
        # cap the payload so the single-pass regex stays cheap.
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        aliases = [{"alias": a, "id": i} for a, i in pairs[:_ALIAS_CAP]]
        _ALIAS_CACHE[uid] = (now, aliases)
        return web.json_response({"ok": True, "aliases": aliases})

    async def mentions_tags(request: web.Request) -> web.Response:
        # Autosuggest for `#tag` (#279): the resident's already-used tags,
        # prefix-filtered. Per-resident scope (owner_uid = resolve_uid).
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        prefix = request.rel_url.query.get("q", "").strip().lstrip("#").lower()
        values = mentions_store.known_tags_for(solaris_db_path, uid, prefix)
        return web.json_response(
            {"ok": True, "tags": [{"kind": "tag", "value": v} for v in values]}
        )

    async def mentions_persons(request: web.Request) -> web.Response:
        # Autosuggest for `@person` (#279): used persons unioned with the seed
        # (residents/uid registry + manual list; CardDAV later, #207). The
        # caller's own uid is always a resident; other residents come from config.
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        prefix = request.rel_url.query.get("q", "").strip().lstrip("@").lower()
        seed = seeded_persons([uid, *resident_uids])
        used = mentions_store.known_persons_for(solaris_db_path, uid)
        # ADR 0010: also suggest the `person` entities (canonical names + aliases),
        # so a `.contacts` person is offered even before it's been chat-mentioned.
        directory = documents_portal_db.person_directory(solaris_db_path, uid) or []
        entity_names = [p["name"] for p in directory]
        entity_aliases = [a for p in directory for a in p["aliases"]]
        merged = _dedup(
            v.lower() for v in [*used, *seed, *entity_names, *entity_aliases]
        )
        if prefix:
            merged = [v for v in merged if v.startswith(prefix)]
        merged.sort()
        return web.json_response(
            {"ok": True, "persons": [{"kind": "person", "value": v} for v in merged]}
        )

    async def session_mentions(request: web.Request) -> web.Response:
        # The tag-cloud for one chat (#279c): the resident's `#tag` / `@person`
        # mentions in this session, each with the message_ref that carried it
        # (first appearance) for jump-to-message. Per-resident scope.
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        session_id = request.match_info["session_id"]
        items = mentions_store.list_session_mentions(solaris_db_path, session_id, uid)
        return web.json_response({"ok": True, "mentions": items})

    def _resolve_image_hook() -> None:
        # An image-only turn fires the `image-upload` event; the hook that acts
        # on it is resolved from the registry so a rebind in the `/hooks` editor
        # changes which definition handles it (no hardcoded id).
        bound = skills.hooks_for_event(skills_dir, _IMAGE_UPLOAD_EVENT)
        log.info("chat.hook.event", event=_IMAGE_UPLOAD_EVENT, hooks=bound)

    async def chat(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )

        images = _images_from(body)
        text = str(body.get("input") or "").strip()
        if not text and not images:
            return web.json_response({"ok": False, "reason": "empty_input"}, status=400)
        if not text:
            text = _IMAGE_PROMPT
            _resolve_image_hook()
        session_id = str(body.get("session_id") or "")
        topic_slug = str(body.get("topic") or "").strip()
        ephemeral_input = bool(body.get("ephemeral"))
        # The shared Zuhause is owned by default_uid but any resident may act in
        # it (#649): owner_uid drives session routing/scope; the real `uid` stays
        # the typed turn's identity (timers/facts) via turn_uid below.
        admin = is_admin(request, remote_groups_header, admin_group)
        owner_uid = effective_uid(uid, session_id, admin=admin)
        if session_id and not owns_session(owner_uid, session_id):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        # Household ("Zuhause") turns are fast-only: never think, never escalate.
        household = is_household_chat(owner_uid, session_id, topic_slug)
        effort = (
            "none"
            if household
            else reasoning.choose_effort(
                text,
                selector=body.get("reasoning"),
                admin=admin,
                pref=other_model_pref,
            )
        )
        client = gateway_for(
            request,
            session_id,
            body.get("personality"),
            uid=owner_uid,
            topic_slug=topic_slug,
        )
        ephemeral = ephemeral_input or client.ephemeral
        ensure_wartung_row(request, session_id)
        pin_admin_identity(request)

        clock = asyncio.get_event_loop().time
        t_start = clock() * 1000.0
        wall_t0 = time.time()  # wall-clock window for proxy trace correlation (#306)
        compacted = False
        try:
            # Only a missing session_id starts a fresh engine session; turn 2+
            # carry the same id back, so consecutive turns reuse one warm
            # engine session (and its KV prefix cache). A cold turn-2 TTFT is
            # therefore Ollama model eviction, not a per-turn session (#268).
            if not session_id or client.ephemeral:
                session_id = await create_turn_session(
                    owner_uid,
                    topic_slug,
                    text,
                    ephemeral,
                    client,
                )
                owner_uid = effective_uid(uid, session_id, admin=admin)
            elif not ephemeral:
                session_id, compacted = await maybe_compact(
                    owner_uid, session_id, client
                )
            turn_text = topic_turn_text(
                text, uid, session_id, ephemeral=ephemeral, extract_topic=topic_slug
            )
            persist_mentions(uid, session_id, text, ephemeral=ephemeral)
            reply = await client.chat(
                session_id, turn_text, images, effort, turn_uid=uid
            )
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        # #1267: `{"ok": true, "reply": ""}` is a lie of omission — the caller
        # reports success while the resident is told nothing, after a turn that
        # may well have switched real devices. An empty reply never ships.
        if not reply.strip():
            reply = NO_ANSWER
        attachments.add(session_id, images)
        await persist_turn_trace(owner_uid, session_id, wall_t0, ephemeral=ephemeral)
        # Non-streamed turn: only total wall-time is observable (no per-phase
        # boundaries without the stream), so the trace carries just the total
        # (#225). The streaming path is where the phase waterfall comes from.
        total_ms = clock() * 1000.0 - t_start
        trace = _trace_from_phases([], total_ms)

        # A completed turn propagates to the owner (#715): live to an open SSE
        # client, or a Web Push with the session deep-link when the app is
        # backgrounded. The non-stream path is the background/voice/API caller —
        # it may push (no live stream is showing the reply).
        if event_bus is not None and not ephemeral:
            await emit_chat(event_bus, notifier, owner_uid, session_id, reply)

        return web.json_response(
            {
                "ok": True,
                "session_id": session_id,
                "reply": reply,
                "trace": trace,
                "compacted": compacted,
            }
        )

    async def chat_stream(request: web.Request) -> web.StreamResponse:
        uid = resolve_uid(request, remote_user_header, default_uid, solaris_db_path)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )

        images = _images_from(body)
        text = str(body.get("input") or "").strip()
        if not text and not images:
            return web.json_response({"ok": False, "reason": "empty_input"}, status=400)
        if not text:
            text = _IMAGE_PROMPT
            _resolve_image_hook()
        session_id = str(body.get("session_id") or "")
        topic_slug = str(body.get("topic") or "").strip()
        ephemeral_input = bool(body.get("ephemeral"))
        # The shared Zuhause is owned by default_uid but any resident may act in
        # it (#649): owner_uid drives session routing/scope; the real `uid` stays
        # the typed turn's identity (timers/facts) via turn_uid below.
        admin = is_admin(request, remote_groups_header, admin_group)
        owner_uid = effective_uid(uid, session_id, admin=admin)
        if session_id and not owns_session(owner_uid, session_id):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        # Household ("Zuhause") turns are fast-only: never think, never escalate.
        household = is_household_chat(owner_uid, session_id, topic_slug)
        effort = (
            "none"
            if household
            else reasoning.choose_effort(
                text,
                selector=body.get("reasoning"),
                admin=admin,
                pref=other_model_pref,
            )
        )
        client = gateway_for(
            request,
            session_id,
            body.get("personality"),
            uid=owner_uid,
            topic_slug=topic_slug,
        )
        ephemeral = ephemeral_input or client.ephemeral
        ensure_wartung_row(request, session_id)
        pin_admin_identity(request)

        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)

        cancel = asyncio.Event()
        # Phase timing for the latency trace (#225). Timestamps are monotonic
        # ms; only the boundaries the proxy can actually see on the wire are
        # captured (see _trace_from_phases for what is/isn't observable).
        clock = asyncio.get_event_loop().time
        t_start = clock() * 1000.0
        wall_t0 = time.time()  # wall-clock window for proxy trace correlation (#306)
        t_first: float | None = None  # first delta -> prefill / TTFT
        t_think_end: float | None = None  # </thinking> seen -> reasoning ends
        tool_ms = 0.0
        t_tool: float | None = None  # open tool round-trip
        answer_buf = ""
        ha_cards: list[dict[str, Any]] = []
        suggestions: list[str] = []
        anchors: list[str] = []
        cancelled = False
        try:
            compacted = False
            if not session_id or client.ephemeral:
                session_id = await create_turn_session(
                    owner_uid,
                    topic_slug,
                    text,
                    ephemeral,
                    client,
                )
                owner_uid = effective_uid(uid, session_id, admin=admin)
            elif not ephemeral:
                session_id, compacted = await maybe_compact(
                    owner_uid, session_id, client
                )
            cancels[session_id] = cancel
            await _send_event(
                resp, "session", {"session_id": session_id, "compacted": compacted}
            )
            # Persist the attachment once the turn is under way (the engine has the
            # user message; we hold the pixels it drops) so history re-renders
            # the thumbnail after a refresh (#202).
            attachments.add(session_id, images)
            turn_text = topic_turn_text(
                text, uid, session_id, ephemeral=ephemeral, extract_topic=topic_slug
            )
            persist_mentions(uid, session_id, text, ephemeral=ephemeral)
            stream = client.chat_stream(
                session_id,
                turn_text,
                images,
                effort,
                suggest_answers=True,
                turn_uid=uid,
            )
            async for event in _heartbeat(stream, resp):
                if cancel.is_set():
                    # Closing the upstream generator aborts the engine/Ollama
                    # run (#192) — stops generation, not just our forwarding.
                    await stream.aclose()
                    await _send_event(resp, "cancelled", {})
                    cancelled = True
                    break
                name, data = _normalize(event)
                now = clock() * 1000.0
                if name == "delta":
                    if t_first is None:
                        t_first = now
                    answer_buf += data.get("text", "")
                    if t_think_end is None and _THINK_CLOSE in answer_buf.lower():
                        t_think_end = now
                elif name == "tool":
                    if data.get("phase") == "started":
                        t_tool = now
                    elif t_tool is not None:
                        tool_ms += now - t_tool
                        t_tool = None
                elif name == "completed":
                    # gemma4 returns its reasoning on every run.completed (the
                    # `reasoning_content` field), regardless of effort — so the
                    # block is gated here on the per-turn effort, not on the engine:
                    # a fast ("none") turn surfaces nothing (#222); a thorough
                    # turn emits a distinct `reasoning` event the panel renders
                    # collapsibly (#231). The forwarded `completed` stays bare.
                    reasoning_text = data.pop("reasoning", "")
                    if effort != "none" and reasoning_text:
                        await _send_event(resp, "reasoning", {"text": reasoning_text})
                    # A tool-invocation turn (e.g. a Home Assistant state query)
                    # streams no answer deltas — the final summary arrives only
                    # on run.completed. Surface it as a late delta so the browser
                    # renders the reply instead of an empty bubble (#258).
                    completed_answer = data.pop("answer", "")
                    if not answer_buf.strip() and completed_answer:
                        await _send_event(resp, "delta", {"text": completed_answer})
                        answer_buf += completed_answer
                elif name == "ha_cards":
                    ha_cards = data.get("cards") or []
                elif name == "suggestions":
                    suggestions = data.get("suggestions") or []
                elif name == "anchors":
                    anchors = data.get("anchors") or []
                await _send_event(resp, name, data)
            if not cancelled:
                t_end = clock() * 1000.0
                trace = _trace_from_phases(
                    _stream_phases(t_start, t_first, t_think_end, t_end, tool_ms),
                    t_end - t_start,
                )
                await _send_event(resp, "trace", trace)
                record_anchors(uid, session_id, anchors, text, ephemeral=ephemeral)
                await persist_turn_trace(
                    owner_uid,
                    session_id,
                    wall_t0,
                    ephemeral=ephemeral,
                    ha_cards=ha_cards,
                    suggestions=suggestions,
                    anchors=anchors,
                )
                # Fan the completed turn to the owner's OTHER open tabs over SSE
                # (#715). This is a foreground typed turn — the requesting client
                # is watching the reply stream live — so it never self-pushes.
                if event_bus is not None and not ephemeral:
                    await emit_chat(
                        event_bus,
                        notifier,
                        owner_uid,
                        session_id,
                        answer_buf,
                        push=False,
                    )
        except EngineError:
            await _send_event(resp, "error", {"reason": "engine_unavailable"})
        finally:
            cancels.pop(session_id, None)
        await _send_event(resp, "done", {})
        return resp

    @web.middleware
    async def csp(request: web.Request, handler: Any) -> web.StreamResponse:
        # CSP frame-ancestors gates who may iframe the chat (#228). Set on
        # every response; no X-Frame-Options (it conflicts with CSP).
        resp = await handler(request)
        resp.headers["Content-Security-Policy"] = f"frame-ancestors {frame_ancestors}"
        return resp

    # Raise the whole-body cap above aiohttp's 1 MB default so a native camera
    # upload / PDF series (#826) reaches its handler; the per-file (413) and
    # per-request-count (400) guards there are the real limits. A Takeout `.zip`
    # (#869) is far larger, so the cap is the archive limit — the upload handler's
    # own per-type guard rejects an oversized non-archive part.
    app = web.Application(
        middlewares=[csp, widget_usage],
        client_max_size=_ARCHIVE_MAX_BYTES + 8 * 1024 * 1024,
    )
    app.router.add_get("/", index)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/.well-known/assetlinks.json", assetlinks)
    app.router.add_get("/download", download)
    app.router.add_get("/health", health)
    app.router.add_get("/api/whoami", whoami)
    app.router.add_post("/api/ha/call", ha_call)
    app.router.add_get("/api/toolsets", list_toolsets)
    app.router.add_get("/api/mcp", list_mcp)
    app.router.add_post("/api/mcp/{server}/test", test_mcp)
    app.router.add_get("/api/personalities", list_personalities)
    app.router.add_get("/api/skills", list_skills)
    app.router.add_get("/api/skills/{skill_id}", get_skill)
    app.router.add_put("/api/skills/{skill_id}", put_skill)
    app.router.add_get("/api/defs/{kind}", list_defs)
    app.router.add_get("/api/defs/{kind}/{def_id}", get_def)
    app.router.add_put("/api/defs/{kind}/{def_id}", put_def)
    app.router.add_delete("/api/defs/{kind}/{def_id}", delete_def_route)
    app.router.add_get("/api/soul", get_soul)
    app.router.add_put("/api/soul", put_soul)
    app.router.add_get("/api/model", get_model)
    app.router.add_put("/api/model", put_model)
    app.router.add_get("/api/voice", get_voice)
    app.router.add_put("/api/voice", put_voice)
    app.router.add_get("/api/vram", get_vram)
    app.router.add_post("/api/model/pull", pull_model)
    app.router.add_post("/api/model-lease", set_model_lease)
    app.router.add_delete("/api/model-lease", clear_model_lease)
    # Loopback-only, like the model lease above — NOT on /napi/ (see the
    # handler). This is the route an HA `rest_command` posts to.
    app.router.add_post("/api/ha/notify", ha_notify_post)
    app.router.add_get("/api/sessions", list_sessions)
    app.router.add_post("/api/sessions", create_session)
    app.router.add_get("/api/sessions/{session_id}", get_session)
    app.router.add_delete("/api/sessions/{session_id}", delete_session)
    app.router.add_get("/api/topics", list_topics)
    app.router.add_post("/api/topics", create_topic)
    app.router.add_get("/api/sessions/{session_id}/topics", get_session_topics)
    app.router.add_post("/api/sessions/{session_id}/topics", set_session_topics)
    app.router.add_get("/api/topics/{slug:.+}/items", topic_items)
    app.router.add_get("/api/concept/{id}", concept_view)
    app.router.add_get("/c/{id}", concept_page)
    app.router.add_get("/api/portal/energy", portal_energy)
    app.router.add_get("/api/portal/energy/history", portal_energy_history)
    app.router.add_get("/api/portal/entity-history", portal_entity_history)
    app.router.add_get("/api/portal/start", portal_start)
    app.router.add_get("/api/portal/start/addable", portal_start_addable)
    # Single-device deep link (#769): the browser/Authelia session reads one
    # entity's card for the #/p/device/<entity_id> route (the /napi/ twin serves
    # the device-token widget path).
    app.router.add_get("/api/portal/state", portal_state)
    app.router.add_get(
        "/api/portal/camera/{entity_id}/snapshot", portal_camera_snapshot
    )
    app.router.add_get("/api/events", portal_events)
    app.router.add_post("/api/inject", inject_message)
    app.router.add_post("/api/action-callback", action_callback)
    # ServiceBay BFF verdict (#811 part 2): the app deep-links Approve/Reject to
    # this Authelia-gated route (trusted admin identity) — NOT /napi (proxy-
    # bypassed, no trusted admin gate). It mints a per-action delegated-admin
    # assertion from the acting admin's session (servicebay#2276), no standing key.
    app.router.add_post(
        "/api/servicebay/approvals/{id}/{verb}", servicebay_approval_verdict
    )
    # What the verdict page shows before it offers the buttons (#1085).
    app.router.add_get("/api/servicebay/approvals/{id}", servicebay_approval_detail)
    app.router.add_get("/api/portal/notes", portal_notes)
    app.router.add_get("/api/portal/notes/browse", portal_notes_browse)
    app.router.add_get("/api/portal/notes/note", portal_notes_note)
    app.router.add_put("/api/portal/notes/note", portal_notes_note_put)
    app.router.add_get("/api/portal/notes/stats", portal_notes_stats)
    app.router.add_get("/api/portal/notes/search", portal_notes_search)
    app.router.add_get("/api/portal/notes/inbox", portal_notes_inbox)
    app.router.add_post("/api/portal/notes/assign", portal_notes_assign)
    app.router.add_post("/api/portal/notes/archive", portal_notes_archive)
    app.router.add_post("/api/portal/notes/curate", portal_notes_curate)
    app.router.add_get("/api/portal/documents", portal_documents)
    app.router.add_get("/api/portal/contacts", portal_contacts)
    app.router.add_get("/api/portal/tasks", portal_tasks)
    app.router.add_get("/api/portal/persons", portal_persons)
    app.router.add_post("/api/portal/documents/correct", portal_documents_correct)
    app.router.add_get("/api/portal/documents/search", portal_documents_search)
    app.router.add_get("/api/portal/documents/{category}", portal_documents_category)
    app.router.add_post("/api/favorites", favorites_create)
    app.router.add_delete("/api/favorites/{fav_id}", favorites_delete)
    app.router.add_put("/api/favorites/{fav_id}", favorites_reorder)
    app.router.add_post("/api/favorites/{fav_id}/scope", favorites_scope)
    app.router.add_post("/api/favorites/{fav_id}/run", favorites_run)
    app.router.add_post("/api/push/subscribe", push_subscribe)
    app.router.add_post("/api/push/unsubscribe", push_unsubscribe)
    app.router.add_get("/api/wakeword/samples", wakeword_samples_state)
    app.router.add_post("/api/wakeword/samples", wakeword_sample_upload)
    app.router.add_get("/api/wakeword/samples/{index}", wakeword_sample_audio)
    app.router.add_get("/api/wakeword/training", wakeword_training_state)
    app.router.add_post("/api/wakeword/training", wakeword_training_enqueue)
    app.router.add_post("/api/device-tokens", device_token_create)
    app.router.add_get("/api/device-tokens", device_token_list)
    app.router.add_delete("/api/device-tokens/{id}", device_token_revoke)
    app.router.add_get("/pair-device", pair_device_page)
    app.router.add_post("/pair-device", pair_device_confirm)
    # Native-API prefix for the Android widgets (#757). SAME handler callables as
    # the `/api/` routes above, but wrapped in `native(...)` so they are
    # device-token-ONLY and fail-closed (401 without a valid `sol_device_`
    # bearer) — because Authelia BYPASSES this prefix, they must never inherit the
    # household `default_uid` or trust a `Remote-User` header. Token MINTING
    # (`POST /api/device-tokens`, `/pair-device`) is deliberately NOT mirrored
    # here: it stays interactive-Authelia-only (#748/#751).
    app.router.add_get("/napi/whoami", native(whoami))
    app.router.add_get("/napi/portal/start", native(portal_start))
    app.router.add_get("/napi/portal/start/addable", native(portal_start_addable))
    app.router.add_get("/napi/portal/active", native(portal_active))
    app.router.add_get("/napi/portal/cameras", native(portal_cameras))
    app.router.add_get("/napi/portal/state", native(portal_state))
    app.router.add_get("/napi/portal/events", native(portal_events))
    app.router.add_post("/napi/portal/watch", native(portal_watch))
    app.router.add_get("/napi/portal/energy", native(portal_energy))
    app.router.add_get("/napi/portal/entity-history", native(portal_entity_history))
    app.router.add_get(
        "/napi/portal/camera/{entity_id}/snapshot", native(portal_camera_snapshot)
    )
    app.router.add_post("/napi/ha/call", native(ha_call))
    app.router.add_post("/napi/upload", native(napi_upload))
    # Browser-session upload (#869): the same handler, reachable from an
    # interactive Authelia session (uid from `Remote-User`) so a resident can drop
    # a Takeout `.zip` from the web app. No `native(...)` wrapper — the device-token
    # gate is only for the proxy-bypassed `/napi/` prefix.
    app.router.add_post("/api/upload", napi_upload)
    # Serve the caller's own uploaded originals for the note `📎 Original öffnen`
    # link — owner-scoped, path-jailed.
    app.router.add_get("/api/uploads/{path:.*}", upload_download)
    # `.photo` dot-command (#961): upload an image to Immich, and filter photos by
    # tagged person / caption. Interactive session; degrades when Immich is unset.
    app.router.add_post("/api/photo", photo_upload)
    app.router.add_get("/api/photo", photo_search)
    # Import job status (#869 P4b): the Notizen import section polls this to show
    # live progress + re-attach to a running import after a reload (owner-scoped).
    app.router.add_get("/api/import/status", import_status)
    # ServiceBay BFF reads (#811): re-serve SB's companion reads under Solaris's
    # own /napi so the app never talks to ServiceBay directly (ADR 0010).
    app.router.add_get("/napi/servicebay/{key}", native(servicebay_read))
    app.router.add_post(
        "/napi/servicebay/services/{name}/operate", native(servicebay_operate)
    )
    app.router.add_get("/napi/device-tokens", native(device_token_list))
    app.router.add_delete("/napi/device-tokens/{id}", native(device_token_revoke))
    # `.tool` plugin system on the native/device-token surface (#1021, ADR 0011):
    # the catalog, each list-tool's declared `tool-api-path`, and the tool actions
    # — all the same handlers as `/api/`, wrapped in `native(...)` so a device
    # token alone authenticates and `resolve_uid` scopes to its owner. This lets a
    # new `.tool` show up as an Android widget with no native code. `home`/`energy`
    # already have their `/napi/portal/*` twins above; these add the rest.
    app.router.add_get("/napi/defs/tool", native(napi_tool_defs))
    app.router.add_get("/napi/portal/tasks", native(portal_tasks))
    app.router.add_get("/napi/portal/persons", native(portal_persons))
    app.router.add_get("/napi/portal/documents/search", native(portal_documents_search))
    app.router.add_get("/napi/photo", native(photo_search))
    app.router.add_post("/napi/action-callback", native(action_callback))
    app.router.add_get("/p/{type}", portal_page)
    app.router.add_post("/api/anchors/resolve", anchors_resolve)
    app.router.add_get("/api/anchors/aliases", anchors_aliases)
    app.router.add_get("/api/mentions/tags", mentions_tags)
    app.router.add_get("/api/mentions/persons", mentions_persons)
    app.router.add_get("/api/sessions/{session_id}/mentions", session_mentions)
    app.router.add_get("/api/sessions/{session_id}/trace", session_trace)
    app.router.add_get("/api/sessions/{session_id}/events", session_events)
    app.router.add_get("/__traces__/{detail_id}", trace_detail)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/chat/stream", chat_stream)
    app.router.add_post("/api/chat/cancel", cancel_chat)
    app.router.add_static("/static/", STATIC_DIR)
    # Ollama-compatible facade under /ollama — HA's `ollama` integration
    # points here so Solaris is the Assist conversation agent; the gatekeeper
    # speaks the same surface for wyoming-satellite hardware.
    if hasattr(engine, "respond"):
        facade_clients = {"solaris": engine}
        # The guest profile (#353) is reachable as its own model but not yet
        # auto-triggered — speaker-ID routing into it is #351 (blocked).
        if engine_guest is not None:
            facade_clients["solaris-guest"] = engine_guest
        # The enrolment profile has to be registered here or the facade's
        # isolation branch can never fire: it looks the client up by name and
        # silently no-ops when it is missing. That left the wakeword dialog on
        # the household profile — full soul, device registry and HA tools —
        # which is exactly the context pollution the profile exists to stop.
        if engine_enrollment is not None:
            facade_clients["solaris-enrollment"] = engine_enrollment
        add_facade_routes(
            app,
            clients=facade_clients,
            api_key=api_key,
            default_uid=default_uid,
            solaris_db_path=solaris_db_path,
            speaker_id_enabled=speaker_id_enabled,
            event_bus=event_bus,
            notifier=notifier,
        )
    return app


# Legacy boundary marker for the latency trace (#225): if a model ever streams
# an inline `</thinking>` close tag in the answer deltas this splits reasoning
# from answer. gemma4 does NOT — it surfaces reasoning as a distinct field on
# run.completed (#231), delivered in one shot at turn end — so this no longer
# fires for gemma4 and the reasoning phase simply folds into the answer span.
_THINK_CLOSE = "</think"


def _stream_phases(
    t_start: float,
    t_first: float | None,
    t_think_end: float | None,
    t_end: float,
    tool_ms: float,
) -> list[tuple[str, float]]:
    """Turn the stream timestamps into labelled phase spans (#225).

    What the proxy can genuinely time, in order: prefill (turn start → first
    token), reasoning (first token → `</thinking>`, only when a block streamed),
    answer (reasoning end / first token → turn end), and the summed tool
    round-trips. The Ollama-internal prefill/eval token split is NOT here — it
    is invisible to the proxy (see _trace_from_phases).
    """
    if t_first is None:  # no tokens streamed (e.g. tool-only or empty turn)
        return [("Tool round-trip", tool_ms)] if tool_ms > 0 else []
    phases: list[tuple[str, float]] = [("Prefill (TTFT)", t_first - t_start)]
    answer_start = t_first
    if t_think_end is not None:
        phases.append(("Reasoning", t_think_end - t_first))
        answer_start = t_think_end
    phases.append(("Answer", t_end - answer_start))
    if tool_ms > 0:
        phases.append(("Tool round-trip", tool_ms))
    return phases


def _trace_from_phases(
    phases: list[tuple[str, float]], total_ms: float
) -> dict[str, Any]:
    """Assemble a per-turn latency trace from measured phase durations (#225).

    `phases` is `[(label, ms), ...]` for the spans the proxy could actually
    time on the wire — what it observes is the engine *session stream*, so the
    honest, measurable breakdown is: time-to-first-token (prefill), reasoning
    generation (the `<thinking>` block, when one streamed), answer generation,
    and tool round-trips (`tool.started`→`tool.completed`). The fine-grained
    Ollama prompt_eval/eval (prefill vs decode token) split happens *inside*
    the engine and is never streamed to this proxy, so it is deliberately absent —
    it would need the engine to expose per-pass timings to be shown.

    Each phase becomes `{label, seconds, pct}` (pct of total wall-time, so a
    sum < 100% is expected — the gaps are orchestration the proxy can't
    attribute). Zero/negative spans are dropped so the waterfall stays honest.
    """
    total = max(total_ms, 0.0)
    out = []
    for label, ms in phases:
        if ms <= 0:
            continue
        pct = (ms / total * 100.0) if total else 0.0
        out.append(
            {"label": label, "seconds": round(ms / 1000.0, 2), "pct": round(pct, 1)}
        )
    return {"total_seconds": round(total / 1000.0, 2), "phases": out}


def _images_from(body: Any) -> list[str]:
    """Pull image-attachment data URLs from a chat body (#183).

    The browser sends `data:image/...;base64,<b64>` URLs. the engine's session-chat
    consumes images as OpenAI `image_url` parts and requires the *full* data URL
    (the `data:` prefix must stay — stripping it makes the engine reject the part as
    a non-image payload, #202), so we keep each URL as-is. Non-strings, empties,
    and anything past `_MAX_IMAGES` are dropped.
    """
    raw = body.get("images") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        out.append(item)
        if len(out) >= _MAX_IMAGES:
            break
    return out


async def _send_event(
    resp: web.StreamResponse, event: str, data: dict[str, Any]
) -> None:
    frame = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    await resp.write(frame.encode("utf-8"))


# A tool-invocation turn runs two sequential Ollama passes (tool-selection, then
# the answer) with a tool round-trip between them — the engine streams nothing for
# the whole prefill of each pass, which on a busy GPU is well over a minute of
# dead air. The browser's streaming fetch (and any reverse proxy in front) drops
# an idle connection long before the late answer arrives (#319), so we emit a
# keepalive frame whenever the upstream is silent for this long. The client
# ignores the unknown event; it only keeps the connection warm.
_HEARTBEAT_S = 10.0


async def _heartbeat(
    stream: AsyncIterator[dict[str, Any]], resp: web.StreamResponse
) -> AsyncIterator[dict[str, Any]]:
    """Forward `stream`'s events, emitting a keepalive on every silent gap."""
    it = stream.__aiter__()
    nxt: asyncio.Future[dict[str, Any]] | None = None
    try:
        while True:
            nxt = asyncio.ensure_future(it.__anext__())
            while True:
                try:
                    event = await asyncio.wait_for(asyncio.shield(nxt), _HEARTBEAT_S)
                except TimeoutError:
                    await _send_event(resp, "keepalive", {})
                    continue
                except StopAsyncIteration:
                    nxt = None
                    return
                break
            nxt = None
            yield event
    finally:
        if nxt is not None:
            nxt.cancel()


def _normalize(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Fold an engine SSE event into a browser-facing `(event, data)` pair.

    The browser needs a token delta, a tool start/stop hint, an end marker,
    and (for the live activity bubble, #347) a per-LLM-pass `step`. Anything
    else collapses to a no-op `keepalive`.
    """
    etype = str(event.get("type") or "")
    data = event.get("data")
    payload = data if isinstance(data, dict) else {}
    if etype == "assistant.delta":
        text = payload.get("delta") or payload.get("text") or payload.get("content")
        if not text and isinstance(data, str):
            text = data
        return "delta", {"text": str(text or "")}
    if etype == "llm.step":
        return "step", {
            "label": str(payload.get("model") or "llm"),
            "wall_s": payload.get("wall_s"),
        }
    if etype in ("tool.started", "tool.completed"):
        name = payload.get("tool") or payload.get("name") or ""
        phase = "started" if etype == "tool.started" else "completed"
        out = {"name": str(name), "phase": phase}
        if etype == "tool.completed" and payload.get("wall_s") is not None:
            out["wall_s"] = payload["wall_s"]
        return "tool", out
    if etype == "ha_cards":
        return "ha_cards", {"cards": payload.get("cards") or []}
    if etype == "quick_replies":
        return "quick_replies", {"options": payload.get("options") or []}
    if etype == "suggestions":
        return "suggestions", {"suggestions": payload.get("suggestions") or []}
    if etype == "anchors":
        return "anchors", {"anchors": payload.get("anchors") or []}
    if etype == "run.completed":
        return "completed", {
            "reasoning": _reasoning_from_completed(payload),
            "answer": _answer_from_messages(payload.get("messages")),
        }
    return "keepalive", {}


def _answer_from_messages(messages: Any) -> str:
    """Last assistant `content` from a `run.completed` messages array, else "".

    Tool-invocation turns surface the model's final answer here rather than in
    streaming deltas, so both chat paths fall back to it (#258). The reasoning
    lives in a separate field and is skipped.
    """
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") not in (None, "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(
                str(p.get("text") or "") if isinstance(p, dict) else str(p)
                for p in content
            )
        if content:
            return str(content)
    return ""


def _reasoning_from_completed(payload: dict[str, Any]) -> str:
    """Pull the reasoning text out of a `run.completed` payload (#231).

    gemma4 does NOT emit a literal `<thinking>` tag inline in the answer
    deltas; it surfaces the reasoning as a distinct field on the final
    assistant message of the `run.completed` event — `reasoning_content`
    (preferred) or `reasoning`. Both carry the same text; the answer text is in
    `content`, separate from the reasoning. Empty string when absent.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = msg.get("reasoning_content") or msg.get("reasoning")
        if text:
            return str(text)
    return ""


async def serve(
    host: str,
    port: int,
    *,
    engine: EngineClient,
    engine_admin: EngineClient | None = None,
    engine_guest: EngineClient | None = None,
    engine_enrollment: EngineClient | None = None,
    remote_user_header: str,
    default_uid: str,
    remote_groups_header: str = "Remote-Groups",
    admin_group: str = "admins",
    skills_dir: str = "/data/skills",
    soul_path: str = "/data/SOUL.md",
    logout_url: str = "",
    context_window: ContextWindow,
    compaction_threshold: float = compaction.DEFAULT_THRESHOLD,
    attachments_dir: str = "/data/attachments",
    frame_ancestors: str = "'self'",
    fast_model: str = "",
    thorough_model: str = "",
    tts_voices: str = "martin",
    solaris_db_path: str = "/var/lib/solaris/solaris.db",
    notes_dir: str = "/opt/data/notes",
    ollama_url: str = "http://127.0.0.1:11434",
    trace_recorder: Any = None,
    api_key: str = "",
    bus: Any = None,
    event_bus: Any = None,
    notifier: Any = None,
    sb_mcp_url: str = "",
    sb_mcp_token_path: str = "",
    sb_read_token_path: str = "",
    sb_api_url: str = "",
    sb_mint_url: str = "",
    hass_url: str = "",
    hass_token: str = "",
    crons: Any = None,
    vapid_public_key: str = "",
    android_package: str = "cloud.dopp.solaris",
    android_cert_fingerprints: tuple[str, ...] = (),
    ha_watcher: Any = None,
    native_watch: Any = None,
    import_jobs: Any = None,
    caldav_url: str = "",
    caldav_username: str = "",
    caldav_password: str = "",
    carddav_url: str = "",
    carddav_username: str = "",
    carddav_password: str = "",
    music_dir: str = "/opt/data/music",
    import_data_dir: str = "/data/imports",
    immich_base_url: str = "",
    immich_api_key: str = "",
    paperless_ui_url: str = "",
    speaker_id_enabled: bool = False,
    model_lease_enabled: bool = True,
) -> None:
    if isinstance(context_window, int):
        context_window = ContextWindow.static(context_window)
    # ServiceBay BFF read client (#811): the app reaches SB's companion reads only
    # through Solaris. Reads use the non-expiring read-only SB token (#818) with
    # SB-MCP fallback, like the pollers; the mutating operate/verdict paths keep
    # the SB-MCP token. Dormant (503) when SB_API_URL is unset.
    sb_companion = sb_companion_module.SbCompanionClient(
        sb_api_url, sb_mcp_token_path, sb_mint_url, sb_read_token_path
    )
    app = build_app(
        engine=engine,
        engine_admin=engine_admin,
        engine_guest=engine_guest,
        engine_enrollment=engine_enrollment,
        remote_user_header=remote_user_header,
        default_uid=default_uid,
        remote_groups_header=remote_groups_header,
        admin_group=admin_group,
        skills_dir=skills_dir,
        soul_path=soul_path,
        logout_url=logout_url,
        context_window=context_window,
        compaction_threshold=compaction_threshold,
        attachments_dir=attachments_dir,
        frame_ancestors=frame_ancestors,
        fast_model=fast_model,
        thorough_model=thorough_model,
        tts_voices=tts_voices,
        solaris_db_path=solaris_db_path,
        speaker_id_enabled=speaker_id_enabled,
        notes_dir=notes_dir,
        ollama_url=ollama_url,
        trace_recorder=trace_recorder,
        api_key=api_key,
        bus=bus,
        event_bus=event_bus,
        notifier=notifier,
        sb_mcp_url=sb_mcp_url,
        sb_mcp_token_path=sb_mcp_token_path,
        sb_api_url=sb_api_url,
        hass_url=hass_url,
        hass_token=hass_token,
        crons=crons,
        vapid_public_key=vapid_public_key,
        android_package=android_package,
        android_cert_fingerprints=android_cert_fingerprints,
        ha_watcher=ha_watcher,
        native_watch=native_watch,
        sb_companion=sb_companion,
        import_jobs=import_jobs,
        caldav_url=caldav_url,
        caldav_username=caldav_username,
        caldav_password=caldav_password,
        carddav_url=carddav_url,
        carddav_username=carddav_username,
        carddav_password=carddav_password,
        music_dir=music_dir,
        import_data_dir=import_data_dir,
        immich_base_url=immich_base_url,
        immich_api_key=immich_api_key,
        paperless_ui_url=paperless_ui_url,
        model_lease_enabled=model_lease_enabled,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("chat.listening", host=host, port=port)
    # Re-derive the context window periodically so a model switch adapts the
    # compaction cap without a restart (no-op when an explicit override pins it).
    refresh = asyncio.create_task(context_window.refresh_loop())
    # A chronicle run that dies never sends its DELETE, so the TTL is what ends
    # the lease — and the re-warm has to happen then, not on the next resident's
    # turn, which would otherwise pay the cold load (#1260).
    lease_watch: asyncio.Task[None] | None = None
    if model_lease_enabled:

        async def rewarm_after_lease() -> None:
            log.info("chat.model_lease.expired", model=fast_model)
            if fast_model:
                await OllamaChat(ollama_url).warm(fast_model)

        lease_watch = asyncio.create_task(
            model_lease.expiry_watch(solaris_db_path, rewarm_after_lease)
        )
    try:
        await asyncio.Event().wait()
    finally:
        refresh.cancel()
        if lease_watch is not None:
            lease_watch.cancel()
        await runner.cleanup()
