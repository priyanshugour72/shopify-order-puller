from __future__ import annotations

import logging
from typing import Optional

import asyncpg

from app.config import get_settings

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS order_details (
    id                  BIGSERIAL PRIMARY KEY,
    shopify_order_id    TEXT        NOT NULL UNIQUE,
    order_number        TEXT,
    order_date          TIMESTAMPTZ NOT NULL,
    processed_at        TIMESTAMPTZ,
    email               TEXT,
    phone               TEXT,
    customer_phone      TEXT,
    customer_first_name TEXT,
    customer_last_name  TEXT,
    total_units         INTEGER     NOT NULL DEFAULT 0,
    total_line_items    INTEGER     NOT NULL DEFAULT 0,
    total_price         NUMERIC(14, 2),
    currency            TEXT,
    financial_status    TEXT,
    fulfillment_status  TEXT,
    raw                 JSONB,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_order_details_order_date
    ON order_details (order_date);

CREATE INDEX IF NOT EXISTS idx_order_details_email
    ON order_details (email);

CREATE INDEX IF NOT EXISTS idx_order_details_phone
    ON order_details (phone);

CREATE TABLE IF NOT EXISTS export_jobs (
    id              TEXT PRIMARY KEY,
    status          TEXT NOT NULL,           -- pending | running | completed | failed
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    rows_per_file   INTEGER NOT NULL,
    total_rows      BIGINT,
    file_count      INTEGER,
    files           JSONB NOT NULL DEFAULT '[]'::jsonb,
    filters         JSONB NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT
);
"""


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool

    s = get_settings()
    ssl = s.postgres_sslmode if s.postgres_sslmode != "disable" else False

    _pool = await asyncpg.create_pool(
        host=s.postgres_host,
        port=s.postgres_port,
        user=s.postgres_user,
        password=s.postgres_password,
        database=s.postgres_db,
        ssl=ssl,
        min_size=1,
        max_size=10,
        command_timeout=60,
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    log.info("Postgres pool ready, schema ensured.")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first.")
    return _pool


UPSERT_ORDER_SQL = """
INSERT INTO order_details (
    shopify_order_id, order_number, order_date, processed_at, email, phone,
    customer_phone, customer_first_name, customer_last_name,
    total_units, total_line_items, total_price, currency,
    financial_status, fulfillment_status, raw, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9,
    $10, $11, $12, $13,
    $14, $15, $16::jsonb, NOW()
)
ON CONFLICT (shopify_order_id) DO UPDATE SET
    order_number        = EXCLUDED.order_number,
    order_date          = EXCLUDED.order_date,
    processed_at        = EXCLUDED.processed_at,
    email               = EXCLUDED.email,
    phone               = EXCLUDED.phone,
    customer_phone      = EXCLUDED.customer_phone,
    customer_first_name = EXCLUDED.customer_first_name,
    customer_last_name  = EXCLUDED.customer_last_name,
    total_units         = EXCLUDED.total_units,
    total_line_items    = EXCLUDED.total_line_items,
    total_price         = EXCLUDED.total_price,
    currency            = EXCLUDED.currency,
    financial_status    = EXCLUDED.financial_status,
    fulfillment_status  = EXCLUDED.fulfillment_status,
    raw                 = EXCLUDED.raw,
    updated_at          = NOW();
"""
