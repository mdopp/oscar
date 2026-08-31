"""The one bearer check the HTTP surface shares, and its no-token decision.

`auth_ok` returns **True when no token is configured**, and #1300 decided it
stays that way. The listener binds loopback by default (`PUSH_HOST=127.0.0.1`,
#116) and the template ships `PUSH_TOKEN` as a generated secret, so a blank
token is an operator saying "this port is pod-internal": with no secret to
present there is nothing a route-level check could compare, and the trust
boundary is the bind address, not this function.

What made a forgotten call invisible (#1290: `GET /rooms` had none at all) was
not this default but that nothing enumerated the routes — a missing check and a
passing one looked identical everywhere. `tests/test_rooms.py::
test_every_route_requires_the_push_token` now drives every registered route
*with* a token set and demands a 401, so a new unguarded route fails a test
instead of inheriting its neighbours' reputation. Both halves are pinned by
tests: change either and one goes red.
"""

from __future__ import annotations

from aiohttp import web


def auth_ok(request: web.Request, token: str) -> bool:
    if not token:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {token}"
