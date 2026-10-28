"""Application settings. The only place that reads `.env` or the environment."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    db_path: Path = Path("data/cloudmetrics.db")
    glossary_path: Path = Path("data/cloudmetrics.yaml")
    row_cap: int = 10_000
    query_timeout_s: float = 10.0
    max_concurrent_queries: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
