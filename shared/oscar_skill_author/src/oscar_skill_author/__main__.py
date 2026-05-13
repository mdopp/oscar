"""CLI: drafts + apply + revert + list — see README.md."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

from oscar_logging import log

from .apply import apply_edit, revert_edit
from .drafts import cancel_draft, confirm_draft, create_draft, list_pending


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN", "")
    if not dsn:
        sys.stderr.write("POSTGRES_DSN not set\n")
        sys.exit(2)
    return dsn


def _skills_local() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("OSCAR_SKILLS_LOCAL_DIR", "/opt/oscar/skills-local")
    )


def _read_file_arg(value: str) -> str:
    """Accept either a literal string or `@/path/to/file` (read its contents)."""
    if value.startswith("@"):
        return pathlib.Path(value[1:]).read_text(encoding="utf-8")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oscar_skill_author")
    sub = parser.add_subparsers(dest="action", required=True)

    p_draft = sub.add_parser("draft", help="Stage a draft for user confirmation.")
    p_draft.add_argument("--uid", required=True)
    p_draft.add_argument("--skill-name", required=True)
    p_draft.add_argument("--proposed-md", required=True, help="SKILL.md text or @file")
    p_draft.add_argument("--current-md", default=None, help="Current SKILL.md or @file")
    p_draft.add_argument("--source", default="user", choices=["user", "reviewer"])
    p_draft.add_argument("--reason", default=None)

    p_conf = sub.add_parser("confirm", help="Apply a pending draft by id.")
    p_conf.add_argument("draft_id")

    p_can = sub.add_parser("cancel", help="Cancel a pending draft.")
    p_can.add_argument("draft_id")

    p_app = sub.add_parser("apply", help="Apply an edit directly (skips draft table).")
    p_app.add_argument("--skill-name", required=True)
    p_app.add_argument("--proposed-md", required=True)
    p_app.add_argument("--source", default="user", choices=["user", "reviewer"])
    p_app.add_argument("--reason", default=None)

    p_rev = sub.add_parser(
        "revert", help="Revert the n-th most recent edit of a skill."
    )
    p_rev.add_argument("--skill-name", required=True)
    p_rev.add_argument("--n", type=int, default=1)

    p_list = sub.add_parser("list-drafts", help="List pending drafts.")
    p_list.add_argument("--uid", default=None)

    args = parser.parse_args(argv)
    dsn = _dsn()
    skills_local = _skills_local()

    if args.action == "draft":
        proposed = _read_file_arg(args.proposed_md)
        current = _read_file_arg(args.current_md) if args.current_md else None
        try:
            draft_id = asyncio.run(
                create_draft(
                    dsn=dsn,
                    uid=args.uid,
                    skill_name=args.skill_name,
                    proposed_md=proposed,
                    current_md=current,
                    source=args.source,
                    reason=args.reason,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warn("skill_author.draft.rejected", error=str(exc))
            sys.stderr.write(f"{exc}\n")
            return 3
        sys.stdout.write(draft_id + "\n")
        return 0

    if args.action == "confirm":
        try:
            result = asyncio.run(
                confirm_draft(
                    dsn=dsn, skills_local=skills_local, draft_id=args.draft_id
                )
            )
        except (LookupError, ValueError) as exc:
            sys.stderr.write(f"{exc}\n")
            return 3
        json.dump(result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write(
            f"\nrestart_hint: oscar-brain pod restart required for {result.skill_name!r}\n"
        )
        return 0

    if args.action == "cancel":
        asyncio.run(cancel_draft(dsn=dsn, draft_id=args.draft_id))
        return 0

    if args.action == "apply":
        proposed = _read_file_arg(args.proposed_md)
        try:
            result = asyncio.run(
                apply_edit(
                    dsn=dsn,
                    skills_local=skills_local,
                    skill_name=args.skill_name,
                    proposed_md=proposed,
                    source=args.source,
                    reason=args.reason,
                )
            )
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"{exc}\n")
            return 3
        json.dump(result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write(
            f"\nrestart_hint: oscar-brain pod restart required for {result.skill_name!r}\n"
        )
        return 0

    if args.action == "revert":
        try:
            result = asyncio.run(
                revert_edit(
                    dsn=dsn,
                    skills_local=skills_local,
                    skill_name=args.skill_name,
                    n=args.n,
                )
            )
        except (LookupError, ValueError) as exc:
            sys.stderr.write(f"{exc}\n")
            return 3
        json.dump(result.to_dict(), sys.stdout, indent=2)
        sys.stdout.write(
            f"\nrestart_hint: oscar-brain pod restart required for {result.skill_name!r}\n"
        )
        return 0

    if args.action == "list-drafts":
        drafts = asyncio.run(list_pending(dsn=dsn, uid=args.uid))
        json.dump(
            [
                {
                    **d,
                    "id": str(d["id"]),
                    "created_at": d["created_at"].isoformat(),
                    "expires_at": d["expires_at"].isoformat(),
                }
                for d in drafts
            ],
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    parser.error(f"unknown action {args.action!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
