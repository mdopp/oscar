"""Read the Solaris skill pack off the filesystem.

the engine's `/v1/skills` lists name + description only; `/v1/skills/{name}`
404s — there is no body API — so the panel reads the markdown straight off
the bind-mounted pack (`SKILLS_DIR`, the host `solarisbay/skills` dir mounted
read-only for reads). This is the shipped *standard set*: everyone reads,
only admins edit. Each skill is `<dir>/<name>/SKILL.md` with a YAML
frontmatter block (name/description/version) and a markdown body.

Admin edits write the raw SKILL.md back through a read-write mount of the
same pack (the skills dir is host-owned, so the chat pod — rootless,
container-root → the host user — can replace files there). The engine reads a
skill *body* live; a frontmatter change (name/description) needs an engine
restart to re-register, which the caller surfaces.

A skill id is its directory name (filesystem-safe, stable). We never accept
a path separator in an id, so a request can't escape the pack.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a `---`-delimited YAML frontmatter head from the markdown body.

    A deliberately small parser (no PyYAML dep): the pack's frontmatter is
    flat `key: value` scalars. Unknown/complex lines are ignored; the body
    is everything after the closing `---`.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        meta[key.strip()] = value.strip().strip("'\"")
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return meta, body


def _is_valid_id(skill_id: str) -> bool:
    return bool(skill_id) and "/" not in skill_id and skill_id not in (".", "..")


# A definition's `kind` (#480): only `skill` belongs in the model-selectable
# pool; the other three fire deterministically (user `/`, the clock, an event).
# `tool` (#1004, ADR 0011) is a declarative `.`-command plugin — a dot-command,
# a list cell-schema, and the action ids the server auto-registers on load.
KINDS = ("skill", "command", "hook", "scheduler", "tool")
# A definition without an explicit `kind:` frontmatter is a skill — the legacy
# default before the taxonomy split, so the existing pack stays valid.
_DEFAULT_KIND = "skill"
_DEFAULT_SCOPE = "household"


def def_kind(meta: dict[str, str]) -> str:
    kind = meta.get("kind", "").strip().lower()
    return kind if kind in KINDS else _DEFAULT_KIND


def _iter_defs(skills_dir: str | Path):
    """Yield `(id, meta, body, file)` for every `<id>/SKILL.md` in the pack."""
    root = Path(skills_dir)
    if not root.is_dir():
        return
    for child in root.iterdir():
        skill_file = child / "SKILL.md"
        if not child.is_dir() or not skill_file.is_file():
            continue
        meta, body = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
        yield child.name, meta, body, skill_file


def list_defs(skills_dir: str | Path, kind: str) -> list[dict[str, str]]:
    """List one kind's registry: `[{id, name, description, kind, scope}]`,
    sorted by name. The per-kind walk that replaces the single flat list —
    `kind` is read from each definition's frontmatter (default `skill`)."""
    out: list[dict[str, str]] = []
    for def_id, meta, _body, _file in _iter_defs(skills_dir):
        if def_kind(meta) != kind:
            continue
        out.append(
            {
                "id": def_id,
                "name": meta.get("name") or def_id,
                "description": meta.get("description", ""),
                "kind": kind,
                "scope": meta.get("scope") or _DEFAULT_SCOPE,
                # The inline `/`-trigger and its prompt-line hint for a typeable
                # command (set on command-kind defs and the dual status/notes/
                # audit skills); empty for the rest. Drives the `/commands` card
                # and the slash-pool entry.
                "command": meta.get("command", ""),
                "argument-hint": meta.get("argument-hint", ""),
                # The 5-field cron a scheduler-kind entry fires on; drives the
                # `/scheduler` card's cron-time picker, empty for the rest.
                "schedule": meta.get("schedule", ""),
                # The lifecycle event a hook-kind entry binds to; drives the
                # `/hooks` card's event selector, empty for the rest.
                "event": meta.get("event", ""),
            }
        )
    out.sort(key=lambda s: s["name"].lower())
    return out


