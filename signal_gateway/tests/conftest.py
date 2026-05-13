"""Seed env vars required by signal_gateway.config at import time."""

from __future__ import annotations

import os

os.environ.setdefault("SIGNAL_ACCOUNT", "+490000000000")
os.environ.setdefault("POSTGRES_DSN", "postgresql://stub")
