"""Engine night jobs — the Hermes cron registry, reborn as code.

The three background jobs (daily-chronicle, problem-summarizer,
chat-compactor) used to be registered into Hermes' jobs.json by the
post-deploy, which de-duped badly across upgrades (#332 follow-up). Here
they are defined in code — idempotent by construction — and run on the
household profile, asking for `reasoning_effort="high"` per call.

Schedules are evaluated in local time (the household clock the prompts talk
about). A durable last-run stamp in solaris.db (`engine_cron_runs`) keys on
the fired slot, so a restart inside the cron minute never double-runs a job
and a restart spanning the slot fires it late instead of skipping the day.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from solaris_chat import compaction, notes_search
from solaris_chat.engine import (
    document_contacts_sync,
    document_deadlines_sync,
    music_affinity,
    store,
)
from solaris_chat.engine.ingest import run_ingest
from solaris_chat.engine.ingest.runner import _run_obsidian
from solaris_chat.engine.ingest.upload_extract import companion_images
from solaris_chat.engine.knowledge import (
    PendingEmbeddingQueue,
    embed_worker,
    projection,
)
from solaris_chat.engine.knowledge.writer import OkfWriter
from solaris_chat.logging import log

if TYPE_CHECKING:
    from solaris_chat.config import Settings
    from solaris_chat.engine.client import EngineClient

_LOCAL_TZ = ZoneInfo("Europe/Berlin")
_POLL_S = 30.0
_CRON_UID = "system"
# The shared-pool scope for the Bibliothekar: a session owned by this uid writes
# to the vault ROOT (`okf/`, `facts/`, `okf/log.md`), not a `users/<uid>/` subtree
# — it must match `notes_search.SHARED_UID` for the re-root logic to leave shared
# writes at the root. A resident scope is that resident's own uid.
_CRON_UID_SHARED = "household"

# A stale chat the nightly compactor picks up: untouched for a week and
# carrying enough transcript that compacting actually frees something.
_STALE_DAYS = 7
_STALE_MIN_USAGE = 0.5

# The nightly Stenograph (#652): how many active sessions it distils per night,
# and the transcript slice cap fed to one extraction turn (head-truncated).
_STENOGRAPH_WATERMARK = "stenograph-watermark"
_STENOGRAPH_MAX_SESSIONS = 20
_STENOGRAPH_SLICE_CHARS = 8000
_STENOGRAPH_MIN_TURNS = 2

# The nightly Bibliothekar (#653): the durable-fact/entity curation agent runs
# one deep turn per ownership scope over a BOUNDED candidate set (§3.3): the
# concept files touched since the last run plus fact files older than this many
# days that aren't yet consolidated, capped per scope. The watermark is a
# naive-UTC stamp — `concepts.updated` is `datetime('now')` (UTC), so comparing
# it against the local-ISO cron slot would silently drop rows.
_BIBLIOTHEKAR_WATERMARK = "bibliothekar-watermark"
_BIBLIOTHEKAR_MAX_PATHS = 40
_BIBLIOTHEKAR_STALE_DAYS = 3

# Journal-dedup pass (#709): how many same-day duplicate journal files the code
# pass consolidates per nightly run. Bounded so a vault that accumulated many
# stray variants heals over a few nights rather than in one long walk.
_JOURNAL_DEDUP_MAX = 50

BIBLIOTHEKAR_PROMPT = (
    "[system: nightly librarian run — unattended, no resident present]\n"
    "Du bist der Bibliothekar. Regeln (bindend):\n"
    "- NIEMALS Inhalte löschen. Nur umschreiben, zusammenführen, ergänzen.\n"
    "- Duplikate: Aliasse in die kanonische Datei (frontmatter aliases), das\n"
    '  Duplikat wird zum Stub mit "merged_into: <pfad>" und einem [[Link]].\n'
    "- Fakten-Dateien: Inhalt in die passende Personen-/Themen-Notiz (nach\n"
    '  #topic/@person) einarbeiten, dann "consolidated: true" ins Frontmatter\n'
    "  der Quelldatei schreiben - Datei behalten.\n"
    '- Veraltete einzeilige "description:"-Felder auffrischen.\n'
    "- Jede Änderung als Zeile an okf/log.md anhängen (append).\n"
    "- Bei unklaren/kaputten Dateien: überspringen, in log.md vermerken.\n"
    "Kandidaten dieser Nacht:\n"
)

# The nightly document extractor (#doc): reads each newly-OCR'd upload companion
# (carries the extracted marker but not yet the classified one) and writes a
# typed `document` note. Marker-gated idempotency — no watermark needed.
_DOC_EXTRACTED_MARKER = "<!-- extracted -->"
_DOC_CLASSIFIED_MARKER = "<!-- classified -->"

DOCUMENT_EXTRACTOR_PROMPT = (
    "[system: nightly document extractor — unattended, no resident present]\n"
    "Du extrahierst strukturierte Daten aus Dokumenten. Die SEITENBILDER des\n"
    "Dokuments sind angehängt — lies sie DIREKT (Tabellen/Layout/Zahlen erkennst\n"
    "du am Bild zuverlässiger als am OCR-Text). Für JEDE Kandidaten-Companion\n"
    "unten (eine Upload-Notiz mit '## Inhalt (extrahiert)'):\n"
    "1. Sieh dir die angehängten Seitenbilder an; der OCR-Text (per notes_read)\n"
    "   ist eine Stütze für exakte Nummern, kann aber Tabellen verstümmeln.\n"
    "2. SUCHE im Text aktiv nach diesen Angaben und übergib ALLE gefundenen als\n"
    "   Argumente von document_extract (ein Feld NUR weglassen, wenn es wirklich\n"
    "   nicht im Text steht — nichts erfinden):\n"
    "     provider (Anbieter/Firma), policy_number (Policen-/Vertrags-/Vers.-Nr.),\n"
    "     policyholder (Versicherungsnehmer/Inhaber), insurance_type (Art, z.B.\n"
    "     KFZ/Haftpflicht), premium_per_year (Beitrag/Jahr), amount (Betrag/Summe),\n"
    "     balance (Guthaben/Saldo/Kontostand — bei Konto-/Bausparauszügen der\n"
    "     aktuelle Stand, oft in einer Tabelle 'Ihr Bausparguthaben am …' oder\n"
    "     'Saldo/Kontostand'; lies den Betrag DIREKT aus dem Seitenbild der\n"
    "     Tabelle), contract_sum (Bauspar-/Vertragssumme), interest_rate (Zins),\n"
    "     coverage, start_date, end_date, renewal_date, cancellation_deadline,\n"
    "     due_date, expiry_date, hu_date, employer, salary, member_number,\n"
    "     provider_phone, provider_email, provider_address (Telefon/E-Mail/\n"
    "     Anschrift des Anbieters), contact_person (Name des Ansprechpartners/\n"
    "     Betreuers, z.B. aus 'Es betreut Sie: Dirk Mutert').\n"
    "   Datumsangaben IMMER als YYYY-MM-DD ('01.01.2026' → '2026-01-01').\n"
    "3. Dazu: source_document (exakter Pfad der Companion unten), category, title.\n"
    "   WÄHLE DIE KATEGORIE SORGFÄLTIG — NICHT alles ist eine Versicherung!\n"
    "   'insurance' NUR bei einer echten Versicherung (Police/Versicherungsschein,\n"
    "   Wort 'Versicherung', eine Policennummer, eine Deckung). Sonst:\n"
    "     contract = Vertrag (Mobilfunk, Internet/Glasfaser, Streaming, Fitness,\n"
    "       Handwerker-Auftrag); invoice = Rechnung/Zahlungsbeleg/Mahnung (ein\n"
    "       Rechnungsbetrag, 'Rechnung Nr.', fällig bis); utility = Strom/Gas/\n"
    "       Wasser/Abschlag (Stadtwerke, Energieanbieter); bank = Konto/Kredit/\n"
    "       Bausparen; tax = Steuer; vehicle = Fahrzeug/HU; employment = Arbeit;\n"
    "       pension = Rente; health_insurance = Krankenkasse; property = Wohnen/\n"
    "       Miete; warranty = Garantie; membership = Mitgliedschaft; legal = Recht;\n"
    "       id_document = Ausweis; family; appliance; other = wenn nichts passt.\n"
    "   Eine Kündigung/ein Tarifwechsel eines Vertrags ist 'contract', KEINE\n"
    "   Versicherung. Im Zweifel die konkretere Nicht-insurance-Kategorie.\n"
    "\n"
    "GUTE Beispiele (Kategorie passend zum Dokument, viele Felder gefüllt):\n"
    "  document_extract(source_document='users/mdopp/uploads/x.md',\n"
    "  category='insurance', title='ERGO KFZ', provider='ERGO Versicherung AG',\n"
    "  policy_number='KR 74536202-003500057', policyholder='Michael Dopp',\n"
    "  insurance_type='KFZ', start_date='2026-01-01', end_date='2027-01-01')\n"
    "  document_extract(source_document='users/mdopp/uploads/y.md',\n"
    "  category='contract', title='osnatel Glasfaser 500', provider='osnatel',\n"
    "  policy_number='Kundennr 12345', amount='39,95 Euro', start_date='2026-03-01')\n"
    "  document_extract(source_document='users/mdopp/uploads/z.md',\n"
    "  category='utility', title='Stadtwerke Strom', provider='Stadtwerke',\n"
    "  amount='89,00 Euro', due_date='2026-08-01')\n"
    "\n"
    "Das Tool schreibt die Notiz und markiert die Companion als erledigt — du musst\n"
    "NICHT selbst schreiben. Genau EIN document_extract-Aufruf pro Companion.\n"
    "Antworte nur mit einer kurzen Zusammenfassung.\n"
    "Kandidaten dieser Nacht:\n"
)

CHRONICLE_PROMPT = (
    "Write today's family chronicle / journal entry for today. "
    "This is the unattended daily run — no resident is present, so "
    "do not ask anyone for highlights; compile from the day's "
    "ingested notes and household events you can see, and write a "
    "short honest entry (or skip a section) rather than inventing. "
    "Write it with note_write to journal/<YYYY>/<YYYY-MM-DD>.md — one file "
    "per day; note_write canonicalizes and overwrites that day in place."
)

PROBLEM_SUMMARIZER_PROMPT = (
    "Update the troubleshooting knowledge base. This is the unattended "
    "weekly run — no admin is present, so do not ask anyone for input. "
    "Search recent notes and past diagnostic threads with notes_search, "
    "extract resolved problem→indicators→solution sequences, and merge "
    "them into knowledge-base/troubleshooting.md with note_write "
    "(append new problems, update existing ones in place). If nothing "
    "new surfaced, leave the file untouched rather than inventing."
)


@dataclass(frozen=True)
class CronJob:
    name: str
    minute: int
    hour: int
    weekday: int | None = None  # 0=Monday … 6=Sunday; None = daily
    prompt: str = ""  # empty => a code job (the compactor)
    skill: str = ""  # skill id whose SKILL.md body rides the prompt


JOBS = (
    CronJob(
        name="daily-chronicle",
        minute=59,
        hour=23,
        prompt=CHRONICLE_PROMPT,
        skill="daily-chronicle",
    ),
    CronJob(
        name="problem-summarizer",
        minute=30,
        hour=4,
        weekday=0,
        prompt=PROBLEM_SUMMARIZER_PROMPT,
        skill="problem-summarizer",
    ),
    CronJob(name="chat-compactor", minute=15, hour=4),
    CronJob(name="knowledge-night-run", minute=30, hour=2),
    CronJob(name="app-release-check", minute=20, hour=9),
)

# Code jobs (empty prompt → dispatched by name, not fed to an agent) can't live
# as prompt-bearing scheduler definitions; they stay defined here and are always
# present in the loaded registry.
_CODE_JOBS = (
    CronJob(name="chat-compactor", minute=15, hour=4),
    CronJob(name="knowledge-night-run", minute=30, hour=2),
    # Mid-morning on purpose (#1326): the companion-release check may run at any
    # hour, but its notice only goes out between 08:00 and 21:00, so a slot
    # inside that window announces a new app version the day it appears instead
    # of the morning after.
    CronJob(name="app-release-check", minute=20, hour=9),
)

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_schedule(spec: str) -> tuple[int, int, int | None] | None:
    """A scheduler def's `schedule:` — a 5-field cron `min hour dom mon dow`,
    restricted to the shapes the runner supports (a single minute + hour, an
    optional single weekday; `*` elsewhere). Returns `(minute, hour, weekday)`
    or None when it isn't a shape we can fire."""
    parts = spec.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, mon, dow = parts
    if dom != "*" or mon != "*":
        return None
    try:
        m, h = int(minute), int(hour)
    except ValueError:
        return None
    if not (0 <= m < 60 and 0 <= h < 24):
        return None
    weekday: int | None = None
    if dow != "*":
        weekday = _WEEKDAYS.get(dow.lower())
        if weekday is None:
            try:
                weekday = int(dow) % 7
            except ValueError:
                return None
    return m, h, weekday


