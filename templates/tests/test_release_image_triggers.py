"""Static guard over the release→image trigger chain (#1275).

A `paths:` filter applies to a workflow's whole `push` event — tag pushes
included. While `tags: ['v*']` and `paths:` sat in the same block of
build-images.yml, a release whose diff touched none of the image sources would
have built nothing at all: the tag would exist and GHCR would stay empty. Two
templates-only releases only escaped because unrelated paths happened to change
in the same batch.

The split is the fix — tags come in through release-images.yml, which has no
`paths:` — and it is exactly the kind of fix that a later "let's tidy the
triggers" commit undoes without noticing. A CI workflow cannot be exercised by
pytest, so these assertions are the regression guard.

Lives under templates/tests because that is the stdlib-only static-assertion
suite CI already runs (`pytest -q templates/tests`).
"""

from __future__ import annotations

import importlib.util
import pathlib
import types

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _load_checker() -> types.ModuleType:
    path = ROOT / "scripts" / "check-release-images.py"
    spec = importlib.util.spec_from_file_location("check_release_images", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _triggers(workflow: dict) -> dict:
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1).
    return workflow.get("on", workflow.get(True))


def test_build_images_push_does_not_carry_tags():
    """`paths:` and `tags:` must never share a push block again."""
    push = _triggers(_workflow("build-images.yml"))["push"]
    assert "paths" in push, push
    assert "tags" not in push, (
        "build-images.yml filters its push by paths, so a `tags:` entry here "
        "would silently skip releases that touched no image source"
    )


def test_build_images_is_callable():
    assert "workflow_call" in _triggers(_workflow("build-images.yml"))


def test_release_images_triggers_on_version_tags():
    push = _triggers(_workflow("release-images.yml"))["push"]
    assert push["tags"] == ["v*"], push


def test_release_images_has_no_path_filter():
    """The whole point: a tag builds regardless of what the diff touched."""
    push = _triggers(_workflow("release-images.yml"))["push"]
    assert "paths" not in push and "paths-ignore" not in push, push


def test_release_images_calls_the_build_matrix():
    job = _workflow("release-images.yml")["jobs"]["build"]
    assert job["uses"] == "./.github/workflows/build-images.yml", job
    assert job["permissions"]["packages"] == "write", job


def test_missing_images_are_checked_on_a_clock():
    """A release without images must surface without anyone remembering to look."""
    check = _workflow("release-image-check.yml")
    assert "schedule" in _triggers(check), _triggers(check)
    assert check["permissions"]["issues"] == "write", check["permissions"]


def test_the_check_covers_every_image_the_matrix_builds():
    """The checker derives its expectations from the matrix, not a copy of it."""
    checker = _load_checker()
    expected = {
        entry["image"]
        for entry in _workflow("build-images.yml")["jobs"]["build"]["strategy"][
            "matrix"
        ]["include"]
    }
    assert {image for image, _context in checker.matrix_images()} == expected
