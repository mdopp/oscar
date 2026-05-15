#!/usr/bin/env python3
"""Post-deploy hook for oscar-household.

Runs after the pod's containers report Ready. Idempotent.

Steps:
  1. Wait for Hermes' API to answer at HERMES_API_URL.
  2. Register HA-MCP with Hermes if not already registered.
  3. Register ServiceBay-MCP with Hermes if not already registered.

Cloud-LLM audit-proxy wiring is deferred until that MCP exists as its
own package (see oscar-architecture.md → "Upstream work").

Variables expected in the environment (ServiceBay substitutes them):
  HERMES_API_URL, HERMES_TOKEN, HA_MCP_TOKEN, SERVICEBAY_MCP_TOKEN.

The script exits 0 on success and on already-registered (no-op).
Exits non-zero on a network or auth error so ServiceBay flags the
deploy as needing attention.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request


HERMES_API_URL = os.environ["HERMES_API_URL"].rstrip("/")
HERMES_TOKEN = os.environ["HERMES_TOKEN"]
HA_MCP_TOKEN = os.environ.get("HA_MCP_TOKEN", "")
SERVICEBAY_MCP_TOKEN = os.environ.get("SERVICEBAY_MCP_TOKEN", "")

# Same-host defaults; the operator overrides these via ServiceBay
# variables if the MCP servers run somewhere else.
HA_MCP_URL = os.environ.get("HA_MCP_URL", "http://127.0.0.1:8123/mcp_server/sse")
SERVICEBAY_MCP_URL = os.environ.get("SERVICEBAY_MCP_URL", "http://127.0.0.1:5888/mcp")


def wait_for_hermes(timeout_s: int = 60) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{HERMES_API_URL}/health", timeout=2)
            return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(2)
    raise SystemExit(f"hermes did not answer at {HERMES_API_URL} within {timeout_s}s")


def register_mcp(name: str, url: str, token: str) -> None:
    if not token:
        print(f"[oscar-household] {name}: no token configured, skipping")
        return
    # Hermes' MCP-add HTTP endpoint contract is project-specific; this
    # function will be filled in when the ServiceBay `hermes` template
    # lands and we know the exact route. For now we emit the intent so
    # the operator sees what the post-deploy would do.
    print(f"[oscar-household] would register {name} at {url} (token present)")


def main() -> int:
    wait_for_hermes()
    register_mcp("ha-mcp", HA_MCP_URL, HA_MCP_TOKEN)
    register_mcp("servicebay-mcp", SERVICEBAY_MCP_URL, SERVICEBAY_MCP_TOKEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
