"""On-boot OKF ingest trigger (#517).

The Phase-1 ingest adapters (Obsidian #448, Immich #206, CalDAV/CardDAV #207)
are merged but nothing ever *runs* them, so the OKF store stays empty on the
box. This runner is that missing trigger: it runs each adapter once, in the
background, after the engine boots — populating `notes/okf/`, the `.db`
projection and the embedding queue.

Degradation is deliberate (§ guardrails): the Obsidian adapter reads the local
vault and always runs; Immich and CalDAV/CardDAV run only when their source
config is present, and any adapter failure is logged and skipped rather than
crashing the trigger. No source credential is read from anything but the
process env (`config.Settings`).
"""

from __future__ import annotations

import asyncio

import aiohttp

from solaris_chat.config import Settings
from solaris_chat.logging import log

from ..knowledge import PendingEmbeddingQueue, embed_worker, projection
from ..knowledge.writer import OkfWriter
from ..ollama import OllamaChat
from .caldav import DavIngest
from .dav_client import HttpDavClient
from .exports import ExportsIngest
from .imap import ImapIngest
from .immich import ImmichIngest
from .immich_client import RestImmichClient
from .jellyfin import JellyfinMusicIngest, RestJellyfinMusicClient
from .obsidian import ObsidianIngest
from .obsidian_reader import VaultObsidianReader
from .paperless import push_uploads
from .paperless_client import RestPaperlessClient
from .prune import (
    prune_empty_note_shells,
    prune_legacy_music_artifacts,
    prune_legacy_photo_artifacts,
)
from .upload_extract import ingest_uploads

# Boot races pod startup: a source may be briefly unreachable (#531). Probe it a
# few times with backoff before running its adapter, but never block boot — cap
# the total wait so a down source is skipped cleanly, not waited on forever.
_HEALTH_BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0)  # seconds — cumulative cap ~30s.


async def run_ingest(settings: Settings) -> None:
    """Run every configured OKF ingest adapter once. Never raises."""
    writer = OkfWriter(
        db_path=settings.solaris_db_path,
        notes_dir=settings.notes_dir,
        embedding_queue=PendingEmbeddingQueue(settings.solaris_db_path),
    )
    uid = settings.default_uid

    # Extract upload text into the companion `.md` bodies BEFORE the obsidian pass
    # so this same cycle projects + indexes the newly written text (#…).
    ingest_uploads(settings.notes_dir)
    _run_obsidian(settings, writer, uid)
    _run_exports(settings, writer)
    await _run_immich(settings, writer, uid)
    await _run_caldav(settings, writer, uid)
    await _run_jellyfin(settings, writer, uid)
    await _run_imap(settings, writer)
    await _run_paperless(settings)

    # One-shot prune (#878, ADR 0002/B7): drop the pre-switch per-item OKF
    # markdown + concepts + okf_vectors rows so a legacy song/album/band/photo
    # matches a projection-only one (its .db projection + facts only). Both
    # idempotent — after one pass each finds nothing.
    prune_legacy_music_artifacts(settings.solaris_db_path, settings.notes_dir)
    prune_legacy_photo_artifacts(settings.solaris_db_path, settings.notes_dir)
    # Drop empty note/journal/preference shells the writer used to accept before
    # it grew the is_empty_note_shell guard (nightly chronicle templates, agent
    # "Internal log: …" title-only notes). Idempotent.
    prune_empty_note_shells(settings.solaris_db_path, settings.notes_dir)

    # Drain the embedding queue the adapters just filled into okf_vectors. Rides
    # this ingest thread (never the voice hot path — nomic-embed-text is a VRAM
    # slot); drain() never raises, but wrap it to match the per-adapter degrade.
    try:
        await embed_worker.drain(settings.solaris_db_path, settings.ollama_url)
    except Exception as e:  # noqa: BLE001 — the drain must not crash the trigger.
        log.error("engine.ingest.embed_drain_failed", error=str(e))


