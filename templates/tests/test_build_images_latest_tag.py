"""Static guard over `.github/workflows/build-images.yml` (#1110).

`:latest` is the tag every Solaris template pulls (`templates/solaris/*`), so the
workflow rule that decides who may move it is effectively part of the deploy
contract — and nothing else in CI reads this file. A `workflow_dispatch` of an
old tag once claimed `:latest` for two of five images because the condition read
`github.ref` (the ref the run was STARTED from) instead of what was built; the
box then ran new gatekeeper code against an old schema. These assertions are the
only regression guard available: a CI workflow cannot be exercised by pytest.

It lives under templates/tests because that is the stdlib-only static-assertion
suite CI already runs (`pytest -q templates/tests`).
"""

from __future__ import annotations

import pathlib

import yaml

WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "build-images.yml"
)


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _latest_tag_line() -> str:
    steps = _workflow()["jobs"]["build"]["steps"]
    meta = next(s for s in steps if s.get("id") == "meta")
    lines = [ln.strip() for ln in meta["with"]["tags"].splitlines() if ln.strip()]
    latest = [ln for ln in lines if "value=latest" in ln]
    assert len(latest) == 1, f"expected exactly one latest tag rule, got {latest}"
    return latest[0]


def test_workflow_parses():
    wf = _workflow()
    assert wf["name"] == "build-images"


def test_latest_requires_a_push_event():
    """A dispatch must never claim `:latest`, whatever ref it was started from."""
    line = _latest_tag_line()
    assert "github.event_name == 'push'" in line, line


def test_latest_requires_main():
    line = _latest_tag_line()
    assert "refs/heads/{0}', 'main'" in line or "refs/heads/main" in line, line


def test_concurrency_group_is_per_built_ref():
    """Overlapping publishes race per image; the group must serialise main pushes."""
    group = _workflow()["concurrency"]["group"]
    assert "inputs.ref" in group and "github.ref" in group, group


def test_publishing_runs_are_never_cancelled():
    """Cancelling a publish mid-flight leaves some images pushed and others not."""
    cancel = str(_workflow()["concurrency"]["cancel-in-progress"])
    assert cancel == "False" or "github.event_name == 'pull_request'" in cancel, cancel
