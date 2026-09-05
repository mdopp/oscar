"""The companion-app download and its version answer (#1326).

`/download` 302s to GitHub's `releases/latest/download/app-release.apk` — which
works for an unauthenticated phone only because `mdopp/solaris-android` is a
public repository; while it was private the redirect 404'd for everyone without
a GitHub login, and that was the bug.

The rest is the half the app was built against: `/download/version` answers
`{versionName, publishedAt, downloadUrl, sizeBytes}` from the public releases
API, cached so a household cannot spend the anonymous 60-calls-an-hour budget;
a daily cron refreshes the same cache and announces a genuinely new version to
the house exactly once, in daylight, with no address in the text.

The two backlog tables are replayed with raw SQL — a chat test must NOT import
alembic (CI runs solaris-chat in a clean env without it).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from solaris_chat import app_release
from solaris_chat.app_release import ReleaseWatch
from solaris_chat.engine.notify import EventBus
from solaris_chat.server import ANDROID_APK_URL, build_app

_TZ = ZoneInfo("Europe/Berlin")

_SCHEMA = """
CREATE TABLE push_subscriptions (
  id         TEXT PRIMARY KEY,
  owner_uid  TEXT NOT NULL,
  endpoint   TEXT NOT NULL UNIQUE,
  p256dh     TEXT NOT NULL,
  auth       TEXT NOT NULL,
  user_agent TEXT NOT NULL DEFAULT '',
  created    TEXT NOT NULL DEFAULT (datetime('now')),
  last_ok    TEXT
);
CREATE TABLE device_tokens (
  id         TEXT PRIMARY KEY,
  owner_uid  TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  label      TEXT,
  created    TEXT NOT NULL DEFAULT (datetime('now')),
  last_used  TEXT,
  revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE ha_notice_backlog (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  target_uid TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
  payload    TEXT NOT NULL
);
"""


def _payload(tag: str = "v2.39.0", asset: str = app_release.APK_ASSET) -> dict:
    return {
        "tag_name": tag,
        "published_at": "2026-09-01T18:22:40Z",
        "assets": [
            {
                "name": asset,
                "size": 12345678,
                "browser_download_url": (
                    f"https://github.com/mdopp/solaris-android/releases/download/{tag}/{asset}"
                ),
            }
        ],
    }


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _watch(tmp_path, **kw) -> ReleaseWatch:
    return ReleaseWatch(str(tmp_path / "app-release.json"), **kw)


def _fake_fetch(payloads: list, calls: list):
    async def fetch():
        calls.append(1)
        return payloads.pop(0) if payloads else None

    return fetch


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


class _RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[tuple[str, str, dict]] = []

    def publish(self, uid, kind, data):
        self.published.append((uid, kind, data))
        super().publish(uid, kind, data)


# ---- /download stays a redirect to the public asset ------------------------


def test_apk_url_is_the_latest_signed_release_asset():
    # `releases/latest/download/<asset>` is GitHub's always-newest redirect, so
    # the link never needs bumping on a new release.
    assert ANDROID_APK_URL == (
        "https://github.com/mdopp/solaris-android"
        "/releases/latest/download/app-release.apk"
    )
    assert ANDROID_APK_URL.startswith("https://")
    assert "/releases/latest/download/" in ANDROID_APK_URL
    assert ANDROID_APK_URL.endswith(".apk")


async def test_download_redirects_to_github(aiohttp_client, tmp_path):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.get("/download", allow_redirects=False)
    assert r.status == 302
    assert r.headers["Location"] == ANDROID_APK_URL


# ---- parsing the release ---------------------------------------------------


def test_version_name_is_the_tag_without_its_v():
    parsed = app_release.parse_release(_payload("v2.39.0"))
    assert parsed == {
        "versionName": "2.39.0",
        "publishedAt": "2026-09-01T18:22:40Z",
        "downloadUrl": (
            "https://github.com/mdopp/solaris-android/releases/download/"
            "v2.39.0/app-release.apk"
        ),
        "sizeBytes": 12345678,
    }
    assert app_release.parse_release(_payload("2.39.0"))["versionName"] == "2.39.0"


def test_a_release_without_the_apk_asset_is_no_release():
    # Announcing it would offer the household a version it cannot install.
    assert app_release.parse_release(_payload(asset="app-debug.apk")) is None
    assert app_release.parse_release({"tag_name": "v1.0.0"}) is None
    assert app_release.parse_release(_payload(tag="")) is None
    assert app_release.parse_release(None) is None


def test_a_non_https_asset_url_is_refused():
    payload = _payload()
    payload["assets"][0]["browser_download_url"] = "http://example.invalid/app.apk"
    assert app_release.parse_release(payload) is None


# ---- the cache -------------------------------------------------------------


async def test_the_answer_is_reused_inside_the_refresh_window(tmp_path, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        app_release, "_fetch_latest", _fake_fetch([_payload(), _payload()], calls)
    )
    watch = _watch(tmp_path)
    now = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    assert (await watch.latest(now=now))["versionName"] == "2.39.0"
    await watch.latest(now=now.replace(minute=4))
    assert len(calls) == 1
    await watch.latest(now=now.replace(minute=6))
    assert len(calls) == 2


async def test_a_failed_fetch_keeps_serving_the_cached_release(tmp_path, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(app_release, "_fetch_latest", _fake_fetch([_payload()], calls))
    watch = _watch(tmp_path)
    now = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    await watch.latest(now=now)
    # GitHub unreachable from here on: the minutes-old answer beats no answer.
    later = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)
    assert (await watch.latest(now=later))["versionName"] == "2.39.0"
    # …and the failed attempt is stamped too, so a polling phone cannot turn an
    # outage into a request storm against the anonymous rate limit.
    await watch.latest(now=later.replace(minute=1))
    assert len(calls) == 2


async def test_the_cache_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(app_release, "_fetch_latest", _fake_fetch([_payload()], []))
    now = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    await _watch(tmp_path).latest(now=now)
    assert _watch(tmp_path).cached()["versionName"] == "2.39.0"


# ---- GET /download/version -------------------------------------------------


async def _version_client(aiohttp_client, tmp_path, watch):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(tmp_path),
        release_watch=watch,
    )
    return await aiohttp_client(app)


async def test_version_answers_the_contract_publicly(
    aiohttp_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(app_release, "_fetch_latest", _fake_fetch([_payload()], []))
    client = await _version_client(aiohttp_client, tmp_path, _watch(tmp_path))
    # No Remote-User header: a phone before its first login must be able to ask.
    r = await client.get("/download/version")
    assert r.status == 200
    assert r.headers["Cache-Control"] == f"max-age={app_release.REFRESH_AFTER_S}"
    body = await r.json()
    assert set(body) == {"versionName", "publishedAt", "downloadUrl", "sizeBytes"}
    assert body["versionName"] == "2.39.0"
    assert body["downloadUrl"].startswith("https://")


async def test_version_503s_when_nothing_is_known(
    aiohttp_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(app_release, "_fetch_latest", _fake_fetch([], []))
    client = await _version_client(aiohttp_client, tmp_path, _watch(tmp_path))
    r = await client.get("/download/version")
    assert r.status == 503
    assert r.headers["Retry-After"] == str(app_release.REFRESH_AFTER_S)


async def test_version_503s_without_a_watch(aiohttp_client, tmp_path):
    client = await _version_client(aiohttp_client, tmp_path, None)
    assert (await client.get("/download/version")).status == 503


# ---- the once-per-version notice -------------------------------------------


def _house_watch(tmp_path, monkeypatch, payloads: list) -> tuple[ReleaseWatch, dict]:
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO device_tokens (id, owner_uid, token_hash) VALUES ('d1','anna','h')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(app_release, "_fetch_latest", _fake_fetch(payloads, []))
    bus = _RecordingBus()
    watch = _watch(tmp_path, db_path=db, event_bus=bus, household_uid="household")
    return watch, {"db": db, "bus": bus}


async def test_the_first_check_only_baselines(tmp_path, monkeypatch):
    # A fresh install has been told nothing, so nothing about the version it
    # finds is "new" — announcing it would be noise on every rebuild.
    watch, ctx = _house_watch(tmp_path, monkeypatch, [_payload(), _payload()])
    at = datetime(2026, 9, 5, 10, 0, tzinfo=_TZ)
    assert await watch.daily_check(now=at) is None
    assert ctx["bus"].published == []
    assert watch.notified_version() == "2.39.0"


async def test_a_new_version_is_announced_exactly_once(tmp_path, monkeypatch):
    watch, ctx = _house_watch(
        tmp_path,
        monkeypatch,
        [_payload("v2.39.0"), _payload("v2.40.0"), _payload("v2.40.0")],
    )
    at = datetime(2026, 9, 5, 10, 0, tzinfo=_TZ)
    await watch.daily_check(now=at)  # baseline on 2.39.0
    assert await watch.daily_check(now=at) == "2.40.0"
    assert await watch.daily_check(now=at) is None
    assert len(ctx["bus"].published) == 1


async def test_a_restart_does_not_re_announce(tmp_path, monkeypatch):
    watch, ctx = _house_watch(
        tmp_path,
        monkeypatch,
        [_payload("v2.39.0"), _payload("v2.40.0"), _payload("v2.40.0")],
    )
    at = datetime(2026, 9, 5, 10, 0, tzinfo=_TZ)
    await watch.daily_check(now=at)
    await watch.daily_check(now=at)
    # The marker lives on the data volume, not in this process.
    reborn = _watch(tmp_path, db_path=ctx["db"], event_bus=ctx["bus"])
    assert await reborn.daily_check(now=at) is None
    assert len(ctx["bus"].published) == 1


async def test_the_notice_names_the_menu_and_carries_no_address(tmp_path, monkeypatch):
    watch, ctx = _house_watch(
        tmp_path, monkeypatch, [_payload("v2.39.0"), _payload("v2.40.0")]
    )
    at = datetime(2026, 9, 5, 10, 0, tzinfo=_TZ)
    await watch.daily_check(now=at)
    await watch.daily_check(now=at)
    uid, kind, data = ctx["bus"].published[0]
    # Published on the stream every client already renders notices from…
    assert (uid, kind) == ("household", "ha")
    # …and told apart by the payload's own discriminator.
    assert data["kind"] == "app-update"
    # The app needs the number to suppress a stale notice (#1329) — but still
    # no address anywhere in the payload.
    assert data["versionName"] == "2.40.0"
    assert "2.40.0" in data["body"]
    assert "Solaris-App für Android" in data["body"]
    assert "http" not in data["body"]
    assert "http" not in json.dumps(data)
    assert data["actions"] == []
    # It is catchable after a screen-off gap, like every other notice.
    conn = sqlite3.connect(ctx["db"])
    rows = conn.execute("SELECT payload FROM ha_notice_backlog").fetchall()
    conn.close()
    assert len(rows) == 1 and "app-update" in rows[0][0]


async def test_a_night_check_defers_the_notice(tmp_path, monkeypatch):
    watch, ctx = _house_watch(
        tmp_path,
        monkeypatch,
        [_payload("v2.39.0"), _payload("v2.40.0"), _payload("v2.40.0")],
    )
    day = datetime(2026, 9, 5, 10, 0, tzinfo=_TZ)
    await watch.daily_check(now=day)  # baseline
    # 23:30 — a push now is a household disturbance; the check may run, the
    # notice waits. Nothing is marked, so the next daytime check still tells.
    assert await watch.daily_check(now=datetime(2026, 9, 5, 23, 30, tzinfo=_TZ)) is None
    assert ctx["bus"].published == []
    assert watch.notified_version() == "2.39.0"
    assert (
        await watch.daily_check(now=datetime(2026, 9, 6, 9, 20, tzinfo=_TZ)) == "2.40.0"
    )
    assert len(ctx["bus"].published) == 1


def test_the_daytime_window_is_08_to_21():
    assert not app_release.is_daytime(datetime(2026, 9, 5, 7, 59, tzinfo=_TZ))
    assert app_release.is_daytime(datetime(2026, 9, 5, 8, 0, tzinfo=_TZ))
    assert app_release.is_daytime(datetime(2026, 9, 5, 20, 59, tzinfo=_TZ))
    assert not app_release.is_daytime(datetime(2026, 9, 5, 21, 0, tzinfo=_TZ))


# ---- the daily job and the menu entry --------------------------------------


def test_the_cron_registry_carries_the_daily_check():
    from solaris_chat.engine import crons

    job = next(j for j in crons.load_jobs("") if j.name == "app-release-check")
    assert not job.prompt  # a code job, never fed to an agent
    assert job.weekday is None
    # Inside the notice window, so a new version is announced the day it lands.
    assert app_release.is_daytime(
        datetime(2026, 9, 5, job.hour, job.minute, tzinfo=_TZ)
    )


def test_the_menu_entry_points_at_download_and_degrades_gracefully():
    from pathlib import Path

    import solaris_chat

    html = (Path(solaris_chat.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="android-link" href="/download"' in html
    assert "Solaris-App für Android" in html
    # No dead end: when no release is reachable the entry stops being a link and
    # says why (docs/design-guidelines.md, rule 6).
    assert "derzeit nicht verfügbar" in html
    assert 'link.removeAttribute("href")' in html
