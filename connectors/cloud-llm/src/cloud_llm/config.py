"""Env-driven settings for the cloud-LLM connector."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # Pod-global
    connectors_bearer: str
    port: int = 8802

    # Provider credentials — at least one must be set, depending on which
    # vendors HERMES will route through this connector. Empty disables
    # the corresponding provider; the tool returns a clear error then.
    anthropic_api_key: str = ""
    google_api_key: str = ""

    # Postgres DSN for cloud_audit writes. Same DB as oscar-brain.
    # Pod-internal localhost connection because oscar-connectors and
    # oscar-brain both run on hostNetwork on the same host.
    postgres_dsn: str

    # Optional override for testing — usually leave at defaults.
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    google_base_url: str = "https://generativelanguage.googleapis.com/v1beta"


settings = Settings()
