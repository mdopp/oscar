"""Small structured-logging helper.

Inlined here when the shared `oscar_logging` library was retired during
the May 2026 lean reset — its responsibilities belong on the ServiceBay
platform side (structured-logging contract every template can follow,
tracked from OSCAR; see oscar-architecture.md → "Upstream work").

Until that contract lands in mdopp/servicebay, the gatekeeper carries
this tiny helper so it can still emit machine-parseable lines to stdout
without depending on a deleted package.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any


COMPONENT = os.environ.get("OSCAR_COMPONENT", "gatekeeper")


def log(event: str, level: str = "info", **fields: Any) -> None:
    """Emit one JSON line to stdout.

    Fields are merged into the record; reserved keys (`event`, `level`,
    `ts`, `component`) take precedence over collisions in `fields`.
    """
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "level": level,
        "component": COMPONENT,
        "event": event,
        **fields,
    }
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()
