"""FastMCP entry point for the weather connector."""

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from oscar_logging import log

from .config import settings
from .tools import current_weather, forecast


def build_server() -> FastMCP:
    auth = StaticTokenVerifier(
        tokens={settings.connectors_bearer: {"sub": "hermes", "client_id": "oscar-brain"}}
    )
    mcp = FastMCP(name="oscar-connector-weather", auth=auth)
    mcp.tool()(current_weather.run)
    mcp.tool()(forecast.run)
    return mcp


def main() -> None:
    mcp = build_server()
    log.info("connector.boot", port=settings.port, language=settings.weather_language, units=settings.weather_units)
    mcp.run(host="0.0.0.0", port=settings.port, transport="streamable-http")


if __name__ == "__main__":
    main()
