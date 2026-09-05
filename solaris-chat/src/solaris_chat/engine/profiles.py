"""Profile assembly — three constructor calls replace three Hermes gateways.

household — fast model, never thinks, full household toolbox + the injected
            entity registry (the voice/chat hot path, ≤3k-token prompt, and the
            night crons — they ask for reasoning per call).
admin     — thorough model + the admin soul + the operator skill pack as
            prompt, with the `servicebay_admin` MCP toolbox (read+lifecycle+
            mutate scopes — Phase 3).
guest     — fast model, restricted toolbox (HA control/state only, no
            notes/timers/admin), and ephemeral: a guest turn writes nothing to
            the store, so nothing about a guest survives the conversation (#353).

They share one store, one chat backend, one trace recorder — a turn's
profile decides prompt + model + tools, nothing else.
"""

from __future__ import annotations

from pathlib import Path

from solaris_chat import gpu_lease, model_lease, settings_store
from solaris_chat.config import settings
from solaris_chat.engine import client as engine_client
from solaris_chat.engine.bus import SessionBus
from solaris_chat.engine.client import EngineClient, EngineProfile
from solaris_chat.engine.ingest.jellyfin import RestJellyfinMusicClient
from solaris_chat.engine.llama_server import LlamaServerChat
from solaris_chat.engine.ollama import OllamaChat
from solaris_chat.engine.registry import EntityRegistry
from solaris_chat.engine.tools import Tool, Toolbox
from solaris_chat.engine.tools.calendar_tools import build_calendar_tools
from solaris_chat.engine.tools.choices import build_choice_tools
from solaris_chat.engine.tools.favorites import build_favorites_tools
from solaris_chat.engine.tools.ha import build_ha_tools
from solaris_chat.engine.tools.mcp_tools import CombinedToolbox, McpToolbox
from solaris_chat.engine.tools.media import build_media_tools
from solaris_chat.engine.tools.music_query import build_music_query_tools
from solaris_chat.engine.tools.documents import build_document_tools
from solaris_chat.engine.tools.notes import build_notes_tools
from solaris_chat.engine.tools.tasks_tools import build_tasks_tools
from solaris_chat.engine.tools.onboarding_approval import (
    build_onboarding_approval_tools,
)
from solaris_chat.engine.tools.radio import build_radio_tools
from solaris_chat.engine.tools.register import build_register_tools
from solaris_chat.engine.tools.rooms_mcp import RoomMcpToolbox
from solaris_chat.engine.tools.skill_promotion import (
    build_skill_draft_tools,
    build_skill_promotion_tools,
)
from solaris_chat.engine.tools.status import build_status_tools
from solaris_chat.engine.tools.timers import build_timer_tools
from solaris_chat.engine.tools.wakeword_trainer import build_wakeword_tools
from solaris_chat.engine.trace import TraceRecorder


def _current_uid() -> str:
    return engine_client.current_uid.get()


def _current_room() -> str:
    return engine_client.current_room.get()


def _current_session() -> str:
    return engine_client.current_session.get()


