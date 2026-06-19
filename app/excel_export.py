from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from app.config import get_settings
from app.db import get_pool

log = logging.getLogger(__name__)

EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("order_number",        "Order #"),
    ("shopify_order_id",    "Shopify Order ID"),
    ("order_date",          "Order Date"),
    ("processed_at",        "Processed At"),
    ("email",               "Email"),
    ("phone",               "Phone"),
    ("customer_phone",      "Customer Phone"),
    ("customer_first_name", "First Name"),
    ("customer_last_name",  "Last Name"),
    ("total_units",         "Total Units"),
    ("total_line_items",    "Line Items"),
    ("total_price",         "Total Price"),
    ("currency",            "Currency"),
    ("financial_status",    "Financial Status"),
    ("fulfillment_status",  "Fulfillment Status"),
]

SELECT_COLS = ", ".join(c for c, _ in EXPORT_COLUMNS)


@dataclass
class ExportResult:
    job_id: str
    total_rows: int
    files: list[dict[str, Any]]   # [{filename, rows, size_bytes}]


def _format_cell(value: Any) -> Any:
    if isinstance(value, datetime):
        # openpyxl handles datetime natively; strip tz so Excel doesn't choke.
        return value.replace(tzinfo=None)
    return value


async def _write_one_file(
    out_path: str,
    header_row: list[str],
    rows: list[tuple[Any, ...]],
) -> int:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title="Orders")
    ws.append([WriteOnlyCell(ws, value=h) for h in header_row])
    for row in rows:
        ws.append([WriteOnlyCell(ws, value=_format_cell(v)) for v in row])
    wb.save(out_path)
    return os.path.getsize(out_path)


def _build_where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if start := filters.get("start_date"):
        params.append(start)
        where.append(f"order_date >= ${len(params)}")
    if end := filters.get("end_date"):
        params.append(end)
        where.append(f"order_date <= ${len(params)}")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return clause, params


async def _count_rows(filters: dict[str, Any]) -> int:
    clause, params = _build_where(filters)
    pool = get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"SELECT COUNT(*) FROM order_details {clause}", *params
        )


async def run_export(job_id: str, filters: Optional[dict[str, Any]] = None) -> ExportResult:
    """Stream all order_details rows into Excel files of `rows_per_file` each."""
    s = get_settings()
    filters = filters or {}
    rows_per_file = s.excel_rows_per_file

    os.makedirs(s.export_dir, exist_ok=True)

    total = await _count_rows(filters)
    log.info("Export %s: %d rows, %d per file → %d files",
             job_id, total, rows_per_file,
             (total + rows_per_file - 1) // max(rows_per_file, 1))

    headers = [label for _, label in EXPORT_COLUMNS]
    where_clause, where_params = _build_where(filters)

    files: list[dict[str, Any]] = []
    pool = get_pool()

    file_index = 0
    rows_written_total = 0

    # Stream with a server-side cursor; load in DB-page chunks, then flush
    # each Excel file as soon as it fills.
    async with pool.acquire() as conn:
        async with conn.transaction():
            cur = conn.cursor(
                f"SELECT {SELECT_COLS} FROM order_details "
                f"{where_clause} ORDER BY order_date, id",
                *where_params,
                prefetch=5000,
            )

            buffer: list[tuple[Any, ...]] = []
            async for record in cur:
                buffer.append(tuple(record))
                if len(buffer) >= rows_per_file:
                    file_index += 1
                    fname = f"{job_id}_part_{file_index:04d}.xlsx"
                    fpath = os.path.join(s.export_dir, fname)
                    size = await _write_one_file(fpath, headers, buffer)
                    files.append({"filename": fname, "rows": len(buffer), "size_bytes": size})
                    rows_written_total += len(buffer)
                    log.info("wrote %s (%d rows, %.1f MiB)",
                             fname, len(buffer), size / (1024 * 1024))
                    buffer = []

            if buffer:
                file_index += 1
                fname = f"{job_id}_part_{file_index:04d}.xlsx"
                fpath = os.path.join(s.export_dir, fname)
                size = await _write_one_file(fpath, headers, buffer)
                files.append({"filename": fname, "rows": len(buffer), "size_bytes": size})
                rows_written_total += len(buffer)
                log.info("wrote %s (%d rows, %.1f MiB) [final]",
                         fname, len(buffer), size / (1024 * 1024))

    return ExportResult(job_id=job_id, total_rows=rows_written_total, files=files)


async def record_job(
    job_id: str,
    rows_per_file: int,
    filters: dict[str, Any],
) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO export_jobs (id, status, rows_per_file, filters)
            VALUES ($1, 'pending', $2, $3::jsonb)
            """,
            job_id, rows_per_file, json.dumps(filters),
        )


async def mark_job_running(job_id: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE export_jobs SET status='running', started_at=NOW() WHERE id=$1",
            job_id,
        )


async def mark_job_done(job_id: str, result: ExportResult) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE export_jobs
               SET status='completed', finished_at=NOW(),
                   total_rows=$2, file_count=$3, files=$4::jsonb
             WHERE id=$1
            """,
            job_id, result.total_rows, len(result.files), json.dumps(result.files),
        )


async def mark_job_failed(job_id: str, error: str) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE export_jobs
               SET status='failed', finished_at=NOW(), error=$2
             WHERE id=$1
            """,
            job_id, error,
        )
