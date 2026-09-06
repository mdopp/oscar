"""`/health` tells the truth, and tells the same truth as `/napi/` (#1272, #1273).

The old handler returned `{"ok": True}` unconditionally, so ServiceBay's tile
stayed green for more than a day through the #1271 outage while every
database-backed path was dead. `/health` now opens solaris.db read-only and asks
it for its schema; the last tests here are the point of the pair — one state must
not produce `200 ok` on the tile and an error on the API.
"""

from __future__ import annotations

import sqlite3

from solaris_chat import db_health, device_token_store
from solaris_chat.server import STATIC_DIR, build_app


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


def _readable_db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    return path


def _app(db_path: str):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db_path,
    )


# ---- the probe ------------------------------------------------------------


def test_probe_passes_on_a_readable_database(tmp_path):
    assert db_health.probe(_readable_db(tmp_path)) is None


def test_probe_reports_a_database_it_cannot_open(tmp_path):
    # A directory where solaris.db should be: unopenable whatever uid we run as.
    assert db_health.probe(str(tmp_path)) is not None


def test_probe_reports_a_missing_database_instead_of_creating_it(tmp_path):
    missing = tmp_path / "gone.db"
    assert db_health.probe(str(missing)) is not None
    assert not missing.exists()


def test_probe_reports_a_corrupt_database(tmp_path):
    path = tmp_path / "solaris.db"
    path.write_bytes(b"this is not a database at all, not even slightly")
    assert db_health.probe(str(path)) is not None


def test_a_missing_table_is_not_an_infrastructure_fault():
    # schema-init may not have migrated yet; the stores degrade to empty there,
    # so this must NOT be classified as unavailable.
    missing_table = sqlite3.OperationalError("no such table: device_tokens")
    assert db_health.unavailable_reason(missing_table) is None


def test_a_programming_error_is_not_an_infrastructure_fault():
    bug = TypeError("someone changed a signature")
    assert db_health.unavailable_reason(bug) is None


# ---- the endpoint ---------------------------------------------------------


async def test_health_is_ok_when_the_database_answers(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(_readable_db(tmp_path)))

    resp = await client.get("/health")
    assert resp.status == 200
    assert (await resp.json())["ok"] is True


async def test_health_is_503_with_the_reason_when_the_database_is_unreadable(
    aiohttp_client, tmp_path
):
    client = await aiohttp_client(_app(str(tmp_path)))

    resp = await client.get("/health")
    assert resp.status == 503
    body = await resp.json()
    assert body["ok"] is False
    assert body["reason"]


async def test_health_ignores_the_neighbours(aiohttp_client, tmp_path):
    # No Home Assistant configured and a backend URL pointing nowhere: Solaris is
    # still a working chat server with its history, so the tile stays green.
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_readable_db(tmp_path),
        llama_server_url="http://127.0.0.1:1",
    )
    client = await aiohttp_client(app)

    assert (await client.get("/health")).status == 200


# ---- the invariant --------------------------------------------------------


async def test_health_and_napi_agree_when_the_database_is_unreadable(
    aiohttp_client, tmp_path
):
    # One state, one answer. A service that reports two different things about
    # the same state is worse than one that is consistently wrong: green on the
    # tile and 500 on the API is how #1271 stayed unnoticed for a day.
    client = await aiohttp_client(_app(str(tmp_path)))

    health = await client.get("/health")
    napi = await client.get(
        "/napi/whoami",
        headers={"Authorization": f"Bearer {device_token_store.TOKEN_PREFIX}x"},
    )
    assert health.status == napi.status == 503
    assert (await health.json())["error"] == (await napi.json())["error"]


async def test_health_and_napi_agree_when_the_database_is_fine(
    aiohttp_client, tmp_path
):
    # The other half of the invariant: a readable store must not leave the tile
    # green while the API claims the database is gone.
    db = _readable_db(tmp_path)
    client = await aiohttp_client(_app(db))

    assert (await client.get("/health")).status == 200
    napi = await client.get(
        "/napi/whoami",
        headers={"Authorization": f"Bearer {device_token_store.TOKEN_PREFIX}x"},
    )
    assert napi.status == 401


# ---- the resident's surface (#1274) ---------------------------------------


async def test_whoami_tells_the_page_the_database_is_unreadable(
    aiohttp_client, tmp_path
):
    # The web path authenticates from the Authelia header and never touches the
    # store, so through #1271 the page loaded normally and the resident found out
    # one dead click at a time. `db` is what raises the in-page banner.
    client = await aiohttp_client(_app(str(tmp_path)))

    resp = await client.get("/api/whoami")
    assert resp.status == 200  # a loaded page with a notice, NOT a 503 page
    assert (await resp.json())["db"] == "unavailable"


async def test_whoami_clears_the_banner_when_the_database_is_readable(
    aiohttp_client, tmp_path
):
    client = await aiohttp_client(_app(_readable_db(tmp_path)))

    assert (await (await client.get("/api/whoami")).json())["db"] == "ok"


async def test_the_page_banner_agrees_with_health_and_napi(aiohttp_client, tmp_path):
    # The third surface on the one predicate: whoami must not say "ok" to the
    # browser while /health and /napi answer 503 about the same state.
    client = await aiohttp_client(_app(str(tmp_path)))

    assert (await client.get("/health")).status == 503
    assert (await (await client.get("/api/whoami")).json())["db"] == "unavailable"


def test_the_banner_is_wired_to_the_whoami_db_token():
    # Frontend contract: one notice element above the views (so the chat view and
    # every portal page carry it), driven by `db`, re-asked so it lifts on
    # recovery without a reload, and worded without jargon or a dead end.
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="db-notice"' in html
    assert "function applyDbHealth(db)" in html
    assert "applyDbHealth(j && j.db)" in html
    assert "setInterval(refreshDbHealth, 30000)" in html
    assert "Solaris kann gerade nicht auf seine Daten zugreifen." in html
