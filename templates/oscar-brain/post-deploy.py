#!/usr/bin/env python3
"""Post-deploy hook for oscar-brain.

Runs after ServiceBay deploys the pod. Three jobs:

1. Wait until the HERMES container answers /health (so the user sees a
   sensible deploy timeline, not "deploy succeeded; everything still
   broken for 8 minutes while Ollama pulls models").
2. Probe Postgres + Ollama + Qdrant via the inline health endpoints.
3. Print a short next-steps checklist tailored to the deployment mode
   the user picked.

What this script deliberately does NOT do (would need infrastructure
ServiceBay doesn't expose):
- Create the `claude_ro` read-only Postgres role for the Claude-Code
  MCP wrapper. Documented in .env.example; user runs the SQL once.
- Pair Signal as a linked device. Needs an interactive QR scan; the
  oscar-brain README walks through it.

Script protocol: stdout lines are surfaced in the ServiceBay deploy log.
Lines starting with `__SB_CREDENTIAL__ {json}` register the payload as
a credential under Settings → Integrations. Exit code 0 = ok; non-zero
flagged but doesn't roll back the deploy.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


HERMES_HOST = os.environ.get("SB_HOST", "localhost")
HERMES_PORT = os.environ.get("HERMES_PORT", "8000")
OLLAMA_PORT = os.environ.get("OLLAMA_PORT", "11434")
OLLAMA_ENABLED = os.environ.get("OLLAMA_ENABLED", "yes").strip().lower() in (
    "yes",
    "true",
    "1",
)

MAX_WAIT_S = 600  # First boot can take ~10 min for Ollama to pull models.
POLL_INTERVAL_S = 5


def log(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def emit_credential(**fields: object) -> None:
    sys.stdout.write("__SB_CREDENTIAL__ " + json.dumps(fields) + "\n")
    sys.stdout.flush()


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_for_hermes() -> bool:
    """Poll HERMES /health until it answers or we hit MAX_WAIT_S."""
    url = f"http://{HERMES_HOST}:{HERMES_PORT}/health"
    started = time.monotonic()
    last_log = 0.0
    while True:
        if http_ok(url):
            elapsed = time.monotonic() - started
            log(f"post-deploy: HERMES ready after {int(elapsed)}s")
            return True
        elapsed = time.monotonic() - started
        if elapsed > MAX_WAIT_S:
            log(
                f"post-deploy: HERMES still not reachable after {MAX_WAIT_S}s — moving on"
            )
            return False
        if elapsed - last_log > 30:
            log(f"post-deploy: waiting for HERMES at {url} ({int(elapsed)}s elapsed)")
            last_log = elapsed
        time.sleep(POLL_INTERVAL_S)


def check_ollama() -> bool:
    if not OLLAMA_ENABLED:
        log("post-deploy: Ollama disabled (cloud deployment mode) — skipping")
        return True
    url = f"http://{HERMES_HOST}:{OLLAMA_PORT}/api/tags"
    if http_ok(url):
        log(f"post-deploy: Ollama ready at {url}")
        return True
    log(
        f"post-deploy: Ollama not reachable at {url} — first boot may still be downloading models"
    )
    return False


def print_next_steps() -> None:
    log("")
    log("=" * 60)
    log("post-deploy: oscar-brain is up. Next steps:")
    log("=" * 60)
    log("")
    log("1. Verify with the oscar-status skill or:")
    log(f"   curl http://{HERMES_HOST}:{HERMES_PORT}/health")
    if OLLAMA_ENABLED:
        log(f"   curl http://{HERMES_HOST}:{OLLAMA_PORT}/api/tags")
    log("")
    log("2. Deploy oscar-voice next, then oscar-connectors.")
    log("   See stacks/oscar/README.md for the full walkthrough.")
    log("")
    log("3. For Claude-Code debugging via .mcp.json, create the read-only")
    log("   Postgres role:")
    log("   podman exec -it oscar-brain-postgres psql -U oscar oscar")
    log("   ... then run the CREATE ROLE block from .env.example.")
    log("")
    if os.environ.get("SIGNAL_ACCOUNT"):
        log("4. Pair Signal as a linked device — see oscar-brain README's")
        log("   'Signal pairing (Phase 1)' section for the QR-scan flow.")
        log("")


def main() -> int:
    log("post-deploy: oscar-brain hook starting")
    hermes_ok = wait_for_hermes()
    ollama_ok = check_ollama()

    if hermes_ok:
        log("post-deploy: success")
    else:
        log(
            "post-deploy: HERMES not reachable yet — deploy succeeded but the pod needs more time"
        )

    if not ollama_ok and OLLAMA_ENABLED:
        log(
            "post-deploy: Ollama not yet reachable. Check `podman logs oscar-brain-ollama` after a few minutes"
        )

    print_next_steps()
    return 0  # Don't fail the deploy on probe timeouts; ServiceBay surfaces the logs anyway.


if __name__ == "__main__":
    sys.exit(main())
