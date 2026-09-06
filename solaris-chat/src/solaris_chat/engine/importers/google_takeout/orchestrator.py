"""Interactive Takeout import flow (P4a, #869, ADR 0006/0007).

The server-side glue that turns an uploaded Google-Takeout ``.zip`` into the
conversation ADR 0007 describes — ``upload → detect/classify → plan card →
process(job) → progress → result`` — reusing the per-datatype importers (#865
calendar / #866 contacts / #867 keep / #868 music) rather than reimplementing
any write path.

Three pieces:

- ``classify_archive`` inspects the zip manifest, counts what each category
  holds (calendar events, contacts, Keep notes, YouTube-Music plays), and
  returns per-category *claims*. The obvious structure (``Takeout/Calendar/*.ics``
  &c.) is mechanical; an ambiguous top-level folder is classified with the LLM
  (fail-open to "unknown") — the same "prefer unresolved over wrong" spirit as
  the music catalog.
- ``build_plan_card`` renders the findings + per-category choices as the existing
  action-card schema (``{kind, title, body, buttons:[{label, action_id, style,
  params}]}``) — one "Importieren" primary, one cancel.
- the ``import`` durable-job kind (``import_runner_factory``) runs the selected
  categories under the ``JobRunner`` — each importer writes into its existing
  target (Radicale / vault / album entities), progress streams back, a
  Posteingang note lands so imported data surfaces for triage, and a result
  summary is returned.

Per-resident (the archive is stored under the owner's upload dir; jobs are
owner-scoped) and idempotent (each importer derives stable resource names /
content hashes, so re-running the same archive overwrites rather than
duplicating — the calendar/contacts UID hrefs, Keep's uuid5 filenames, and the
music ``ingest_log`` short-circuit).
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from solaris_chat.logging import log

# Category → the Takeout folder segment that identifies it. The manifest walk is
# case/locale-tolerant: Takeout localises the top-level folder names ("Kalender",
# "Kontakte", "Notizen") but keeps the English data-file layout, so we match on
# both the folder hint and the file extension.
_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "calendar": ("calendar", "kalender"),
    "contacts": ("contacts", "kontakte"),
    "keep": ("keep", "notizen"),
    "music": ("youtube", "youtube and youtube music", "youtube und youtube music"),
}

# The classify prompt for an ambiguous top-level folder the mechanical hints
# don't recognise. One bare label; anything else falls through to "unknown".
_CLASSIFY_SYS = (
    "Ein Google-Takeout-Archiv enthält einen Ordner. Ordne ihn genau EINER"
    " Kategorie zu: 'calendar' (Kalender/Termine), 'contacts' (Kontakte),"
    " 'keep' (Notizen), 'music' (YouTube-Music-Verlauf) oder 'unknown'."
    " Antworte NUR mit dem einen Wort."
)

# Action ids the plan/result cards fire (registered in server.py's action_cards).
CONFIRM_ACTION = "import_takeout_confirm"
CANCEL_ACTION = "import_takeout_cancel"


def content_hash(zip_bytes: bytes) -> str:
    """A stable digest of the archive — the idempotency key a re-upload reuses."""
    return hashlib.sha256(zip_bytes).hexdigest()[:16]


def _members(zip_bytes: bytes) -> list[zipfile.ZipInfo]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return [i for i in zf.infolist() if not i.is_dir()]


def _read(zip_bytes: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return zf.read(name)


def _find_watch_history(zip_bytes: bytes, names: list[str]) -> str | None:
    """The YouTube watch-history JSON, found by CONTENT — Takeout **localises the
    filename** (`watch-history.json` EN, `Wiedergabeverlauf.json` DE,
    `historique-de-visionnage.json` FR, …), so a fixed English name silently
    misses every non-English export.

    The watch history is **dominated** by video-watch URLs (`watch?v=`); its
    sibling search history (`Suchverlauf.json`) is mostly `results?search_query=`
    with only a stray watched-ad — so merely *containing* a `watch?v=` is not
    enough to tell them apart. Pick the `.json` where watches most outweigh
    searches (net-positive)."""
    best_name, best_score = None, 0
    for name in names:
        if not name.lower().endswith(".json"):
            continue
        try:
            data = json.loads(_read(zip_bytes, name))
        except Exception:  # noqa: BLE001 — a non-JSON / other file just isn't it.
            continue
        if not isinstance(data, list):
            continue
        watches = sum(
            1
            for r in data
            if isinstance(r, dict) and "watch?v=" in str(r.get("titleUrl", ""))
        )
        searches = sum(
            1
            for r in data
            if isinstance(r, dict)
            and "results?search_query=" in str(r.get("titleUrl", ""))
        )
        if watches - searches > best_score:
            best_score = watches - searches
            best_name = name
    return best_name


def _top_folder(name: str) -> str:
    """The Takeout sub-folder a member sits under (``Takeout/<X>/…`` → ``<X>``)."""
    parts = name.replace("\\", "/").split("/")
    # Takeout/<Category>/… — the category is the second segment; fall back to the
    # first for a flat archive.
    return (parts[1] if len(parts) > 2 else parts[0]).strip().lower()


def _category_for(folder: str, llm: Callable[[str], str] | None) -> str:
    for category, hints in _CATEGORY_HINTS.items():
        if any(hint in folder for hint in hints):
            return category
    if llm is not None:
        try:
            label = (llm(folder) or "").strip().lower()
        except Exception as exc:  # noqa: BLE001 — classification never crashes upload.
            log.warn("import.classify.llm_failed", folder=folder, error=str(exc))
            label = ""
        if label in _CATEGORY_HINTS:
            return label
    return "unknown"


def _bucket_members(
    zip_bytes: bytes, llm: Callable[[str], str] | None
) -> dict[str, list[str]]:
    """Group archive members by category using the folder hint (+ LLM fallback).

    An ``unknown`` bucket collects members no hint/LLM placed — e.g. a bare
    Takeout ``.json`` uploaded on its own, or an oddly-named folder. It is fed to
    the content-verifying music counter so a watch history still classifies."""
    buckets: dict[str, list[str]] = {c: [] for c in (*_CATEGORY_HINTS, "unknown")}
    folder_labels: dict[str, str] = {}
    for info in _members(zip_bytes):
        folder = _top_folder(info.filename)
        category = folder_labels.get(folder)
        if category is None:
            category = _category_for(folder, llm)
            folder_labels[folder] = category
        buckets[category if category in buckets else "unknown"].append(info.filename)
    return buckets


def _count_calendar(zip_bytes: bytes, names: list[str]) -> dict[str, Any]:
    from .importers import calendar as cal

    events = 0
    calendars = 0
    samples: list[dict[str, Any]] = []
    for name in names:
        if not name.lower().endswith((".ics", ".ical")):
            continue
        try:
            preview = cal.preview(name, _read(zip_bytes, name))
        except Exception as exc:  # noqa: BLE001 — a bad file skips, not crashes.
            log.warn("import.classify.calendar_failed", file=name, error=str(exc))
            continue
        events += preview["objects"]
        calendars += 1
        if len(samples) < 5:
            samples.extend(preview["samples"][: 5 - len(samples)])
    return {
        "category": "calendar",
        "count": events,
        "calendars": calendars,
        "samples": samples,
    }


def _count_contacts(zip_bytes: bytes, names: list[str]) -> dict[str, Any]:
    from .importers import contacts as con

    cards = 0
    samples: list[str] = []
    for name in names:
        if not name.lower().endswith(".vcf"):
            continue
        try:
            preview = con.preview(name, _read(zip_bytes, name))
        except Exception as exc:  # noqa: BLE001
            log.warn("import.classify.contacts_failed", file=name, error=str(exc))
            continue
        cards += preview["cards"]
        if len(samples) < 5:
            samples.extend(preview["samples"][: 5 - len(samples)])
    return {"category": "contacts", "count": cards, "samples": samples}


def _count_keep(zip_bytes: bytes, names: list[str]) -> dict[str, Any]:
    from .importers import keep

    files = [(n, _read(zip_bytes, n)) for n in names if n.lower().endswith(".json")]
    if not files:
        return {"category": "keep", "count": 0, "samples": []}
    try:
        preview = keep.preview(files)
    except Exception as exc:  # noqa: BLE001
        log.warn("import.classify.keep_failed", error=str(exc))
        return {"category": "keep", "count": 0, "samples": []}
    return {
        "category": "keep",
        "count": preview["notes"],
        "samples": preview["samples"],
    }


def _count_music(zip_bytes: bytes, names: list[str]) -> dict[str, Any]:
    from .music_shopping import aggregate_plays

    history = _find_watch_history(zip_bytes, names)
    if history is None:
        return {"category": "music", "count": 0, "samples": []}
    try:
        plays = aggregate_plays(_read(zip_bytes, history))
    except Exception as exc:  # noqa: BLE001
        log.warn("import.classify.music_failed", error=str(exc))
        return {"category": "music", "count": 0, "samples": []}
    samples = [f"{p['artist']} – {p['title']}" for p in list(plays.values())[:5]]
    # `count` is the number of distinct played tracks — the resolvable-album count
    # is only known after the (slow) ytmusic resolution the job runs, so the card
    # summarises tracks and the result summary reports albums.
    return {
        "category": "music",
        "count": len(plays),
        "samples": samples,
        "history": history,
    }


def classify_archive(
    zip_bytes: bytes, *, llm: Callable[[str], str] | None = None
) -> dict[str, Any]:
    """Inspect a Takeout ``.zip`` and return per-category import claims.

    Returns ``{"hash", "claims": [{category, count, samples, …}, …]}``. Only
    categories that actually hold importable data appear in ``claims``. ``llm``,
    when supplied, classifies an ambiguous top-level folder the mechanical hints
    miss (fail-open to "unknown")."""
    buckets = _bucket_members(zip_bytes, llm)
    claims: list[dict[str, Any]] = []
    for claim in (
        _count_calendar(zip_bytes, buckets["calendar"]),
        _count_contacts(zip_bytes, buckets["contacts"]),
        _count_keep(zip_bytes, buckets["keep"]),
        # `unknown` (e.g. a bare uploaded .json) joins music: `_find_watch_history`
        # verifies by content, so a non-watch-history just yields count 0.
        _count_music(zip_bytes, buckets["music"] + buckets["unknown"]),
    ):
        if claim["count"] > 0:
            claims.append(claim)
    return {"hash": content_hash(zip_bytes), "claims": claims}


# ---- plan card -----------------------------------------------------------

_CATEGORY_LABEL = {
    "calendar": ("Kalender", "Termine"),
    "contacts": ("Kontakte", "Kontakte"),
    "keep": ("Notizen", "Notizen"),
    "music": ("YouTube-Music", "Titel"),
}


def _claim_line(claim: dict[str, Any]) -> str:
    noun, unit = _CATEGORY_LABEL[claim["category"]]
    return f"- **{noun}**: {claim['count']} {unit}"


def build_plan_card(classification: dict[str, Any], archive_id: str) -> dict[str, Any]:
    """Render the classify findings as an action card (ADR 0007 schema reuse).

    The card lists each category + count and offers a single "Importieren"
    primary (imports every detected category — per-category deselect is P4b's
    checkbox UI; the confirm params already carry the selectable category list)
    and a cancel. ``archive_id`` threads the stored archive through the callback
    so the job knows which file to read."""
    claims = classification["claims"]
    categories = [c["category"] for c in claims]
    body_lines = ["Ich habe in deinem Google-Takeout-Archiv gefunden:", ""]
    body_lines += [_claim_line(c) for c in claims]
    body_lines += ["", "Soll ich das importieren?"]
    params = {
        "archive_id": archive_id,
        "hash": classification["hash"],
        "categories": categories,
    }
    return {
        "kind": "action",
        "title": "Import aus Google Takeout",
        "body": "\n".join(body_lines),
        "buttons": [
            {
                "label": "Importieren",
                "action_id": CONFIRM_ACTION,
                "style": "primary",
                "params": params,
            },
            {
                "label": "Abbrechen",
                "action_id": CANCEL_ACTION,
                "style": "secondary",
                "params": {"archive_id": archive_id},
            },
        ],
    }


def build_result_card(result: dict[str, Any]) -> dict[str, Any]:
    """Render an import job's final summary as a plain (button-less) result card."""
    parts = []
    per = result.get("per_category", {})
    if "calendar" in per:
        parts.append(f"{per['calendar']} Kalendertermine")
    if "contacts" in per:
        parts.append(f"{per['contacts']} Kontakte")
    if "keep" in per:
        parts.append(f"{per['keep']} Notizen")
    if "music" in per:
        parts.append(f"{per['music']} Wunsch-Alben ergänzt")
    body = ", ".join(parts) + " importiert." if parts else "Nichts zu importieren."
    left_out = result.get("skipped", {}).get("contacts", 0)
    if left_out:
        body += (
            f" {left_out} Kontakte konnte ich nicht lesen"
            " und habe sie übersprungen — alle anderen sind da."
        )
    return {
        "kind": "action",
        "title": "Import abgeschlossen",
        "body": body,
        "buttons": [],
    }


