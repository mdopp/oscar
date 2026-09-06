"""Assembled household prompt stays within its two budgets (#643, #1336).

The stated soul budget lives in profiles.py:4. The static core the household
soul contributes — the shipped SOUL.md plus the pinned-last _TOOL_DISCIPLINE —
is asserted here under a conservative ~4-chars/token estimate (the same
convention as store.truncate_session_head). A ~1000-token allowance is left for
the dynamic entity-registry block + identity that _system_prompt() appends at
runtime, inside the 3k ceiling.

The second budget is the whole turn-1 prefill, which is what the box actually
pays. #1291 pushed it to 8202-8207 real tokens (baseline ~7800, tolerance +200)
while the char estimator read 7871 for the same prompt — German tokenizes ~4%
heavier than the estimator's chars/3.65. So the real bound is asserted here by
converting the estimate with that measured ratio; `scripts/bench_tool_decision.py`
reports llama-server's own `prompt_tokens` against the same 8000 on the box.

Must NOT import alembic (it lives only in database/; CI's solaris-chat env has
none — #378).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from solaris_chat.engine.client import _TOOL_DISCIPLINE
from solaris_chat.engine.tools import estimate_tokens

_STATIC_CORE_BUDGET_TOKENS = 2000
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_models.py"

# The box budget the whole prefill must stay under, and the estimator→real
# factor measured on the box on 2026-09-06 (8205 real tokens for a prompt the
# estimator scored 7871). Re-measure both together, never one alone.
_PREFILL_BUDGET_TOKENS = 8000
_REAL_PER_ESTIMATED = 8205 / 7871


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_models", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def household(bench):
    return bench.build_household("http://127.0.0.1:11434")


def _shipped_soul() -> str:
    pack = (
        Path(__file__).resolve().parents[2]
        / "templates"
        / "solaris"
        / "skills"
        / "household"
    )
    return (pack / "SOUL.md").read_text(encoding="utf-8")


def test_household_static_prompt_within_budget():
    soul_est = len(_shipped_soul()) // 4
    discipline_est = len(_TOOL_DISCIPLINE) // 4
    total = soul_est + discipline_est
    assert total <= _STATIC_CORE_BUDGET_TOKENS, (
        f"soul {soul_est} + discipline {discipline_est} = {total} est-tok "
        f"> {_STATIC_CORE_BUDGET_TOKENS} (leaves <1k for registry+identity in 3k)"
    )


def test_assembled_prefill_within_the_box_budget(bench, household):
    system = bench.build_system(household)
    est = estimate_tokens(len(system) + household._profile.toolbox.schema_chars())
    real = est * _REAL_PER_ESTIMATED
    assert real <= _PREFILL_BUDGET_TOKENS, (
        f"{est} est-tok ≈ {real:.0f} real tok from "
        f"{len(household._profile.toolbox.names())} tools "
        f"> {_PREFILL_BUDGET_TOKENS} — trim a tool description, don't raise this"
    )
