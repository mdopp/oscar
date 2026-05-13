"""CLI: `python -m oscar_help list | describe`.

Default skills dir = $OSCAR_SKILLS_DIR or `/opt/oscar/skills` (the
read-only mount of the public repo's skills/).

Optional local-overrides dir = $OSCAR_SKILLS_LOCAL_DIR or
`/opt/oscar/skills-local` (the writable hostPath mount where
user-initiated and reviewer-applied edits land). When set and present,
entries there shadow same-named entries in the public dir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from oscar_logging import log

from .core import describe, filter_by_tag, load_all


_DEFAULT_DIR = "/opt/oscar/skills"
_DEFAULT_LOCAL_DIR = "/opt/oscar/skills-local"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oscar_help")
    parser.add_argument(
        "--skills-dir",
        default=os.environ.get("OSCAR_SKILLS_DIR", _DEFAULT_DIR),
        help="Directory containing one subdir per skill, each with SKILL.md.",
    )
    parser.add_argument(
        "--local-dir",
        default=os.environ.get("OSCAR_SKILLS_LOCAL_DIR", _DEFAULT_LOCAL_DIR),
        help="Optional writable overrides dir; entries shadow --skills-dir on name collision.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="List every skill in the registry.")
    p_list.add_argument("--tag", help="Filter to skills carrying this tag.")

    p_desc = sub.add_parser("describe", help="Detail one skill by name.")
    p_desc.add_argument("name", help="Skill name (e.g. oscar-light).")

    args = parser.parse_args(argv)
    dirs = [args.skills_dir, args.local_dir]

    if args.action == "list":
        entries = load_all(dirs)
        if args.tag:
            entries = filter_by_tag(entries, args.tag)
        log.info(
            "oscar_help.list",
            count=len(entries),
            tag=args.tag,
            skills_dir=args.skills_dir,
            local_dir=args.local_dir,
        )
        json.dump([e.to_dict() for e in entries], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.action == "describe":
        entry = describe(dirs, args.name)
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