def _run_obsidian(settings: Settings, writer: OkfWriter, uid: str) -> None:
    """The vault adapter needs no external creds — always runs."""
    try:
        reader = VaultObsidianReader(settings.notes_dir)
        ingest = ObsidianIngest(
            reader, writer, db_path=settings.solaris_db_path, ingesting_uid=uid
        )
        stats = ingest.run()
        log.info(
            "engine.ingest.obsidian",
            notes=stats.notes,
            written=stats.written,
            skipped=stats.skipped,
        )
    except Exception as e:  # noqa: BLE001 — one adapter failing must not crash boot.
        log.error("engine.ingest.obsidian_failed", error=str(e))


def _run_exports(settings: Settings, writer: OkfWriter) -> None:
    """The drop-folder adapter scans the vault it already has — always runs.

    Ownership is per-file from the vault path (#576), so it takes no ingesting
    uid; a shared drop is `household`, a `users/<uid>/…` drop is that resident."""
    try:
        stats = ExportsIngest(
            writer,
            db_path=settings.solaris_db_path,
            notes_dir=settings.notes_dir,
        ).run()
        log.info(
            "engine.ingest.exports",
            files=stats.files,
            processed=stats.processed,
            events=stats.events_written,
            people=stats.people_written,
            skipped=stats.skipped,
            unrecognized=stats.unrecognized,
        )
    except Exception as e:  # noqa: BLE001 — one adapter failing must not crash boot.
        log.error("engine.ingest.exports_failed", error=str(e))


async def _wait_for_health(source: str, url: str) -> bool:
    """Probe `url` with bounded retry/backoff; True once it answers, else
    False after the cap. Any 2xx-4xx response means the server is up (an
    unauthenticated ping may 401/404 — that still proves reachability); only a
    connection-level failure counts as not-yet-ready. Never raises."""
    for i in range(len(_HEALTH_BACKOFF) + 1):
        try:
            timeout = aiohttp.ClientTimeout(total=5.0)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url) as resp,
            ):
                if resp.status < 500:
                    return True
        except (aiohttp.ClientError, TimeoutError):
            pass
        if i < len(_HEALTH_BACKOFF):
            log.info("engine.ingest.health_retry", source=source, attempt=i + 1)
            await asyncio.sleep(_HEALTH_BACKOFF[i])
    log.error("engine.ingest.health_unreachable", source=source)
    return False


async def _run_immich(settings: Settings, writer: OkfWriter, uid: str) -> None:
    if not (settings.immich_base_url and settings.immich_api_key):
        log.info("engine.ingest.immich_skipped", reason="unconfigured")
        return
    base = settings.immich_base_url.rstrip("/")
    if not await _wait_for_health("immich", f"{base}/api/server/ping"):
        return
    try:
        cursor = _load_cursor(settings, "immich")
        client = RestImmichClient(settings.immich_base_url, settings.immich_api_key)
        # Checkpoint the cursor mid-run so a disconnect after N pages still
        # advances the high-water mark and the next boot resumes (#597).
        stats = await ImmichIngest(client, writer, ingesting_uid=uid).run(
            updated_after=cursor,
            checkpoint=lambda c: _save_cursor(settings, "immich", c),
        )
        _save_cursor(settings, "immich", stats.cursor)
        log.info(
            "engine.ingest.immich",
            assets=stats.assets,
            events=stats.events_written,
            people=stats.people_written,
            places=stats.places_written,
            cursor=stats.cursor,
        )
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any source error.
        log.error("engine.ingest.immich_failed", error=str(e))