def load_jobs(skills_dir: str) -> tuple[CronJob, ...]:
    """Build the cron registry from the scheduler-kind definitions in the pack
    (`schedule:` frontmatter + body-as-prompt), plus the code jobs.

    Falls back to the hardcoded `JOBS` when no scheduler-kind definition is
    present yet — the pack carries the `kind`/`schedule` frontmatter only after
    the #484 reorg, so cron keeps firing on the current pack meanwhile.
    """
    from solaris_chat import skills

    jobs: list[CronJob] = list(_CODE_JOBS)
    code_job_names = {j.name for j in _CODE_JOBS}
    for entry in skills.list_defs(skills_dir, "scheduler"):
        # The compactor is a scheduler-kind def for the editor/registry, but it
        # runs as the code job above — its body is not a prompt to feed an agent.
        if entry["id"] in code_job_names:
            continue
        one = skills.read_def(skills_dir, "scheduler", entry["id"])
        if one is None:
            continue
        meta, body = skills._split_frontmatter(one["raw"])
        parsed = _parse_schedule(meta.get("schedule", ""))
        if parsed is None or not body.strip():
            log.warning("engine.cron.skipped_invalid_scheduler", id=entry["id"])
            continue
        minute, hour, weekday = parsed
        jobs.append(
            CronJob(
                name=entry["id"],
                minute=minute,
                hour=hour,
                weekday=weekday,
                prompt=body.strip(),
                skill=entry["id"],
            )
        )
    has_scheduler_entry = any(j.prompt for j in jobs)
    return tuple(jobs) if has_scheduler_entry else JOBS


