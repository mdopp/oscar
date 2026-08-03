"""Frontend-contract checks for the persona × speed selection (#278, #420).

The persona × speed choice no longer lives in a chat-header dropdown: #420
removed the Settings button and the top persona dropdown and moved the choice
to the `/persona` slash command, which crosses each persona with a speed
(schnell/Thinking) plus the admin profile and persists the pick for the NEXT
new chat (mapping back to the unchanged payload.personality + payload.reasoning
wiring). The household "Zuhause" chat always runs fast. The user-facing Thema
topic picker is retired (#279d) — inline #tag/@person mentions replace it; only
the internal household binding stays. The real check is the box-verify across
the contexts; these lock the markup/JS contract.
"""

from __future__ import annotations

import re

from solaris_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_header_persona_dropdown_and_settings_button_are_gone():
    # #420: the standalone Thinking toggle, the header persona dropdown, and the
    # Settings button are all removed — the choice moved to the /persona command.
    assert 'id="reasoning-control"' not in _HTML
    assert 'id="reasoning-mode"' not in _HTML
    assert 'id="persona-control"' not in _HTML
    assert 'id="personality"' not in _HTML
    assert 'id="open-settings"' not in _HTML
    assert "personalitySel" not in _HTML


def test_persona_choices_cross_persona_with_speed():
    # Each persona is crossed with a speed: schnell (reasoning none) and Thinking
    # (reasoning high). personaChoices() builds the /persona card entries; the
    # household chat is always Solaris/schnell regardless of the persisted pick.
    assert '{ suffix: "schnell", reasoning: "none" }' in _HTML
    assert '{ suffix: "Thinking", reasoning: "high" }' in _HTML
    assert "function personaChoices()" in _HTML
    assert 'value: p.id + "|" + sp.reasoning,' in _HTML
    assert 'label: p.label + " · " + sp.suffix,' in _HTML


def test_selection_maps_back_to_persona_and_reasoning():
    # currentPersonality()/currentReasoning() unpack the persisted combined value
    # so the backend personality + reasoning routing is unchanged.
    assert "function parsePersonaSpeed(v)" in _HTML
    assert (
        "function currentPersonality() { return parsePersonaSpeed(personaSpeed()).id; }"
        in _HTML
    )
    assert (
        "function currentReasoning() { return parsePersonaSpeed(personaSpeed()).reasoning; }"
        in _HTML
    )
    # The turn payload still sends both fields.
    assert "personality: currentPersonality(), reasoning: currentReasoning()" in _HTML


def test_persona_choice_persists_for_next_chat():
    # #420: the /persona pick is persisted (next-new-chat scope), not applied to a
    # live header control — set via setPersonaSpeed, read via personaSpeed.
    assert "function setPersonaSpeed(value)" in _HTML
    assert 'localStorage.setItem("solaris.persona-speed", value);' in _HTML
    assert "function personaSpeed()" in _HTML


def test_thema_picker_is_retired():
    # The user-facing Thema topic picker is gone (#279d): no dropdown element,
    # no fixed-context option gating, no picker-facing JS.
    assert 'id="topic-control"' not in _HTML
    assert 'id="topic-primary"' not in _HTML
    assert 'id="topic-tags"' not in _HTML
    assert "FIXED_CONTEXT_TOPICS" not in _HTML
    assert "function syncSessionTopics" not in _HTML
    assert "function setSessionTopic" not in _HTML
    assert "topicCtrl" not in _HTML


def test_topic_dashboard_modal_is_removed():
    # The #244 topic dashboard modal (only reachable from the removed picker /
    # chip click) is gone; the session-row chip stays as display-only.
    assert 'id="topic-modal"' not in _HTML
    assert "function openTopicDashboard" not in _HTML


def test_no_header_persona_or_topic_controls():
    # #420: with the header dropdown gone there is nothing to hide in the embed —
    # neither the persona control nor the retired Thema control exists.
    assert "#persona-control" not in _HTML
    assert "#topic-control" not in _HTML


def test_household_pin_binding_intact():
    # The internal household topic binding stays: the rail Zuhause entry pre-binds
    # the `household` topic via the #242 pendingTopic path before the resident's
    # durable session exists.
    assert 'var HOUSEHOLD_TOPIC = "household";' in _HTML
    assert "pendingTopic = HOUSEHOLD_TOPIC;" in _HTML
    assert "payload.topic = pendingTopic;" in _HTML
    # Zuhause is a rail-nav entry now (#700), not a topic-gated hidden pin.
    assert 'id="rail-home"' in _HTML


def test_chat_search_moved_out_of_the_header():
    # The in-chat search box is no longer in the chat-header (#411): it moved
    # onto the /search command card, so the header carries no search input.
    header = re.search(r"<header class=\"chat-header\">(.*?)</header>", _HTML, re.S)
    assert header, "chat-header not found"
    assert 'class="chat-search-wrap"' not in header.group(1)
    assert 'id="chat-search"' not in _HTML  # the fixed header input is gone


def test_household_chat_title_reads_zuhause():
    # The header title is context-aware (#671): "Geräte" on #/p/start
    # (matching the bottom tab, #677), "Zuhause" in the household chat, else the
    # session title — never the generic "Neuer Chat" placeholder on the
    # start/household views. All three live in the single syncChatTitle helper.
    sync = re.search(
        r"function syncChatTitle\(activeS\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert sync, "syncChatTitle not found"
    body = sync.group(1)
    assert '"Geräte"' in body
    assert '"#/p/start"' in body
    assert '"Zuhause"' in body
    assert (
        "topicsBySlug[HOUSEHOLD_TOPIC] && topicsBySlug[HOUSEHOLD_TOPIC].display_name"
        in body
    )
    # The plain default for non-household, non-portal chats is preserved.
    assert (
        'ct.textContent = (activeS && (activeS.title || activeS.preview)) || "Neuer Chat";'
        in body
    )


def test_admin_persona_choice_selects_admin_gateway():
    # The #293 admin profile is an admin-gated /persona choice whose value packs
    # the maintenance persona id, so a new chat under it routes to the admin
    # engine gateway server-side (the server re-checks Remote-Groups).
    assert 'var ADMIN_PERSONA = "servicebay-maintenance";' in _HTML
    # The admin entry is appended by personaChoices() only when isAdmin.
    choices = re.search(r"function personaChoices\(\) \{(.*?)\n      \}", _HTML, re.S)
    assert choices, "personaChoices not found"
    body = choices.group(1)
    assert "if (isAdmin) {" in body
    # The choice value carries the maintenance persona (so parsePersonaSpeed +
    # currentPersonality() send it as payload.personality → admin routing).
    assert 'value: ADMIN_PERSONA + "|none",' in body
    assert 'label: "Admin",' in body
    # isAdmin is established from whoami before the choice is offered.
    assert "function loadWhoami()" in _HTML


def test_standalone_thorough_dropdown_option_is_removed():
    # The separate "Solaris Gründlich" persona option is gone (#1121): there is
    # one everyday profile, and per-turn reasoning is the persona × speed cross.
    assert "function addDeepOption" not in _HTML
    assert "addDeepOption();" not in _HTML
    assert "Solaris Gründlich (12b)" not in _HTML
