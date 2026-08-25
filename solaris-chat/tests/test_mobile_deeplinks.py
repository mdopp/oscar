"""Frontend-contract checks for the mobile deep-link changes (#765/#766/#769).

Source-text asserts over the single-file PWA (index.html) lock the markup/JS
contract; the real behaviour is box-verified. Covers: the removed standalone
Energie page (now the `.energy` dot-command, #973) + the de-duped Chats-page nav
(#765), the `?ask=` household deep link (#766), and the
`#/p/device/<entity_id>` single-device route (#769).
"""

from __future__ import annotations

from solaris_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_energie_standalone_page_removed():
    # #973: energy is no longer a standalone page/tab — it lives in the `.energy`
    # dot-command. The bottom-tab + rail + route are gone; the renderers stay.
    assert 'id="tab-energy"' not in _HTML
    assert 'id="rail-energy"' not in _HTML
    assert "tabEnergy" not in _HTML
    assert "railEnergy" not in _HTML
    assert '"#/p/energy"' not in _HTML
    # Renderers reused by `.energy` remain.
    assert "function renderEnergyPage(card, e)" in _HTML
    assert "function drawEnergyChart(body, series)" in _HTML
    # Dispatched through the client tool-registry (#1006): toolBuilders[cmd].
    assert "energy: buildEnergyCard," in _HTML


def test_chats_page_nav_deduped_on_mobile_only():
    # #765: the redundant primary nav group at the top of the Chats full-page
    # view is hidden on mobile (the bottom tab bar replaces it); desktop keeps
    # the rail-nav — the rule lives inside the mobile media query.
    assert ".rail-nav { display: none; }" in _HTML


def test_ask_param_household_deep_link():
    # #766: `#/?ask=<urlencoded>` opens a NEW household chat and AUTO-SENDS the
    # decoded text, consuming the param once (strip before send → no double-send).
    assert "function consumeAskParam()" in _HTML
    assert 'get("ask")' in _HTML
    assert "history.replaceState(null" in _HTML  # consume once
    assert "pendingTopic = HOUSEHOLD_TOPIC;" in _HTML
    assert "runTurn(text, []);" in _HTML
    assert (
        "if (!consumeAskParam() && !consumeToolParam()) routeFromLocation();" in _HTML
    )
    # The chosen scheme is documented in a code comment for the Android app.
    assert "#/?ask=<urlencodierter-text>" in _HTML


def test_single_device_route():
    # #769: #/p/device/<entity_id> opens a one-device page reusing renderHaCard
    # over the /api/portal/state card.
    assert (
        'if (type.indexOf("device/") === 0) { openDevicePage(type.slice(7)); return; }'
        in _HTML
    )
    assert "function openDevicePage(entityId)" in _HTML
    assert "/api/portal/state?entity_id=" in _HTML
    assert "renderHaCard(j.card, false, { pin: true })" in _HTML


def test_single_camera_route():
    # #782: #/p/camera/<entity_id> opens a page showing one camera's live HA
    # snapshot (replaces the #770 placeholder), served to the browser/Authelia
    # session via the /api/portal/camera/<id>/snapshot twin.
    assert (
        'if (type.indexOf("camera/") === 0) { openCameraPage(type.slice(7)); return; }'
        in _HTML
    )
    assert "function openCameraPage(entityId)" in _HTML
    assert '"/api/portal/camera/" + encodeURIComponent(entityId) + "/snapshot"' in _HTML
    # The still is refreshed on a timer that is torn down when the route leaves.
    assert "cameraTimer = setInterval(refresh, 5000)" in _HTML
    assert "function stopCameraRefresh()" in _HTML
    server_src = (STATIC_DIR.parent / "server.py").read_text(encoding="utf-8")
    # The browser/Authelia session reaches the snapshot on /api/ (the /napi/
    # twin is device-token only), so the /api/ GET route must be registered.
    assert '"/api/portal/camera/{entity_id}/snapshot", portal_camera_snapshot' in (
        server_src
    )


def test_state_route_registered_for_browser_session():
    # #769: /api/portal/state must be reachable on the Authelia session (not only
    # the /napi/ device-token twin) so the deep-link route can fetch the card.
    server_src = (STATIC_DIR.parent / "server.py").read_text(encoding="utf-8")
    assert 'app.router.add_get("/api/portal/state", portal_state)' in server_src


def test_tool_compose_route_is_catalog_driven():
    # #1213 (contract with mdopp/solaris-android#71): `#/p/<tool-id>/new` opens
    # the create path of ANY tool in /api|/napi/defs/tool. Catalog-driven — the
    # route resolves the id against the registry, so a `.tool` shipped after this
    # build works with no PWA rebuild and no app update; there is no per-tool
    # branch like the hand-written notes/documents doorways.
    assert (
        "if (/^[^/]+\\/new$/.test(type)) { openToolCompose(type.slice(0, -4)); return; }"
        in _HTML
    )
    assert "function openToolCompose(toolId)" in _HTML
    # Only a tool that DECLARES a compose path is opened — the same declaration
    # the app reads to decide whether to offer the tile at all.
    assert (
        'if (!def || !def["tool-compose-path"]) { toolDeepLinkFallback(); return; }'
        in _HTML
    )
    # The card comes from the shared dot-command entry, not a new page per tool.
    assert "dotcmd.openTool(toolId)" in _HTML
    assert "function openTool(toolId)" in _HTML
    assert "openTool: openTool" in _HTML


def test_tool_chat_card_param_is_consumed_once():
    # #1213: `#/?tool=<tool-id>` opens the chat with that tool's card already
    # open, consumed ONCE via history.replaceState — the same one-shot mechanism
    # as `?ask=` (#766), so a reload can't re-fire it.
    assert "function consumeToolParam()" in _HTML
    assert 'get("tool")' in _HTML
    assert "history.replaceState(null" in _HTML
    assert "openToolChat(toolId)" in _HTML
    assert "function openToolChat(toolId)" in _HTML
    # The chosen scheme is documented in a code comment for the Android app.
    assert "#/?tool=<tool-id>" in _HTML
    # `?ask=` no longer strips a hash it isn't going to act on, or it would eat
    # the `?tool=` param before consumeToolParam ever sees it.
    assert "if (!text) return false;" in _HTML


def test_unknown_tool_id_falls_back_to_start_page():
    # #1213: a home-screen tile outlives the tool it points at (uninstall,
    # renamed id). Both routes then land on `#/p/start` — never a blank router
    # state, never a dead end.
    assert 'var TOOL_DEEPLINK_FALLBACK = "#/p/start";' in _HTML
    assert (
        "function toolDeepLinkFallback() { location.hash = TOOL_DEEPLINK_FALLBACK; }"
        in _HTML
    )
    assert "if (!toolRegistry[toolId]) { toolDeepLinkFallback(); return; }" in _HTML
    # An id that is in the catalog but whose card won't build also falls back.
    assert _HTML.count("if (!dotcmd.openTool(toolId)) toolDeepLinkFallback();") == 2
    # The routes wait for the catalog instead of racing its first load.
    assert "var toolRegistryReady = loadToolRegistry();" in _HTML
    assert _HTML.count("toolRegistryReady.then(function () {") == 2