# The `tool-cell-schema` contract is renderer-AGNOSTIC (#1022, ADR 0011): the
# schema maps item fields to SEMANTIC ROLES, never to markup — so a DOM renderer
# (`renderListCell`) and a non-browser one (Android RemoteViews: no HTML/CSS/JS,
# a fixed set of view types, click → PendingIntent) can both render it. The
# closed role vocabulary a role maps a field to; `actions` references action ids
# only. Anything outside this is browser-only and native must skip/degrade — so
# we forbid it here rather than let it silently break a native consumer.
_CELL_SCHEMA_STRING_ROLES = ("title", "subtitle", "badge", "state", "icon")
_CELL_SCHEMA_LIST_ROLES = ("meta", "actions")
_CELL_SCHEMA_ROLES = _CELL_SCHEMA_STRING_ROLES + _CELL_SCHEMA_LIST_ROLES

# `tool-action-params` (#1214): per action id, `param name → source`, where a
# source is either an item field reference (`$`-prefixed — read the field of that
# name off the rendered row) or a literal string. The `$` marker is what makes the
# two unambiguous; without it `{"status": "done"}` could mean either. Nothing
# nests beyond that, so the whole map stays one JSON line in the pack's flat,
# no-PyYAML frontmatter.
_ACTION_PARAM_FIELD_PREFIX = "$"


def _action_param_violations(action_id: str, mapping: Any) -> list[str]:
    if not isinstance(mapping, dict) or not mapping:
        return [f"action '{action_id}' declares no params (tool-action-params)"]
    out: list[str] = []
    for param, source in mapping.items():
        if not isinstance(param, str) or not param:
            out.append(f"action '{action_id}' has a non-string / empty param name")
        elif not isinstance(source, str) or not source:
            out.append(f"action '{action_id}' param '{param}' has a non-string source")
        elif source == _ACTION_PARAM_FIELD_PREFIX:
            out.append(f"action '{action_id}' param '{param}' names no item field")
    return out


def cell_schema_violations(
    schema: dict[str, Any], action_params: dict[str, Any] | None = None
) -> list[str]:
    """Renderer-agnostic-schema lint for a `tool-cell-schema` (#1022, ADR 0011).

    Returns the reasons a schema would NOT render on a non-browser consumer —
    empty means clean. Enforced: only the closed role vocabulary
    (`title`/`subtitle`/`badge`/`state`/`icon` map to one field; `meta`/`actions`
    are field lists); a role maps a bare field name, never an HTML/CSS/`<…>`
    string or an inline handler. `actions` names `action.id`s the def declares in
    `tool-actions`, not JS. This is the promise that one `SKILL.md` drives both a
    PWA card and a native widget.

    Every id in the `actions` role must also carry a param mapping in the def's
    `tool-action-params` (#1214) — a renderer knows the action id but cannot guess
    which params `/api/action-callback` wants or where in the row their values
    come from, so an undeclared one is a button no native consumer can wire."""
    out: list[str] = []
    if not isinstance(schema, dict):
        return ["schema must be a JSON object of role→field mappings"]

    def _looks_like_markup(value: str) -> bool:
        # A field name is a plain key; markup/CSS/handlers leak the browser.
        return any(c in value for c in "<>{};") or value.strip().startswith(".")

    declared = action_params if isinstance(action_params, dict) else {}
    for action_id in schema.get("actions") or []:
        if isinstance(action_id, str) and action_id:
            out += _action_param_violations(action_id, declared.get(action_id))

    for role, value in schema.items():
        if role not in _CELL_SCHEMA_ROLES:
            out.append(f"unknown role '{role}' (not in the closed vocabulary)")
            continue
        fields = value if isinstance(value, list) else [value]
        if role in _CELL_SCHEMA_LIST_ROLES and not isinstance(value, list):
            out.append(f"role '{role}' must be a list of field names")
            continue
        if role in _CELL_SCHEMA_STRING_ROLES and isinstance(value, list):
            out.append(f"role '{role}' must be a single field name, not a list")
            continue
        for field in fields:
            if not isinstance(field, str) or not field:
                out.append(f"role '{role}' maps a non-string / empty field")
            elif _looks_like_markup(field):
                out.append(
                    f"role '{role}' field '{field}' looks like markup, not a field name"
                )
    return out


