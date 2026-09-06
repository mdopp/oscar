"""Frontend-contract checks for the per-turn LLM-step trace panel (#307).

Each assistant reply carries a "steps" panel — one row per model call showing
`model · wall_s · tokens`; clicking a step opens a modal with that call's exact
request/response, fetched through the chat server's `/__traces__/<id>`
pass-through (the #305 detail). The panel is loaded from the persisted per-turn
trace (`/api/sessions/<id>/trace`, #306) so it survives chat reload, with the
turns reconstructed by grouping consecutive same-`trace_id` steps and lined up
1:1 with the assistant bubbles in order. The real check is the box-verify of the
rendered panel + click-to-open; these assert the wiring.
"""

from __future__ import annotations

from solaris_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_panel_loaded_from_persisted_trace_endpoint():
    # Loaded from the #306 persistence endpoint, not just an in-memory live event.
    assert "function loadSessionTrace()" in _HTML
    assert 'fetch("/api/sessions/" + encodeURIComponent(sid) + "/trace")' in _HTML


def test_panel_loaded_on_open_and_after_each_turn():
    # Survives reload (loaded on session open) and updates after a live turn.
    assert _HTML.count("loadSessionTrace();") >= 2


def test_steps_grouped_by_trace_id_into_turns():
    # Consecutive same-trace_id steps form one turn; turns map to the solaris bubbles
    # in DOM order, so reopen renders the same per-turn traces in order.
    assert "function renderStepTrace(steps)" in _HTML
    assert "g.trace_id !== s.trace_id" in _HTML
    assert 'log.querySelectorAll(".msg.sol")' in _HTML


def test_step_row_shows_model_time_tokens():
    # Each model row: model · wall_s · combined in+out tokens (#406).
    assert 'Number(s.wall_s).toFixed(2) + "s"' in _HTML
    assert "function fmtTokens(s)" in _HTML
    assert "var toks = fmtTokens(s);" in _HTML
    assert "s.model" in _HTML


def test_step_row_shows_profile_badge():
    # Each row carries the engine profile that served the call, so household vs
    # admin turns are unambiguous in the trace.
    assert 'prof.className = "st-profile"' in _HTML
    assert "prof.textContent = s.profile" in _HTML


def test_clicking_a_step_opens_the_detail_modal():
    # A step click opens the exact-content modal via its detail_id, fetched from
    # the chat server's /__traces__/<id> pass-through (#305 detail).
    assert "function openTraceDetail(detailId)" in _HTML
    assert 'fetch("/__traces__/" + encodeURIComponent(detailId))' in _HTML
    assert "openTraceDetail(s.detail_id)" in _HTML
    assert '<div class="modal-backdrop" id="trace-modal" hidden>' in _HTML


def test_detail_modal_reads_nested_request_response_shape():
    # /__traces__/<id> returns {path, request:{model,tools,messages},
    # response:{final,tool_calls}} — the modal must read from the nested shape,
    # not flat d.model/d.tools/d.final (#316: flat reads always rendered empty).
    assert "var req = d.request || {};" in _HTML
    assert "var resp = d.response || {};" in _HTML
    assert "var msgs = req.messages || [];" in _HTML
    # #416: the modal renders structured collapsible sections via head()/sec()
    # instead of the old flat section() dump — the model headline, a tools[]
    # section, one section per message (role-driven), the response and tool_calls.
    assert 'head("model: " + req.model)' in _HTML
    assert 'sec("tools[" + req.tools.length + "]"' in _HTML
    assert "msgs.forEach(function (m) {" in _HTML
    assert 'sec(m.role || "message"' in _HTML
    assert 'sec("response", preview(resp.final), resp.final, true)' in _HTML
    assert 'sec("tool_calls[" + resp.tool_calls.length + "]"' in _HTML
    # No leftover flat-shape reads that the proxy never returns at top level.
    assert "d.model" not in _HTML
    assert "d.final" not in _HTML


def test_panel_not_double_appended_on_refetch():
    # Re-loading the trace (after each turn) must not stack a second panel on a
    # bubble that already has one.
    assert 'meta.querySelector("details.steptrace")' in _HTML


def test_tool_step_renders_as_tool_name_not_model_row(_HTML=_HTML):
    # #371: a persisted step_kind=tool step must render as the tool name + its
    # own wall_s — NOT as an LLM row, which produced the broken
    # `(model?) · — tok` (a tool step has no model/tokens). The renderer branches
    # on step_kind: tool → tool_name + wall_s; llm → model + tokens.
    assert 'if (s.step_kind === "tool")' in _HTML
    assert "s.tool_name" in _HTML
    # The branch returns before the model/tokens code, so a tool step never
    # reaches the `(model?)`/`— tok` LLM rendering.
    tool_branch = _HTML.split('if (s.step_kind === "tool")', 1)[1]
    tool_branch = tool_branch.split("return;", 1)[0]
    assert "(model?)" not in tool_branch
    assert '" tok"' not in tool_branch


def test_steps_panel_shown_collapsed_by_default(_HTML=_HTML):
    # #492: the per-step trace is collapsed by default. Users click to expand and
    # view individual LLM step details, with each step opening its own detail modal.
    panel = _HTML.split("function renderStepTrace(steps)", 1)[1].split(
        "return det;", 1
    )[0]
    assert "det.open = false;" in panel


def test_fmt_tokens_sums_in_and_out(_HTML=_HTML):
    # #406: a model step's token figure is prompt + completion as one `N tok`.
    fn = _HTML.split("function fmtTokens(s)", 1)[1].split("}\n", 1)[0]
    assert "s.prompt_tokens" in fn
    assert "s.completion_tokens" in fn
    assert 'return total + " tok";' in fn


def test_redundant_bottom_trace_block_removed(_HTML=_HTML):
    # #406: the separate bottom latency-waterfall block (#225) is folded away —
    # metaLine no longer renders it and the renderTrace helper + its event/CSS
    # are gone, leaving the single step list as the only trace surface.
    assert "function renderTrace(trace)" not in _HTML
    assert 'event === "trace"' not in _HTML
    assert "details.trace" not in _HTML
    # metaLine takes only (when, tookMs) now — no trace argument.
    assert "function metaLine(when, tookMs)" in _HTML


def test_live_bubble_removed_on_finish_so_one_step_list(_HTML=_HTML):
    # #406: on turn finish the live activity bubble is dropped, so the persisted
    # rich step list (loadSessionTrace) is the single per-turn trace — not the
    # terse live bubble AND a separate step panel on the same turn.
    finish = _HTML.split("finish: function ()", 1)[1].split("},", 1)[0]
    assert "el.removeChild(bubble)" in finish


def test_live_bubble_removed_on_stopped_so_no_ghost(_HTML=_HTML):
    # #414: a stopped turn must also drop the live activity bubble (mirrors the
    # finish() guard from #406), or a turn cancelled with tool-step rows leaves a
    # ghost bubble in the DOM alongside the standalone stopped message.
    stopped = _HTML.split("stopped: function ()", 1)[1].split("fail: function", 1)[0]
    assert "el.removeChild(bubble)" in stopped
