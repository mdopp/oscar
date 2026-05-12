"""FastMCP entry point for the connector. Adapts to the concrete tool list.

The pattern is:
1. Build a StaticTokenVerifier from CONNECTORS_BEARER so all connectors in
   the pod share the same auth surface against HERMES.
2. Register each tool module's `run` coroutine.
3. Start streamable-http transport on PORT.
"""

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from oscar_logging import log

from .config import settings
from .tools import example


def build_server() -> FastMCP:
    auth = StaticTokenVerifier(
        tokens={
            settings.connectors_bearer: {"sub": "hermes", "client_id": "oscar-brain"}
        }
    )
    mcp = FastMCP(name=f"oscar-connector-{settings.connector_name}", auth=auth)
    mcp.tool()(example.run)
    return mcp


def main() -> None:
    mcp = build_server()
    log.info("connector.boot", port=settings.port)
    mcp.run(host="0.0.0.0", port=settings.port, transport="streamable-http")


if __name__ == "__main__":
    main()