# ---- Posteingang -----------------------------------------------------------


# ---- durable import job ----------------------------------------------------


def _run_calendar(zip_bytes: bytes, names: list[str], cfg: dict[str, Any]) -> int:
    import asyncio

    from ...ingest.dav_client import HttpDavClient
    from .importers import calendar as cal

    base = cfg["caldav_url"].rstrip("/")
    client = HttpDavClient(
        caldav_url=cfg["caldav_url"],
        caldav_username=cfg["caldav_username"],
        caldav_password=cfg["caldav_password"],
    )
    written = 0
    for name in names:
        if not name.lower().endswith((".ics", ".ical")):
            continue
        ics = _read(zip_bytes, name)
        cal_name = cal._calendar_name(name)
        collection = f"{base}/{cfg['owner_uid']}/{cal_name}/"
        report = asyncio.run(cal.import_to_dav(client, collection, name, ics))
        written += report["written"]
    return written


def _run_contacts(
    zip_bytes: bytes, names: list[str], cfg: dict[str, Any]
) -> tuple[int, int]:
    import asyncio

    from ...ingest.dav_client import HttpDavClient
    from .importers import contacts as con

    base = cfg["carddav_url"].rstrip("/")
    client = HttpDavClient(
        carddav_url=cfg["carddav_url"],
        carddav_username=cfg["carddav_username"],
        carddav_password=cfg["carddav_password"],
    )
    collection = f"{base}/{cfg['owner_uid']}/contacts/"
    written = 0
    skipped = 0
    for name in names:
        if not name.lower().endswith(".vcf"):
            continue
        vcf = _read(zip_bytes, name)
        report = asyncio.run(con.import_to_dav(client, collection, name, vcf))
        written += report["written"]
        skipped += report["skipped"]
    return written, skipped


