"""Entrypoint: run the chat server on the Solaris Engine."""

from __future__ import annotations

import asyncio
import threading

from solaris_chat import notes_index
from solaris_chat.config import settings
from solaris_chat.context import build_context_window
from solaris_chat.engine.crons import CronRunner
from solaris_chat.engine.ha_watch import HaStateWatcher
from solaris_chat.engine.native_watch import NativeWatchStore
from solaris_chat.engine.importers.jobs import JobRunner
from solaris_chat.engine.ingest import run_ingest
from solaris_chat.engine.notify import EventBus, Notifier
from solaris_chat.engine.profiles import build_engine_clients
from solaris_chat.engine.approvals import ApprovalPoller
from solaris_chat.engine.sb_events import SbApprovalEventBridge
from solaris_chat.engine.scheduler import TimerScheduler
from solaris_chat.engine.updates import UpdatePoller
from solaris_chat.logging import log
from solaris_chat.server import serve


async def _run() -> None:
    context_window = await build_context_window(
        settings.ollama_url, settings.context_window_override
    )
    household, deep, admin, guest, librarian, recorder, bus = build_engine_clients(
        db_path=settings.solaris_db_path,
        ollama_url=settings.ollama_url,
        fast_model=settings.fast_model,
        thorough_model=settings.thorough_model,
        soul_path=settings.soul_path,
        admin_soul_path=settings.admin_soul_path,
        admin_skills_dir=settings.admin_skills_dir,
        skills_dir=settings.skills_dir,
        sb_mcp_url=settings.sb_mcp_url,
        sb_mcp_token_path=settings.sb_mcp_token_path,
        sb_api_url=settings.sb_api_url,
        hass_url=settings.hass_url,
        hass_token=settings.hass_token,
        tavily_api_key=settings.tavily_api_key,
        notes_dir=settings.notes_dir,
        gatekeeper_url=settings.gatekeeper_url,
        gatekeeper_token=settings.gatekeeper_token,
        context_window=context_window.value,
        default_uid=settings.default_uid,
        jellyfin_url=settings.jellyfin_url,
        jellyfin_cast_url=settings.jellyfin_cast_url,
        jellyfin_username=settings.jellyfin_username,
        jellyfin_password=settings.jellyfin_password,
    )
    notifier = Notifier(
        settings.solaris_db_path,
        settings.vapid_public_key,
        settings.vapid_private_key,
        settings.vapid_subject,
    )
    scheduler = TimerScheduler(
        settings.solaris_db_path,
        settings.hass_url,
        settings.hass_token,
        settings.alarm_sound_media_id,
        settings.alarm_sound_path,
        notifier=notifier,
    )
    scheduler.start()
    # Live status propagation (#714): the event bus fans HA state changes to
    # every open /p/start client via SSE; the HA-WS watcher feeds it, bounded to
    # the residents' pinned entities, and pushes only noteworthy transitions when
    # no client is watching that uid.
    event_bus = EventBus()
    # Per-device native watch-sets (#810): a native widget can watch an entity the
    # resident hasn't web-favorited; ha_watch unions these into its pinned owners.
    native_watch = NativeWatchStore()
    ha_watcher = HaStateWatcher(
        settings.hass_url,
        settings.hass_token,
        event_bus,
        settings.solaris_db_path,
        notifier=notifier,
        native_watch=native_watch,
    )
    ha_watcher.start()
    # ServiceBay update signal → Wartung update-cards (#788): poll the pending
    # image/template updates and card each NEW one into the shared Wartung admin
    # chat (dedup persisted, phone push via inject). Reads the non-expiring
    # read-only SB token (#818); dormant when SB_API_URL is unset.
    update_poller = UpdatePoller(
        settings.solaris_db_path,
        settings.sb_api_url,
        settings.sb_read_token_path,
        settings.sb_mcp_token_path,
        event_bus,
        settings.default_uid,
        notifier=notifier,
    )
    update_poller.start()
    # ServiceBay approval signal → Wartung approval-cards (#790): poll the generic
    # pending-approval feed and card each NEW request into the shared Wartung admin
    # chat (dedup persisted, phone push via inject). Reads the non-expiring
    # read-only SB token (#818); dormant when SB_API_URL is unset. The
    # [Approve]/[Deny] verdict handlers (server.py) run under the acting admin's
    # session-exchanged token.
    approval_poller = ApprovalPoller(
        settings.solaris_db_path,
        settings.sb_api_url,
        settings.sb_read_token_path,
        settings.sb_mcp_token_path,
        event_bus,
        settings.default_uid,
        notifier=notifier,
    )
    approval_poller.start()
    # ServiceBay approval-event bridge → Solaris event bus → app (BFF, #811): hold
    # SB's server-server SSE (/napi/approvals/events) open and republish each new
    # approval onto the event bus so it reaches the paired admin device over the
    # existing /napi/portal/events SSE — the app never subscribes to SB directly
    # (ADR 0010). Non-expiring read-only SB token (#818); dormant when SB_API_URL
    # is unset.
    sb_event_bridge = SbApprovalEventBridge(
        settings.sb_api_url,
        settings.sb_read_token_path,
        settings.sb_mcp_token_path,
        event_bus,
        settings.default_uid,
        notifier=notifier,
    )
    sb_event_bridge.start()
    crons = CronRunner(
        db_path=settings.solaris_db_path,
        deep=deep,
        skills_dir=settings.skills_dir,
        context_window=context_window.value,
        ingest_settings=settings,
        librarian=librarian,
    )
    crons.start()

    # Durable import jobs (#864): re-attach any import still `running` when the
    # server last died. This single engine process serves every owner, so resume
    # ALL running jobs — not just the household default — else a resident's
    # in-flight import (#941) zombies at `status='running'`. A job with no
    # registered runner is marked `interrupted` rather than lost.
    import_jobs = JobRunner(settings.solaris_db_path)
    import_jobs.resume()

    # Populate the OKF store on boot (#517). Run in a dedicated worker thread
    # with its own event loop, NOT as a task on the chat server's loop: the
    # ingest does synchronous sqlite writes + per-asset embedding work, and on
    # the main loop that grinds through the whole Immich/Jellyfin library while
    # blocking every request — /health times out for the entire run and the
    # chat becomes unreachable (#586). Daemon so it never delays shutdown;
    # run_ingest never raises, but guard the thread regardless.
    def _bg_ingest() -> None:
        try:
            asyncio.run(run_ingest(settings))
        except Exception as e:  # noqa: BLE001 — ingest must never crash the box.
            log.error("engine.ingest.thread_failed", error=str(e))
        # Full-vault FTS backfill (#830): index every note so notes_search covers
        # 100% of the ~99k-file vault (the ingest incrementally indexes its own
        # writes; this catches everything already on disk). Content-hash gated, so
        # a re-boot only touches changed notes. On the same off-loop thread.
        try:
            notes_index.backfill(settings.solaris_db_path, settings.notes_dir)
        except Exception as e:  # noqa: BLE001 — backfill must never crash the box.
            log.error("engine.notes_index.backfill_failed", error=str(e))

    threading.Thread(target=_bg_ingest, name="okf-ingest", daemon=True).start()
    await serve(
        settings.host,
        settings.port,
        engine=household,
        engine_admin=admin,
        engine_deep=deep,
        engine_guest=guest,
        remote_user_header=settings.remote_user_header,
        default_uid=settings.default_uid,
        remote_groups_header=settings.remote_groups_header,
        admin_group=settings.admin_group,
        skills_dir=settings.skills_dir,
        soul_path=settings.soul_path,
        logout_url=settings.logout_url,
        context_window=context_window,
        compaction_threshold=settings.compaction_threshold,
        attachments_dir=settings.attachments_dir,
        frame_ancestors=settings.frame_ancestors,
        fast_model=settings.fast_model,
        thorough_model=settings.thorough_model,
        tts_voices=settings.tts_voices,
        solaris_db_path=settings.solaris_db_path,
        notes_dir=settings.notes_dir,
        ollama_url=settings.ollama_url,
        trace_recorder=recorder,
        api_key=settings.api_key,
        bus=bus,
        event_bus=event_bus,
        notifier=notifier,
        sb_mcp_url=settings.sb_mcp_url,
        sb_mcp_token_path=settings.sb_mcp_token_path,
        sb_read_token_path=settings.sb_read_token_path,
        sb_api_url=settings.sb_api_url,
        sb_mint_url=settings.sb_mint_url,
        hass_url=settings.hass_url,
        hass_token=settings.hass_token,
        crons=crons,
        vapid_public_key=settings.vapid_public_key,
        android_package=settings.android_package,
        android_cert_fingerprints=settings.android_cert_fingerprints,
        ha_watcher=ha_watcher,
        native_watch=native_watch,
        import_jobs=import_jobs,
        caldav_url=settings.caldav_url,
        caldav_username=settings.caldav_username,
        caldav_password=settings.caldav_password,
        carddav_url=settings.carddav_url,
        carddav_username=settings.carddav_username,
        carddav_password=settings.carddav_password,
        music_dir=settings.music_dir,
        import_data_dir=settings.import_data_dir,
        immich_base_url=settings.immich_base_url,
        immich_api_key=settings.immich_api_key,
        paperless_ui_url=settings.paperless_ui_url,
    )


def main() -> None:
    log.info(
        "chat.boot",
        host=settings.host,
        port=settings.port,
        ollama=settings.ollama_url,
        engine="solaris",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
