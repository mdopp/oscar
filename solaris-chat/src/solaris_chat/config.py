"""Env-driven configuration for the Solaris Engine chat server.

One process owns the agent loop, the chat surface, the Ollama facade for
HA Assist, the timer scheduler and the night crons. It maps the Authelia
trusted-proxy identity header to a resident uid and holds the API key
server-side.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from solaris_chat import context


@dataclass(frozen=True)
class ImapAccount:
    """One curated IMAP mailbox the email-ingest adapter reads (#654).

    Numbered flat env: `IMAP_<n>_HOST/PORT/USERNAME/PASSWORD/FOLDER/RESIDENT`.
    The folder IS the filter (read-only that folder only); each account maps to
    exactly one resident so its mail is per-person scoped by construction.
    """

    host: str
    port: int
    username: str
    password: str
    folder: str
    resident_uid: str


def _parse_imap_accounts(environ: dict[str, str]) -> tuple[ImapAccount, ...]:
    """Scan `IMAP_<n>_*` (n=1..) while `IMAP_<n>_HOST` is set.

    An account missing USERNAME/PASSWORD/RESIDENT is skipped (the caller logs
    it) — we never build a half-configured account. PORT defaults to 993 (SSL),
    FOLDER to `Solaris`. Passwords live only here + the process env, never a log.
    """
    accounts: list[ImapAccount] = []
    n = 1
    while host := environ.get(f"IMAP_{n}_HOST", "").strip():
        username = environ.get(f"IMAP_{n}_USERNAME", "").strip()
        password = environ.get(f"IMAP_{n}_PASSWORD", "")
        resident = environ.get(f"IMAP_{n}_RESIDENT", "").strip()
        if username and password and resident:
            accounts.append(
                ImapAccount(
                    host=host,
                    port=int(environ.get(f"IMAP_{n}_PORT", "993")),
                    username=username,
                    password=password,
                    folder=environ.get(f"IMAP_{n}_FOLDER", "Solaris").strip()
                    or "Solaris",
                    resident_uid=resident,
                )
            )
        n += 1
    return tuple(accounts)


def _parse_cert_fingerprints(raw: str) -> tuple[str, ...]:
    """Parse comma-separated SHA256 cert fingerprints, stripped; empty ⇒ ()."""
    return tuple(fp.strip() for fp in raw.split(",") if fp.strip())


def _parse_library_owners(raw: str) -> dict[str, str]:
    """Parse `Name=uid;Name2=uid2` into a {library_name: owner_uid} map.

    Blank/malformed entries are skipped. A library NAME may contain `()`/spaces
    (e.g. `Music (cdopp)`); only the first `=` splits name from uid."""
    owners: dict[str, str] = {}
    for entry in raw.split(";"):
        name, sep, uid = entry.partition("=")
        if sep and name.strip() and uid.strip():
            owners[name.strip()] = uid.strip()
    return owners


def _load_vapid_private_key(private_key: str):
    """Load a VAPID private key in any accepted format to a cryptography object.

    Accepts three formats: PEM (-----BEGIN EC PRIVATE KEY-----), raw 32-byte
    base64url scalar, and DER base64url — the last two via py_vapid. The single
    place that turns the operator's `VAPID_PRIVATE_KEY` into an EC private-key
    object, shared by the public-key derive (below) and the notifier's raw-scalar
    conversion (engine/notify.py). Raises on a malformed key; each caller decides
    how to degrade."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01

    if private_key.strip().startswith("-----"):
        return serialization.load_pem_private_key(
            private_key.encode(), password=None, backend=default_backend()
        )
    return Vapid01.from_string(private_key).private_key


def _derive_vapid_public_key(private_key: str) -> str:
    """Derive the base64url VAPID public key from the private key (#801).

    VAPID_PUBLIC_KEY is a non-secret text var that a ServiceBay install does not
    preserve across a redeploy unless passed explicitly, so it silently empties
    and Web Push breaks. It's fully derivable from VAPID_PRIVATE_KEY (the EC
    P-256 private key): the uncompressed public point, base64url, no padding —
    the same encoding `web-push generate-vapid-keys` emits. Returns "" if the key
    can't load, so a malformed private key just disables push rather than crashing
    boot."""
    import base64

    from cryptography.hazmat.primitives import serialization

    try:
        public_key = _load_vapid_private_key(private_key).public_key()
    except Exception:  # noqa: BLE001 — a bad key disables push, never breaks boot
        return ""
    point = public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(point).rstrip(b"=").decode()


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    api_key: str
    remote_user_header: str
    remote_groups_header: str
    admin_group: str
    default_uid: str
    skills_dir: str
    soul_path: str
    logout_url: str
    context_window_override: int | None
    ollama_url: str
    compaction_threshold: float
    attachments_dir: str
    frame_ancestors: str
    fast_model: str
    thorough_model: str
    tts_voices: str
    solaris_db_path: str
    notes_dir: str
    hass_url: str
    hass_token: str
    alarm_sound_media_id: str
    alarm_sound_path: str
    tavily_api_key: str
    admin_soul_path: str
    admin_skills_dir: str
    sb_mcp_url: str
    sb_mcp_token_path: str
    sb_read_token_path: str
    sb_api_url: str
    sb_mint_url: str
    gatekeeper_url: str
    gatekeeper_token: str
    immich_base_url: str
    immich_api_key: str
    # The household paperless-ngx instance the document push adapter (#931) feeds
    # (loopback REST API + token). For each uploaded document Solaris POSTs the
    # file OCR-skipped, then PATCHes `content` with the gemma4:12b vision text so
    # paperless indexes clean text for full-text search. Empty url ⇒ push disabled.
    paperless_url: str
    paperless_token: str
    # The PUBLIC paperless Web-UI URL residents click through to for full-text
    # search + corrections (the Dokumente portal is read-only, #1043). This is
    # the operator's `https://paperless.<domain>` host behind Authelia — NOT
    # `paperless_url`, which is the loopback REST endpoint the ingest adapter
    # PATCHes. Empty ⇒ the portal hides the outbound link (no dead link).
    paperless_ui_url: str
    caldav_url: str
    caldav_username: str
    caldav_password: str
    carddav_url: str
    carddav_username: str
    carddav_password: str
    # The dedicated `solaris` DAV account (its own LLDAP identity) + its two
    # collection URLs — the WRITE targets that sync provider contacts and document
    # deadlines into the phone book (#doc-graph). Authenticated CardDAV/CalDAV PUT,
    # NOT a filesystem mount: Radicale's owner_only scopes this account to only its
    # own collections. Empty URL ⇒ that sync is disabled.
    sync_dav_username: str
    sync_dav_password: str
    contacts_sync_url: str
    deadlines_sync_url: str
    # DAV base for the PER-RESIDENT calendar sync (#997): the deadlines/tasks sync
    # writes `{deadlines_sync_url_base}/{resident_uid}/{calendar}/` — the same URL
    # shape the Takeout calendar importer uses. Empty ⇒ per-resident sync disabled.
    deadlines_sync_url_base: str
    # The resident whose calendar receives HOUSEHOLD-wide dated items (shared
    # document deadlines + household tasks), which have no principal of their own
    # (#997/#1011). "household" isn't a real Radicale principal, so household
    # items are routed to this resident's own `/uid/solaris/` calendar instead.
    # Empty ⇒ keep the `household` uid (only valid where a household principal
    # exists); set it to the primary resident's uid to land them there.
    household_calendar_uid: str
    # Where the interactive Takeout import (#869) reads library ownership from
    # (`music_dir`) and stores its scratch state / stored archives (`import_data_dir`,
    # also the ytmusicapi album cache). Defaults match the stack's data mounts.
    music_dir: str
    import_data_dir: str
    jellyfin_url: str
    jellyfin_cast_url: str
    jellyfin_username: str
    jellyfin_password: str
    jellyfin_library_owners: dict[str, str]
    imap_accounts: tuple[ImapAccount, ...]
    vapid_public_key: str
    vapid_private_key: str
    vapid_subject: str
    android_package: str
    android_cert_fingerprints: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Settings":
        # Web Push / VAPID (#713, #801): a redeploy can empty the non-secret
        # VAPID_PUBLIC_KEY env, so derive it from the private key when unset
        # rather than depend on the env surviving. An explicit public key wins.
        vapid_private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
        vapid_public_key = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
        if not vapid_public_key and vapid_private_key:
            vapid_public_key = _derive_vapid_public_key(vapid_private_key)
        return cls(
            host=os.environ.get("CHAT_HOST", "127.0.0.1"),
            port=int(os.environ.get("CHAT_PORT", "8787")),
            # Server-side bearer: the HA conversation agent and the
            # voice-gatekeeper present it on the Ollama facade. Empty leaves
            # the facade open — acceptable only on the loopback-only bind.
            api_key=os.environ.get("SOLARIS_API_KEY", ""),
            # Authelia forwards the authenticated identity on this header
            # via the trusted reverse proxy. We never trust it from an
            # untrusted source: the pod binds loopback and only NPM
            # (which sets the header after Authelia) can reach it.
            remote_user_header=os.environ.get("REMOTE_USER_HEADER", "Remote-User"),
            # Authelia also forwards the user's groups (comma-separated) on
            # this header. Panel writes (skills/soul/model) gate on
            # membership of `admin_group`; same trusted-proxy trust as above.
            remote_groups_header=os.environ.get(
                "REMOTE_GROUPS_HEADER", "Remote-Groups"
            ),
            admin_group=os.environ.get("ADMIN_GROUP", "admins"),
            # Fallback uid when the header is absent (e.g. offline test
            # access straight to the loopback port, no Authelia in front).
            default_uid=os.environ.get("DEFAULT_UID", "household"),
            # The Solaris skill pack (host solarisbay/skills) — the panel renders
            # and edits it, and the engine reads cron-job skill bodies from it.
            skills_dir=os.environ.get("SKILLS_DIR", "/data/skills"),
            soul_path=os.environ.get("SOUL_PATH", "/var/lib/solaris/SOUL.md"),
            # Optional Authelia logout URL for the sidebar footer. Empty ⇒ the
            # panel hides the logout link (avoids a dead link when unset).
            logout_url=os.environ.get("LOGOUT_URL", ""),
            # Context window (tokens): empty/"auto" => derive from the live
            # Ollama active model at runtime (#235), so the compaction cap always
            # matches what the model is actually loaded with and adapts per
            # model. A positive integer here is an explicit operator OVERRIDE
            # that wins over the derived value (ops control).
            context_window_override=context.parse_override(
                os.environ.get("CONTEXT_WINDOW")
            ),
            # Where Ollama's API lives (host loopback — the chat pod is
            # hostNetwork). The engine's only LLM backend.
            ollama_url=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
            # Fraction of the context window at which a chat is auto-compacted
            # (#210): extract durable learnings to memory, then continue in a
            # fresh small-context session. ~0.90 leaves headroom so a turn never
            # truncates while the two compaction turns run.
            compaction_threshold=float(os.environ.get("COMPACTION_THRESHOLD", "0.90")),
            # Host-mounted dir where the proxy persists image attachments (#202).
            attachments_dir=os.environ.get("ATTACHMENTS_DIR", "/data/attachments"),
            # CSP `frame-ancestors` source list — who may iframe the chat
            # (#228). Default `'self'`; the ServiceBay maintenance embed sets
            # `'self' https://admin.dopp.cloud` so admin.dopp.cloud can frame it.
            frame_ancestors=os.environ.get("FRAME_ANCESTORS", "'self'"),
            # The engine model map: one model (e4b) for everything on this 16GB
            # GPU (12b retired 2026-07-13 — does not fit shared with e4b/Whisper/
            # Kokoro/embed). "Gründlich" is e4b WITH reasoning; the admin persona
            # and night crons keep think_default on the same e4b.
            fast_model=os.environ.get("FAST_MODEL", "gemma4:e4b").strip(),
            thorough_model=os.environ.get("THOROUGH_MODEL", "gemma4:e4b").strip(),
            # The Kokoro voices the global voice picker offers (#368),
            # comma-separated. The first is the default; the box's solaris-tts
            # image bakes "martin", so that stays the single default voice.
            tts_voices=os.environ.get("TTS_VOICES", "martin").strip(),
            # solaris.db (bind-mounted into the pod) holds the engine sessions,
            # timers, cron stamps, topics and traces. Same path the gatekeeper
            # and schema-init sidecar use.
            solaris_db_path=os.environ.get(
                "SOLARIS_DB_PATH", "/var/lib/solaris/solaris.db"
            ),
            # The Obsidian notes vault (Syncthing-synced) — the engine's notes
            # tools and the topic dashboard read/write here.
            notes_dir=os.environ.get("NOTES_DIR", "/opt/data/notes"),
            # The Solaris Engine's direct Home Assistant access: device control
            # tools + the prompt-injected entity registry + timer announce.
            hass_url=os.environ.get("HASS_URL", "").strip(),
            hass_token=os.environ.get("HASS_TOKEN", "").strip(),
            # An alarm (kind=alarm) rings a sound instead of speaking. The
            # media_id rides assist_satellite.announce — HA resolves the
            # media-source URI server-side, then the Voice PE plays it. The
            # path is the chat-visible copy of that same file: the scheduler
            # checks it exists before choosing the sound, and falls back to the
            # TTS sentence if it's missing/unreadable (HA can't tell us up
            # front whether it'll play). Operators override the media_id with
            # their own file dropped into HA's media folder.
            alarm_sound_media_id=os.environ.get(
                "ALARM_SOUND_MEDIA_ID",
                "media-source://media_source/local/solaris-alarm.ogg",
            ).strip(),
            alarm_sound_path=os.environ.get(
                "ALARM_SOUND_PATH", "/data/skills/media/solaris-alarm.ogg"
            ).strip(),
            # Web search backend. Empty => the keyless ddgs backend.
            tavily_api_key=os.environ.get("TAVILY_API_KEY", "").strip(),
            # The operator persona's soul for the admin profile; falls back to
            # the household soul when unset.
            admin_soul_path=os.environ.get("ADMIN_SOUL_PATH", "").strip(),
            # The operator skill pack, folded into the admin profile's prompt.
            admin_skills_dir=os.environ.get("ADMIN_SKILLS_DIR", "").strip(),
            # The servicebay_admin MCP endpoint + the token file the
            # post-deploy mints (read+lifecycle+mutate; no destroy/exec).
            sb_mcp_url=os.environ.get("SB_MCP_URL", "").strip(),
            sb_mcp_token_path=os.environ.get(
                "SB_MCP_TOKEN_PATH", "/var/lib/solaris/sb-admin-token"
            ),
            # The non-expiring read-only SB token (servicebay#2302) the
            # unattended pollers use so they don't 401-churn when the rotating
            # deploy-time SB_MCP_TOKEN_PATH lapses (~1h TTL, #818). The
            # post-deploy mints it once and drops it here; the pollers fall back
            # to SB_MCP_TOKEN_PATH when this file is absent (pre-deploy).
            sb_read_token_path=os.environ.get(
                "SB_READ_TOKEN_PATH", "/var/lib/solaris/sb-read-token"
            ),
            # ServiceBay control-plane base for the Authelia-session token
            # exchange (#794): the admin toolbox mints its SB-MCP token from the
            # acting admin's live forward-auth identity via
            # token-from-authelia-session — no standing minting credential in
            # the pod. Empty ⇒ no runtime exchange (a token rotation then needs
            # a redeploy of the deploy-time token file).
            sb_api_url=os.environ.get("SB_API_URL", "").strip(),
            # ServiceBay's public portal base (through NPM) for the session-mint
            # routes (servicebay#2278/#2285). The BFF forwards the acting admin's
            # authelia_session cookie to the delegated-admin mint
            # (`delegated-admin-from-authelia-session`); NPM's forward-auth
            # validates the cookie and injects the CSRF-exempt
            # `X-SB-Internal-Token`. Must be a *.dopp.cloud host (www, not the
            # apex — the apex is Authelia default-deny; loopback has neither the
            # token nor Authelia). Empty ⇒ fall back to SB_API_URL (the loopback
            # path — correct for a LAN/no-portal deploy).
            sb_mint_url=os.environ.get("SB_MINT_URL", "").strip(),
            # The gatekeeper's in-pod HTTP listener (push + /enrol), reached
            # over loopback like the other pod-internal callers. The
            # onboarding dialog (#354) uses /enrol to register a resident's
            # voice profile; the token is the gatekeeper's PUSH_TOKEN (empty
            # is unauthenticated, the loopback default).
            gatekeeper_url=os.environ.get(
                "GATEKEEPER_URL", "http://127.0.0.1:10750"
            ).strip(),
            gatekeeper_token=os.environ.get("PUSH_TOKEN", "").strip(),
            # The household Immich instance the photo-ingest adapter reads
            # (read-only) to map assets/faces/EXIF-geo into OKF
            # events/people/places (#206). Empty ⇒ ingest disabled.
            immich_base_url=os.environ.get("IMMICH_BASE_URL", "").strip(),
            immich_api_key=os.environ.get("IMMICH_API_KEY", "").strip(),
            paperless_url=os.environ.get("PAPERLESS_URL", "").strip(),
            paperless_token=os.environ.get("PAPERLESS_TOKEN", "").strip(),
            paperless_ui_url=os.environ.get("PAPERLESS_UI_URL", "").strip(),
            # The household CalDAV calendar + CardDAV address book the
            # calendar/contacts-ingest adapter reads (read-only, #207) to map
            # events/contacts into OKF event/person concepts. An empty url
            # disables that half of the adapter.
            caldav_url=os.environ.get("CALDAV_URL", "").strip(),
            caldav_username=os.environ.get("CALDAV_USERNAME", "").strip(),
            caldav_password=os.environ.get("CALDAV_PASSWORD", "").strip(),
            carddav_url=os.environ.get("CARDDAV_URL", "").strip(),
            carddav_username=os.environ.get("CARDDAV_USERNAME", "").strip(),
            carddav_password=os.environ.get("CARDDAV_PASSWORD", "").strip(),
            sync_dav_username=os.environ.get("SYNC_DAV_USERNAME", "").strip(),
            sync_dav_password=os.environ.get("SYNC_DAV_PASSWORD", "").strip(),
            contacts_sync_url=os.environ.get("CONTACTS_SYNC_URL", "").strip(),
            deadlines_sync_url=os.environ.get("DEADLINES_SYNC_URL", "").strip(),
            deadlines_sync_url_base=os.environ.get(
                "DEADLINES_SYNC_URL_BASE", ""
            ).strip(),
            household_calendar_uid=os.environ.get("HOUSEHOLD_CALENDAR_UID", "").strip(),
            music_dir=os.environ.get("MUSIC_DIR", "/opt/data/music").strip(),
            import_data_dir=os.environ.get("IMPORT_DATA_DIR", "/data/imports").strip(),
            # The household Jellyfin server the music-ingest adapter reads
            # (read-only, #564 slice 1) to map the music catalog into OKF
            # band/song concepts. Reuses the existing JELLYFIN_* stack vars
            # (username/password, not an API key). Empty JELLYFIN_URL ⇒ skipped.
            jellyfin_url=os.environ.get("JELLYFIN_URL", "").strip(),
            # A castable stream URL must use a base the Cast device can reach on
            # the LAN, not the engine's localhost (#604). Defaults to JELLYFIN_URL
            # when unset, so the engine's own (fast, local) API calls are unchanged.
            jellyfin_cast_url=(
                os.environ.get("JELLYFIN_CAST_URL", "").strip()
                or os.environ.get("JELLYFIN_URL", "").strip()
            ),
            jellyfin_username=os.environ.get("JELLYFIN_USERNAME", "").strip(),
            jellyfin_password=os.environ.get("JELLYFIN_PASSWORD", "").strip(),
            # Per-library music ownership (#576): a Jellyfin library NAME -> the
            # owner resident uid, so a private library ('Music (cdopp)') ingests
            # under that resident's path; any unlisted library is household.
            # Format: `Name=uid;Name2=uid2`. Default maps 'Music (cdopp)'->cdopp.
            jellyfin_library_owners=_parse_library_owners(
                os.environ.get("JELLYFIN_LIBRARY_OWNERS", "Music (cdopp)=cdopp")
            ),
            # Curated IMAP mailboxes the email-ingest adapter reads (read-only,
            # #654). Numbered flat env `IMAP_<n>_*`; no account ⇒ ingest skipped.
            imap_accounts=_parse_imap_accounts(dict(os.environ)),
            # Web Push / VAPID keys (#713): the public key is surfaced to the
            # browser via /api/whoami so it can subscribe; the private key +
            # subject sign the push. An operator prerequisite (generated once,
            # dropped in the pod env) — NOT in the repo. Empty ⇒ Web Push
            # no-ops end-to-end, so the box is safe before they are set.
            vapid_public_key=vapid_public_key,
            vapid_private_key=vapid_private_key,
            vapid_subject=os.environ.get("VAPID_SUBJECT", "").strip(),
            # Android TWA / Digital Asset Links (#716): the /.well-known/
            # assetlinks.json route binds the app to this domain. The package
            # name identifies the app; the SHA256 cert fingerprints of its
            # signing key let Google verify the binding (so the TWA drops its
            # URL bar). The signing key doesn't exist until the android repo
            # scaffolds it, so fingerprints default empty — the route then
            # serves `[]` (valid; Google just won't verify yet).
            android_package=os.environ.get(
                "ANDROID_PACKAGE", "cloud.dopp.solaris"
            ).strip(),
            android_cert_fingerprints=_parse_cert_fingerprints(
                os.environ.get("ANDROID_CERT_FINGERPRINTS", "")
            ),
        )


settings = Settings.from_env()
