"""A tombstoned definition reaches no registry.

ServiceBay's asset transport never deletes (mdopp/servicebay#2703), so a retired
definition stays on the box as a file forever; the only lever is to overwrite it.
`retired: true` is what makes that overwritten file inert — the engine must skip
it in every list, catalog and dispatch, not merely list it with an empty body.
"""

from __future__ import annotations

from pathlib import Path

from solaris_chat import skills

TOMBSTONE = (
    "---\nname: audit-query\ndescription: Retired.\nretired: true\n---\n\nRetired.\n"
)


def _pack(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    (root / "status").mkdir(parents=True)
    (root / "status").joinpath("SKILL.md").write_text(
        "---\nname: solaris-status\nkind: skill\ncommand: /status\n---\n\nProbe.\n",
        encoding="utf-8",
    )
    (root / "audit-query").mkdir()
    (root / "audit-query").joinpath("SKILL.md").write_text(TOMBSTONE, encoding="utf-8")
    return root


def test_a_tombstone_is_not_listed_in_any_registry(tmp_path):
    root = _pack(tmp_path)
    assert [s["id"] for s in skills.list_skills(root)] == ["status"]
    for kind in skills.KINDS:
        listed = [d["id"] for d in skills.list_defs(root, kind)]
        assert "audit-query" not in listed, f"tombstone listed in the {kind} registry"
    assert [d["id"] for d in skills.list_tool_defs(root)] == []


def test_a_tombstone_never_binds_a_hook_event(tmp_path):
    root = tmp_path / "skills"
    (root / "guest-onboarding").mkdir(parents=True)
    (root / "guest-onboarding").joinpath("SKILL.md").write_text(
        "---\nname: guest-onboarding\nkind: hook\nevent: guest-session-start\n"
        "retired: true\n---\n\nRetired.\n",
        encoding="utf-8",
    )
    assert skills.hooks_for_event(root, "guest-session-start") == []


def test_retired_flag_reads_the_documented_spellings():
    for value in ("true", "True", "yes", "1"):
        assert skills.is_retired({"retired": value})
    for value in ("", "false", "no"):
        assert not skills.is_retired({"retired": value})
    assert not skills.is_retired({})
