"""Behaviour-wiring guards for the `.tool` dot-commands (ADR 0009).

The other frontend tests assert that a symbol *exists*. These assert that each
dot-command is actually WIRED to do its job — the create posts the right action,
and every result row is actionable (opens / edits / toggles), not an inert label.
They exist because "looks wired but the click does nothing" bugs (a note result
with no click handler, duplicate hits, a create pointed at the wrong action) slip
past string-presence checks. Still static (regex over the served HTML), so they
gate the wiring, not the rendered runtime — the real proof is the box-verify.
"""

from __future__ import annotations

import re

from solaris_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _has(pattern: str) -> bool:
    return re.search(pattern, _HTML) is not None


def test_all_dot_commands_registered_and_dispatched():
    # Every `.tool` is offered, has a head label, and dispatches to a builder
    # through the client tool-registry (#1006): `ensureCard` looks the tool-id up
    # in `toolBuilders` instead of a hardcoded if/else chain.
    assert "var toolBuilders = {" in _HTML
    # #1213: a tool with no specialised builder falls through to the generic
    # schema-driven card, so a `.tool` shipped after this build still opens.
    assert "var build = toolBuilders[cmd]" in _HTML
    assert "(def ? function (el) { buildGenericToolCard(el, def); } : null)" in _HTML
    for cmd in ("task", "note", "doc", "contacts", "photo", "home", "energy", "model"):
        assert _has(r'\["\.' + cmd + r'",'), f".{cmd} missing from DOT_COMMANDS"
        assert _has(r"\b" + cmd + r": (?:build|function)"), (
            f".{cmd} not registered in toolBuilders"
        )
    # `.task` is the reference tool: its builder runs the generic schema-driven
    # card off its /api/defs/tool def, not an inline buildTaskCard.
    assert _has(
        r"task: function \(el\) \{ buildGenericToolCard\(el, toolRegistry\.task"
    ), ".task not dispatched through the generic tool card"


def test_migrated_tools_carry_declarative_kind_tool_defs():
    # #1006: every existing .tool now ships a declarative `kind: tool` SKILL.md so
    # the server auto-registers its actions and it joins /api/defs/tool. The head
    # label falls back to the def's tool-label when the registry has loaded.
    from pathlib import Path

    from solaris_chat.skills import list_tool_defs

    pack = Path(__file__).resolve().parents[2] / "templates/solaris/skills/household"
    by_id = {d["tool-id"]: d for d in list_tool_defs(pack)}
    for tid in ("task", "note", "doc", "contacts", "photo", "home", "energy"):
        assert tid in by_id, f".{tid} has no kind:tool def"
        assert by_id[tid]["command"] == "." + tid
        assert by_id[tid]["tool-label"], f".{tid} def has no tool-label"
    # The list/edit tools declare their card actions; the widget/upload tools
    # (photo/home/energy post to their own endpoints) declare none.
    assert by_id["task"]["tool-actions"] == [
        "task.set_status",
        "task.add",
        "task.update",
    ]
    assert by_id["note"]["tool-actions"] == ["note.add"]
    assert by_id["doc"]["tool-actions"] == ["doc.classify"]
    assert by_id["contacts"]["tool-actions"] == ["contact.add", "person.update"]
    assert by_id["home"]["tool-actions"] == []
    assert _has(r'def && def\["tool-label"\]')


def test_the_model_tile_declares_a_window_per_action_id():
    # #1374: a `tool-action-params` value is a flat literal or a `$field` (ADR
    # 0014) and a RemoteViews row has no chooser, so each offered duration is
    # its OWN action id with the window wired in. `model.lease` keeps the free
    # `until` for chat/PWA and never renders as a tile button, because the row
    # carries no `until` field for its `$until` source to read.
    from pathlib import Path

    from solaris_chat.skills import list_tool_defs

    pack = Path(__file__).resolve().parents[2] / "templates/solaris/skills/household"
    model = {d["tool-id"]: d for d in list_tool_defs(pack)}["model"]
    assert model["tool-label"] == "Modell"
    assert model["command"] == ".model"
    assert model["scope"] == "household"
    assert model["tool-api-path"] == "/api/portal/models"
    # A view+act tool: no create path, so no "Erfassen" tile is offered.
    assert model["tool-compose-path"] == ""
    assert model["tool-item-id-field"] == "id"
    assert model["tool-cell-schema"]["actions"] == [
        "model.lease.1h",
        "model.lease.4h",
        "model.lease.until_morning",
        "model.release",
    ]
    for action_id, params in model["tool-action-params"].items():
        assert action_id in model["tool-actions"], action_id
        assert params["model"] == "$id", action_id
    assert model["tool-action-params"]["model.lease"]["until"] == "$until"
    for action_id in model["tool-cell-schema"]["actions"]:
        assert "until" not in model["tool-action-params"][action_id]


