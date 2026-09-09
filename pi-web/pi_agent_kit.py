#!/usr/bin/env python3
"""`pi-web-agent-kit` — turn ServiceBay's delivered agent kit into Pi's own shapes.

ServiceBay delivers one read-only checkout to the box (servicebay#2908) and the
pod mounts three of its directories at `/opt/servicebay`: `agent-cli/` (the
`servicebay` CLI), `agent-docs/AGENTS.md` (the box's agent handbook) and
`assists/` (the 55-entry catalog of ADRs, recipes, guides and footguns). Pi does
not read any of those where they lie — it reads **its own** locations, and this
script is the bridge:

  * an assist becomes `<agent-dir>/skills/servicebay/<id>/SKILL.md`, because Pi
    loads skills as Agent-Skills packages (`name` + `description` frontmatter,
    one directory per skill) and an assist's frontmatter is `title`/`whenToUse`
    /`kind`/`tags` — the same content in a different shape, so it is generated
    rather than symlinked;
  * `agent-docs/AGENTS.md` becomes `<agent-dir>/AGENTS.md` with a short
    box-specific prelude in front of it. Pi concatenates the context files it
    finds; it has no include, so the prelude and the shipped file have to be one
    file.

It runs as an init container on **every** pod start and is idempotent — a file
whose content already matches is left alone. That is also how a catalog update
reaches the sessions: ServiceBay refreshes the checkout hourly, the next pod
start regenerates from it.

Nothing here writes into the mount: it is read-only, and an edit there would be
gone within the hour anyway (ADR 0014).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

DEFAULT_KIT = "/opt/servicebay"
DEFAULT_AGENT_DIR = "/data/pi-agent"

# One folder we own entirely, so pruning a retired assist is a scoped delete and
# never touches a skill somebody put in the agent directory by hand.
SKILL_GROUP = "servicebay"

# The Agent-Skills limits Pi validates against (docs/skills.md).
NAME_MAX = 64
DESCRIPTION_MAX = 1024

PRELUDE = """# Where you are: the PI WEB container on this box

- **Your projects** are checkouts under `/workspace`, one folder each. That
  folder is where you work and commit; nothing else on this filesystem is yours
  to change.
- **`servicebay` is on `$PATH`.** It is the CLI described below, already
  pointing at this container's token — the project's own read-only token when
  the folder under `/workspace` has one, otherwise the pod's. Run
  `servicebay --help` for the verbs. You never pass a token to it.
- **The agent kit** is mounted read-only at `$SERVICEBAY_AGENT_KIT`
  (`/opt/servicebay`). Its assists are also loaded as Pi skills, so
  `/skill:<assist-id>` opens one without leaving the session.
- **A project's gate is the project's own.** Its `AGENTS.md` or `CLAUDE.md` and
  its CI workflow name the lint, type and test commands; run them before you
  commit, and do not invent a substitute when you cannot find them.
"""


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """`({key: value}, body)` for a `---`-delimited YAML head.

    Deliberately not a YAML parser: the catalog's frontmatter is 55 files of
    flat `key: value` lines, and the container's Python is the stdlib alone.
    A file without a head is `({}, text)` — the caller decides what that means.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return fields, "\n".join(lines[index + 1 :]).lstrip("\n")
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            fields[key.strip()] = unquote(value.strip())
    return {}, text


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def skill_name(assist_id: str) -> str:
    """The assist id as an Agent-Skills name: `[a-z0-9-]`, no doubled or edge
    hyphens, at most 64 characters. Every id in the catalog already qualifies;
    this is what keeps a future one from loading with a warning."""
    name = re.sub(r"[^a-z0-9]+", "-", assist_id.lower()).strip("-")
    return re.sub(r"-+", "-", name)[:NAME_MAX].strip("-")


def skill_description(fields: dict[str, str]) -> str:
    """What Pi keeps in context for every skill, so it decides when to open it.

    `whenToUse` is written for exactly that question, and the title says what
    the entry is; a description missing both would leave the skill unfindable.
    """
    title = fields.get("title", "").strip()
    when = fields.get("whenToUse", "").strip()
    return clip(" — ".join(part for part in (title, when) if part), DESCRIPTION_MAX)


def render_skill(assist_id: str, text: str) -> str | None:
    """The `SKILL.md` for one assist, or `None` when it carries no description.

    Pi does not load a skill without a description, so generating one would only
    produce a warning on every start.
    """
    fields, body = parse_frontmatter(text)
    description = skill_description(fields)
    if not description:
        return None
    return (
        "---\n"
        f"name: {skill_name(assist_id)}\n"
        f"description: {description}\n"
        "---\n"
        f"<!-- Generated from the ServiceBay assist catalog: assists/{assist_id}.md.\n"
        "     Edit it in mdopp/servicebay; this copy is rewritten on every pod"
        " start. -->\n\n"
        f"{body.rstrip()}\n"
    )


