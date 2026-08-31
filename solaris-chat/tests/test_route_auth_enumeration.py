"""Every route that touches a session must scope it to the caller (#1300).

`/api/chat/cancel` (#1287) shipped without the ownership gate its two sibling
chat endpoints enforce, and `GET /rooms` (#1290) did the same in the gatekeeper.
Neither was visible while reading the diff: the check is opt-in per call site,
so a missing one looks exactly like a passing one. This walks the registered
routes instead of trusting that a new handler copied its neighbours — a handler
that reads a `session_id` without an owner check fails here, and the only way
past is to write a line into `_SCOPED_WITHOUT_OWNS_SESSION` saying why it is safe.
"""

from __future__ import annotations

import inspect
import sqlite3

from solaris_chat.server import build_app

from .test_chat_session_scope import _SCHEMA
from .test_server import _FakeEngine

# Handlers that reference a session id but are scoped by something other than
# `owns_session` — each entry names the mechanism that actually protects it.
_SCOPED_WITHOUT_OWNS_SESSION = {
    "create_session": "mints a new session for the caller's own uid; takes no id",
    "get_session": "engine.get_session filters on the effective_uid owner",
    "delete_session": "engine.delete_session takes uid; wrong owner is a 404 (#438)",
    "session_events": "compares store.session_owner to the effective uid, else 403",
    "session_mentions": "mentions_store.list_session_mentions filters on uid",
    "get_session_topics": "topics_store.get_session_topics filters on uid",
    "set_session_topics": "every topics_store write takes the caller's uid",
    "session_trace": "trace_store.list_session_trace filters on the effective uid",
    "trace_detail": "trace_store.detail_for and the ring read are uid-scoped (#1171)",
    "inject_message": "admin-only (#785); writes the target uid's own household row",
    "whoami": "returns the shared session ids, gated by is_admin; reads no session",
}


def _app(tmp_path):
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return build_app(
        engine=_FakeEngine(),
        engine_admin=None,
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solaris_db_path=path,
    )


def _session_handlers(app):
    """{handler name: one route label} for every handler that reads a session id."""
    found = {}
    for route in app.router.routes():
        handler = getattr(route.handler, "__wrapped__", route.handler)
        source = inspect.getsource(handler)
        if "session_id" in source:
            found.setdefault(
                handler.__name__,
                (f"{route.method} {route.resource.canonical}", source),
            )
    return found


def test_every_session_route_checks_ownership(tmp_path):
    handlers = _session_handlers(_app(tmp_path))
    assert handlers, "the enumeration found no session routes — did build_app change?"

    unguarded = [
        f"{name} ({label})"
        for name, (label, source) in sorted(handlers.items())
        if "owns_session(" not in source and name not in _SCOPED_WITHOUT_OWNS_SESSION
    ]
    assert not unguarded, (
        "these handlers act on a session_id without an ownership check: "
        f"{unguarded}. Call `owns_session(owner_uid, session_id)` and 403 on "
        "failure, or — if the handler is scoped some other way — add it to "
        "_SCOPED_WITHOUT_OWNS_SESSION with the mechanism that protects it."
    )


def test_the_exemption_list_has_no_stale_entries(tmp_path):
    """A handler that gained `owns_session` or vanished must leave the list."""
    handlers = _session_handlers(_app(tmp_path))
    stale = [
        name
        for name in sorted(_SCOPED_WITHOUT_OWNS_SESSION)
        if name not in handlers or "owns_session(" in handlers[name][1]
    ]
    assert not stale, (
        f"_SCOPED_WITHOUT_OWNS_SESSION lists handlers that no longer need it: {stale}"
    )
