from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

import redis.asyncio as aioredis
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db import close_pool, get_pool, init_pool
from app.excel_export import (
    mark_job_done,
    mark_job_failed,
    mark_job_running,
    record_job,
    run_export,
)
from app.puller import REDIS_CURSOR_KEY, REDIS_DONE_KEY, REDIS_STATE_KEY

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    s = get_settings()
    logging.basicConfig(
        level=s.log_level,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    await init_pool()
    os.makedirs(s.export_dir, exist_ok=True)
    app.state.redis = aioredis.from_url(s.redis_url, decode_responses=True)
    yield
    await app.state.redis.close()
    await close_pool()


app = FastAPI(title="Shopify Order Download", lifespan=lifespan)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


class ExportRequest(BaseModel):
    start_date: Optional[datetime] = Field(
        default=None, description="Inclusive lower bound on order_date"
    )
    end_date: Optional[datetime] = Field(
        default=None, description="Inclusive upper bound on order_date"
    )


# ----------------------------------------------------------------------------
# health / status
# ----------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/stats")
async def stats() -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              COUNT(*)                AS total,
              MIN(order_date)         AS first_order,
              MAX(order_date)         AS last_order,
              MAX(ingested_at)        AS last_ingest
            FROM order_details
            """
        )
    redis = app.state.redis
    state_raw = await redis.get(REDIS_STATE_KEY)
    cursor = await redis.get(REDIS_CURSOR_KEY)
    done = await redis.get(REDIS_DONE_KEY)
    return {
        "orders": {
            "total":       row["total"],
            "first_order": row["first_order"].isoformat() if row["first_order"] else None,
            "last_order":  row["last_order"].isoformat() if row["last_order"] else None,
            "last_ingest": row["last_ingest"].isoformat() if row["last_ingest"] else None,
        },
        "puller": {
            "state":   json.loads(state_raw) if state_raw else None,
            "cursor":  (cursor[:32] + "…") if cursor else None,
            "backfill_done": done == "1",
        },
    }


# ----------------------------------------------------------------------------
# export jobs
# ----------------------------------------------------------------------------

async def _background_export(job_id: str, filters: dict[str, Any]) -> None:
    try:
        await mark_job_running(job_id)
        result = await run_export(job_id, filters)
        await mark_job_done(job_id, result)
        log.info("Export %s finished: %d rows in %d files",
                 job_id, result.total_rows, len(result.files))
    except Exception as exc:
        log.exception("Export %s failed", job_id)
        await mark_job_failed(job_id, str(exc))


@app.post("/exports")
async def create_export(
    req: ExportRequest, background: BackgroundTasks
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex[:16]
    filters: dict[str, Any] = {}
    if req.start_date:
        filters["start_date"] = req.start_date
    if req.end_date:
        filters["end_date"] = req.end_date

    serialisable = {k: v.isoformat() if isinstance(v, datetime) else v
                    for k, v in filters.items()}
    await record_job(job_id, get_settings().excel_rows_per_file, serialisable)
    background.add_task(_background_export, job_id, filters)
    return {"job_id": job_id, "status": "pending"}


@app.get("/exports/{job_id}")
async def get_export(job_id: str) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM export_jobs WHERE id=$1", job_id
        )
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    files = row["files"]
    if isinstance(files, str):
        files = json.loads(files)
    return {
        "job_id":       row["id"],
        "status":       row["status"],
        "requested_at": row["requested_at"].isoformat(),
        "started_at":   row["started_at"].isoformat() if row["started_at"] else None,
        "finished_at":  row["finished_at"].isoformat() if row["finished_at"] else None,
        "rows_per_file": row["rows_per_file"],
        "total_rows":   row["total_rows"],
        "file_count":   row["file_count"],
        "files":        files,
        "error":        row["error"],
    }


@app.get("/exports/{job_id}/files/{filename}")
async def download_file(job_id: str, filename: str):
    # Path traversal guard.
    if "/" in filename or ".." in filename or not filename.startswith(job_id):
        raise HTTPException(status_code=400, detail="bad filename")
    path = os.path.join(get_settings().export_dir, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


@app.get("/exports")
async def list_exports(limit: int = 50) -> dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, status, requested_at, finished_at, total_rows, file_count "
            "FROM export_jobs ORDER BY requested_at DESC LIMIT $1",
            limit,
        )
    return {
        "jobs": [
            {
                "job_id":       r["id"],
                "status":       r["status"],
                "requested_at": r["requested_at"].isoformat(),
                "finished_at":  r["finished_at"].isoformat() if r["finished_at"] else None,
                "total_rows":   r["total_rows"],
                "file_count":   r["file_count"],
            }
            for r in rows
        ]
    }