# The catalog-driven compose deep link (#1213, cross-repo contract with
# mdopp/solaris-android#71). `tool-compose-path` is the app-facing sibling of
# `tool-api-path`: a tool states in its own frontmatter that it HAS a create path
# and where the PWA opens it, so a native consumer offers a "create" tile only
# for tools that can serve one instead of linking into a dead end. There is
# exactly ONE shape the router resolves — anything else would land the tile on
# the `#/p/start` fallback, so a mistyped declaration is rejected here rather
# than shipped as a tile that quietly goes to the wrong page.
TOOL_COMPOSE_ROUTE = "#/p/{tool_id}/new"


def compose_path_violations(tool_id: str, compose_path: str) -> list[str]:
    """Lint a def's declared `tool-compose-path` — empty means clean.

    No declaration is clean (the tool simply has no create path). A declaration
    must be the canonical route for THIS tool-id; a path that names another tool,
    or any other shape, does not resolve in the router."""
    if not compose_path:
        return []
    expected = TOOL_COMPOSE_ROUTE.format(tool_id=tool_id)
    if compose_path != expected:
        return [
            f"tool-compose-path '{compose_path}' does not resolve; "
            f"the router serves '{expected}'"
        ]
    return []


def _json_field(meta: dict[str, str], key: str) -> dict[str, Any]:
    raw = meta.get(key, "").strip()
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _tool_fields(meta: dict[str, str]) -> dict[str, Any]:
    """The declarative `.tool` plugin surface (#1004, ADR 0011) read off a
    tool-kind def's flat frontmatter.

    `tool-actions` is a comma-separated action-id list; `tool-cell-schema` and
    `tool-action-params` are one-line JSON objects — all three stay within the
    pack's no-PyYAML flat parser. The client (#1005) dispatches on these instead
    of the hardcoded `DOT_COMMANDS`/`ensureCard`; the server auto-registers the
    actions; `tool-action-params` (#1214) lets a renderer build an action's
    callback body straight from the row it drew; `tool-compose-path` (#1213)
    declares whether the tool has a create path at all, and where.
    """
    actions = [a.strip() for a in meta.get("tool-actions", "").split(",") if a.strip()]
    cell_schema = _json_field(meta, "tool-cell-schema")
    action_params = _json_field(meta, "tool-action-params")
    return {
        "tool-id": meta.get("tool-id", ""),
        "tool-label": meta.get("tool-label", ""),
        "command": meta.get("command", ""),
        "tool-api-path": meta.get("tool-api-path", ""),
        "tool-search-path": meta.get("tool-search-path", ""),
        "tool-compose-path": meta.get("tool-compose-path", ""),
        "tool-actions": actions,
        "tool-cell-schema": cell_schema,
        "tool-action-params": action_params,
    }


def list_tool_defs(skills_dir: str | Path) -> list[dict[str, Any]]:
    """List the tool-kind registry with each entry's declarative surface —
    the shape `/api/defs/tool` serves and the server auto-registers from."""
    out: list[dict[str, Any]] = []
    for def_id, meta, _body, _file in _iter_defs(skills_dir):
        if def_kind(meta) != "tool":
            continue
        out.append(
            {
                "id": def_id,
                "name": meta.get("name") or def_id,
                "description": meta.get("description", ""),
                "kind": "tool",
                "scope": meta.get("scope") or _DEFAULT_SCOPE,
                **_tool_fields(meta),
            }
        )
    out.sort(key=lambda s: s["name"].lower())
    return out


def hooks_for_event(skills_dir: str | Path, event: str) -> list[str]:
    """The hook-kind definition ids bound to `event` (their frontmatter
    `event:` field), sorted by id — the registry server flow points resolve a
    hook by, instead of hardcoding a skill id. Phase 5 (#483) wires the actual
    flow points to it; foundation just exposes the lookup."""
    out = [
        def_id
        for def_id, meta, _body, _file in _iter_defs(skills_dir)
        if def_kind(meta) == "hook" and meta.get("event", "").strip() == event
    ]
    out.sort()
    return out


