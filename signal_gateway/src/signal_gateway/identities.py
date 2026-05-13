"""Sender-number → LLDAP uid lookup against the gateway_identities table.

`gateway_identities` is owned by the oscar-brain Postgres (alembic
baseline migration). We open one connection per lookup; pooling is
overkill for a service that sees maybe a few dozen messages per day.
"""

from __future__ import annotations

import asyncpg


async def lookup_uid(dsn: str, number: str) -> tuple[str, str | None] | None:
    """Return (uid, display_name) for a Signal sender number, or None.

    Number is normalized E.164 (`+49…`). Match is exact — we never partial-match.
    """
    conn = await asyncpg.connect(dsn=dsn)
    try:
        row = await conn.fetchrow(
            "SELECT uid, display_name FROM gateway_identities "
            "WHERE gateway = 'signal' AND external_id = $1",
            number,
        )
    finally:
        await conn.close()
    if row is None:
        return None
    return (row["uid"], row["display_name"])
