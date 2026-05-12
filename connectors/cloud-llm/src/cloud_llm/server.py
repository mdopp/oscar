"""FastMCP entry point for the cloud-LLM connector."""

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from oscar_logging import log

from .config import settings
from .tools import complete


def build_server() -> FastMCP:
    auth = StaticTokenVerifier(
        tokens={
            settings.connectors_bearer: {"sub": "hermes", "client_id": "oscar-brain"}
        }
    )
    mcp = FastMCP(name="oscar-connector-cloud-llm", auth=auth)
    mcp.tool()(complete.run)
    return mcp


def main() -> None:
    mcp = build_server()
    log.info(
        "connector.boot",
        port=settings.port,
        anthropic_enabled=bool(settings.anthropic_api_key),
        google_enabled=bool(settings.google_api_key),
    )
    mcp.run(host="0.0.0.0", port=settings.port, transport="streamable-http")


if __name__ == "__main__":
    main()
