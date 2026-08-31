"""A shipped skill must not drive storage nothing in the engine can touch —
and retiring one means REPLACING it, never deleting it.

#1293: `audit-query` (`/audit`) and `debug-set` (`/debug`) instructed the model
to SELECT and UPDATE `cloud_audit` / `system_settings` in `solaris.db`. No tool
in `solaris-chat` reads or writes either table (they are cloud-gateway leftovers
from before Solaris went local-model-only), so `/debug on` confirmed a debug mode
that was never persisted and `/audit` reported "nothing went to the cloud" as a
finding rather than as dead code.

#1293 retired them by deleting the two directories, and on the box that did
nothing: ServiceBay's asset transport is additive (mdopp/servicebay#2703) — it
never removes a file that disappeared from a template's source tree, so both
SKILL.md files stayed live with their original `command:` bindings. The lever
that does work is content: a file the template still ships IS overwritten. So a
retired definition ships as a tombstone (`retired: true`, no `command:`), which
the engine skips in every registry.

#1309 added the reverse direction. `status` and `notes-search` still declared
`command: /status` / `command: /notes` long after #965 stopped feeding skill-kind
defs into the typeable `/` pool, so a resident who typed `/status` got "Unknown
command" for a capability the shipped skill advertised. Declared-and-unreachable
is the same defect as declared-and-nonexistent, so both directions are guarded:
a def without a `command:` must not be offered, and a def *with* one must be
routable by the surface its kind belongs to.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "templates" / "solaris" / "skills"
INDEX_HTML = ROOT / "solaris-chat" / "src" / "solaris_chat" / "static" / "index.html"

# Tables the baseline migration still creates but no engine code reads or writes.
UNSERVED_TABLES = ("cloud_audit", "system_settings")

# Every definition ever removed from a shipped pack instead of being replaced.
# Each must be present again as a tombstone, or it is still live on the box.
TOMBSTONED = (
    "audit-query",
    "debug-set",
    "guest-onboarding",
    "resident-registration",
    "self-enrollment",
)


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip("'\"")
    return meta


def _defs() -> dict[Path, tuple[dict[str, str], str]]:
    return {
        p: (_frontmatter(p.read_text(encoding="utf-8")), p.read_text(encoding="utf-8"))
        for p in SKILLS.glob("*/*/SKILL.md")
    }


def _is_retired(meta: dict[str, str]) -> bool:
    return meta.get("retired", "").strip().lower() in ("true", "yes", "1")


def _live_defs() -> dict[Path, tuple[dict[str, str], str]]:
    return {p: v for p, v in _defs().items() if not _is_retired(v[0])}


def _dot_commands_offered() -> list[str]:
    """The `.`-commands the chat frontend offers in its own menu, read off the
    `DOT_COMMANDS` table in index.html."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    block = re.search(r"var DOT_COMMANDS = \[(.*?)\];", html, re.S)
    assert block, "DOT_COMMANDS table not found in index.html"
    return re.findall(r'\["(\.[^"]+)"', block.group(1))


def _slash_command_kinds() -> set[str]:
    """The def kinds `loadCommandPool()` actually feeds into the typeable `/`
    pool, read off the `typeable` expression in index.html rather than
    hardcoded — so this tracks the frontend if it ever changes again."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    block = re.search(r"var typeable = (.*?);\n", html, re.S)
    assert block, "typeable command pool not found in index.html"
    return set(re.findall(r'kind: "([a-z]+)"', block.group(1)))


def test_retired_cloud_audit_skills_ship_as_tombstones():
    # Present again (the additive transport can only be beaten by overwriting),
    # but carrying nothing: no command binding, no body to act on.
    for def_id in TOMBSTONED:
        path = SKILLS / "household" / def_id / "SKILL.md"
        assert path.is_file(), f"{def_id} must ship a tombstone, not be deleted"
        meta = _frontmatter(path.read_text(encoding="utf-8"))
        assert _is_retired(meta), f"{def_id} tombstone must set retired: true"
        assert "command" not in meta, f"{def_id} tombstone still binds a command"
        assert meta.get("kind", "") != "tool", f"{def_id} tombstone must not be a tool"


def test_no_skill_drives_a_table_no_tool_serves():
    for path, (_meta, text) in _live_defs().items():
        for table in UNSERVED_TABLES:
            assert table not in text, f"{path.relative_to(ROOT)} references {table}"


def test_no_skill_declares_the_retired_commands():
    for path, (_meta, text) in _defs().items():
        for cmd in ("command: /audit", "command: /debug"):
            assert cmd not in text, f"{path.relative_to(ROOT)} declares {cmd}"


def test_a_def_without_a_command_is_never_offered_as_a_dot_command():
    """A shipped definition that declares no `command:` — a tombstone above all —
    must not be reachable as a `.`-command.

    Two routes get you into the dot pool: the frontend's own `DOT_COMMANDS`
    table, and the catalog (`known()` accepts any `kind: tool` def, matching
    either its `tool-id` or its `command`). A def with no `command:` must be on
    neither.
    """
    offered = _dot_commands_offered()
    for path, (meta, _text) in _defs().items():
        if meta.get("command", "").strip():
            continue
        def_id = path.parent.name
        assert "." + def_id not in offered, f"{def_id} is offered as a dot-command"
        assert meta.get("kind", "") != "tool", (
            f"{def_id} declares kind: tool with no command — the catalog would "
            f"make it typeable as '.{meta.get('tool-id') or def_id}'"
        )


def test_a_def_that_declares_a_command_is_reachable_as_one():
    """The reverse of the test above (#1309): a live def that declares a
    `command:` must be routable, or it advertises a command that answers
    "Unknown command".

    Two surfaces route a declaration, and each only accepts certain kinds:
    `.`-commands come from `DOT_COMMANDS` or the `kind: tool` catalog, and
    `/`-commands come from `loadCommandPool()`, which since #965 draws on
    command- and scheduler-kind defs only — never on skills. A skill has no
    editable body, so a `/`-alias could only send its own trigger string as a
    turn, which is text and not a skill invocation.
    """
    slash_kinds = _slash_command_kinds()
    offered = set(_dot_commands_offered())
    for path, (meta, _text) in _live_defs().items():
        cmd = meta.get("command", "").strip()
        if not cmd:
            continue
        kind = meta.get("kind", "").strip()
        rel = path.relative_to(ROOT)
        if cmd.startswith("."):
            assert kind == "tool" or cmd in offered, (
                f"{rel} declares {cmd} but is kind: {kind or '(none)'} and is not "
                f"in DOT_COMMANDS — nothing routes it"
            )
        elif cmd.startswith("/"):
            assert kind in slash_kinds, (
                f"{rel} declares {cmd} but the `/` pool is built from "
                f"{sorted(slash_kinds)} defs only — typing {cmd} answers "
                f"'Unknown command'. Drop the declaration or route the kind."
            )
        else:
            raise AssertionError(f"{rel} declares {cmd!r}: no surface has that prefix")


def test_every_offered_dot_command_has_a_live_def_declaring_it():
    declared = {
        meta.get("command", "").strip()
        for meta, _text in _live_defs().values()
        if meta.get("command", "").strip().startswith(".")
    }
    for cmd in _dot_commands_offered():
        assert cmd in declared, f"{cmd} is offered but no live def declares it"
