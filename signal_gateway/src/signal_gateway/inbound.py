"""Inbound message loop: poll signal-cli, route through HERMES, reply.

The signal-cli-rest-api `/v1/receive` endpoint returns a list of
envelopes; each envelope may contain a `dataMessage.message` text we
care about. Other envelope shapes (typing, receipts, sync) are ignored.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from oscar_logging import log

from .hermes import HermesClient
from .identities import lookup_uid
from .signal_client import SignalRestClient, SignalRestError


UNKNOWN_NUMBER_REPLY = "Unbekannte Nummer — bitte erst per `oscar-identity-link` mit einer LLDAP-uid verknüpfen."


def _extract_text(envelope: dict[str, Any]) -> tuple[str, str] | None:
    """Pull (sender, text) out of a Signal envelope, or None if not chat."""
    env = envelope.get("envelope") or envelope
    source = env.get("source") or env.get("sourceNumber")
    msg = env.get("dataMessage") or {}
    text = msg.get("message") if isinstance(msg, dict) else None
    if not source or not isinstance(text, str) or not text.strip():
        return None
    return (str(source), text.strip())


async def handle_envelope(
    envelope: dict[str, Any],
    *,
    signal: SignalRestClient,
    hermes: HermesClient,
    postgres_dsn: str,
) -> None:
    parsed = _extract_text(envelope)
    if parsed is None:
        return
    sender, text = parsed
    trace_id = str(uuid.uuid4())
    log.info("signal_gateway.recv", trace_id=trace_id, sender=sender, chars=len(text))

    identity = await lookup_uid(postgres_dsn, sender)
    if identity is None:
        log.warn("signal_gateway.unknown_number", trace_id=trace_id, sender=sender)
        try:
            await signal.send(sender, UNKNOWN_NUMBER_REPLY)
        except SignalRestError as exc:
            log.error("signal_gateway.send.error", trace_id=trace_id, error=str(exc))
        return

    uid, _display = identity
    reply = await hermes.converse(
        text=text, uid=uid, endpoint=f"signal:{sender}", trace_id=trace_id
    )
    if not reply:
        log.warn("signal_gateway.hermes.empty", trace_id=trace_id, uid=uid)
        return

    try:
        await signal.send(sender, reply)
    except SignalRestError as exc:
        log.error("signal_gateway.send.error", trace_id=trace_id, error=str(exc))
        return
    log.info("signal_gateway.reply", trace_id=trace_id, uid=uid, chars=len(reply))


async def run_inbound_loop(
    *,
    signal: SignalRestClient,
    hermes: HermesClient,
    postgres_dsn: str,
    poll_interval_s: float,
) -> None:
    log.info("signal_gateway.inbound.start", poll_s=poll_interval_s)
    while True:
        try:
            envelopes = await signal.receive()
        except SignalRestError as exc:
            log.warn("signal_gateway.receive.error", error=str(exc))
            await asyncio.sleep(poll_interval_s)
            continue
        except Exception as exc:  # noqa: BLE001
            log.error("signal_gateway.receive.crash", error=str(exc))
            await asyncio.sleep(poll_interval_s)
            continue
        for envelope in envelopes:
            try:
                await handle_envelope(
                    envelope, signal=signal, hermes=hermes, postgres_dsn=postgres_dsn
                )
            except Exception as exc:  # noqa: BLE001
                log.error("signal_gateway.handle.crash", error=str(exc))
        await asyncio.sleep(poll_interval_s)
