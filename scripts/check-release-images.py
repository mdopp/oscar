#!/usr/bin/env python3
"""Do the recent release tags actually have images in GHCR? (#1275)

A release that ends without images is invisible: the tag exists, the CHANGELOG
looks right, and only the next deploy discovers that GHCR holds nothing for that
version. In the neighbouring repo that gap went unnoticed for days. This script
is the noise: it compares every recent `v*` tag against the images the build
matrix would have produced for that tag, and exits non-zero listing what is
missing, so a scheduled run can turn the silence into an issue.

Expected images are derived from the build matrix in build-images.yml, gated on
the same `Dockerfile` presence check the workflow itself does — so a tag from
before an image existed is not reported as missing.

Requires `docker` on PATH, logged in to ghcr.io.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build-images.yml"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=ROOT, capture_output=True, text=True)


def matrix_images() -> list[tuple[str, str]]:
    """[(image, context)] from the build-images matrix."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    include = workflow["jobs"]["build"]["strategy"]["matrix"]["include"]
    return [(entry["image"], entry["context"]) for entry in include]


def recent_tags(limit: int) -> list[str]:
    # By creation date, not version order: the repo still carries pre-Solaris
    # v1.x tags that sort above every v0.4x release-please tag.
    out = _run("git", "tag", "--list", "v*", "--sort=-creatordate")
    out.check_returncode()
    return out.stdout.split()[:limit]


def tag_age_hours(tag: str) -> float:
    out = _run("git", "log", "-1", "--format=%ct", tag)
    out.check_returncode()
    return (time.time() - int(out.stdout.strip())) / 3600


def has_dockerfile(tag: str, context: str) -> bool:
    return _run("git", "cat-file", "-e", f"{tag}:{context}/Dockerfile").returncode == 0


def is_published(owner: str, image: str, version: str) -> bool:
    ref = f"ghcr.io/{owner}/{image}:{version}"
    return _run("docker", "manifest", "inspect", ref).returncode == 0


def missing_for_tag(owner: str, tag: str) -> list[str]:
    version = tag[1:] if tag.startswith("v") else tag
    return [
        f"ghcr.io/{owner}/{image}:{version}"
        for image, context in matrix_images()
        if has_dockerfile(tag, context) and not is_published(owner, image, version)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tags", nargs="*", help="tags to check (default: the newest ones)"
    )
    parser.add_argument(
        "--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "mdopp")
    )
    parser.add_argument(
        "--limit", type=int, default=5, help="how many recent tags to check"
    )
    parser.add_argument(
        "--grace-hours",
        type=float,
        default=3.0,
        help="skip tags younger than this — their build may still be running",
    )
    args = parser.parse_args()

    tags = args.tags or recent_tags(args.limit)
    if not tags:
        print("no v* tags found — nothing to check")
        return 0

    failures: list[str] = []
    for tag in tags:
        if not args.tags and tag_age_hours(tag) < args.grace_hours:
            print(f"{tag}: too fresh, build may still be running — skipped")
            continue
        missing = missing_for_tag(args.owner, tag)
        if missing:
            failures.append(tag)
            print(f"{tag}: MISSING {len(missing)} image(s)")
            for ref in missing:
                print(f"  - {ref}")
        else:
            print(f"{tag}: ok")

    if failures:
        print()
        print("Republish with, for each tag:")
        for tag in failures:
            print(f"  gh workflow run build-images.yml --ref {tag}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
