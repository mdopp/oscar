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


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


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


def load_all(skills_dir: pathlib.Path | str) -> list[SkillEntry]:
    """Walk `skills_dir`, return one SkillEntry per `<dir>/SKILL.md`.

    Files that lack frontmatter or fail to parse are skipped silently —
    we'd rather drop one broken skill from the help output than crash
    the whole list. Caller can re-parse explicitly via `describe()`.
    """
    root = pathlib.Path(skills_dir)
    if not root.is_dir():
        return []
    entries: list[SkillEntry] = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            entries.append(_parse(skill_md))
        except (ValueError, OSError):
            continue
    return entries


def describe(skills_dir: pathlib.Path | str, name: str) -> SkillEntry | None:
    for entry in load_all(skills_dir):
        if entry.name == name:
            return entry
    return None


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
