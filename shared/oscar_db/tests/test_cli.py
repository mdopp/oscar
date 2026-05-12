"""Tests for the wait-for-postgres + alembic-dispatch wrapper.

Doesn't run actual alembic — just verifies the CLI parses arguments,
respects the DSN env var, and routes to the right alembic command.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

os.environ.setdefault("OSCAR_COMPONENT", "test")
os.environ.setdefault("POSTGRES_DSN", "postgresql://stub")


def _reset_modules():
    for mod in [m for m in list(sys.modules) if m.startswith("oscar_db")]:
        del sys.modules[mod]


def test_upgrade_calls_alembic_upgrade(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://stub")
    _reset_modules()
    from oscar_db import cli

    with (
        patch.object(cli, "_wait_for_postgres") as wait_mock,
        patch("alembic.command.upgrade") as upgrade_mock,
    ):
        rc = cli._run_alembic("upgrade")
    assert rc == 0
    upgrade_mock.assert_called_once()
    args, kwargs = upgrade_mock.call_args
    assert args[1] == "head"
    wait_mock.assert_not_called()  # _run_alembic alone doesn't wait; main() does


def test_history_routes_through(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://stub")
    _reset_modules()
    from oscar_db import cli

    with patch("alembic.command.history") as history_mock:
        rc = cli._run_alembic("history")
    assert rc == 0
    history_mock.assert_called_once()


def test_unknown_action_returns_2(monkeypatch, capsys):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://stub")
    _reset_modules()
    from oscar_db import cli

    rc = cli._run_alembic("does-not-exist")
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown action" in captured.err


def test_dsn_missing_exits_2(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    _reset_modules()
    from oscar_db import cli

    with pytest.raises(SystemExit) as excinfo:
        cli._dsn()
    assert excinfo.value.code == 2


def test_wait_for_postgres_eventually_succeeds(monkeypatch):
    """Simulates two transient failures then success."""
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://stub")
    _reset_modules()
    from oscar_db import cli

    state = {"calls": 0}

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, *_):
            return None

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def cursor(self):
            return FakeCursor()

    def fake_connect(*_args, **_kwargs):
        state["calls"] += 1
        if state["calls"] < 3:
            raise OSError("still warming up")
        return FakeConn()

    with patch("psycopg.connect", side_effect=fake_connect), patch("time.sleep"):
        cli._wait_for_postgres("postgresql://stub")
    assert state["calls"] >= 3
