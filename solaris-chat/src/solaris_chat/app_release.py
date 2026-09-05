"""Which Android companion release the household is offered (#1326).

`/download` redirects to the newest signed APK on GitHub; this module answers
the other half — *which* version that is, whether it is newer than the one on
the phone, and telling the house once when it changes.

It reads the **public** releases API of `mdopp/solaris-android` anonymously (no
token, 60 calls an hour per IP), so the on-request read (`/download/version`)
and the daily cron share one cached answer instead of each spending a call.
Every attempt is stamped, failures included, so an unreachable GitHub cannot be
turned into a request storm by a phone that keeps asking.

The state file sits next to `solaris.db` on the Solaris data volume and holds
the parsed release, when it was last asked for, and which `versionName` the
household has already been told about. That last one is why it is on disk: a
restart must not re-announce a version the house has already seen.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from solaris_chat import ha_notify, notice_backlog
from solaris_chat.logging import log

RELEASES_API = "https://api.github.com/repos/mdopp/solaris-android/releases/latest"
APK_ASSET = "app-release.apk"

# The anonymous GitHub API allows 60 calls an hour per IP. A phone asking on
# every app start would spend that in a morning, so an answer — or a failure —
# is reused for this long. It is also the `Cache-Control`/`Retry-After` the
# route hands out, so client and server agree on how stale the answer may be.
REFRESH_AFTER_S = 300

# The payload discriminator the app reads. The notice itself rides the ordinary
# `ha` bus kind, so a client that only knows household notices still shows it.
NOTICE_KIND = "app-update"
NOTICE_TITLE = "Solaris-App"

# A push in the night is a household disturbance. The check may run at any hour;
# the announcement waits for the next check inside this window.
DAY_START_HOUR = 8
DAY_END_HOUR = 21

_LOCAL_TZ = ZoneInfo("Europe/Berlin")
_TIMEOUT_S = 10


def parse_release(payload: Any) -> dict[str, Any] | None:
    """`/download/version`'s body from a GitHub release payload, or None.

    `versionName` is the tag without its leading `v` — what the app compares
    against its own `BuildConfig.VERSION_NAME`. A release without the APK asset
    is no release at all here: announcing it would offer a version nobody can
    install. `downloadUrl` must be https because the app refuses anything else.
    """
    if not isinstance(payload, dict):
        return None
    tag = str(payload.get("tag_name") or "").strip()
    assets = payload.get("assets")
    asset = next(
        (
            a
            for a in (assets if isinstance(assets, list) else [])
            if isinstance(a, dict) and a.get("name") == APK_ASSET
        ),
        None,
    )
    url = str((asset or {}).get("browser_download_url") or "")
    if not tag or not url.startswith("https://"):
        return None
    return {
        "versionName": tag[1:] if tag.startswith("v") else tag,
        "publishedAt": str(payload.get("published_at") or ""),
        "downloadUrl": url,
        "sizeBytes": int((asset or {}).get("size") or 0),
    }


def notice_body(version: str) -> str:
    """The one sentence the household reads. Deliberately carries **no address**:
    a notice is not linked text on the phone, so a URL here would be there to be
    typed out — and a notice that opens an address it brought along is a
    phishing surface for nothing. It names the menu entry; the app resolves the
    address itself."""
    return (
        f"Neue Version {version} der Solaris-App ist da — "
        "im Menü unter „Solaris-App für Android“."
    )


def is_daytime(now: datetime) -> bool:
    return DAY_START_HOUR <= now.hour < DAY_END_HOUR


async def _fetch_latest() -> Any:
    """The raw `releases/latest` payload, or None when GitHub says otherwise."""
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                RELEASES_API, headers={"Accept": "application/vnd.github+json"}
            ) as resp,
        ):
            if resp.status != 200:
                log.warning("chat.app_release.http", status=resp.status)
                return None
            return await resp.json()
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
        log.warning("chat.app_release.fetch_failed", error=str(e))
        return None


class ReleaseWatch:
    """The cached companion release, and the once-per-version announcement."""

    def __init__(
        self,
        state_path: str,
        *,
        db_path: str = "",
        event_bus: Any = None,
        notifier: Any = None,
        household_uid: str = "household",
    ):
        self._path = Path(state_path)
        self._db_path = db_path
        self._event_bus = event_bus
        self._notifier = notifier
        self._household_uid = household_uid
        self._state = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            state = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _write(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._state), "utf-8")
        except OSError as e:
            log.error("chat.app_release.state_write_failed", error=str(e))

    def cached(self) -> dict[str, Any] | None:
        release = self._state.get("release")
        return release if isinstance(release, dict) else None

    def notified_version(self) -> str:
        return str(self._state.get("notified_version") or "")

    def _due(self, now: datetime) -> bool:
        raw = str(self._state.get("checked_at") or "")
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return True
        return (now - last).total_seconds() >= REFRESH_AFTER_S

    async def latest(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """What `/download/version` serves; None when nothing is known yet.

        A failed refresh keeps serving the cached release rather than going
        dark — a version answer minutes out of date is right far more often
        than no answer at all."""
        now = now or datetime.now(UTC)
        if self._due(now):
            await self.refresh(now=now)
        return self.cached()

    async def refresh(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or datetime.now(UTC)
        release = parse_release(await _fetch_latest())
        self._state["checked_at"] = now.isoformat()
        if release is not None:
            self._state["release"] = release
        self._write()
        return self.cached()

    async def daily_check(self, *, now: datetime | None = None) -> str | None:
        """The cron pass: refresh, and announce a genuinely new version once.

        Returns the announced `versionName`, or None. The first check on a box
        only records what is current — "new" is new relative to something the
        household was told, and a fresh install has been told nothing.
        """
        local = now or datetime.now(_LOCAL_TZ)
        release = await self.refresh(now=local.astimezone(UTC))
        if release is None:
            return None
        version = str(release.get("versionName") or "")
        if not version or version == self.notified_version():
            return None
        if not self.notified_version():
            self._state["notified_version"] = version
            self._write()
            return None
        if not is_daytime(local):
            log.info("chat.app_release.deferred", version=version, hour=local.hour)
            return None
        await self._announce(version)
        self._state["notified_version"] = version
        self._write()
        return version

    async def _announce(self, version: str) -> None:
        """Exactly one household notice, on the channel every other notice uses.

        Best effort like all of them (see `solaris_chat.ha_notify`): the bus
        reaches an open client, the short backlog covers a screen-off gap, and a
        backgrounded PWA gets a Web Push.
        """
        if not self._db_path:
            return
        resolved = ha_notify.resolve(
            self._db_path, self._household_uid, household_uid=self._household_uid
        )
        if resolved is None:
            log.info("chat.app_release.no_recipients", version=version)
            return
        bus_uid, push_uids = resolved
        body = notice_body(version)
        data = ha_notify.event_data(
            bus_uid, NOTICE_TITLE, body, urgency="low", kind=NOTICE_KIND
        )
        if self._event_bus is not None:
            self._event_bus.publish(bus_uid, ha_notify.EVENT_KIND, data)
        notice_backlog.record(self._db_path, bus_uid, data)
        log.info("chat.app_release.announced", version=version)
        if self._notifier is None:
            return
        for uid in push_uids:
            if self._event_bus is not None and self._event_bus.has_subscriber(uid):
                continue
            try:
                await self._notifier.push(uid, NOTICE_TITLE, body, data)
            except Exception as e:  # noqa: BLE001 — best effort, per resident
                log.error("chat.app_release.push_failed", uid=uid, error=str(e))