def _run_keep(zip_bytes: bytes, names: list[str], cfg: dict[str, Any]) -> int:
    from solaris_chat import notes_search

    from .importers import keep

    root = Path(cfg["notes_dir"])
    owner = cfg["owner_uid"]
    target = (
        root if owner == notes_search.SHARED_UID else root / "users" / owner
    ) / "keep"
    files = [(n, _read(zip_bytes, n)) for n in names if n.lower().endswith(".json")]
    if not files:
        return 0
    report = keep.do_import(target, files)
    return report["written"]


def _run_music(zip_bytes: bytes, history: str, cfg: dict[str, Any], is_canceled=None):
    """Generator: yields `run_music_import`'s progress events (so the slow ytmusic
    album resolution surfaces a MOVING bar — „Alben auflösen … 500/5768" — instead
    of a frozen 0%), the final one carrying the `result` (albums_written)."""
    from .importers.music import run_music_import
    from .paths import ImporterPaths

    paths = ImporterPaths(
        radicale_data=Path(cfg.get("radicale_data", "")),
        music_dir=Path(cfg["music_dir"]),
        data_dir=Path(cfg["data_dir"]),
    )
    yield from run_music_import(
        _read(zip_bytes, history),
        paths,
        owner_uid=cfg["owner_uid"],
        db_path=cfg["db_path"],
        notes_dir=cfg["notes_dir"],
        model=cfg.get("model", ""),
        is_canceled=is_canceled,
    )


