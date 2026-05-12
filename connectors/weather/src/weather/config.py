"""Weather connector settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", env_prefix="")

    connectors_bearer: str
    port: int = 8801
    weather_api_key: str
    weather_language: str = "de"
    weather_units: str = "metric"
    weather_base_url: str = "https://api.openweathermap.org/data/2.5"


settings = Settings()
