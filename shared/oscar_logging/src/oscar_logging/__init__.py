"""OSCAR shared structured logger.

Emits one JSON line per call on stdout with standard fields enforced
(`ts`, `level`, `component`, `event`, plus caller-provided body).
Every OSCAR container imports this — direct `print` / `logging.info` is verboten.

Spec: docs/logging.md
Component name read from `OSCAR_COMPONENT` env var (set in pod-yaml).
Debug-level emission gated by `OSCAR_DEBUG_MODE` (phase-0 env-var stub).
Will be replaced by a Postgres lookup against `system_settings` once
oscar-brain is up; the public surface stays the same.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any


COMPONENT = os.environ.get("OSCAR_COMPONENT", "unknown")


def _debug_active() -> bool:
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

__all__ = ["log", "COMPONENT"]
