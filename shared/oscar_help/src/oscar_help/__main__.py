"""CLI: `python -m oscar_help list | describe`.

Default skills dir = $OSCAR_SKILLS_DIR or `/opt/oscar/skills` (the
read-only mount inside the HERMES container).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from oscar_logging import log

from .core import describe, filter_by_tag, load_all


_DEFAULT_DIR = "/opt/oscar/skills"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oscar_help")
    parser.add_argument(
        "--skills-dir",
        default=os.environ.get("OSCAR_SKILLS_DIR", _DEFAULT_DIR),
        help="Directory containing one subdir per skill, each with SKILL.md.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="List every skill in the registry.")
    p_list.add_argument("--tag", help="Filter to skills carrying this tag.")

    p_desc = sub.add_parser("describe", help="Detail one skill by name.")
    p_desc.add_argument("name", help="Skill name (e.g. oscar-light).")

    args = parser.parse_args(argv)

    if args.action == "list":
        entries = load_all(args.skills_dir)
        if args.tag:
            entries = filter_by_tag(entries, args.tag)
        log.info(
            "oscar_help.list",
            count=len(entries),
            tag=args.tag,
            skills_dir=args.skills_dir,
        )
        json.dump([e.to_dict() for e in entries], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.action == "describe":
        entry = describe(args.skills_dir, args.name)
        if entry is None:
            log.warn("oscar_help.describe.miss", name=args.name)
            sys.stderr.write(f"no skill named {args.name!r}\n")
            return 1
        json.dump(entry.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    parser.error(f"unknown action {args.action!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