def test_shipped_tool_cell_schemas_are_renderer_agnostic():
    # #1022: every shipped .tool's cell-schema is a renderer-agnostic role→field
    # mapping (no HTML/CSS/DOM) so a non-browser consumer (Android RemoteViews)
    # can render the same schema — enforced by the schema lint, not just intent.
    from pathlib import Path

    from solaris_chat.skills import cell_schema_violations, list_tool_defs

    pack = Path(__file__).resolve().parents[2] / "templates/solaris/skills/household"
    for d in list_tool_defs(pack):
        violations = cell_schema_violations(
            d["tool-cell-schema"], d["tool-action-params"]
        )
        assert violations == [], (
            f"{d['tool-id']} cell-schema not renderer-agnostic: {violations}"
        )


def test_shipped_tool_compose_paths_resolve():
    # #1213: a tool declares its create path in its own frontmatter
    # (`tool-compose-path`), and the lint rejects a declaration the router does
    # not serve — a tile pointed at a non-resolving path would silently land on
    # the start-page fallback instead of the create form it promised.
    from pathlib import Path

    from solaris_chat.skills import compose_path_violations, list_tool_defs

    pack = Path(__file__).resolve().parents[2] / "templates/solaris/skills/household"
    by_id = {d["tool-id"]: d for d in list_tool_defs(pack)}
    for tid, d in by_id.items():
        violations = compose_path_violations(tid, d["tool-compose-path"])
        assert violations == [], f"{tid} compose path does not resolve: {violations}"
    # The tools with a create path declare it; the pure view tools do not, so the
    # app never offers an "Erfassen" tile that would open nothing.
    for tid in ("task", "note", "contacts", "doc", "photo"):
        assert by_id[tid]["tool-compose-path"] == f"#/p/{tid}/new"
    for tid in ("home", "energy"):
        assert by_id[tid]["tool-compose-path"] == ""


def test_shipped_tool_item_id_fields_are_declared_not_sniffed():
    # #1256: a tool declares WHICH item field addresses one entry
    # (`tool-item-id-field`), so `#/p/<tool-id>/item/<item-id>` is built from the
    # catalog instead of a consumer probing entity_id/id/item_id/uid/key in order.
    from pathlib import Path

    from solaris_chat.skills import TOOL_ITEM_ROUTE, item_id_field_violations
    from solaris_chat.skills import list_tool_defs

    pack = Path(__file__).resolve().parents[2] / "templates/solaris/skills/household"
    by_id = {d["tool-id"]: d for d in list_tool_defs(pack)}
    for tid, d in by_id.items():
        violations = item_id_field_violations(
            d["tool-item-id-field"], d["tool-cell-schema"]
        )
        assert violations == [], f"{tid} item id field does not resolve: {violations}"
    # The tools whose rows address one entry declare the field their own list
    # endpoint returns it under; the rest declare none, so a consumer leaves
    # their rows un-tappable instead of linking into nowhere.
    assert by_id["task"]["tool-item-id-field"] == "id"
    assert by_id["doc"]["tool-item-id-field"] == "entity_id"
    assert by_id["contacts"]["tool-item-id-field"] == "id"
    assert by_id["photo"]["tool-item-id-field"] == "id"
    for tid in ("note", "home", "energy"):
        assert by_id[tid]["tool-item-id-field"] == ""
    # The one route the declaration feeds — the literal the Android side pins.
    assert (
        TOOL_ITEM_ROUTE.format(tool_id="doc", item_id="doc:42") == "#/p/doc/item/doc:42"
    )


def test_item_id_field_lint_rejects_a_field_the_cell_schema_lacks():
    # #1256: no declaration is clean (the tool offers no item route); a
    # declaration must name a field the def's own cell-schema provides — a field
    # nothing declares is the guessing this key exists to end.
    from solaris_chat.skills import item_id_field_violations

    schema = {"id": "entity_id", "title": "title", "meta": ["category"]}
    assert item_id_field_violations("", schema) == []
    assert item_id_field_violations("entity_id", schema) == []
    assert item_id_field_violations("title", schema) == []  # any declared field
    assert item_id_field_violations("uid", schema)  # not in the schema
    assert item_id_field_violations("id", schema)  # the ROLE name, not the field
    assert item_id_field_violations("entity_id", {})  # schema declares no fields
    # `actions` names action ids, not item fields — it can't provide an id.
    assert item_id_field_violations("task.set_status", {"actions": ["task.set_status"]})