def write_if_changed(path: str, text: str) -> bool:
    try:
        if open(path, encoding="utf-8").read() == text:
            return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def generate_skills(assists_dir: str, skills_dir: str) -> dict[str, object]:
    """Regenerate `<skills_dir>/servicebay/` from the mounted catalog.

    An empty or missing catalog leaves what is there alone rather than emptying
    the directory: a delivery that failed is ServiceBay's outage to report, and
    wiping the skills would turn it into a second, silent one here.
    """
    group = os.path.join(skills_dir, SKILL_GROUP)
    try:
        sources = sorted(
            name for name in os.listdir(assists_dir) if name.endswith(".md")
        )
    except OSError:
        sources = []
    if not sources:
        return {"written": 0, "skills": 0, "pruned": [], "catalog": "missing"}

    written = 0
    kept: set[str] = set()
    for source in sources:
        assist_id = source[:-3]
        try:
            text = open(os.path.join(assists_dir, source), encoding="utf-8").read()
        except OSError:
            continue
        skill = render_skill(assist_id, text)
        if skill is None:
            continue
        kept.add(assist_id)
        if write_if_changed(os.path.join(group, assist_id, "SKILL.md"), skill):
            written += 1

    pruned = []
    for entry in sorted(os.listdir(group)) if os.path.isdir(group) else []:
        if entry in kept:
            continue
        target = os.path.join(group, entry)
        for root, dirs, files in os.walk(target, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(target)
        pruned.append(entry)
    return {"written": written, "skills": len(kept), "pruned": pruned, "catalog": "ok"}


def render_agents_md(shipped: str) -> str:
    """The global context file: this box's prelude, then ServiceBay's handbook.

    The prelude comes first because it is what makes the handbook usable here —
    the handbook talks about `$SERVICEBAY_AGENT_KIT` and a `servicebay` wrapper
    "some containers" provide, and the prelude is where this container says it
    is one of them.
    """
    parts = [PRELUDE.rstrip()]
    if shipped.strip():
        parts.append(shipped.strip())
    return "\n\n".join(parts) + "\n"


def install_agents_md(docs_dir: str, agent_dir: str) -> dict[str, object]:
    path = os.path.join(agent_dir, "AGENTS.md")
    source = os.path.join(docs_dir, "AGENTS.md")
    try:
        shipped = open(source, encoding="utf-8").read()
    except OSError:
        shipped = ""
    changed = write_if_changed(path, render_agents_md(shipped))
    return {"path": path, "changed": changed, "shipped": bool(shipped.strip())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pi-web-agent-kit",
        description=(
            "Generate Pi's skills and global AGENTS.md from the ServiceBay "
            "agent kit mounted read-only in this pod."
        ),
    )
    parser.add_argument("--kit", default=os.environ.get("SERVICEBAY_AGENT_KIT", ""))
    parser.add_argument(
        "--agent-dir", default=os.environ.get("PI_CODING_AGENT_DIR", "")
    )
    args = parser.parse_args(argv)
    kit = args.kit or DEFAULT_KIT
    agent_dir = args.agent_dir or DEFAULT_AGENT_DIR

    skills = generate_skills(
        os.path.join(kit, "assists"), os.path.join(agent_dir, "skills")
    )
    agents = install_agents_md(os.path.join(kit, "agent-docs"), agent_dir)

    # Nothing at all under the mount is the ordinary state of a box whose
    # ServiceBay predates the agent-kit delivery. It costs the skills, not the
    # pod: one line naming the cause, then exit 0 — a non-zero init container
    # would put pi-web in a crash loop over a missing directory (#1403).
    if skills["catalog"] == "missing" and not agents["shipped"]:
        print(
            f"pi-web-agent-kit: no agent kit mounted at {kit} — PI WEB starts "
            "without the ServiceBay skills and with the box prelude alone",
            file=sys.stderr,
        )
        return 0

    if skills["catalog"] == "missing":
        print(
            f"pi-web-agent-kit: no assists under {kit}/assists — leaving the "
            "generated skills as they are; ServiceBay's delivery is what failed",
            file=sys.stderr,
        )
    else:
        print(
            f"pi-web-agent-kit: {skills['skills']} assists as skills "
            f"({skills['written']} rewritten, {len(skills['pruned'])} pruned)"
        )

    if not agents["shipped"]:
        print(
            f"pi-web-agent-kit: no AGENTS.md under {kit}/agent-docs — wrote the "
            "box prelude alone",
            file=sys.stderr,
        )
    else:
        print(
            f"pi-web-agent-kit: {agents['path']} "
            f"{'rewritten' if agents['changed'] else 'already current'}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
