"""Filesystem + git mechanics, with Postgres mocked."""

from __future__ import annotations

import pathlib
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oscar_skill_author.apply import apply_edit, revert_edit


GOOD_NEW = """---
name: oscar-new
description: A brand-new skill.
metadata:
  hermes:
    tags: [phase-1]
---

# new skill body
"""


def _git_init(repo: pathlib.Path) -> None:
    """Tiny git repo for the apply path. Local config so commit works in CI."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / ".gitkeep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


@pytest.fixture
def db_mock():
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        yield conn


async def test_apply_new_skill_writes_file_and_commits(tmp_path, db_mock):
    _git_init(tmp_path)
    result = await apply_edit(
        dsn="dsn",
        skills_local=tmp_path,
        skill_name="oscar-new",
        proposed_md=GOOD_NEW,
        source="user",
        reason="just because",
    )
    written = (tmp_path / "oscar-new" / "SKILL.md").read_text()
    assert written == GOOD_NEW
    assert result.skill_name == "oscar-new"
    assert len(result.git_sha) == 40
    # diff against empty 'before' contains the new content.
    assert "+name: oscar-new" in result.diff


async def test_apply_edit_to_existing_skill_keeps_frontmatter_invariant(
    tmp_path, db_mock
):
    _git_init(tmp_path)
    skill_dir = tmp_path / "oscar-existing"
    skill_dir.mkdir()
    existing = (
        "---\nname: oscar-existing\ndescription: Keeps doing its job.\n"
        "metadata:\n  hermes:\n    tags: [phase-1]\n---\n\n# body v1\n"
    )
    (skill_dir / "SKILL.md").write_text(existing)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)

    edited = existing.replace("# body v1", "# body v2 with more detail")
    result = await apply_edit(
        dsn="dsn",
        skills_local=tmp_path,
        skill_name="oscar-existing",
        proposed_md=edited,
        source="reviewer",
    )
    assert "# body v2" in (skill_dir / "SKILL.md").read_text()
    assert "v2 with more detail" in result.diff


async def test_apply_rejects_description_change(tmp_path, db_mock):
    _git_init(tmp_path)
    skill_dir = tmp_path / "oscar-existing"
    skill_dir.mkdir()
    existing = (
        "---\nname: oscar-existing\ndescription: original purpose.\n---\n# body\n"
    )
    (skill_dir / "SKILL.md").write_text(existing)
    bad = existing.replace("original purpose.", "totally different purpose.")
    with pytest.raises(Exception, match="description"):
        await apply_edit(
            dsn="dsn",
            skills_local=tmp_path,
            skill_name="oscar-existing",
            proposed_md=bad,
            source="user",
        )


async def test_apply_rejects_admin_tagged_proposal(tmp_path, db_mock):
    _git_init(tmp_path)
    body = (
        "---\nname: oscar-evil\ndescription: tries.\n"
        "metadata:\n  hermes:\n    tags: [admin]\n---\n# x\n"
    )
    with pytest.raises(Exception, match="admin"):
        await apply_edit(
            dsn="dsn",
            skills_local=tmp_path,
            skill_name="oscar-evil",
            proposed_md=body,
            source="user",
        )


async def test_revert_picks_up_most_recent_edit(tmp_path):
    """Mock skill_edits returning a real prior sha; verify git revert runs."""
    _git_init(tmp_path)
    skill_dir = tmp_path / "oscar-x"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: oscar-x\ndescription: x.\n---\n# v1\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v1"], cwd=tmp_path, check=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: oscar-x\ndescription: x.\n---\n# v2 — to-be-reverted\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v2"], cwd=tmp_path, check=True)
    v2_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()

    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": "11111111-1111-1111-1111-111111111111",
            "source": "user",
            "git_sha": v2_sha,
        }
    )
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    with patch("asyncpg.connect", AsyncMock(return_value=conn)):
        result = await revert_edit(
            dsn="dsn", skills_local=tmp_path, skill_name="oscar-x", n=1
        )

    after = (skill_dir / "SKILL.md").read_text()
    assert "v1" in after
    assert "v2 — to-be-reverted" not in after
    assert result.git_sha != v2_sha
