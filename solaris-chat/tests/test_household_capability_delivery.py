"""The four behaviour packs reach residents as tools, not as prompt text (#1291).

`media`, `notes-search`, `dynamic-skills` and `status` were the only household
packs still written as model-facing prose, and no household turn ever read them:
`_skills_prompt()` is wired to the admin profile alone. Injecting them would have
cost ~10.6k prefill tokens on a home box. The operator's 2026-09-06 decision is
the same shape the other 19 packs already have — the tool binding plus a short
SOUL pointer — so these pin both halves: the capabilities exist on the household
toolbox, and none of the pack prose reaches the prompt.

Must NOT import alembic (it lives only in database/; #378).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_models.py"
_PACKS = (
    Path(__file__).resolve().parents[2]
    / "templates"
    / "solaris"
    / "skills"
    / "household"
)

# The tools each converted pack's capability is delivered by.
CAPABILITY_TOOLS = {
    "media": ("play_music", "play_radio", "media_find_podcast"),
    "notes-search": ("notes_search", "notes_read"),
    "dynamic-skills": ("fact_store", "note_write"),
    "status": ("get_solaris_status",),
}

# #1336: having the tool was not enough. Measured on the box after #1291,
# "merk dir, dass der Müll dienstags kommt" hit fact_store 1 of 9 and "wie geht
# es dir" hit get_solaris_status 0 of 9, while the status pointer's own literal
# example "läuft alles" hit 3 of 3 — both halves were written as one example
# sentence rather than as the intent. The schema description is what the model
# weighs at call time, so it carries the phrasings; the SOUL pointer carries the
# intent behind them.
INTENT_PHRASINGS = {
    "fact_store": ("merk dir", "denk daran", "notier dir", "nicht vergessen"),
    "get_solaris_status": (
        "wie geht es dir",
        "bist du da",
        "läuft alles",
        "hast du probleme",
    ),
}
SOUL_POINTERS = {
    "get_solaris_status": ("befinden", "erreichbarkeit", "probleme gibt"),
    "fact_store": ("merk dir", "denk daran", "notier dir"),
}


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_models", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def household(bench):
    return bench.build_household("http://127.0.0.1:11434")


def _body(pack: str) -> str:
    text = (_PACKS / pack / "SKILL.md").read_text(encoding="utf-8")
    end = text.find("---", 3)
    return text[end + 3 :].strip()


@pytest.mark.parametrize("pack,tools", CAPABILITY_TOOLS.items())
def test_household_holds_the_capabilitys_tools(household, pack, tools):
    names = set(household._profile.toolbox.names())
    assert set(tools) <= names, f"{pack} is unreachable: {set(tools) - names} missing"


def test_promotion_stays_admin_only(household):
    """A resident may have a skill drafted for approval, never promoted."""
    names = set(household._profile.toolbox.names())
    assert not {"file_skill_approval", "check_skill_approval"} & names


def test_no_pack_prose_reaches_the_household_prompt(bench, household):
    assert not household._profile.extra_prompt
    system = bench.build_system(household)
    for pack in CAPABILITY_TOOLS:
        for line in _body(pack).splitlines():
            line = line.strip()
            if len(line) > 40:
                assert line not in system, f"{pack} prose is in the household prompt"


@pytest.mark.parametrize("pack", CAPABILITY_TOOLS)
def test_the_pack_no_longer_carries_model_facing_prose(pack):
    """The file stays (ServiceBay never deletes a delivered asset) but holds a
    pointer, not the procedure the model used to be meant to follow."""
    body = _body(pack)
    assert len(body) <= 900, f"{pack} is prose again ({len(body)} chars)"
    for heading in ("## When to use", "## Wann", "## Ablauf", "## Operating"):
        assert heading not in body, f"{pack} still carries a {heading} procedure"


@pytest.mark.parametrize("tool,phrasings", INTENT_PHRASINGS.items())
def test_the_tool_description_names_the_intent(household, tool, phrasings):
    definitions = {
        d["function"]["name"]: d for d in household._profile.toolbox.definitions()
    }
    description = definitions[tool]["function"]["description"].lower()
    for phrase in phrasings:
        assert phrase in description, f"{tool} description misses {phrase!r} (#1336)"


@pytest.mark.parametrize("tool,phrasings", SOUL_POINTERS.items())
def test_the_soul_pointer_names_the_intent(bench, tool, phrasings):
    soul = Path(bench.soul_path()).read_text(encoding="utf-8")
    assert tool in soul, f"{tool} has no SOUL pointer"
    lowered = soul.lower()
    for phrase in phrasings:
        assert phrase in lowered, f"{tool} pointer misses {phrase!r} (#1336)"
