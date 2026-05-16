#!/usr/bin/env python3
"""Post-deploy hook for oscar-household.

Runs on the host (non-interactive) after the pod's containers report
Ready. Idempotent on every invocation.

What it does
============

1. **Wait for Hermes** to be ready by polling `GET /health` with the
   bearer token. The endpoint is documented at
   https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server#endpoints.
   Whether `/health` is auth-exempt is undocumented as of writing; sending
   the bearer either way works as the strict superset.

2. **Read `${DATA_DIR}/hermes/config.yaml`** — the file ServiceBay's
   `hermes` template's post-deploy wrote with the `model:` block.

3. **Splice in our `mcp_servers:` block** with HA-MCP and ServiceBay-MCP
   entries (and the cloud-LLM audit-proxy once that package ships).
   Remote-MCP shape per the
   https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference:

     mcp_servers:
       <name>:
         url: "<url>"
         headers:
           Authorization: "Bearer <token>"

   Hand-rolled YAML — same approach as ServiceBay's hermes/post-deploy.py,
   no PyYAML dependency. Re-running the script replaces the existing
   mcp_servers block in-place (idempotent).

4. **Write the merged file back.**

5. **POST `/api/services/hermes/action {action: "restart"}`** via
   `SB_API_URL` so Hermes picks up the new mcp_servers block on next
   start. Hermes reads config.yaml only at boot; a slash-command
   `/reload-mcp` exists but only via an active gateway session, which
   we don't have from a post-deploy.

Caveat
======

If ServiceBay's `hermes` template is re-deployed, its post-deploy
overwrites config.yaml with just the model block — losing our
mcp_servers block. Re-deploy `oscar-household` to restore it.

Variables (ServiceBay substitutes them)
=======================================

From our variables.json:
  HERMES_API_PORT, HERMES_API_KEY,
  HA_MCP_URL, HA_MCP_TOKEN,
  SERVICEBAY_MCP_URL, SERVICEBAY_MCP_TOKEN

From the ServiceBay platform:
  DATA_DIR, SB_API_URL, HOST, SB_API_TOKEN
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request


# ───── env ─────────────────────────────────────────────────────────────────

DATA_DIR = os.environ.get("DATA_DIR", "/mnt/data")
SB_API_URL = os.environ.get("SB_API_URL", "http://127.0.0.1:3000").rstrip("/")
SB_API_TOKEN = os.environ.get("SB_API_TOKEN", "")

HERMES_API_PORT = os.environ.get("HERMES_API_PORT", "8642")
HERMES_API_KEY = os.environ["HERMES_API_KEY"]
HERMES_API_URL = f"http://127.0.0.1:{HERMES_API_PORT}"

# MCPs we register. Optional — empty url/token means "operator hasn't
# wired this side yet", we skip without failing.
HA_MCP_URL = os.environ.get("HA_MCP_URL", "")
HA_MCP_TOKEN = os.environ.get("HA_MCP_TOKEN", "")
SERVICEBAY_MCP_URL = os.environ.get("SERVICEBAY_MCP_URL", "")
SERVICEBAY_MCP_TOKEN = os.environ.get("SERVICEBAY_MCP_TOKEN", "")

CONFIG_PATH = os.path.join(DATA_DIR, "hermes", "config.yaml")
READINESS_TIMEOUT_S = int(os.environ.get("HERMES_READINESS_TIMEOUT_S", "120"))


# ───── helpers ─────────────────────────────────────────────────────────────


def jlog(level: str, tag: str, message: str, **args: object) -> None:
    """Emit one JSON line on stdout matching ServiceBay's logger contract
    (docs/TEMPLATE_LOGGING.md): {ts, level, tag, message, args}."""
    sys.stdout.write(
        json.dumps(
            {
                "ts": datetime.datetime.now().astimezone().isoformat(),
                "level": level,
                "tag": tag,
                "message": message,
                "args": args,
            }
        )
        + "\n"
    )
    sys.stdout.flush()


def hermes_get(path: str, timeout: float = 5.0) -> int:
    """GET against Hermes' API with bearer auth. Returns HTTP status, or 0
    for connection failure."""
    req = urllib.request.Request(f"{HERMES_API_URL}{path}", method="GET")
    req.add_header("Authorization", f"Bearer {HERMES_API_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return 0


def sb_post(path: str, payload: dict[str, object], timeout: float = 30.0) -> int:
    """POST against ServiceBay's API with the internal-token header (the
    same shape ServiceBay's own post-deploys use)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SB_API_URL}{path}",
        data=body,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    if SB_API_TOKEN:
        req.add_header("X-SB-Internal-Token", SB_API_TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return 0


# ───── config.yaml merge ───────────────────────────────────────────────────


def strip_mcp_servers_block(content: str) -> str:
    """Remove an existing `mcp_servers:` top-level block from the file.

    Top-level = line starts at column 0 with `mcp_servers:` (any trailing
    whitespace OK). Block ends at the next column-0 non-comment non-blank
    line (the next top-level key), or EOF.

    Idempotent for the empty case: input without an mcp_servers block
    returns unchanged.
    """
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    in_block = False
    for line in lines:
        if not in_block:
            stripped = line.lstrip()
            # Match `mcp_servers:` at column 0 (no leading whitespace).
            if line[:1] not in (" ", "\t") and stripped.startswith("mcp_servers:"):
                in_block = True
                continue  # drop this line
            out.append(line)
        else:
            # In block: end on the next column-0 non-comment non-blank line.
            stripped = line.lstrip()
            if (
                line[:1] not in (" ", "\t")
                and stripped
                and not stripped.startswith("#")
            ):
                in_block = False
                out.append(line)
            # else: still inside the block (indented, blank, or comment) → drop
    return "".join(out)


def render_mcp_block(servers: list[tuple[str, str, str]]) -> str:
    """Render an `mcp_servers:` block for the given (name, url, token) entries.

    Remote-MCP shape per the Hermes MCP Config Reference; static-bearer
    auth is conveyed via the `headers` field and works without an explicit
    `auth:` declaration (the `auth: oauth` form in the reference is
    specific to OAuth flows).
    """
    if not servers:
        return ""
    parts: list[str] = ["mcp_servers:\n"]
    for name, url, token in servers:
        parts.append(f"  {name}:\n")
        parts.append(f'    url: "{url}"\n')
        parts.append(f"    headers:\n")
        parts.append(f'      Authorization: "Bearer {token}"\n')
    return "".join(parts)


def merge_config_yaml(servers: list[tuple[str, str, str]]) -> bool:
    """Read config.yaml, strip any existing mcp_servers block, append the
    rendered one. Returns True on write, False if config.yaml doesn't
    exist yet (caller decides whether that's fatal)."""
    if not os.path.exists(CONFIG_PATH):
        jlog(
            "warn", "oscar-household:config", "config.yaml not found", path=CONFIG_PATH
        )
        return False
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        existing = f.read()
    stripped = strip_mcp_servers_block(existing)
    block = render_mcp_block(servers)
    # Append separator if there's preceding content
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    merged = stripped + ("\n" + block if block else "")
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(merged)
    jlog(
        "info",
        "oscar-household:config",
        "config.yaml mcp_servers block updated",
        path=CONFIG_PATH,
        mcp_servers=[name for name, _, _ in servers],
    )
    return True


# ───── steps ───────────────────────────────────────────────────────────────


def wait_for_hermes() -> None:
    deadline = time.time() + READINESS_TIMEOUT_S
    last_status: int | None = None
    while time.time() < deadline:
        status = hermes_get("/health")
        # 200 = healthy. 401/403 = auth wall but listener up — sufficient
        # for "container ready" (we won't be able to call further routes
        # if our token is wrong, but the post-deploy will surface that
        # via the restart-API call instead).
        if status in (200, 401, 403):
            jlog("info", "oscar-household:hermes", "ready", status=status)
            return
        last_status = status
        time.sleep(2)
    jlog(
        "error",
        "oscar-household:hermes",
        "not reachable within readiness window",
        last_status=last_status,
        timeout_s=READINESS_TIMEOUT_S,
    )
    raise SystemExit(1)


def restart_hermes_via_sb_api() -> bool:
    status = sb_post(f"/api/services/hermes/action", {"action": "restart"})
    if status == 200:
        jlog(
            "info",
            "oscar-household:restart",
            "hermes restart requested via ServiceBay API",
        )
        return True
    jlog(
        "warn",
        "oscar-household:restart",
        "restart request failed; new mcp_servers block lands on next manual restart",
        status=status,
    )
    return False


def collect_mcp_servers() -> list[tuple[str, str, str]]:
    """Pair each MCP with its token; skip empty entries."""
    servers: list[tuple[str, str, str]] = []
    if HA_MCP_URL and HA_MCP_TOKEN:
        servers.append(("ha-mcp", HA_MCP_URL, HA_MCP_TOKEN))
    else:
        jlog(
            "info",
            "oscar-household:mcp",
            "ha-mcp skipped",
            reason="missing url or token",
        )
    if SERVICEBAY_MCP_URL and SERVICEBAY_MCP_TOKEN:
        servers.append(("servicebay-mcp", SERVICEBAY_MCP_URL, SERVICEBAY_MCP_TOKEN))
    else:
        jlog(
            "info",
            "oscar-household:mcp",
            "servicebay-mcp skipped",
            reason="missing url or token",
        )
    return servers


def main() -> int:
    wait_for_hermes()
    servers = collect_mcp_servers()
    if not merge_config_yaml(servers):
        return 0  # config.yaml doesn't exist; nothing to do, not fatal
    if servers:
        restart_hermes_via_sb_api()
    jlog("info", "oscar-household:post-deploy", "done", mcp_count=len(servers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
