"""Every tool name a shipped skill body tells the model to call must be a tool
that really exists somewhere in this repo's tool surfaces.

#1294 shipped two admin skills instructing `get_container_logs` /
`get_service_logs`; ServiceBay-MCP has only `get_logs(source=...)`, so the call
came back `unknown tool: ...` and the operator persona had to improvise around
it. A wrong name in prompt text is invisible to ruff, to pytest and to CI, and
only surfaces as a bad turn on the box — so pin it here.

Two of the three inventories are read from the code that defines them, so a
renamed tool fails this test instead of drifting. ServiceBay-MCP is an external
server, so its half is the checked-in list of what the skills currently use:
naming a new SB tool fails here until it is verified against the live MCP and
added.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "templates" / "solaris" / "skills"

# A backticked token is a tool call when it opens with one of these prefixes;
# every other backticked span in the packs is a field, an arg or a value.
_VERBS = (
    "add_",
    "create_",
    "delete_",
    "diagnose",
    "get_",
    "ha_",
    "list_",
    "poll_",
    "read_",
    "remove_",
    "request_",
    "restart_",
    "run_",
    "search_",
    "set_",
    "start_",
    "stop_",
    "update_",
    "write_",
)
_TOKEN = re.compile(r"`([a-z][a-z0-9_]*)\b")

# ServiceBay-MCP (external): the tools the admin pack calls, as the live server
# advertises them. Verify a new entry against the running MCP before adding it.
SB_MCP = {
    "create_health_check",
    "diagnose",
    "get_health_checks",
    "get_logs",
    "get_service_files",
    "list_containers",
    "list_services",
}


def _engine_tools() -> set[str]:
    tools = ROOT / "solaris-chat" / "src" / "solaris_chat" / "engine" / "tools"
    return {
        m.group(1)
        for path in tools.glob("*.py")
        for m in re.finditer(r'name="([a-z][a-z0-9_]*)"', path.read_text())
    }


def _gatekeeper_mcp_tools() -> set[str]:
    src = (
        ROOT / "voice-gatekeeper" / "src" / "gatekeeper" / "mcp_server.py"
    ).read_text()
    return set(re.findall(r"@mcp\.tool\(\)\s+async def (\w+)", src))


def _named_tools(body: str) -> set[str]:
    return {
        m.group(1)
        for m in _TOKEN.finditer(body)
        if m.group(1).startswith(_VERBS) and not m.group(1).endswith("_")
    }


def test_shipped_skill_bodies_only_name_real_tools():
    known = SB_MCP | _engine_tools() | _gatekeeper_mcp_tools()
    unknown: dict[str, set[str]] = {}
    for path in sorted(SKILLS.rglob("*.md")):
        if path.name not in ("SKILL.md", "SOUL.md"):
            continue
        missing = _named_tools(path.read_text()) - known
        if missing:
            unknown[str(path.relative_to(ROOT))] = missing
    assert not unknown, f"skill bodies name tools nothing exposes: {unknown}"
