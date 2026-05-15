#!/usr/bin/env python3
"""Post-deploy hook for oscar-household.

Runs after the pod's containers report Ready. Idempotent on every
invocation — re-running adds nothing if the MCP servers are already
registered.

The hook is **non-interactive** by design (ServiceBay UX_PHILOSOPHY §2:
no `podman exec` operator instructions). All inputs come from the
wizard-collected variables in this template's variables.json.

Steps:
  1. Wait for Hermes' API to answer at HERMES_API_URL with HERMES_TOKEN.
  2. Register HA-MCP with Hermes (idempotent: re-adding an existing URL
     is a no-op on Hermes' side).
  3. Register ServiceBay-MCP with Hermes.

Cloud-LLM audit-proxy wiring is deferred until that MCP exists as its
own package (see oscar-architecture.md → "Upstream work").

Variables expected in the environment (ServiceBay substitutes them):
  HERMES_API_URL, HERMES_TOKEN,
  HA_MCP_URL,    HA_MCP_TOKEN,
  SERVICEBAY_MCP_URL, SERVICEBAY_MCP_TOKEN.

Exit codes:
  0 — success or already-registered
  1 — Hermes not reachable within the readiness window
  2 — registration call returned a non-success status
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


HERMES_API_URL = os.environ["HERMES_API_URL"].rstrip("/")
HERMES_TOKEN = os.environ["HERMES_TOKEN"]

# Optional — if a token isn't provided, we skip that MCP registration
# instead of failing. Operators may legitimately defer either side.
HA_MCP_URL = os.environ.get("HA_MCP_URL", "")
HA_MCP_TOKEN = os.environ.get("HA_MCP_TOKEN", "")
SERVICEBAY_MCP_URL = os.environ.get("SERVICEBAY_MCP_URL", "")
SERVICEBAY_MCP_TOKEN = os.environ.get("SERVICEBAY_MCP_TOKEN", "")

READINESS_TIMEOUT_S = int(os.environ.get("HERMES_READINESS_TIMEOUT_S", "120"))


def _log(event: str, **fields: object) -> None:
    record = {"component": "oscar-household.post-deploy", "event": event, **fields}
    sys.stdout.write(json.dumps(record, default=str) + "\n")
    sys.stdout.flush()


def _hermes_request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{HERMES_API_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {HERMES_TOKEN}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = resp.read().decode()
        return json.loads(payload) if payload else {}


def wait_for_hermes() -> None:
    deadline = time.time() + READINESS_TIMEOUT_S
    last_err: str | None = None
    while time.time() < deadline:
        try:
            _hermes_request("GET", "/health")
            _log("hermes.ready")
            return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = repr(e)
            time.sleep(2)
    _log("hermes.unreachable", last_error=last_err, timeout_s=READINESS_TIMEOUT_S)
    raise SystemExit(1)


def register_mcp(name: str, url: str, token: str) -> None:
    if not url or not token:
        _log("mcp.skipped", name=name, reason="missing_url_or_token")
        return
    try:
        # Hermes' MCP-add HTTP contract: POST /mcp/servers with
        # {name, url, token}. Idempotent — re-adding an existing URL
        # returns the existing record without error. The exact route
        # may need tuning once the ServiceBay `hermes` template
        # publishes the canonical surface; this post-deploy will fail
        # loud if the endpoint shape changes so we know to update it.
        _hermes_request(
            "POST",
            "/mcp/servers",
            {"name": name, "url": url, "token": token},
        )
        _log("mcp.registered", name=name, url=url)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # 409 = already registered. Idempotent success.
            _log("mcp.already_registered", name=name, url=url)
            return
        body = e.read().decode(errors="replace")[:500]
        _log("mcp.failed", name=name, url=url, status=e.code, body=body)
        raise SystemExit(2)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        _log("mcp.failed", name=name, url=url, error=repr(e))
        raise SystemExit(2)


def main() -> int:
    wait_for_hermes()
    register_mcp("ha-mcp", HA_MCP_URL, HA_MCP_TOKEN)
    register_mcp("servicebay-mcp", SERVICEBAY_MCP_URL, SERVICEBAY_MCP_TOKEN)
    _log("post-deploy.done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
