from __future__ import annotations

from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # -- Shopify identity --------------------------------------------------
    # Shop *.myshopify.com handle (no protocol).
    shopify_store_url: str

    # REQUIRED. Admin GraphQL access token (`shpat_...`).
    shopify_access_token: str

    # Shopify App API key / secret. Not used for the GraphQL call itself,
    # but kept for HMAC verification on future webhook endpoints.
    shopify_api_key: Optional[str] = None
    shopify_api_secret: Optional[str] = None

    shopify_api_version: str = "2025-01"
    shopify_http_timeout_sec: int = 30

    # -- Backfill window ---------------------------------------------------
    shopify_start_date: Optional[str] = None
    shopify_end_date: str = "2026-07-31T23:59:59Z"
    shopify_page_size: int = 250
    shopify_cost_safety_multiplier: float = 2.0

    # -- Misc --------------------------------------------------------------
    # Email substituted when Shopify returns null for an order's email.
    customer_dummy_email: str = "noemail@customer.internal"

    # -- Postgres ----------------------------------------------------------
    postgres_host: str
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: str
    postgres_sslmode: str = "disable"

    # -- Redis -------------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"

    # -- Excel export ------------------------------------------------------
    excel_rows_per_file: int = 100_000
    export_dir: str = "/data/exports"

    # -- API ---------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    log_level: str = "INFO"

    @property
    def shopify_graphql_url(self) -> str:
        return (
            f"https://{self.shopify_store_url}/admin/api/"
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