# category -> (progress label, the per-category runner)
_CATEGORY_RUNNERS: dict[str, tuple[str, Any]] = {
    "calendar": ("Kalender importieren …", _run_calendar),
    "contacts": ("Kontakte importieren …", _run_contacts),
    "keep": ("Notizen importieren …", _run_keep),
    "music": ("YouTube-Music auswerten …", _run_music),
}


def run_import(payload: dict[str, Any], is_canceled=None):
    """Generator: run each selected category's importer, yielding progress.

    Reuses the per-datatype importers (#865-#868) — no write path is
    reimplemented. Each importer is idempotent on its own key (calendar/contacts
    UID hrefs, Keep uuid5 filenames, the music ingest_log), so a re-run of the
    same archive overwrites rather than duplicating. Terminates with an event
    carrying the ``result`` summary + Posteingang note path.

    The archive is read from ``payload['archive_path']`` (or ``archive_hex`` in a
    test payload) so the durable row never holds the archive bytes — a resumed job
    re-reads the stored file."""
    if payload.get("archive_path"):
        zip_bytes = Path(payload["archive_path"]).read_bytes()
    else:
        zip_bytes = bytes.fromhex(payload["archive_hex"])
    categories = payload["categories"]
    llm = None  # classify already ran at upload; the job trusts its bucketing.
    buckets = _bucket_members(zip_bytes, llm)
    per_category: dict[str, int] = {}
    skipped: dict[str, int] = {}
    total = len(categories)
    for i, category in enumerate(categories):
        if is_canceled is not None and is_canceled():
            return
        label, fn = _CATEGORY_RUNNERS[category]
        yield {
            "stage": category,
            "message": label,
            "pct": int(100 * i / max(total, 1)),
            "done": i,
            "total": total,
        }
        try:
            if category == "music":
                # Same as classify_archive: a bare uploaded .json lands in
                # `unknown` (no Takeout folder hint); `_find_watch_history`
                # content-verifies, so include it or the run finds no history.
                history = _find_watch_history(
                    zip_bytes, buckets["music"] + buckets["unknown"]
                )
                count = 0
                for ev in (
                    _run_music(zip_bytes, history, payload, is_canceled)
                    if history is not None
                    else ()
                ):
                    if "result" in ev:
                        count = ev["result"].get("albums_written", 0)
                    else:
                        # Surface the inner resolution progress on the job so the
                        # bar actually moves during the long ytmusic lookups.
                        yield {
                            "stage": "music",
                            "message": ev.get("message", label),
                            "pct": ev.get("pct", 0),
                            "done": i,
                            "total": total,
                        }
            else:
                count = fn(zip_bytes, buckets[category], payload)
                if isinstance(count, tuple):
                    count, skipped[category] = count
        except Exception as exc:  # noqa: BLE001 — one category failing must not abort the rest.
            log.error("import.category_failed", category=category, error=str(exc))
            count = 0
        per_category[category] = count
    result = {
        "type": "import",
        "hash": payload["hash"],
        "per_category": per_category,
        "skipped": {k: v for k, v in skipped.items() if v},
    }
    note = write_posteingang_note(payload["notes_dir"], payload["owner_uid"], result)
    result["posteingang"] = note
    yield {
        "stage": "done",
        "message": "Import abgeschlossen",
        "pct": 100,
        "done": total,
        "total": total,
        "result": result,
    }


