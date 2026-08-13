"""End-to-end MCP transport test (#1106).

The engine's MCP imports are function-local, so a green suite is otherwise no
evidence the client can still reach a server: the whole `servicebay_admin` /
room-tool path used to break only at runtime on the box when the SDK renamed
`streamablehttp_client`. This drives `McpToolbox` and `call_sb_tool` over real
streamable HTTP against a real MCP server so the next such rename fails CI.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer

from solaris_chat.engine.tools.mcp_tools import McpToolbox, call_sb_tool


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def mcp_url():
    server = MCPServer("test-server")

    @server.tool()
    async def echo(text: str) -> dict:
        """Return `text` back to the caller."""
        return {"echoed": text}

    port = _free_port()
    uv = uvicorn.Server(
        uvicorn.Config(
            server.streamable_http_app(),
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="on",
        )
    )
    task = asyncio.create_task(uv.serve())
    while not uv.started:
        await asyncio.sleep(0.01)
    yield f"http://127.0.0.1:{port}/mcp"
    uv.should_exit = True
    await task


async def test_toolbox_lists_and_calls_over_streamable_http(mcp_url, tmp_path):
    box = McpToolbox(mcp_url, str(tmp_path / "absent-token"))
    await box.prepare()

    assert box.names() == ["echo"]
    schema = box.definitions()[0]["function"]["parameters"]
    assert "text" in schema["properties"]

    assert "hello" in await box.dispatch("echo", {"text": "hello"})


async def test_call_sb_tool_over_streamable_http(mcp_url, tmp_path):
    out = await call_sb_tool(
        mcp_url, str(tmp_path / "absent-token"), "echo", {"text": "hi"}
    )
    assert "hi" in out
