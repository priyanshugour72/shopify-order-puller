from __future__ import annotations

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Shopify
    shopify_shop: str
    shopify_access_token: str
    shopify_api_version: str = "2025-01"
    shopify_start_date: Optional[str] = None
    shopify_end_date: str = "2026-07-31T23:59:59Z"
    shopify_page_size: int = 250
    shopify_cost_safety_multiplier: float = 2.0

    # Postgres
    postgres_host: str
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: str
    postgres_sslmode: str = "disable"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Excel
    excel_rows_per_file: int = 100_000
    export_dir: str = "/data/exports"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Logging
    log_level: str = "INFO"

    @property
    def shopify_graphql_url(self) -> str:
        return (
            f"https://{self.shopify_shop}/admin/api/"
            f"{self.shopify_api_version}/graphql.json"
        )

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgres://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
