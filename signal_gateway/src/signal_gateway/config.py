"""Env-driven settings for the Signal gateway."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    signal_rest_url: str = "http://127.0.0.1:8080"
    signal_account: str  # required — paired number
    signal_token: str = ""  # bearer for POST /send; empty disables auth

    hermes_url: str = "http://127.0.0.1:8000"
    hermes_token: str = ""

    postgres_dsn: str  # required — for gateway_identities lookup

    listen_host: str = "0.0.0.0"
    listen_port: int = 8090

    poll_interval_s: float = 2.0
