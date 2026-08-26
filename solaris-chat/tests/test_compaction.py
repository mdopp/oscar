"""Tests for chat compaction (#210): the threshold trigger logic and the
extract-before-compact ordering."""

from __future__ import annotations

import pytest

from solaris_chat import compaction


# --- Threshold trigger -----------------------------------------------------


def test_usage_fraction_sums_input_and_output():
    s = {"input_tokens": 6000, "output_tokens": 2000}
    assert compaction.usage_fraction(s, 10000) == pytest.approx(0.8)


def test_usage_fraction_unknown_is_none():
    # No token totals at all -> unknown, never 0 (don't compact on a guess).
    assert compaction.usage_fraction({}, 10000) is None
    assert compaction.usage_fraction({"input_tokens": None}, 10000) is None


def test_usage_fraction_tolerates_one_side_missing():
    assert compaction.usage_fraction({"input_tokens": 5000}, 10000) == pytest.approx(
        0.5
    )


def test_usage_fraction_zero_window_is_none():
    assert compaction.usage_fraction({"input_tokens": 100}, 0) is None


def test_needs_compaction_at_and_over_threshold():
    cw = 10000  # round so the boundary is exactly representable
    over = {"input_tokens": 9500, "output_tokens": 0}  # 0.95
    at = {"input_tokens": 9000, "output_tokens": 0}  # exactly 0.90 (>= inclusive)
    under = {"input_tokens": 5000, "output_tokens": 0}  # 0.50
    assert compaction.needs_compaction(over, cw, 0.90) is True
    assert compaction.needs_compaction(at, cw, 0.90) is True
    assert compaction.needs_compaction(under, cw, 0.90) is False


def test_needs_compaction_unknown_usage_is_false():
    # An unknown usage must never trigger compaction.
    assert compaction.needs_compaction({}, 32768, 0.90) is False


# --- Extract prompt skips trivial / device-control turns (#250) -------------


def test_extract_prompt_skips_device_control_and_tool_turns():
    # A pure device-control turn (e.g. "schalte licht an" / "welche Lichter sind
    # an") must not become a stored fact: the prompt names device-control,
    # tool-call and trivial-confirmation turns as explicit skip cases.
    p = compaction.EXTRACT_PROMPT.lower()
    assert "skip" in p
    assert "device control" in p
    assert "tool call" in p
    assert "trivial" in p


def test_extract_prompt_does_not_memorise_ha_derivable_mappings():
    # device/room/entity mappings + device state live in Home Assistant; the
    # prompt must NOT invite storing them, and must say so explicitly.
    p = compaction.EXTRACT_PROMPT.lower()
    assert "do not memorise device/room/entity mappings" in p
    assert "home assistant" in p
    # The old invitation that caused the bug must be gone.
    assert "store every durable, reusable learning" not in p


def test_extract_prompt_still_targets_substantive_knowledge():
    # A substantive, informative turn must still be extractable: the prompt
    # keeps the genuine durable-learning categories so real conversations are
    # not silenced by the trivial-turn guard.
    p = compaction.EXTRACT_PROMPT.lower()
    assert "fact_store" in p
    for category in ("facts", "decisions", "household preferences", "routines"):
        assert category in p


# --- Extract-before-compact ordering ---------------------------------------


class _RecordingEngine:
    """Records the order of chat turns and create_session calls so we can
    assert extract precedes summary precedes the continuation create."""

    def __init__(self, *, session, summary="continue here", fail_on=None):
        self._session = session
        self._summary = summary
        self._fail_on = fail_on  # one of "extract", "summary", "create"
        self.calls: list[str] = []
        self.created_prompts: list[str] = []
        self.created_titles: list[str] = []
        # The confirm gate's stash, keyed like the real client's (#1247).
        self.pending: dict[str, object] = {}

    def detach_pending(self, key):
        return self.pending.pop(key, None)

    def attach_pending(self, key, held):
        self.pending[key] = held

    async def get_session(self, session_id, uid):
        return dict(self._session) if self._session is not None else None

    async def chat(self, session_id, text, images=None, reasoning_effort="none"):
        if text == compaction.EXTRACT_PROMPT:
            if self._fail_on == "extract":
                raise RuntimeError("extract boom")
            self.calls.append("extract")
            return "stored 3 facts"
        if text == compaction.SUMMARY_PROMPT:
            if self._fail_on == "summary":
                raise RuntimeError("summary boom")
            self.calls.append("summary")
            return self._summary
        raise AssertionError(f"unexpected chat text: {text!r}")

    async def create_session(
        self, uid, system_prompt=None, *, maintenance=False, title=""
    ):
        if self._fail_on == "create":
            raise RuntimeError("create boom")
        self.calls.append("create")
        self.created_prompts.append(system_prompt or "")
        self.created_titles.append(title)
        return "cont-1"


