"""Seed env vars required by gatekeeper.config at module-import time.

gatekeeper.config initializes a singleton `settings = Settings.from_env()`
at import. The push-endpoint tests don't touch that singleton, but
collecting any test triggers the import via `gatekeeper.push`. Without
HERMES_URL the import explodes before pytest reaches the test bodies.
"""

from __future__ import annotations

import os

os.environ.setdefault("HERMES_URL", "http://hermes-test:8000")
