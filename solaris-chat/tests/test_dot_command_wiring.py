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
    assert "var build = toolBuilders[cmd];" in _HTML
    for cmd in ("task", "note", "doc", "contacts", "photo", "home", "energy"):
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


def test_shipped_tool_cell_schemas_are_renderer_agnostic():
    # #1022: every shipped .tool's cell-schema is a renderer-agnostic role→field
    # mapping (no HTML/CSS/DOM) so a non-browser consumer (Android RemoteViews)
    # can render the same schema — enforced by the schema lint, not just intent.
    from pathlib import Path

    from solaris_chat.skills import cell_schema_violations, list_tool_defs

    pack = Path(__file__).resolve().parents[2] / "templates/solaris/skills/household"
    for d in list_tool_defs(pack):
        violations = cell_schema_violations(d["tool-cell-schema"])
        assert violations == [], (
            f"{d['tool-id']} cell-schema not renderer-agnostic: {violations}"
        )


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