_OVER = {"id": "s1", "input_tokens": 31000, "output_tokens": 1000}  # ~0.98 of 32768


async def test_compaction_extracts_before_summary_before_create():
    h = _RecordingEngine(session=_OVER)
    new_id = await compaction.compact_session(
        h, "mdopp", "s1", base_system_prompt="", context_window=32768
    )
    assert new_id == "cont-1"
    # The whole point: learnings are stored FIRST, summary second, the
    # continuation opened last — nothing dropped before memory is written.
    assert h.calls == ["extract", "summary", "create"]


async def test_compaction_seeds_continuation_with_summary_and_overlay():
    h = _RecordingEngine(session=_OVER, summary="we discussed the boiler")
    await compaction.compact_session(
        h, "mdopp", "s1", base_system_prompt="Be concise.", context_window=32768
    )
    seed = h.created_prompts[0]
    assert "Be concise." in seed  # the personality overlay is preserved
    assert "we discussed the boiler" in seed  # the compacted summary is seeded
    assert "memory" in seed.lower()  # points at the stored learnings


async def test_compaction_continuation_title_is_unique_not_bare_marker(monkeypatch):
    # #267: the continuation must NOT be created with a bare uid marker as its
    # title — the engine enforces title uniqueness, so a bare marker collides with
    # an abandoned stub session and 400s, stalling compaction. The continuation
    # gets a non-empty, unique human suffix so two continuations never clash.
    from solaris_chat import marker

    h = _RecordingEngine(session=_OVER)
    await compaction.compact_session(h, "mdopp", "s1", context_window=32768)
    title = h.created_titles[0]
    assert title  # a human suffix is passed (not the empty default = bare marker)
    assert title.strip() != marker.marker_for("mdopp").strip()

    # Two continuations opened at different times must get distinct titles, so a
    # later compaction can never collide with this one's persisted session.
    class _Clock:
        n = 0

        @classmethod
        def now(cls):
            from datetime import datetime, timedelta

            cls.n += 1
            return datetime(2026, 6, 8, 12, 0, 0) + timedelta(seconds=cls.n)

    monkeypatch.setattr(compaction, "datetime", _Clock)
    h2 = _RecordingEngine(session=_OVER)
    await compaction.compact_session(h2, "mdopp", "s1", context_window=32768)
    await compaction.compact_session(h2, "mdopp", "s1", context_window=32768)
    assert h2.created_titles[0] != h2.created_titles[1]


async def test_compaction_skips_when_under_threshold():
    under = {"id": "s1", "input_tokens": 1000, "output_tokens": 0}
    h = _RecordingEngine(session=under)
    new_id = await compaction.compact_session(h, "mdopp", "s1", context_window=32768)
    assert new_id is None
    assert h.calls == []  # no extract, no summary, no create


async def test_compaction_force_ignores_threshold():
    under = {"id": "s1", "input_tokens": 10, "output_tokens": 0}
    h = _RecordingEngine(session=under)
    new_id = await compaction.compact_session(
        h, "mdopp", "s1", context_window=32768, force=True
    )
    assert new_id == "cont-1"
    assert h.calls == ["extract", "summary", "create"]


async def test_compaction_aborts_if_extract_fails_no_create():
    # A failed extraction must abort: never summarise/continue past a failed
    # learning-store (that would risk losing durable facts).
    h = _RecordingEngine(session=_OVER, fail_on="extract")
    new_id = await compaction.compact_session(h, "mdopp", "s1", context_window=32768)
    assert new_id is None
    assert "create" not in h.calls


async def test_compaction_missing_session_is_noop():
    h = _RecordingEngine(session=None)
    new_id = await compaction.compact_session(
        h, "mdopp", "missing", context_window=32768
    )
    assert new_id is None
    assert h.calls == []
