#!/usr/bin/env python3
"""Post-deploy hook for oscar-brain (data plane only).

Runs after ServiceBay deploys the pod. Two jobs:

1. Probe Postgres + Ollama via the published ports so the user sees a
   sensible deploy timeline, not "deploy succeeded; everything still
   broken for 8 minutes while Ollama pulls models".
2. Print a short next-steps checklist pointing at the Hermes install.

Script protocol: stdout lines are surfaced in the ServiceBay deploy log.
Exit code 0 = ok; non-zero flagged but doesn't roll back the deploy.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request


SB_HOST = os.environ.get("SB_HOST", "localhost")
POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))
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


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def tcp_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (TimeoutError, OSError):
        return False


def wait_for_postgres() -> bool:
    started = time.monotonic()
    last_log = 0.0
    while True:
        if tcp_ok(SB_HOST, POSTGRES_PORT):
            log(
                f"post-deploy: postgres ready after {int(time.monotonic() - started)}s"
            )
            return True
        elapsed = time.monotonic() - started
        if elapsed > 120:
            log("post-deploy: postgres not reachable after 120s — moving on")
            return False
        if elapsed - last_log > 15:
            log(
                f"post-deploy: waiting for postgres at {SB_HOST}:{POSTGRES_PORT} ({int(elapsed)}s)"
            )
            last_log = elapsed
        time.sleep(POLL_INTERVAL_S)


def check_ollama() -> bool:
    if not OLLAMA_ENABLED:
        log("post-deploy: Ollama disabled (cloud deployment mode) — skipping")
        return True
    url = f"http://{SB_HOST}:{OLLAMA_PORT}/api/tags"
    started = time.monotonic()
    while time.monotonic() - started < MAX_WAIT_S:
        if http_ok(url):
            log(f"post-deploy: Ollama ready at {url}")
            return True
        time.sleep(POLL_INTERVAL_S)
    log(
        f"post-deploy: Ollama not reachable at {url} after {MAX_WAIT_S}s "
        "— first boot may still be downloading models. "
        "Check `podman logs oscar-brain-ollama`."
    )
    return False


def print_next_steps() -> None:
    log("")
    log("=" * 60)
    log("post-deploy: oscar-brain (data plane) is up. Next:")
    log("=" * 60)
    log("")
    log("1. Install Hermes Agent on this host (or a separate machine):")
    log("   curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash")
    log("")
    log("2. Configure Hermes to point at this pod's Ollama + Postgres:")
    log(f"   - Model provider:      http://{SB_HOST}:{OLLAMA_PORT}  (Ollama)")
    log(f"   - OSCAR domain DB:     postgresql://oscar@{SB_HOST}:{POSTGRES_PORT}/oscar")
    log("   - MCP servers to add:  ha-mcp, servicebay-mcp, oscar-connector-*")
    log("")
    log("3. Pair Hermes with Signal/Telegram/etc. via `hermes gateway setup`.")
    log("")
    log("4. Symlink OSCAR's skills into Hermes' skills dir (or copy):")
    log("   ln -s $(pwd)/skills ~/.hermes/skills/oscar")
    log("")
    log("5. Deploy oscar-voice + oscar-connectors next.")
    log("   See stacks/oscar/README.md for the full walkthrough.")
    log("")


def main() -> int:
    log("post-deploy: oscar-brain hook starting")
    pg_ok = wait_for_postgres()
    ollama_ok = check_ollama()

    if pg_ok and ollama_ok:
        log("post-deploy: success")
    else:
        log("post-deploy: pod is up but probes did not all succeed (see above)")

    print_next_steps()
    return 0


if __name__ == "__main__":
    sys.exit(main())
