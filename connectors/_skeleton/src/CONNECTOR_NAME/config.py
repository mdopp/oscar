"""Env-driven settings for the connector. Use pydantic-settings so the
container fails fast if a required env var is missing.

Phase-0 minimum: a port, the shared bearer, and whatever the connector
itself needs (API keys, endpoint overrides). Extend per connector.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # Pod-global
    connectors_bearer: str
    port: int = 8800

    # Connector-specific — override in subclass or via env vars per connector
    connector_name: str = "CONNECTOR_NAME"


settings = Settings()
