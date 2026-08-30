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
    assert "function openTool(toolId, filter)" in _HTML
    assert "openTool: openTool" in _HTML


def test_tool_item_route_is_catalog_driven():
    # #1256 (contract with mdopp/solaris-android#107): `#/p/<tool-id>/item/<item-id>`
    # opens ONE entry of ANY tool in /api|/napi/defs/tool. `item` is its own
    # segment, so an item id can never collide with `new` or a later subpage, and
    # the id is matched greedily so an entity_id keeps its dots.
    assert "var itemRoute = /^([^/]+)\\/item\\/(.+)$/.exec(type);" in _HTML
    assert (
        "if (itemRoute) { openToolItem(itemRoute[1], itemRoute[2]); return; }" in _HTML
    )
    assert "function openToolItem(toolId, itemId)" in _HTML
    # Only a tool that DECLARES which field carries its item id has an item route
    # — the same declaration the app reads to decide whether a row tap gets an
    # address at all.
    assert (
        'if (!def || !def["tool-item-id-field"] || !itemId) '
        "{ toolDeepLinkFallback(); return; }" in _HTML
    )
    # The item is resolved against the tool's own list endpoint, generically: no
    # per-tool payload-key table, so a `.tool` shipped after this build works
    # with no PWA rebuild and no app update.
    assert "function findToolItem(def, itemId)" in _HTML
    assert (
        'var path = def["tool-api-path"], field = def["tool-item-id-field"];' in _HTML
    )
    assert (
        "for (var k in j) { if (Array.isArray(j[k])) { items = j[k]; break; } }"
        in _HTML
    )
    # The entry opens through the shared dot-command entry with the row's title as
    # the live filter — no per-tool item page.
    assert "if (!dotcmd.openTool(toolId, title)) toolDeepLinkFallback();" in _HTML


def test_unknown_tool_item_falls_back_to_start_page():
    # #1256: a home-screen tile outlives the entry it points at (a task ticked
    # off, a document deleted). An id the tool's list no longer returns lands on
    # `#/p/start` — the same fallback as an unresolvable tool-id (#1213), never a
    # blank router state.
    assert "findToolItem(def, itemId).then(function (item) {" in _HTML
    assert "if (!item) { toolDeepLinkFallback(); return; }" in _HTML
    # A failed/unauthorised list read resolves to "no item", not to a rejection
    # that would leave the route hanging on a blank view.
    assert ".catch(function () { return null; });" in _HTML


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
    # The routes wait for the catalog instead of racing its first load — compose,
    # chat-card, and the item route (#1256).
    assert "var toolRegistryReady = loadToolRegistry();" in _HTML
    assert _HTML.count("toolRegistryReady.then(function () {") == 3
