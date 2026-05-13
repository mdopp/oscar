"""Parse SKILL.md frontmatter from a skill-registry directory.

We bring a tiny YAML-subset parser instead of PyYAML — the frontmatter
shape is fully under our control (flat `key: value` plus the
`metadata.hermes.{tags,related_skills}` block) and adding a transitive
dep just for that is overkill.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from typing import Iterable


_FRONTMATTER = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclasses.dataclass(frozen=True)
class SkillEntry:
    name: str
    description: str
    version: str
    tags: tuple[str, ...]
    related_skills: tuple[str, ...]
    path: pathlib.Path

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "tags": list(self.tags),
            "related_skills": list(self.related_skills),
            "path": str(self.path),
        }


def load_all(
    skills_dir: pathlib.Path | str | Iterable[pathlib.Path | str],
) -> list[SkillEntry]:
    """Walk one or more skill-registry dirs, return one SkillEntry per skill.

    Accepts either a single path (back-compat) or an iterable of paths.
    When multiple dirs are given they're treated as layers in order:
    later layers shadow earlier ones on `name:` collision. This is what
    lets the local writable `skills-local/` dir override defaults from
    the public repo without copying the whole file.

    Files that lack frontmatter or fail to parse are skipped silently —
    we'd rather drop one broken skill from the help output than crash
    the whole list. Caller can re-parse explicitly via `describe()`.
    """
    dirs = _coerce_dirs(skills_dir)
    merged: dict[str, SkillEntry] = {}
    order: list[str] = []
    for root in dirs:
        for skill_md in sorted(root.glob("*/SKILL.md")):
            try:
                entry = _parse(skill_md)
            except (ValueError, OSError):
                continue
            if entry.name not in merged:
                order.append(entry.name)
            merged[entry.name] = entry
    return [merged[name] for name in order]


def describe(
    skills_dir: pathlib.Path | str | Iterable[pathlib.Path | str],
    name: str,
) -> SkillEntry | None:
    for entry in load_all(skills_dir):
        if entry.name == name:
            return entry
    return None


def _coerce_dirs(
    skills_dir: pathlib.Path | str | Iterable[pathlib.Path | str],
) -> list[pathlib.Path]:
    """Normalize the input to a list of existing directories."""
    if isinstance(skills_dir, (str, pathlib.Path)):
        candidates = [skills_dir]
    else:
        candidates = list(skills_dir)
    paths: list[pathlib.Path] = []
    for c in candidates:
        p = pathlib.Path(c)
        if p.is_dir():
            paths.append(p)
    return paths


def filter_by_tag(entries: Iterable[SkillEntry], tag: str) -> list[SkillEntry]:
    return [e for e in entries if tag in e.tags]


def _parse(path: pathlib.Path) -> SkillEntry:
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER.match(text)
    if not m:
        raise ValueError(f"{path}: no frontmatter")
    block = m.group(1)

    name = _scalar(block, "name")
    description = _scalar(block, "description")
    version = _scalar(block, "version", default="0.0.0")
    tags = _hermes_list(block, "tags")
    related = _hermes_list(block, "related_skills")
    if not name or not description:
        raise ValueError(f"{path}: missing name or description")

    return SkillEntry(
        name=name,
        description=description,
        version=version,
        tags=tags,
        related_skills=related,
        path=path,
    )


def _scalar(block: str, key: str, *, default: str = "") -> str:
    """Match `key: value` at the top level only (no indent)."""
    for line in block.splitlines():
        if not line or line.startswith((" ", "\t", "#")):
            continue
        head, _, value = line.partition(":")
        if head.strip() == key:
            return value.strip()
    return default


def _hermes_list(block: str, key: str) -> tuple[str, ...]:
    """Pull a bracketed list `key: [a, b, c]` from the nested hermes block."""
    in_hermes = False
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.startswith(" ") and ":" in line:
            in_hermes = line.split(":", 1)[0].strip() == "metadata"
        if not in_hermes:
            continue
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            value = stripped[len(key) + 1 :].strip()
            if value.startswith("[") and value.endswith("]"):
                items = [v.strip() for v in value[1:-1].split(",")]
                return tuple(v for v in items if v)
    return ()
