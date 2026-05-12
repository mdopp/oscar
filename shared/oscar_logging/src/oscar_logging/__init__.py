"""OSCAR shared structured logger.

Emits one JSON line per call on stdout with standard fields enforced
(`ts`, `level`, `component`, `event`, plus caller-provided body).
Every OSCAR container imports this — direct `print` / `logging.info` is verboten.

Spec: docs/logging.md
Component name read from `OSCAR_COMPONENT` env var (set in pod-yaml).
Debug-level emission gated by `OSCAR_DEBUG_MODE` (env-var default) and
optionally overridden by a runtime watcher (`oscar_logging.runtime`)
that polls `system_settings.debug_mode` in Postgres. Set via the
`debug.set` HERMES skill / `python -m oscar_logging.admin debug-set …`.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any


COMPONENT = os.environ.get("OSCAR_COMPONENT", "unknown")

# Runtime override: set by oscar_logging.runtime.watch_debug_mode when a
# component opts into Postgres-driven toggling. None means "fall back to
# the env var" — the default behaviour for containers that don't watch.
_debug_override: bool | None = None


def set_debug_override(active: bool | None) -> None:
    """Set or clear the runtime debug-mode override. `None` re-enables env-var fallback."""
    global _debug_override
    _debug_override = active


def _debug_active() -> bool:
    if _debug_override is not None:
        return _debug_override
    return os.environ.get("OSCAR_DEBUG_MODE", "false").lower() in ("true", "1", "yes")


def _emit(level: str, event: str, **fields: Any) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "component": COMPONENT,
        "event": event,
        **fields,
    }
    sys.stdout.write(json.dumps(payload, default=str) + "\n")
    sys.stdout.flush()


class _Log:
    @staticmethod
    def error(event: str, **fields: Any) -> None:
        _emit("error", event, **fields)

    @staticmethod
    def warn(event: str, **fields: Any) -> None:
        _emit("warn", event, **fields)

    @staticmethod
    def info(event: str, **fields: Any) -> None:
        _emit("info", event, **fields)

    @staticmethod
    def debug(event: str, **fields: Any) -> None:
        if _debug_active():
            _emit("debug", event, **fields)


log = _Log()

__all__ = ["log", "COMPONENT", "set_debug_override"]
