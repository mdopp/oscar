"""Tests for the SKILL.md introspection parser."""

from __future__ import annotations

import textwrap
import pathlib

import pytest

from oscar_help.core import describe, filter_by_tag, load_all


def _write_skill(root: pathlib.Path, dirname: str, body: str) -> None:
    (root / dirname).mkdir(parents=True, exist_ok=True)
    (root / dirname / "SKILL.md").write_text(textwrap.dedent(body), encoding="utf-8")


def test_load_all_parses_well_formed_skill(tmp_path):
    _write_skill(
        tmp_path,
        "light",
        """
        ---
        name: oscar-light
        description: Turn lights on or off.
        version: 0.1.0
        author: OSCAR
        license: MIT
        metadata:
          hermes:
            tags: [home, light, phase-0]
            related_skills: [oscar-timer]
        ---

        # OSCAR — Light
        """,
    )
    entries = load_all(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e.name == "oscar-light"
    assert "Turn lights" in e.description
    assert "phase-0" in e.tags
    assert e.related_skills == ("oscar-timer",)


def test_load_all_skips_broken_frontmatter(tmp_path):
    _write_skill(tmp_path, "ok", "---\nname: ok\ndescription: fine\n---\n")
    _write_skill(tmp_path, "broken", "no frontmatter here\n")
    _write_skill(tmp_path, "partial", "---\nname: only-name\n---\n")
    entries = load_all(tmp_path)
    assert [e.name for e in entries] == ["ok"]


def test_describe_returns_one_entry(tmp_path):
    _write_skill(
        tmp_path,
        "timer",
        """
        ---
        name: oscar-timer
        description: Relative-duration reminders.
        metadata:
          hermes:
            tags: [time, phase-0]
        ---
        """,
    )
    entry = describe(tmp_path, "oscar-timer")
    assert entry is not None
    assert entry.tags == ("time", "phase-0")
    assert describe(tmp_path, "nope") is None


def test_filter_by_tag(tmp_path):
    _write_skill(
        tmp_path,
        "a",
        "---\nname: a\ndescription: x\nmetadata:\n  hermes:\n    tags: [phase-0]\n---\n",
    )
    _write_skill(
        tmp_path,
        "b",
        "---\nname: b\ndescription: y\nmetadata:\n  hermes:\n    tags: [phase-1, admin]\n---\n",
    )
    entries = load_all(tmp_path)
    assert [e.name for e in filter_by_tag(entries, "phase-0")] == ["a"]
    assert [e.name for e in filter_by_tag(entries, "admin")] == ["b"]


def test_missing_dir_returns_empty(tmp_path):
    assert load_all(tmp_path / "nonexistent") == []


def test_layered_dirs_local_overrides_public(tmp_path):
    """Same name in both dirs → local wins, but appears at the original position."""
    public = tmp_path / "public"
    local = tmp_path / "local"
    _write_skill(
        public,
        "light",
        "---\nname: oscar-light\ndescription: PUBLIC version.\n---\n",
    )
    _write_skill(
        public,
        "timer",
        "---\nname: oscar-timer\ndescription: timer.\n---\n",
    )
    _write_skill(
        local,
        "light",
        "---\nname: oscar-light\ndescription: LOCAL version.\n---\n",
    )
    entries = load_all([public, local])
    by_name = {e.name: e for e in entries}
    assert by_name["oscar-light"].description == "LOCAL version."
    # local-version's path lives under the local dir
    assert "local" in str(by_name["oscar-light"].path)
    # timer is unchanged
    assert by_name["oscar-timer"].description == "timer."


def test_layered_dirs_local_can_add_new_skill(tmp_path):
    public = tmp_path / "public"
    local = tmp_path / "local"
    _write_skill(public, "a", "---\nname: a\ndescription: public-a.\n---\n")
    _write_skill(local, "b", "---\nname: b\ndescription: local-b.\n---\n")
    entries = load_all([public, local])
    names = {e.name for e in entries}
    assert names == {"a", "b"}


def test_layered_dirs_missing_local_is_ignored(tmp_path):
    """Missing local dir shouldn't blow up — just falls back to public only."""
    public = tmp_path / "public"
    _write_skill(public, "a", "---\nname: a\ndescription: x.\n---\n")
    entries = load_all([public, tmp_path / "does-not-exist"])
    assert [e.name for e in entries] == ["a"]


def test_single_path_arg_still_works(tmp_path):
    """Back-compat: passing one path (not a list) keeps the old signature."""
    _write_skill(tmp_path, "a", "---\nname: a\ndescription: x.\n---\n")
    entries = load_all(tmp_path)
    assert [e.name for e in entries] == ["a"]


def test_real_registry_is_parseable():
    """Every committed SKILL.md must parse — guards against drift."""
    repo_skills = pathlib.Path(__file__).resolve().parents[3] / "skills"
    if not repo_skills.exists():
        pytest.skip("skills/ not visible from this checkout")
    entries = load_all(repo_skills)
    assert entries, "expected at least one skill to parse from skills/"
    for entry in entries:
        assert entry.name, f"empty name for {entry.path}"
        assert entry.description, f"empty description for {entry.path}"