def _slot(job: CronJob, now: datetime) -> str | None:
    """The job's most recent due slot at/before `now` (ISO), or None when the
    job was never due in the lookback window."""
    candidate = now.replace(hour=job.hour, minute=job.minute, second=0, microsecond=0)
    for _ in range(8):  # at most a week + a day back (weekly jobs)
        if candidate <= now and (
            job.weekday is None or candidate.weekday() == job.weekday
        ):
            return candidate.isoformat()
        candidate -= timedelta(days=1)
    return None


def _last_run(db_path: str, name: str) -> str:
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            row = conn.execute(
                "SELECT last_run FROM engine_cron_runs WHERE name = ?", (name,)
            ).fetchone()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def _mark_run(db_path: str, name: str, slot: str) -> None:
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute(
            "INSERT INTO engine_cron_runs (name, last_run) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET last_run = excluded.last_run",
            (name, slot),
        )


def _path_in_scope(okf_path: str, scope: str) -> bool:
    """Whether a vault-relative path belongs to `scope` (#653).

    A path under `users/<uid>/` belongs to resident `<uid>`; anything else
    belongs to the shared household pool. Cross-scope files are excluded so a
    scope's turn only ever sees paths it can actually edit (private-vs-shared
    merges across scopes are forbidden by design)."""
    from solaris_chat.notes_search import resident_for_path

    owner = resident_for_path(okf_path)
    return owner == scope if scope != _CRON_UID_SHARED else owner is None


