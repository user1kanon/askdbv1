"""Application settings. This is the only module that reads `.env` or the environment."""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: SecretStr | None = None
    db_path: Path = Path("data/cloudmetrics.db")
    glossary_path: Path = Path("data/cloudmetrics.yaml")
    row_cap: int = Field(10_000, gt=0)
    query_timeout_s: float = Field(10.0, gt=0)
    max_concurrent_queries: int = Field(2, gt=0)

    @field_validator("db_path", "glossary_path")
    @classmethod
    def _anchor_to_project_root(cls, value: Path) -> Path:
        """Relative paths resolve against the project root, not the launch directory."""
        return value if value.is_absolute() else PROJECT_ROOT / value

    @property
    def has_api_key(self) -> bool:
        return bool(self.anthropic_api_key and self.anthropic_api_key.get_secret_value().strip())


def get_settings() -> Settings:
    return Settings()
