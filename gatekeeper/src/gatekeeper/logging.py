"""Structured-logging helper for the gatekeeper.

Emits JSON lines on stdout matching ServiceBay's logger contract
(documented as `docs/TEMPLATE_LOGGING.md` in mdopp/servicebay):

    {"ts": "...", "level": "info|warn|error|debug", "tag": "...", "message": "...", "args": {...}}

Until ServiceBay ships a Python helper package implementing the contract,
the gatekeeper carries this small module so it can emit machine-parseable
lines without an external dependency.

Caller pattern:

    from gatekeeper.logging import log

    log.info("gatekeeper.boot", uri=settings.gatekeeper_uri)
    log.warn("gatekeeper.push.unauthorized", trace_id=trace_id)
    log.error("gatekeeper.hermes.error", trace_id=trace_id, status=503)

`tag` defaults to `gatekeeper` (overridable via `OSCAR_COMPONENT`).
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Any


class _Logger:
    def __init__(self, tag: str) -> None:
        self._tag = tag

    def _emit(self, level: str, message: str, **args: Any) -> None:
        record = {
            "ts": datetime.datetime.now().astimezone().isoformat(),
            "level": level,
            "tag": self._tag,
            "message": message,
            "args": args,
        }
        sys.stdout.write(json.dumps(record, default=str) + "\n")
        sys.stdout.flush()

    def debug(self, message: str, **args: Any) -> None:
        self._emit("debug", message, **args)

    def info(self, message: str, **args: Any) -> None:
        self._emit("info", message, **args)

    def warn(self, message: str, **args: Any) -> None:
        self._emit("warn", message, **args)

    def error(self, message: str, **args: Any) -> None:
        self._emit("error", message, **args)


log = _Logger(os.environ.get("OSCAR_COMPONENT", "gatekeeper"))
