"""Entry point: run inbound poll loop + outbound HTTP server concurrently.

If either task crashes, propagate so the pod restarts cleanly. Same
shape as the gatekeeper composition (#34).
"""

from __future__ import annotations

import asyncio

from oscar_logging import log

from .config import Settings
from .hermes import HermesClient
from .inbound import run_inbound_loop
from .outbound import serve as serve_outbound
from .signal_client import SignalRestClient


async def _serve(settings: Settings) -> None:
    signal = SignalRestClient(settings.signal_rest_url, settings.signal_account)
    hermes = HermesClient(settings.hermes_url, settings.hermes_token)

    inbound = asyncio.create_task(
        run_inbound_loop(
            signal=signal,
            hermes=hermes,
            postgres_dsn=settings.postgres_dsn,
            poll_interval_s=settings.poll_interval_s,
        ),
        name="inbound",
    )
    outbound = asyncio.create_task(
        serve_outbound(
            settings.listen_host,
            settings.listen_port,
            signal=signal,
            signal_token=settings.signal_token,
        ),
        name="outbound",
    )

    done, pending = await asyncio.wait(
        {inbound, outbound}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in done:
        if task.exception():
            log.error(
                "signal_gateway.task.crashed",
                task=task.get_name(),
                error=str(task.exception()),
            )
            raise task.exception()


def main() -> None:
    settings = Settings()
    if not settings.signal_account:
        log.error("signal_gateway.no_account")
        raise SystemExit(2)
    log.info(
        "signal_gateway.boot",
        account=settings.signal_account,
        signal_rest=settings.signal_rest_url,
        listen=f"{settings.listen_host}:{settings.listen_port}",
    )
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
