"""No Mustache-eatable tag may survive in a post-deploy hook.

ServiceBay renders every post-deploy script through Mustache before executing it
(install/runner.ts -> Mustache.render), so anything in double braces is consumed
at install time and podman/the shell only ever sees what Mustache made of it.
That is what cost #1092 five attempts: two Go-template `--format` strings were
rendered away to `|`, podman printed `|` and exited 0, and every image identity
read came back silently empty with no error to retry against.

These scripts take all their config from `env()`, never from Mustache, so the
invariant is total: no double-brace tag anywhere in them.
"""

from __future__ import annotations

import pathlib

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
POST_DEPLOYS = sorted(TEMPLATES.rglob("post-deploy.py"))


def test_there_are_post_deploy_scripts_to_check():
    # A rename that emptied the glob would make this suite pass by checking
    # nothing — the exact silence this guard exists to prevent.
    assert POST_DEPLOYS


@pytest.mark.parametrize("script", POST_DEPLOYS, ids=lambda p: p.parent.name)
def test_post_deploy_has_no_mustache_tag(script):
    hits = [
        f"{script.parent.name}/post-deploy.py:{n}: {line.strip()}"
        for n, line in enumerate(script.read_text("utf-8").splitlines(), 1)
        if "{{" in line
    ]
    assert not hits, (
        "ServiceBay Mustache-renders this script before running it, so these "
        "tags are erased before the code executes:\n" + "\n".join(hits)
    )