def list_skills(skills_dir: str | Path) -> list[dict[str, str]]:
    """List the skill-kind pack: `[{id, name, description}]`, sorted by name.

    A directory counts as a skill when it holds a `SKILL.md` and its
    frontmatter `kind` is `skill` (or absent — the legacy default). Missing
    dir = empty list (the mount may not be present in offline test).
    """
    return [
        {"id": d["id"], "name": d["name"], "description": d["description"]}
        for d in list_defs(skills_dir, "skill")
    ]


def read_skill(skills_dir: str | Path, skill_id: str) -> dict[str, Any] | None:
    """Return `{id, name, description, kind, scope, body, raw}` for one
    definition, or None.

    `body` is the markdown after the frontmatter — what the panel renders.
    `raw` is the full SKILL.md (frontmatter + body) — what the editor loads.
    """
    if not _is_valid_id(skill_id):
        return None
    skill_file = Path(skills_dir) / skill_id / "SKILL.md"
    if not skill_file.is_file():
        return None
    raw = skill_file.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(raw)
    return {
        "id": skill_id,
        "name": meta.get("name") or skill_id,
        "description": meta.get("description", ""),
        "kind": def_kind(meta),
        "scope": meta.get("scope") or _DEFAULT_SCOPE,
        "body": body,
        "raw": raw,
    }


def read_def(skills_dir: str | Path, kind: str, def_id: str) -> dict[str, Any] | None:
    """Read one definition, but only when its frontmatter `kind` matches the
    requested registry — so `/api/defs/scheduler/status` 404s rather than
    leaking a skill through the wrong card."""
    one = read_skill(skills_dir, def_id)
    if one is None or one["kind"] != kind:
        return None
    return one


def delete_def(skills_dir: str | Path, kind: str, def_id: str) -> bool:
    """Delete a definition's directory (the whole `<id>/`), but only when its
    frontmatter `kind` matches. Returns True on delete, False when the id is
    invalid / not found / wrong kind."""
    if read_def(skills_dir, kind, def_id) is None:
        return False
    shutil.rmtree(Path(skills_dir) / def_id)
    return True


def write_skill(
    skills_dir: str | Path, skill_id: str, content: str
) -> dict[str, Any] | None:
    """Replace an existing skill's SKILL.md with `content` (the full raw
    markdown). Returns `{id, frontmatter_changed}` or None when the id is
    invalid or the skill doesn't exist (we only edit the shipped set, never
    create arbitrary files).

    `frontmatter_changed` is True when name/description/version differs from
    the old file — the signal that the engine needs a restart to re-register the
    skill (a body-only edit is picked up live). The write is atomic (temp in
    the same dir + os.replace) so a reader never sees a half-written file.
    """
    if not _is_valid_id(skill_id):
        return None
    skill_file = Path(skills_dir) / skill_id / "SKILL.md"
    if not skill_file.is_file():
        return None
    old_meta, _ = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
    new_meta, _ = _split_frontmatter(content)
    _atomic_write(skill_file, content)
    return {"id": skill_id, "frontmatter_changed": new_meta != old_meta}


def _atomic_write(skill_file: Path, content: str) -> None:
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(skill_file.parent), prefix=".SKILL.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, skill_file)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_def(
    skills_dir: str | Path, kind: str, def_id: str, content: str
) -> dict[str, Any] | None:
    """Create-or-update one definition in the `kind` registry (the CRUD PUT).

    Returns `{id, created, frontmatter_changed}`, or None when the id is
    invalid or the new content's frontmatter `kind` contradicts the registry
    being written (so a PUT to /api/defs/skill can't drop a scheduler in). A
    create is allowed (the editor cards add new definitions); an update to an
    existing def of a *different* kind is rejected. Atomic write.
    """
    if not _is_valid_id(def_id):
        return None
    new_meta, _ = _split_frontmatter(content)
    if def_kind(new_meta) != kind:
        return None
    skill_file = Path(skills_dir) / def_id / "SKILL.md"
    existing = read_skill(skills_dir, def_id)
    if existing is not None and existing["kind"] != kind:
        return None
    old_meta, _ = (
        _split_frontmatter(skill_file.read_text(encoding="utf-8"))
        if skill_file.is_file()
        else ({}, "")
    )
    _atomic_write(skill_file, content)
    return {
        "id": def_id,
        "created": existing is None,
        "frontmatter_changed": new_meta != old_meta,
    }
