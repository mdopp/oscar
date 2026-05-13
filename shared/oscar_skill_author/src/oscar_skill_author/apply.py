"""Filesystem + git mechanics for skill edits.

Side-effects:
- Writes `<skills_local>/<skill_name>/SKILL.md`.
- Runs `git add` + `git commit` inside `skills_local`.
- INSERTs into the `skill_edits` table (with the new commit's sha).

Revert path is symmetric: `git revert <git_sha>` + a `reverted_at`
UPDATE on the original `skill_edits` row + a new `skill_edits` row
recording the revert (source = original source).
"""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import pathlib
import subprocess
import uuid

import asyncpg
from oscar_logging import log

from .validation import validate_edit


@dataclasses.dataclass(frozen=True)
class ApplyResult:
    edit_id: str
    skill_name: str
    git_sha: str
    diff: str
    path: pathlib.Path

    def to_dict(self) -> dict[str, str]:
        return {
            "edit_id": self.edit_id,
            "skill_name": self.skill_name,
            "git_sha": self.git_sha,
            "diff": self.diff,
            "path": str(self.path),
        }


async def apply_edit(
    *,
    dsn: str,
    skills_local: pathlib.Path,
    skill_name: str,
    proposed_md: str,
    source: str,
    reason: str | None = None,
) -> ApplyResult:
    if source not in ("user", "reviewer"):
        raise ValueError(f"invalid source {source!r}")
    skill_dir = skills_local / _safe_dirname(skill_name)
    target = skill_dir / "SKILL.md"

    current_md = target.read_text(encoding="utf-8") if target.exists() else None
    validate_edit(proposed_md, current_md)
    diff = _diff(current_md or "", proposed_md, skill_name)

    skill_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(proposed_md, encoding="utf-8")

    msg_parts = [f"{source}: edit {skill_name}"]
    if reason:
        msg_parts.append(reason)
    _git_commit(skills_local, target, "\n\n".join(msg_parts))
    git_sha = _git_head_sha(skills_local)

    edit_id = await _record_edit(
        dsn=dsn, skill_name=skill_name, source=source, diff=diff, git_sha=git_sha
    )
    log.info(
        "skill_author.apply",
        edit_id=edit_id,
        skill=skill_name,
        source=source,
        sha=git_sha[:8],
        diff_lines=diff.count("\n"),
    )
    return ApplyResult(
        edit_id=edit_id, skill_name=skill_name, git_sha=git_sha, diff=diff, path=target
    )


async def revert_edit(
    *, dsn: str, skills_local: pathlib.Path, skill_name: str, n: int = 1
) -> ApplyResult:
    """Revert the n-th most recent unreverted edit of `skill_name`.

    Uses `git revert <sha>` so the action itself becomes a commit. The
    original `skill_edits` row's `reverted_at` is stamped; a new
    `skill_edits` row records the revert commit.
    """
    target_edit = await _nth_recent_unreverted(dsn=dsn, skill_name=skill_name, n=n)
    if target_edit is None:
        raise LookupError(f"no unreverted edits found for {skill_name!r}")

    _git_revert(skills_local, target_edit["git_sha"])
    revert_sha = _git_head_sha(skills_local)

    skill_file = skills_local / _safe_dirname(skill_name) / "SKILL.md"
    new_md = skill_file.read_text(encoding="utf-8") if skill_file.exists() else ""
    diff = _diff(target_edit.get("snapshot") or "", new_md, skill_name)

    await _mark_reverted(dsn=dsn, edit_id=str(target_edit["id"]))
    edit_id = await _record_edit(
        dsn=dsn,
        skill_name=skill_name,
        source=target_edit["source"],
        diff=f"REVERT of {target_edit['git_sha'][:8]}",
        git_sha=revert_sha,
    )
    log.info(
        "skill_author.revert",
        skill=skill_name,
        reverted_sha=target_edit["git_sha"][:8],
        new_sha=revert_sha[:8],
        edit_id=edit_id,
    )
    return ApplyResult(
        edit_id=edit_id,
        skill_name=skill_name,
        git_sha=revert_sha,
        diff=diff,
        path=skill_file,
    )


# ---- helpers ------------------------------------------------------------


def _safe_dirname(name: str) -> str:
    # We don't allow `..`, `/`, or empty — directory names mirror the
    # skill `name:` but must be filesystem-safe.
    cleaned = name.strip().replace("/", "_").replace("..", "_")
    if not cleaned:
        raise ValueError("empty skill name")
    return cleaned


def _diff(before: str, after: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
            n=3,
        )
    )


def _git(args: list[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _git_commit(skills_local: pathlib.Path, path: pathlib.Path, msg: str) -> None:
    _git(["add", str(path.relative_to(skills_local))], cwd=skills_local)
    _git(["commit", "-m", msg], cwd=skills_local)


def _git_revert(skills_local: pathlib.Path, sha: str) -> None:
    _git(["revert", "--no-edit", sha], cwd=skills_local)


def _git_head_sha(skills_local: pathlib.Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd=skills_local).stdout.strip()


async def _record_edit(
    *, dsn: str, skill_name: str, source: str, diff: str, git_sha: str
) -> str:
    edit_id = str(uuid.uuid4())
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            """
            INSERT INTO skill_edits (id, skill_name, source, diff, git_sha)
            VALUES ($1::uuid, $2, $3, $4, $5)
            """,
            edit_id,
            skill_name,
            source,
            diff,
            git_sha,
        )
    finally:
        await conn.close()
    return edit_id


async def _nth_recent_unreverted(
    *, dsn: str, skill_name: str, n: int
) -> dict[str, object] | None:
    if n < 1:
        raise ValueError("n must be >= 1")
    conn = await asyncpg.connect(dsn=dsn)
    try:
        row = await conn.fetchrow(
            """
            SELECT id, source, git_sha
            FROM skill_edits
            WHERE skill_name = $1 AND reverted_at IS NULL
              AND diff NOT LIKE 'REVERT of%'
            ORDER BY applied_at DESC
            OFFSET $2 LIMIT 1
            """,
            skill_name,
            n - 1,
        )
    finally:
        await conn.close()
    return dict(row) if row else None


async def _mark_reverted(*, dsn: str, edit_id: str) -> None:
    conn = await asyncpg.connect(dsn=dsn)
    try:
        await conn.execute(
            "UPDATE skill_edits SET reverted_at = now() WHERE id = $1::uuid", edit_id
        )
    finally:
        await conn.close()


# Lightweight sync wrappers so the CLI can call without re-implementing
# the asyncio.run boilerplate.


def apply_edit_sync(**kwargs) -> ApplyResult:
    return asyncio.run(apply_edit(**kwargs))


def revert_edit_sync(**kwargs) -> ApplyResult:
    return asyncio.run(revert_edit(**kwargs))
