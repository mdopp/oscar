"""Frontmatter parsing + constraint checks.

Same flat-YAML approach as `oscar_help.core` — purpose-built parser
for the SKILL.md shape, no PyYAML dep. We keep the two parsers separate
so neither library has to import the other.
"""

from __future__ import annotations

import re


PROTECTED_FRONTMATTER_FIELDS: tuple[str, ...] = ("name", "description")
ADMIN_TAG = "admin"
_FRONTMATTER = re.compile(r"\A\s*---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class ValidationError(ValueError):
    """Constraint violation in a proposed skill edit."""


def parse_frontmatter(text: str) -> dict[str, object]:
    """Return a flat dict of the known fields (best-effort, never raises)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}
    block = m.group(1)
    return {
        "name": _scalar(block, "name"),
        "description": _scalar(block, "description"),
        "version": _scalar(block, "version", default="0.0.0"),
        "tags": _hermes_list(block, "tags"),
        "related_skills": _hermes_list(block, "related_skills"),
    }


def validate_edit(proposed: str, current: str | None) -> None:
    """Raise ValidationError if the edit violates a constraint.

    - `current is None` → creating a new skill. Frontmatter must parse,
      name + description must be non-empty, tags must not contain
      `admin`.
    - `current` set → editing an existing skill. Protected fields can't
      change; tags can change but can't gain `admin`.
    """
    p = parse_frontmatter(proposed)
    if not p or not p.get("name") or not p.get("description"):
        raise ValidationError("proposed file missing required frontmatter fields")

    if ADMIN_TAG in (p.get("tags") or ()):
        raise ValidationError(
            f"refusing to apply skill carrying the {ADMIN_TAG!r} tag via skill-author"
        )

    if current is None:
        return  # new skill, frontmatter validated above

    c = parse_frontmatter(current)
    if not c:
        raise ValidationError("existing file has no parseable frontmatter")

    if ADMIN_TAG in (c.get("tags") or ()):
        raise ValidationError(
            f"existing skill carries the {ADMIN_TAG!r} tag — admin-skill edits go through a different gate"
        )

    for field in PROTECTED_FRONTMATTER_FIELDS:
        if c.get(field) != p.get(field):
            raise ValidationError(
                f"{field!r} is immutable via skill-author (was {c.get(field)!r}, proposed {p.get(field)!r})"
            )


def _scalar(block: str, key: str, *, default: str = "") -> str:
    for line in block.splitlines():
        if not line or line.startswith((" ", "\t", "#")):
            continue
        head, _, value = line.partition(":")
        if head.strip() == key:
            return value.strip()
    return default


def _hermes_list(block: str, key: str) -> tuple[str, ...]:
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