def _render_transcript(msgs: list[tuple[str, str]]) -> str:
    """Render `(role, content)` turns as `User:`/`Solaris:` lines, keeping the
    NEWEST within the slice cap (drop the oldest lines when it overflows)."""
    lines = [
        f"{'User' if role == 'user' else 'Solaris'}: {content}"
        for role, content in msgs
    ]
    while lines and sum(len(line) + 1 for line in lines) > _STENOGRAPH_SLICE_CHARS:
        lines.pop(0)
    return "\n".join(lines)


def _skill_body(skills_dir: str, skill_id: str) -> str:
    """The skill markdown that used to ride the Hermes cron's `skills` list."""
    path = Path(skills_dir) / skill_id / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    return text.strip()


class CronRunner:
    """Polls the job table against the wall clock; runs due jobs once."""

    def __init__(
        self,
        *,
        db_path: str,
        household: EngineClient,
        skills_dir: str,
        context_window: int,
        jobs: tuple[CronJob, ...] | None = None,
        ingest_settings: Settings | None = None,
        librarian: EngineClient | None = None,
        extractor_model: str = "gemma4:12b",
        release_watch: Any = None,
    ):
        self._db_path = db_path
        self._household = household
        self._skills_dir = skills_dir
        self._context_window = context_window
        self._jobs = jobs if jobs is not None else load_jobs(skills_dir)
        self._ingest_settings = ingest_settings
        self._librarian = librarian
        self._release_watch = release_watch
        # Document extraction runs on a stronger model than the bibliothekar's
        # default (the 8B librarian couldn't fill fields) — a per-turn override on
        # the same restricted librarian client (#doc).
        self._extractor_model = extractor_model
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 — the loop must outlive any hiccup
                log.error("engine.cron.error", error=str(e))
            await asyncio.sleep(_POLL_S)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(_LOCAL_TZ)
        for job in self._jobs:
            slot = _slot(job, now)
            if slot is None:
                continue
            last = _last_run(self._db_path, job.name)
            if not last:
                # First-ever boot: baseline on the current slot instead of
                # back-running last night's job mid-day on a fresh install.
                _mark_run(self._db_path, job.name, slot)
                continue
            if last >= slot:
                continue
            _mark_run(self._db_path, job.name, slot)
            log.info("engine.cron.fired", job=job.name, slot=slot)
            if job.prompt:
                await self._run_agent_job(job)
            elif job.name == "knowledge-night-run":
                await self._knowledge_night_run()
            elif job.name == "app-release-check":
                await self._app_release_check()
            else:
                await self._compact_stale()

    async def _run_agent_job(self, job: CronJob) -> None:
        """One unattended agent turn on the household profile, in an ephemeral
        session (the run's durable output is its tool effects, not the chat)."""
        prompt = job.prompt
        body = _skill_body(self._skills_dir, job.skill) if job.skill else ""
        if body:
            prompt = f"{body}\n\n---\n\n{prompt}"
        session_id = await self._household.create_session(_CRON_UID, ephemeral=True)
        try:
            reply = await self._household.chat(session_id, prompt, None, "high")
            log.info("engine.cron.done", job=job.name, reply_len=len(reply))
        finally:
            await self._household.delete_session(session_id, _CRON_UID)

    async def _app_release_check(self) -> None:
        """Refresh the companion-release cache and announce a new version once
        (#1326). Dormant when no watch was wired in."""
        if self._release_watch is None:
            return
        await self._release_watch.daily_check()

    async def _knowledge_night_run(self) -> None:
        """The nightly knowledge pipeline (#652): re-ingest every source, run
        the bibliothekar hook (#653), re-ingest the vault, then drain embeddings.

        The ingest steps do synchronous sqlite writes + per-asset embedding work;
        run them in a worker thread with its own loop, NOT on the chat server's
        loop, so `/health` never starves during the run (the #586 lesson, same as
        the boot ingest in `__main__._bg_ingest`). Each step is isolated so one
        failing source doesn't abort the rest of the pipeline."""
        if self._ingest_settings is None:
            log.info("engine.night.skipped", reason="no_ingest_settings")
            return
        settings = self._ingest_settings

        # The Stenograph runs LIVE engine turns, so it stays on the chat loop;
        # the sqlite-heavy ingest pipeline below moves to a worker thread.
        try:
            await self._stenograph()
        except Exception as e:  # noqa: BLE001 — one step must not kill the rest.
            log.error("engine.night.stenograph_failed", error=str(e))

        async def _ingest_step() -> None:
            try:
                await run_ingest(settings)
            except Exception as e:  # noqa: BLE001 — one step must not kill the rest.
                log.error("engine.night.ingest_failed", error=str(e))

        async def _reingest_step() -> None:
            try:
                writer = OkfWriter(
                    db_path=settings.solaris_db_path,
                    notes_dir=settings.notes_dir,
                    embedding_queue=PendingEmbeddingQueue(settings.solaris_db_path),
                )
                _run_obsidian(settings, writer, settings.default_uid)
            except Exception as e:  # noqa: BLE001
                log.error("engine.night.obsidian_failed", error=str(e))
            try:
                await embed_worker.drain(
                    settings.solaris_db_path, settings.llama_embed_url
                )
            except Exception as e:  # noqa: BLE001
                log.error("engine.night.embed_drain_failed", error=str(e))

        # Ingest fills the projection (bibliothekar reads `concepts.updated` from
        # it), so it runs first — in a worker thread (#586). The Bibliothekar then
        # runs LIVE librarian turns on the chat loop, editing vault files. Its
        # rewrites change what the Obsidian re-ingest reads, so it must land
        # BETWEEN ingest and re-ingest; the re-ingest picks up the merged/stubbed
        # files and re-embeds via content_hash → #650 drain.
        await self._run_in_worker(_ingest_step)
        # Bibliothekar first (it and the stenograph share the fast model), THEN
        # the document extractor. Both edit vault files the re-ingest below
        # projects.
        try:
            await self._bibliothekar()
        except Exception as e:  # noqa: BLE001 — one bad scope must not kill the run.
            log.error("engine.night.bibliothekar_failed", error=str(e))
        try:
            await self._document_extractor()
        except Exception as e:  # noqa: BLE001 — one bad scope must not kill the run.
            log.error("engine.night.document_extractor_failed", error=str(e))
        await self._run_in_worker(_reingest_step)
        # Providers + `.contacts` persons → phone book: now that the re-ingest has
        # (re)built the entities + their contact facts, project them into the
        # residents' Radicale address books (#doc-graph / #996). No-op when the
        # sync URL/creds are unset.
        try:
            await document_contacts_sync.sync_contacts(
                settings.solaris_db_path,
                settings.contacts_sync_url,
                settings.sync_dav_username,
                settings.sync_dav_password,
                settings.contacts_sync_url_base,
                settings.household_calendar_uid,
            )
        except Exception as e:  # noqa: BLE001 — one bad step must not kill the run.
            log.error("engine.night.contacts_sync_failed", error=str(e))
        # Document deadlines + dated tasks → each resident's PER-RESIDENT Solaris
        # calendar (#doc-graph / #997), where the calendar app raises the reminder
        # — the passive, non-push channel. No-op when the base URL/creds are unset.
        try:
            await document_deadlines_sync.sync_deadlines(
                settings.solaris_db_path,
                settings.deadlines_sync_url_base,
                settings.sync_dav_username,
                settings.sync_dav_password,
                settings.household_calendar_uid,
            )
        except Exception as e:  # noqa: BLE001 — one bad step must not kill the run.
            log.error("engine.night.deadlines_sync_failed", error=str(e))

    @staticmethod
    async def _run_in_worker(coro_factory) -> None:
        """Run a coroutine on its own loop in a daemon thread, so its synchronous
        sqlite + embedding work never starves the chat server's `/health` (#586).
        Awaits completion without blocking the chat loop."""
        done = threading.Event()

        def _worker() -> None:
            try:
                asyncio.run(coro_factory())
            except Exception as e:  # noqa: BLE001 — the run must never crash the box.
                log.error("engine.night.thread_failed", error=str(e))
            finally:
                done.set()

        thread = threading.Thread(
            target=_worker, name="knowledge-night-run", daemon=True
        )
        thread.start()
        while not done.is_set():
            await asyncio.sleep(1.0)

    def _bibliothekar_scopes(self, notes_dir: str) -> list[str]:
        """The ownership scopes to curate: the shared household pool plus every
        `users/<uid>/` subtree. Session owner == scope, so each turn can only
        touch its own subtree (default-deny, #576)."""
        scopes = [_CRON_UID_SHARED]
        users_dir = Path(notes_dir) / "users"
        if users_dir.is_dir():
            scopes += sorted(d.name for d in users_dir.iterdir() if d.is_dir())
        return scopes

    def _bibliothekar_candidates(
        self, notes_dir: str, scope: str, since: str
    ) -> list[str]:
        """Bounded candidate paths for one scope: concept files touched since the
        last run (filtered to the scope's subtree) plus stale, unconsolidated fact
        files. Capped at `_BIBLIOTHEKAR_MAX_PATHS` (the §3.3 bounded-input guard)."""
        root = Path(notes_dir)
        conn = projection.open_conn(self._db_path)
        try:
            changed = projection.concepts_changed_since(conn, since)
        finally:
            conn.close()
        in_scope = [p for p in changed if _path_in_scope(p, scope)]

        facts_dir = (
            root / "facts"
            if scope == _CRON_UID_SHARED
            else (root / "users" / scope / "facts")
        )
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_BIBLIOTHEKAR_STALE_DAYS)
        ).strftime("%Y-%m-%d")
        stale_facts: list[str] = []
        if facts_dir.is_dir():
            for path in sorted(facts_dir.glob("*.md")):
                # `fact_store` names files `YYYY-MM-DD-<slug>.md`; the date prefix
                # is the age. Older than the cutoff and not yet consolidated.
                if path.name[:10] >= cutoff:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "consolidated: true" in text:
                    continue
                stale_facts.append(str(path.relative_to(root)))

        candidates = list(dict.fromkeys(in_scope + stale_facts))
        return candidates[:_BIBLIOTHEKAR_MAX_PATHS]

    def _consolidate_journal_duplicates(self, notes_dir: str) -> int:
        """Merge same-day journal duplicates into one canonical file (#709).

        The daily-chronicle path is prompt-driven, so a day accumulated across
        `journal/<date>.md`, `journal/journal_<date>.md`, and
        `journal/<YYYY>/<date>.md`. Group every journal file by the date it is
        FOR, keep the canonical `journal/<YYYY>/<date>.md` carrying the
        newest/most-complete content, and rewrite the other same-day variants as
        `merged_into:` stubs (never delete, #653 contract) — each logged to
        `okf/log.md`. Uses the prune-bounded off-loop vault walk (#705), bounded
        at `_JOURNAL_DEDUP_MAX` per run. Returns the number of stubs written."""
        root = Path(notes_dir)
        by_date: dict[str, list[Path]] = {}
        for path in notes_search.iter_vault_md(root):
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                continue
            date = notes_search.journal_date(rel)
            if date is not None:
                by_date.setdefault(date, []).append(path)

        stubs = 0
        for date in sorted(by_date):
            if stubs >= _JOURNAL_DEDUP_MAX:
                break
            variants = by_date[date]
            if len(variants) < 2:
                continue
            canon_rel = notes_search.canonical_journal_path(f"journal/{date}.md")
            canon_path = (root / canon_rel).resolve()

            # Pick the surviving content: the longest body (most complete), the
            # newest mtime breaking a tie. A pure stub (already `merged_into:`)
            # never wins.
            def _weight(p: Path) -> tuple[int, float]:
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    mtime = p.stat().st_mtime
                except OSError:
                    return (-1, 0.0)
                if "merged_into:" in text:
                    return (-1, mtime)
                return (len(text), mtime)

            winner = max(variants, key=_weight)
            try:
                winner_text = winner.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            canon_path.parent.mkdir(parents=True, exist_ok=True)
            if winner.resolve() != canon_path:
                canon_path.write_text(winner_text, encoding="utf-8")

            for variant in variants:
                if stubs >= _JOURNAL_DEDUP_MAX:
                    break
                if variant.resolve() == canon_path:
                    continue
                try:
                    text = variant.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if "merged_into:" in text:
                    continue
                var_rel = str(variant.relative_to(root))
                variant.write_text(
                    f"---\nmerged_into: {canon_rel}\n---\n\n"
                    f"Journaleintrag zusammengeführt → [[{canon_rel}]]\n",
                    encoding="utf-8",
                )
                self._journal_log_append(
                    root, f"journal-dedup: {var_rel} → {canon_rel}"
                )
                stubs += 1
        return stubs

    @staticmethod
    def _journal_log_append(root: Path, line: str) -> None:
        """Append one journal-dedup line to `okf/log.md` (#709, never-delete trail)."""
        log_path = root / "okf" / "log.md"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"- {stamp} {line}\n")

    async def _bibliothekar(self) -> None:
        """The nightly vault curation (#653): one librarian turn per ownership
        scope over a bounded candidate set, editing vault files through the
        notes tools (concept §3.3 — files are truth, the projection heals on the
        following re-ingest). Never deletes: merges become alias+stub, stale facts
        get a `consolidated:` stamp, every change is logged to `okf/log.md`. The
        librarian toolbox has no HA/media/web tools, so the run cannot touch a
        device (the whole point of a fifth, restricted client)."""
        if self._librarian is None or self._ingest_settings is None:
            log.info("engine.night.bibliothekar_skipped", reason="no_librarian")
            return
        notes_dir = self._ingest_settings.notes_dir
        if not notes_dir:
            log.info("engine.night.bibliothekar_skipped", reason="no_notes_dir")
            return
        since = _last_run(self._db_path, _BIBLIOTHEKAR_WATERMARK)
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            stubs = await asyncio.to_thread(
                self._consolidate_journal_duplicates, notes_dir
            )
            if stubs:
                log.info("engine.night.journal_dedup", stubs=stubs)
        except Exception as e:  # noqa: BLE001 — a bad file must not abort the run.
            log.error("engine.night.journal_dedup_failed", error=str(e))
        # Music-wishlist enrichment (#859) returns in P2 (#873) as
        # owned_physical/wishlist/used_to_love facts on the album entity, with
        # the wishlist itself a query — not a written-back markdown note.
        for scope in self._bibliothekar_scopes(notes_dir):
            try:
                candidates = self._bibliothekar_candidates(notes_dir, scope, since)
                if not candidates:
                    continue
                prompt = (
                    BIBLIOTHEKAR_PROMPT
                    + "\n".join(candidates)
                    + "\n\nArbeite sie mit notes_read/note_write ab."
                    " Antworte nur mit einer Zusammenfassung."
                )
                session_id = await self._librarian.create_session(scope, ephemeral=True)
                try:
                    reply = await self._librarian.chat(session_id, prompt, None, "high")
                    log.info(
                        "engine.night.bibliothekar_scope",
                        scope=scope,
                        candidates=len(candidates),
                        reply_len=len(reply),
                    )
                finally:
                    await self._librarian.delete_session(session_id, scope)
            except Exception as e:  # noqa: BLE001 — one bad scope/file mustn't abort.
                log.error(
                    "engine.night.bibliothekar_scope_failed", scope=scope, error=str(e)
                )
        # Advance the watermark only after every scope, so a mid-run crash
        # re-curates the same candidates rather than dropping them.
        _mark_run(self._db_path, _BIBLIOTHEKAR_WATERMARK, now_utc)

    def _document_extractor_candidates(self, notes_dir: str, scope: str) -> list[str]:
        """Upload companions in this scope that were OCR'd (`<!-- extracted -->`)
        but not yet classified (`<!-- classified -->`). Marker-gated, bounded per
        run like the bibliothekar — no watermark, the classified marker is the
        idempotency."""
        root = Path(notes_dir)
        uploads_dir = (
            root / "uploads"
            if scope == _CRON_UID_SHARED
            else root / "users" / scope / "uploads"
        )
        out: list[str] = []
        if uploads_dir.is_dir():
            for path in sorted(uploads_dir.glob("*.md")):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if _DOC_EXTRACTED_MARKER in text and _DOC_CLASSIFIED_MARKER not in text:
                    out.append(str(path.relative_to(root)))
        return out[:_BIBLIOTHEKAR_MAX_PATHS]

    async def _document_extractor(self) -> None:
        """Nightly document extraction (#doc): ONE librarian turn PER companion
        (not per scope) — a small model drops documents when handed a batch, so
        each doc gets a focused turn on a stronger model (`extractor_model`). The
        turn classifies the document and calls `document_extract`, which writes
        the typed note + marks the companion done; the re-ingest projects it to a
        `document` entity + facts. Uses the restricted librarian client (no device
        tools) — right for sensitive financial/health documents."""
        if self._librarian is None or self._ingest_settings is None:
            log.info("engine.night.document_extractor_skipped", reason="no_librarian")
            return
        notes_dir = self._ingest_settings.notes_dir
        if not notes_dir:
            return
        work = [
            (scope, cand)
            for scope in self._bibliothekar_scopes(notes_dir)
            for cand in self._document_extractor_candidates(notes_dir, scope)
        ]
        if not work:
            return
        # Log BEFORE the first turn: it loads the (bigger) extractor model — a
        # multi-minute, disk-bound step that otherwise looks like a silent stall
        # between the ingest and the first extracted document.
        log.info(
            "engine.night.document_extractor_start",
            candidates=len(work),
            model=self._extractor_model,
        )
        extracted = 0
        for scope, candidate in work:
            try:
                prompt = DOCUMENT_EXTRACTOR_PROMPT + candidate
                # Attach the document's page images so the vision model reads
                # tables/layout directly (Tesseract garbles them); rasterizing is
                # blocking, so keep it off the chat loop.
                images = await asyncio.to_thread(companion_images, notes_dir, candidate)
                session_id = await self._librarian.create_session(scope, ephemeral=True)
                try:
                    reply = await self._librarian.chat(
                        session_id,
                        prompt,
                        images,
                        "high",
                        model_override=self._extractor_model,
                    )
                    extracted += 1
                    log.info(
                        "engine.night.document_extractor_doc",
                        scope=scope,
                        doc=candidate,
                        reply_len=len(reply),
                    )
                finally:
                    await self._librarian.delete_session(session_id, scope)
            except Exception as e:  # noqa: BLE001 — one bad doc mustn't abort.
                log.error(
                    "engine.night.document_extractor_doc_failed",
                    scope=scope,
                    doc=candidate,
                    error=str(e),
                )
        log.info(
            "engine.night.document_extractor_done",
            extracted=extracted,
            total=len(work),
        )

    async def curate_scope(self, notes_dir: str, scope: str) -> dict[str, object]:
        """Run one on-demand librarian turn for a single scope (#697).

        The Notizen-portal "Jetzt kuratieren" button reuses the nightly
        Bibliothekar machinery (candidates + prompt + per-scope ephemeral
        session) bounded to the one scope the caller may curate — no watermark
        advance, so the nightly run still sweeps the full candidate set. Returns
        `{ok, scope, candidates, summary}`; a missing librarian degrades to
        `ok: False` rather than raising into the request."""
        if self._librarian is None:
            return {"ok": False, "scope": scope, "error": "no_librarian"}
        since = _last_run(self._db_path, _BIBLIOTHEKAR_WATERMARK)
        candidates = self._bibliothekar_candidates(notes_dir, scope, since)
        if not candidates:
            return {"ok": True, "scope": scope, "candidates": 0, "summary": ""}
        prompt = (
            BIBLIOTHEKAR_PROMPT
            + "\n".join(candidates)
            + "\n\nArbeite sie mit notes_read/note_write ab."
            " Antworte nur mit einer Zusammenfassung."
        )
        session_id = await self._librarian.create_session(scope, ephemeral=True)
        try:
            reply = await self._librarian.chat(session_id, prompt, None, "high")
        finally:
            await self._librarian.delete_session(session_id, scope)
        log.info(
            "engine.night.bibliothekar_scope",
            scope=scope,
            candidates=len(candidates),
            reply_len=len(reply),
            on_demand=True,
        )
        return {
            "ok": True,
            "scope": scope,
            "candidates": len(candidates),
            "summary": reply,
        }

    async def _stenograph(self) -> None:
        """Distil each active session's day into durable facts (#652).

        Reusing compaction's extract pass on the LIVE session would append the
        extraction turn to that session's durable history and mirror it to open
        tabs — unacceptable on the active household chat. So each session's new
        turns are rendered into the prompt and run through one extraction turn in
        an EPHEMERAL session owned by the source session's owner; the household
        client sets `current_uid` from the session owner, so `fact_store` writes
        land under that resident's facts exactly as if they'd said "merk dir das".

        The watermark is a UTC timestamp: `engine_messages.created_at` /
        `last_activity` are sqlite UTC strings, while the cron slot stamps are
        local-ISO with an offset — comparing the two would silently drop rows.
        """
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        last = _last_run(self._db_path, _STENOGRAPH_WATERMARK)
        if not last:
            # First run: baseline to the last 24h rather than the whole history.
            last = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        try:
            with sqlite3.connect(self._db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, owner_uid FROM engine_sessions"
                    " WHERE ephemeral = 0 AND maintenance = 0 AND last_activity > ?"
                    " ORDER BY last_activity DESC LIMIT ?",
                    (last, _STENOGRAPH_MAX_SESSIONS),
                ).fetchall()
        except sqlite3.Error as e:
            log.error("engine.stenograph.query_failed", error=str(e))
            return

        skipped = 0
        for row in rows:
            slice_msgs = store.messages_since(self._db_path, row["id"], last)
            if len(slice_msgs) < _STENOGRAPH_MIN_TURNS:
                skipped += 1
                continue
            # Music affinity ("das war früher mein Lieblingsalbum X von Y") →
            # a `used_to_love` fact on the album entity (source=stenograph, #881,
            # B9). Deterministic (not the LLM turn below) so the queryable signal
            # lands reliably on the right album; the LLM turn still stores general
            # durable facts via fact_store.
            self._music_affinities(slice_msgs, row["owner_uid"])
            transcript = _render_transcript(slice_msgs)
            prompt = compaction.STENOGRAPH_PREFIX + transcript
            session_id = await self._household.create_session(
                row["owner_uid"], ephemeral=True
            )
            try:
                reply = await self._household.chat(session_id, prompt, None, "high")
                log.info(
                    "engine.stenograph.session",
                    source=row["id"],
                    owner=row["owner_uid"],
                    reply_len=len(reply),
                )
            finally:
                await self._household.delete_session(session_id, row["owner_uid"])

        log.info(
            "engine.stenograph.done", distilled=len(rows) - skipped, skipped=skipped
        )
        # Advance the watermark only after the whole loop completes, so a crash
        # mid-run re-distils the same day rather than dropping sessions.
        _mark_run(self._db_path, _STENOGRAPH_WATERMARK, now_utc)

    def _music_affinities(
        self, slice_msgs: list[tuple[str, str]], owner_uid: str
    ) -> None:
        """Route a day's chat-derived music affinities to `used_to_love` album
        facts (#881). No-op without ingest settings (no writer target) or when the
        slice carries no affinity. Never raises — one bad slice must not abort the
        Stenograph (mirrors the LLM-turn isolation above)."""
        if self._ingest_settings is None:
            return
        affinities = music_affinity.extract_affinities(slice_msgs)
        if not affinities:
            return
        try:
            writer = OkfWriter(
                db_path=self._ingest_settings.solaris_db_path,
                notes_dir=self._ingest_settings.notes_dir,
                embedding_queue=PendingEmbeddingQueue(
                    self._ingest_settings.solaris_db_path
                ),
            )
            written = music_affinity.route_affinities(
                writer,
                self._ingest_settings.solaris_db_path,
                owner_uid,
                affinities,
            )
            log.info(
                "engine.stenograph.music_affinity",
                owner=owner_uid,
                affinities=len(affinities),
                written=written,
            )
        except Exception as e:  # noqa: BLE001 — one slice must not kill the run.
            log.error("engine.stenograph.music_affinity_failed", error=str(e))

    async def _compact_stale(self) -> None:
        """The nightly chat-compactor: extract-then-compact stale, long chats
        via the same compaction path the per-turn hard cap uses (force=True —
        staleness, not cap pressure, selected them)."""
        cutoff = (datetime.now(_LOCAL_TZ) - timedelta(days=_STALE_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            with sqlite3.connect(self._db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, owner_uid, input_tokens, output_tokens"
                    " FROM engine_sessions"
                    " WHERE ephemeral = 0 AND last_activity < ?",
                    (cutoff,),
                ).fetchall()
        except sqlite3.Error as e:
            log.error("engine.cron.compact_query_failed", error=str(e))
            return
        for row in rows:
            # The durable household session (#345) is never forked into a
            # `Fortsetzung` continuation — that would surface as a second
            # "Zuhause" row (#419). Skip it; it stays in-place.
            if row["id"] == store.household_session_id(row["owner_uid"]):
                continue
            usage = compaction.usage_fraction(dict(row), self._context_window)
            if usage is None or usage < _STALE_MIN_USAGE:
                continue
            new_id = await compaction.compact_session(
                self._household,
                row["owner_uid"],
                row["id"],
                context_window=self._context_window,
                force=True,
            )
            if new_id:
                # The continuation replaces the stale chat going forward; the
                # original transcript stays (never deleted), same as the
                # per-turn path.
                log.info(
                    "engine.cron.compacted",
                    session_id=row["id"],
                    continuation_id=new_id,
                )