def import_runner_factory(payload: dict[str, Any]):
    """Build the durable-job factory for the ``import`` kind (registered in jobs.py)."""

    def factory(is_canceled):
        return run_import(payload, is_canceled=is_canceled)

    return factory


def write_posteingang_note(notes_dir: str, uid: str, result: dict[str, Any]) -> str:
    """Land an import summary in the resident's Posteingang (the vault inbox).

    ADR 0007: imported items surface for triage on the one inbox rather than a
    bespoke list. The nightly Bibliothekar inbox (#697/#653) draws from the
    per-owner ``facts/`` dir on a ``YYYY-MM-DD-`` name prefix with no
    ``consolidated: true`` stamp — so a dated fact-note there shows up in the
    Posteingang. Returns the note's vault-relative path."""
    from datetime import datetime, timezone

    from solaris_chat import notes_search

    root = Path(notes_dir).resolve()
    base = root if uid == notes_search.SHARED_UID else root / "users" / uid
    facts_dir = base / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    per = result.get("per_category", {})
    lines = [f"- {k}: {v}" for k, v in per.items()]
    lines += [
        f"- {k}: {v} übersprungen" for k, v in result.get("skipped", {}).items() if v
    ]
    note = facts_dir / f"{day}-google-takeout-import-{result['hash']}.md"
    note.write_text(
        f"---\nadded_by: {uid}\ndate: {day}\nkind: import\nsource: google-takeout\n---\n\n"
        f"# Google-Takeout-Import\n\n"
        f"Importiert am {day}:\n\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return str(note.relative_to(root))