async def _run_caldav(settings: Settings, writer: OkfWriter, uid: str) -> None:
    if not (settings.caldav_url or settings.carddav_url):
        log.info("engine.ingest.caldav_skipped", reason="unconfigured")
        return
    probe = settings.caldav_url or settings.carddav_url
    if not await _wait_for_health("caldav", probe):
        return
    try:
        client = HttpDavClient(
            caldav_url=settings.caldav_url,
            caldav_username=settings.caldav_username,
            caldav_password=settings.caldav_password,
            carddav_url=settings.carddav_url,
            carddav_username=settings.carddav_username,
            carddav_password=settings.carddav_password,
        )
        stats = await DavIngest(client, writer, ingesting_uid=uid).run()
        log.info(
            "engine.ingest.caldav",
            contacts=stats.contacts,
            people=stats.people_written,
            events=stats.events,
            events_written=stats.events_written,
        )
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any source error.
        log.error("engine.ingest.caldav_failed", error=str(e))


async def _run_jellyfin(settings: Settings, writer: OkfWriter, uid: str) -> None:
    if not settings.jellyfin_url:
        log.info("engine.ingest.jellyfin_skipped", reason="unconfigured")
        return
    base = settings.jellyfin_url.rstrip("/")
    if not await _wait_for_health("jellyfin", f"{base}/System/Info/Public"):
        return
    try:
        client = RestJellyfinMusicClient(
            settings.jellyfin_url,
            settings.jellyfin_username,
            settings.jellyfin_password,
        )
        stats = await JellyfinMusicIngest(
            client,
            writer,
            ingesting_uid=uid,
            library_owners=settings.jellyfin_library_owners,
        ).run()
        log.info(
            "engine.ingest.jellyfin",
            items=stats.items,
            bands=stats.bands_written,
            songs=stats.songs_written,
            skipped=stats.skipped,
            cursor=stats.cursor,
        )
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any source error.
        log.error("engine.ingest.jellyfin_failed", error=str(e))


async def _run_imap(settings: Settings, writer: OkfWriter) -> None:
    if not settings.imap_accounts:
        log.info("engine.ingest.imap_skipped", reason="unconfigured")
        return
    ingest = ImapIngest(writer)
    for account in settings.imap_accounts:
        # One dead server must not block the other accounts — isolate each.
        # The IMAP login is the health probe (no HTTP ping); a per-account+folder
        # cursor key keeps the high-water marks independent.
        source = f"imap:{account.username}@{account.host}/{account.folder}"
        try:
            cursor = _load_cursor(settings, source)
            stats = await asyncio.to_thread(
                ingest.run_account,
                account,
                cursor,
                checkpoint=lambda c, s=source: _save_cursor(settings, s, c),
            )
            _save_cursor(settings, source, stats.cursor)
            log.info(
                "engine.ingest.imap",
                account=stats.account,
                seen=stats.seen,
                written=stats.written,
                skipped=stats.skipped,
                cursor=stats.cursor,
            )
        except Exception as e:  # noqa: BLE001 — degrade gracefully on any source error.
            # Log the account as user@host/folder only — never the password.
            log.error(
                "engine.ingest.imap_failed",
                account=f"{account.username}@{account.host}/{account.folder}",
                error=str(e),
            )


async def _run_paperless(settings: Settings) -> None:
    if not (settings.paperless_url and settings.paperless_token):
        log.info("engine.ingest.paperless_skipped", reason="unconfigured")
        return
    base = settings.paperless_url.rstrip("/")
    if not await _wait_for_health("paperless", f"{base}/api/"):
        return
    try:
        client = RestPaperlessClient(settings.paperless_url, settings.paperless_token)
        ollama = OllamaChat(settings.ollama_url)
        pushed = await push_uploads(settings.notes_dir, ollama, client)
        log.info("engine.ingest.paperless_run", pushed=pushed)
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any source error.
        log.error("engine.ingest.paperless_failed", error=str(e))


def _load_cursor(settings: Settings, source: str) -> str:
    conn = projection.open_conn(settings.solaris_db_path)
    try:
        return projection.get_cursor(conn, source)
    finally:
        conn.close()


def _save_cursor(settings: Settings, source: str, cursor: str) -> None:
    if not cursor:
        return
    conn = projection.open_conn(settings.solaris_db_path)
    try:
        projection.set_cursor(conn, source, cursor)
        conn.commit()
    finally:
        conn.close()