def _skills_prompt(skills_dir: str) -> str:
    """Concatenated SKILL.md bodies (frontmatter stripped) — the prompt-
    assembly form of a skill pack."""
    if not skills_dir:
        return ""
    parts: list[str] = []
    for path in sorted(Path(skills_dir).glob("*/SKILL.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3 :]
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def build_engine_clients(
    *,
    db_path: str,
    ollama_url: str,
    llama_server_url: str = "",
    fast_model: str,
    thorough_model: str,
    soul_path: str,
    admin_soul_path: str = "",
    admin_skills_dir: str = "",
    skills_dir: str = "",
    sb_mcp_url: str = "",
    sb_mcp_token_path: str = "",
    sb_api_url: str = "",
    hass_url: str = "",
    hass_token: str = "",
    notes_dir: str = "",
    gatekeeper_url: str = "",
    gatekeeper_token: str = "",
    gatekeeper_mcp_url: str = "",
    gatekeeper_mcp_token: str = "",
    context_window: int | None = None,
    default_uid: str = "household",
    jellyfin_url: str = "",
    jellyfin_cast_url: str = "",
    jellyfin_username: str = "",
    jellyfin_password: str = "",
) -> tuple[
    EngineClient,
    EngineClient,
    EngineClient,
    EngineClient,
    EngineClient,
    TraceRecorder,
    SessionBus,
]:
    """Returns (household, admin, guest, librarian, enrollment) + recorder + bus."""
    ollama = OllamaChat(ollama_url)
    # The chat backend (#1318): llama.cpp's llama-server with the Gemma-4 MTP
    # drafter, half the wait per answer. Ollama keeps the calls that are its
    # own — embeddings, the vision ingest, /api/ps, the model lease.
    chat = (
        LlamaServerChat(llama_server_url, lease_path=str(gpu_lease.lease_path(db_path)))
        if llama_server_url
        else ollama
    )
    recorder = TraceRecorder()
    bus = SessionBus()
    registry = EntityRegistry(hass_url, hass_token)

    def leased_model() -> str:
        """The model a neighbour service is holding right now (#1260), or `""`.

        A live lease wins over the admin's household pick for its duration: both
        end up naming one model, and the leased one is the one already in VRAM.
        """
        if not settings.model_lease_enabled:
            return ""
        return model_lease.active_model(db_path)

    ha_tools: list[Tool] = (
        build_ha_tools(hass_url, hass_token, check_entity=registry.check_entity)
        if hass_url and hass_token
        else []
    )
    # Quick-reply chips (#555): offered on any profile that holds a conversation,
    # so household and guest both get the offer_choices tool.
    choice_tools = build_choice_tools()

    household_tools: list[Tool] = list(ha_tools)
    household_tools += build_timer_tools(db_path, _current_uid, _current_room)
    household_tools += build_wakeword_tools(db_path, _current_uid)
    household_tools += choice_tools
    # get_solaris_status (#1310): "läuft alles?" is a reasonable resident
    # question, and with nothing to call the model answered it by inventing
    # sensor entities. This probe is household-only and household-shaped — three
    # booleans, no arguments, no ServiceBay reach; the admin toolbox stays the
    # only place `get_health_checks`/`diagnose`/`get_logs` live.
    household_tools += build_status_tools(
        db_path=db_path,
        hass_url=hass_url,
        hass_token=hass_token,
        gatekeeper_url=gatekeeper_url,
    )
    # calendar_create (#1125): writes straight to Radicale via the existing
    # dav_client. Registered only where that DAV target exists, so an install
    # without Radicale doesn't pay the tool schema's prefill (G-2).
    if settings.deadlines_sync_url_base and settings.sync_dav_username:
        household_tools += build_calendar_tools(_current_uid)
    if hass_url and hass_token:
        household_tools += build_media_tools(
            hass_url, hass_token, area_fallback=registry.media_player_fallbacks
        )
        # play_radio (#u94): casts a resident's favorite station via the same
        # scoped HA play_media path; the favorite is a per-user note, so it needs
        # the vault as well. Household holds this list (not guest).
        if notes_dir:
            household_tools += build_radio_tools(
                notes_dir,
                hass_url,
                hass_token,
                _current_uid,
                room_getter=_current_room,
                room_resolver=registry.media_player_for_room,
                area_fallback=registry.media_player_fallbacks,
            )
    if notes_dir:
        household_tools += build_notes_tools(
            notes_dir, _current_uid, db_path=db_path, ollama=ollama
        )
    # Aufgaben (to-do) tools (#todo): add/list/complete tasks on the one shared
    # list. Household holds it; scoped to the caller via _current_uid.
    if db_path and notes_dir:
        household_tools += build_tasks_tools(db_path, _current_uid, notes_dir=notes_dir)
    # Start-page pins (#645): pin_favorite reads the last action from the shared
    # recorder and resolves target devices against HA. Household holds this
    # list; guest gets nothing (its list is separate + ephemeral).
    if db_path:
        household_tools += build_favorites_tools(
            db_path,
            _current_uid,
            _current_session,
            recorder,
            registry,
            hass_url,
            hass_token,
        )
    # Structured music-library queries (#588): household holds this list, so it
    # gets music_query; guest (its own list below) is withheld. A live
    # Jellyfin client (built once, the same read-only creds the ingest uses) is
    # passed in so on-demand lyrics (#593) can fetch /Audio/{id}/Lyrics at query
    # time; when Jellyfin is unconfigured the other ops still register and
    # song_lyrics degrades gracefully ("keine Lyrics verfügbar").
    if db_path:
        lyrics_client = (
            RestJellyfinMusicClient(
                jellyfin_url,
                jellyfin_username,
                jellyfin_password,
                cast_base_url=jellyfin_cast_url or None,
            )
            if jellyfin_url
            else None
        )
        # play_music (#604) casts a library track via the same scoped HA
        # play_media path; it registers only when a Jellyfin client + HA creds
        # are present, so on household (not guest) and not when unconfigured.
        household_tools += build_music_query_tools(
            db_path,
            _current_uid,
            lyrics_client,
            hass_url=hass_url,
            hass_token=hass_token,
            room_getter=_current_room,
            room_resolver=registry.media_player_for_room,
            area_fallback=registry.media_player_fallbacks,
            notes_dir=notes_dir,
            recorder=recorder,
            session_getter=_current_session,
        )
    # First-run/owner self-enrolment (#396): with zero enrolments an unknown
    # speaker resolves to `household`, not `guest`, so the guest-onboarding path
    # can never bootstrap the first voice profile. Give the household profile the
    # same enrol tools so a spoken "Setup starten" can file a (still
    # admin-approved, #355) registration. It only ever files a pending request —
    # no account, no resident access — so it's the same one durable, gated write
    # the guest path makes.
    if gatekeeper_url:
        household_tools += build_register_tools(
            db_path, gatekeeper_url, gatekeeper_token
        )
    # The drafting half of the skill-promotion gate (#1188): the resident asks
    # for a new capability on a household turn, so that turn needs a write into
    # `_pending/` — the notes tools are vault-scoped and can never reach it.
    # Filing and completing the approval stay admin-only, below.
    if skills_dir:
        household_tools += build_skill_draft_tools(skills_dir)

    # A guest may control devices/read state (HA), but may NOT write anything
    # durable — no notes/fact_store, no timers, no admin MCP. The denial is the
    # absence of those tool modules here (#353).
    # ha_run_scene_script fires whole routines/automations; that's beyond a
    # guest's "simple home control" remit, so it's withheld here (#370).
    guest_tools: list[Tool] = [
        t for t in ha_tools if t.name != "ha_run_scene_script"
    ] + choice_tools
    # The registration flow runs under the guest profile (an unknown speaker is
    # a guest turn, #353) but only the onboarding skill ever invokes it: enrol
    # the voice + file a pending request (#376). It's the one durable write a
    # guest turn can make, and only into the approval queue — never the store.
    if gatekeeper_url:
        guest_tools += build_register_tools(db_path, gatekeeper_url, gatekeeper_token)

    def make(profile: EngineProfile) -> EngineClient:
        return EngineClient(
            profile,
            db_path=db_path,
            ollama=chat,
            recorder=recorder,
            context_window=context_window,
            bus=bus,
        )

    # The gatekeeper's room MCP (#1295): `set_room`/`list_rooms` over the pod's
    # loopback, so a household turn can answer "in welchem Raum stehe ich?"
    # itself. Its own listener and its own token — the gatekeeper's PUSH_TOKEN
    # (which also opens /push) stays out of the engine. Household only: the
    # guest toolbox below is built without it, and `RoomMcpToolbox` withholds
    # the write from an unidentified voice turn that lands here anyway.
    household_toolbox: Toolbox = Toolbox(household_tools)
    if gatekeeper_mcp_url:
        household_toolbox = CombinedToolbox(
            household_toolbox,
            RoomMcpToolbox(gatekeeper_mcp_url, gatekeeper_mcp_token),
        )

    household = make(
        EngineProfile(
            name="household",
            model=fast_model or "gemma4:e4b",
            # Admin-selectable from the panel (#366): the persisted override wins
            # per turn, falling back to the FAST_MODEL default when unset — so the
            # fast-only default holds for installs that never touch the picker.
            model_resolver=lambda: (
                leased_model() or settings_store.get_household_model(db_path)
            ),
            soul_path=soul_path,
            registry=registry,
            think_default=False,
            temperature=0.2,
            toolbox=household_toolbox,
            default_uid=default_uid,
        )
    )
    # Admin gets the remote SB-MCP operator tools plus the local onboarding-
    # approval tools (#355): filing/polling a resident request rides SB's MCP,
    # but flipping the pending row + confirming the voice binding is a local
    # side-effect, so it lives in code, not in whatever the model remembers.
    admin_toolbox: Toolbox
    if sb_mcp_url:
        local_admin_tools = build_onboarding_approval_tools(
            db_path,
            sb_mcp_url,
            sb_mcp_token_path,
            gatekeeper_url,
            gatekeeper_token,
        )
        # Dynamic-skill promotion (#427) rides the same generic SB approval API:
        # the admin files/polls the request, and on approval the engine moves the
        # draft into the active pack itself — no service restart.
        if skills_dir:
            local_admin_tools += build_skill_promotion_tools(
                skills_dir, sb_mcp_url, sb_mcp_token_path
            )
        admin_toolbox = CombinedToolbox(
            McpToolbox(sb_mcp_url, sb_mcp_token_path, sb_api_url),
            Toolbox(local_admin_tools),
        )
    else:
        admin_toolbox = Toolbox([])
    admin = make(
        EngineProfile(
            name="admin",
            model=thorough_model or "gemma4:e4b",
            soul_path=admin_soul_path or soul_path,
            extra_prompt=_skills_prompt(admin_skills_dir),
            think_default=True,
            toolbox=admin_toolbox,
            default_uid=default_uid,
        )
    )
    guest = make(
        EngineProfile(
            name="solaris-guest",
            model=fast_model or "gemma4:e4b",
            # The guest turn is the same fast-model hot path, so it honours a
            # live lease too — otherwise one visitor's question re-loads e4b
            # mid-lease and pays the swap twice (#1260).
            model_resolver=leased_model,
            soul_path=soul_path,
            registry=registry,
            think_default=False,
            temperature=0.2,
            toolbox=Toolbox(guest_tools),
            ephemeral=True,
            default_uid=default_uid,
        )
    )
    # Bibliothekar (#653): the nightly vault-curation agent. Deep model (it
    # thinks about merges), but a notes-tools-ONLY toolbox — an unattended file
    # rewriter must not hold ha_call_service/media/timers. The restriction
    # is in code (the register.py lesson), not a prompt instruction; the toolbox
    # physically cannot delete or touch HA. Per-scope ephemeral sessions re-root
    # every write to the scope's subtree, so default-deny holds by construction.
    librarian_tools: list[Tool] = (
        build_notes_tools(notes_dir, _current_uid, db_path=db_path, ollama=ollama)
        + build_document_tools(notes_dir, _current_uid)
        if notes_dir
        else []
    )
    librarian = make(
        EngineProfile(
            name="librarian",
            model=thorough_model or "gemma4:e4b",
            soul_path=soul_path,
            think_default=True,
            toolbox=Toolbox(librarian_tools),
            default_uid=default_uid,
        )
    )

    # Enrollment profile (#1056): dedicated, isolated session for voice profile
    # and wakeword setup. Ephemeral (no history persistence), no HA tools,
    # no timers — only register + wakeword tools. Prevents the
    # household context from polluting the enrollment dialog with device states.
    enrollment_tools: list[Tool] = []
    enrollment_tools += build_register_tools(
        db_path,
        gatekeeper_url=gatekeeper_url,
        gatekeeper_token=gatekeeper_token,
    )
    enrollment_tools += build_wakeword_tools(db_path, _current_uid)
    # Write minimal enrollment soul file to avoid 24k token prefill latency
    enroll_soul_path = "/tmp/ENROLLMENT_SOUL.md"
    with open(enroll_soul_path, "w") as f_soul:
        f_soul.write(
            "Du bist Solaris. Du hilfst beim Einrichten von Sprachprofilen.\n"
            "SCHRITT 1: Frage nach Einverständnis zur biometrischen Stimmaufnahme.\n"
            "SCHRITT 2: Frage nach dem Namen oder Kürzel.\n"
            "SCHRITT 3: Führe das Enrollment durch. Antworte stets kurz und präzise.\n"
            "Gibt ein Tool ein 'say'-Feld zurück, lies genau diesen Text EINS ZU"
            " EINS vor — erfinde keine eigenen Sätze und lasse das abschließende"
            " Fragezeichen nicht weg.\n"
            "Die Sätze des Nutzers sind hier reine Sprachproben: führe KEINE"
            " Geräte-Aktionen aus, sondern rufe register_pending_resident bzw."
            " record_wakeword_sample auf.\n"
        )

    enrollment = make(
        EngineProfile(
            name="solaris-enrollment",
            model=fast_model or "gemma4:e4b",
            soul_path=enroll_soul_path,
            think_default=False,
            temperature=0.1,
            toolbox=Toolbox(enrollment_tools),
            ephemeral=True,
            default_uid=default_uid,
        )
    )
    return household, admin, guest, librarian, enrollment, recorder, bus