def test_compose_path_lint_rejects_a_path_the_router_cannot_serve():
    # #1213: no declaration is clean (the tool has no create path); a declaration
    # must be THIS tool's canonical route.
    from solaris_chat.skills import compose_path_violations

    assert compose_path_violations("task", "") == []
    assert compose_path_violations("task", "#/p/task/new") == []
    assert compose_path_violations("task", "#/p/note/new")  # another tool's route
    assert compose_path_violations("task", "#/p/task")  # not the compose route
    assert compose_path_violations("task", "/p/task/new")  # not a hash route
    assert compose_path_violations("task", "#/p/task/new/")  # trailing slash


def test_task_row_action_carries_a_declarative_param_map():
    # #1214: the reference tool declares its row action AND where that action's
    # /api/action-callback params come from, so a native renderer wires the button
    # off the catalog alone. `$field` = read the row's field; else a literal.
    from pathlib import Path

    from solaris_chat.skills import list_tool_defs

    pack = Path(__file__).resolve().parents[2] / "templates/solaris/skills/household"
    task = {d["tool-id"]: d for d in list_tool_defs(pack)}["task"]
    assert task["tool-cell-schema"]["actions"] == ["task.set_status"]
    assert task["tool-action-params"]["task.set_status"] == {
        "entity_id": "$id",
        "status": "done",
    }
    # every declared mapping names an action the def actually registers
    for action_id in task["tool-action-params"]:
        assert action_id in task["tool-actions"]


def test_task_dispatches_through_the_generic_tool_registry_card():
    # #1005: the client fetches the tool registry (/api/defs/tool) into
    # toolRegistry at init and dispatches .task through buildGenericToolCard.
    assert '"/api/defs/tool"' in _HTML  # registry fetched at init
    assert "var toolRegistry = {}" in _HTML
    assert "function loadToolRegistry()" in _HTML
    assert 'if (d["tool-id"]) toolRegistry[d["tool-id"]] = d;' in _HTML
    assert "function buildGenericToolCard(el, def)" in _HTML
    # The generic card is schema-driven: list/search off the def's tool-api-path,
    # rows off its tool-cell-schema resolved against the item.
    assert 'var apiPath = (el._tool && el._tool["tool-api-path"])' in _HTML
    assert 'var schema = (el._tool && el._tool["tool-cell-schema"])' in _HTML
    assert "function resolveCell(item, cellSchema)" in _HTML
    assert "renderListCell(t, resolveCell(t, schema))" in _HTML


def test_task_create_find_edit_wired():
    # create → task.add; a row is a checkbox that toggles task.set_status; tapping
    # the row opens the inline editor which PATCHes task.update.
    assert _has(r'taskAction\(\s*"task\.add"')
    assert _has(r'taskAction\(\s*"task\.set_status"')
    assert "beginTaskEdit(el, row, t)" in _HTML
    assert _has(r'taskAction\(\s*"task\.update"')


def test_note_create_and_clickable_deduped_results():
    # create → note.add; results are de-duplicated by display LABEL (an upload's
    # companion + its extracted OKF note share a title) and each row is CLICKABLE,
    # opening the note viewer — not an inert label.
    assert _has(r'taskAction\(\s*"note\.add"')
    assert "byLabel[key]" in _HTML  # de-dupe by display label, not path
    assert "openNoteViewer(u.path)" in _HTML
    # kept restrictive: every query word must appear in the hit.
    assert "words.every" in _HTML


def test_contacts_create_find_edit_wired():
    # create → contact.add; tapping a contact row opens the editor → person.update.
    assert _has(r'taskAction\(\s*"contact\.add"')
    assert "beginPersonEdit(el, rw, c)" in _HTML
    assert _has(r'taskAction\(\s*"person\.update"')


def test_doc_upload_and_search_wired():
    # upload classifies (doc.classify); typing filters via the search endpoint.
    assert _has(r'taskAction\(\s*"doc\.classify"')
    assert "function searchDocs(el, q)" in _HTML
    assert '"/api/portal/documents/search' in _HTML


def test_home_filters_devices_and_reflects_favourite():
    # typing filters devices live; matches render as controllable widget cards
    # that carry the favourite ★ toggle (pin:true).
    assert _has(r'cmd === "home"[\s\S]*?renderHomeList\(card')
    assert "renderHaCard(c, false, { pin: true })" in _HTML


def test_energy_renders_inline():
    # .energy is its own display-only card that reuses the energy renderer.
    assert "function buildEnergyCard(el)" in _HTML
    assert "renderEnergyPage(listEl, j.energy)" in _HTML


def test_favourite_toggle_adds_and_removes():
    # the ★/☆ on a card both pins (POST) and unpins (DELETE by id) — a real
    # toggle that reflects hcPins state, not a one-way pin.
    assert "hcPins" in _HTML
    assert '"/api/favorites"' in _HTML
    assert _has(r'"/api/favorites/"\s*\+')  # DELETE by favourite id
