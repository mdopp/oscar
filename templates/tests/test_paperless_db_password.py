"""Tests for the paperless database-password rotation (#1297).

`PAPERLESS_DB_PASSWORD` is now a generated secret, but postgres applies
`POSTGRES_PASSWORD` only during `initdb` — an existing `pgdata` keeps the
password it was stamped with at first install, so the webserver would render
with the new secret and be locked out of its own database. The post-deploy
converges the role: probe with the deployed secret, and only on a refusal
`ALTER ROLE` over the container-local trust socket.

Only the decision logic is exercised here (no postgres): when a rotation is
owed, and that the password reaches SQL as a correctly quoted literal.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("paperless_pd", TEMPLATES / "paperless" / "post-deploy.py")


# ── the variable is a generated secret, not a shipped literal ────────────────


def test_db_password_is_a_generated_secret_with_no_default():
    variables = json.loads(
        (TEMPLATES / "paperless" / "variables.json").read_text("utf-8")
    )
    var = variables["PAPERLESS_DB_PASSWORD"]
    assert var["type"] == "secret"
    # A default would be a password shipped in the repo — the whole point of #1297.
    assert "default" not in var


# ── needs_rotation ───────────────────────────────────────────────────────────


def test_rotation_needed_when_the_role_rejects_the_deployed_password(pd):
    assert pd.needs_rotation("s3cret", auth_ok=False) is True


def test_no_rotation_when_the_role_already_accepts_it(pd):
    assert pd.needs_rotation("s3cret", auth_ok=True) is False


def test_no_rotation_when_the_deployed_password_could_not_be_read(pd):
    # Rotating to '' would lock the webserver out of its own database.
    assert pd.needs_rotation("", auth_ok=False) is False


# ── build_alter_sql ──────────────────────────────────────────────────────────


def test_alter_sql_stamps_the_role_with_the_password(pd):
    assert pd.build_alter_sql("s3cret") == "ALTER ROLE paperless PASSWORD 's3cret';"


def test_alter_sql_doubles_single_quotes(pd):
    assert pd.build_alter_sql("pa'ss") == "ALTER ROLE paperless PASSWORD 'pa''ss';"


def test_alter_sql_leaves_a_backslash_alone(pd):
    # standard_conforming_strings is on, so a backslash is a plain character.
    assert pd.build_alter_sql("a\\b") == "ALTER ROLE paperless PASSWORD 'a\\b';"


def test_alter_sql_refuses_an_empty_password(pd):
    with pytest.raises(ValueError):
        pd.build_alter_sql("")
