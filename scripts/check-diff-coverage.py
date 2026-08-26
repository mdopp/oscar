#!/usr/bin/env python3
"""Fail when changed lines under voice-gatekeeper/src/gatekeeper fall below a
coverage floor. Diff-coverage (new code only), not a whole-repo threshold —
legacy debt must not block (house-style gate 2, project-standards catalog #9).

Reads voice-gatekeeper/coverage.xml (produced by `pytest --cov=gatekeeper
--cov-report=xml`), intersects its executable lines with the lines this branch
changed vs the merge base, and checks the changed-line coverage ratio. A no-op
when no gatekeeper source lines changed. Ratchet COVERAGE_FLOOR up over time;
never down to make a red run pass — add the test instead.
"""

from __future__ import annotations

import re
import subprocess
import sys

# The only XML this script ever reads is voice-gatekeeper/coverage.xml, written
# by our own `pytest --cov-report=xml` a step earlier in the same CI job. It is
# not resident data, not fetched over the network, and not anything a PR author
# supplies — so the XXE / XML-bomb attacks defusedxml guards against have no way
# in. Overturn this suppression the moment the script reads a coverage report it
# did not produce itself.
# nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
import xml.etree.ElementTree as ET
from pathlib import Path

COVERAGE_FLOOR = 0.70
COVERAGE_XML = Path("voice-gatekeeper/coverage.xml")
WATCHED_PREFIX = "voice-gatekeeper/src/gatekeeper/"


def merge_base() -> str:
    base_ref = "origin/main"
    try:
        return subprocess.check_output(
            ["git", "merge-base", base_ref, "HEAD"], text=True
        ).strip()
    except subprocess.CalledProcessError:
        return base_ref


def changed_lines() -> dict[str, set[int]]:
    """{repo-relative path under WATCHED_PREFIX: {added/changed line numbers}}."""
    diff = subprocess.check_output(
        ["git", "diff", "--unified=0", merge_base(), "--", WATCHED_PREFIX],
        text=True,
    )
    result: dict[str, set[int]] = {}
    cur: str | None = None
    hunk = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
            result.setdefault(cur, set())
        elif line.startswith("@@") and cur is not None:
            m = hunk.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                result[cur].update(range(start, start + count))
    return {p: lines for p, lines in result.items() if lines}


def covered_lines() -> dict[str, dict[int, bool]]:
    """{repo-relative path: {line number: covered?}} from coverage.xml."""
    if not COVERAGE_XML.exists():
        sys.exit(f"{COVERAGE_XML} not found — run pytest --cov-report=xml first.")
    # Same CI-produced input as the import above — see the reason there.
    # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
    root = ET.parse(COVERAGE_XML).getroot()
    sources = [s.text for s in root.findall("./sources/source") if s.text]
    result: dict[str, dict[int, bool]] = {}
    for cls in root.iter("class"):
        filename = cls.get("filename")
        if not filename:
            continue
        repo_path = _to_repo_path(filename, sources)
        if repo_path is None:
            continue
        lines = result.setdefault(repo_path, {})
        for ln in cls.iter("line"):
            num = int(ln.get("number"))
            lines[num] = int(ln.get("hits", "0")) > 0
    return result


def _to_repo_path(filename: str, sources: list[str]) -> str | None:
    """Resolve a coverage.xml filename to a repo-relative path under the prefix."""
    candidates = [filename]
    for src in sources:
        candidates.append(str(Path(src) / filename))
    for cand in candidates:
        norm = cand.replace("\\", "/").lstrip("./")
        idx = norm.find("voice-gatekeeper/src/gatekeeper/")
        if idx != -1:
            return norm[idx:]
        if norm.startswith("gatekeeper/"):
            return WATCHED_PREFIX[: -len("gatekeeper/")] + norm
    return None


def main() -> int:
    changed = changed_lines()
    if not changed:
        print("diff-coverage: no gatekeeper source lines changed — skip.")
        return 0

    cov = covered_lines()
    measured = 0
    hit = 0
    misses: list[str] = []
    for path, lines in changed.items():
        file_cov = cov.get(path, {})
        for ln in sorted(lines):
            if ln not in file_cov:  # non-executable (comment/blank) — ignore
                continue
            measured += 1
            if file_cov[ln]:
                hit += 1
            else:
                misses.append(f"{path}:{ln}")

    if measured == 0:
        print("diff-coverage: changed lines are all non-executable — skip.")
        return 0

    ratio = hit / measured
    print(
        f"diff-coverage: {hit}/{measured} changed executable lines covered "
        f"({ratio:.0%}); floor {COVERAGE_FLOOR:.0%}."
    )
    if ratio < COVERAGE_FLOOR:
        print("Uncovered changed lines:")
        for m in misses:
            print(f"  {m}")
        print("\nAdd tests for the changed gatekeeper code — do not lower the floor.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
