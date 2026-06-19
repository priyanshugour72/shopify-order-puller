from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import redis.asyncio as aioredis

from app.config import get_settings
from app.db import UPSERT_ORDER_SQL, close_pool, get_pool, init_pool
from app.shopify_client import ShopifyClient

log = logging.getLogger(__name__)

REDIS_CURSOR_KEY = "shopify:puller:cursor"
REDIS_STATE_KEY = "shopify:puller:state"     # JSON: {phase, processed, last_order_date}
REDIS_DONE_KEY = "shopify:puller:done"
REDIS_TAIL_HIGH_WATERMARK = "shopify:puller:tail_after"

TAIL_POLL_SECONDS = 300  # 5 minutes between incremental sweeps


def _build_query_filter() -> str:
    s = get_settings()
    parts: list[str] = []
    if s.shopify_start_date:
        parts.append(f"created_at:>={s.shopify_start_date}")
    parts.append(f"created_at:<={s.shopify_end_date}")
    parts.append("status:any")
    return " ".join(parts)


def _to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    # Shopify returns 'Z'-suffixed RFC3339 strings.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_decimal(s: Optional[str]) -> Optional[Decimal]:
    if s is None:
        return None
    try:
        return Decimal(str(s))
    except (InvalidOperation, TypeError):
        return None


def _flatten_order(o: dict[str, Any]) -> tuple:
    line_items_edges = ((o.get("lineItems") or {}).get("edges")) or []
    total_units = sum(
        int(((e.get("node") or {}).get("quantity")) or 0)
        for e in line_items_edges
    )

    customer = o.get("customer") or {}
    money = ((o.get("currentTotalPriceSet") or {}).get("shopMoney")) or {}

    return (
        o["id"],                                              # shopify_order_id (gid)
        o.get("name"),                                        # order_number e.g. "#1001"
        _to_dt(o.get("createdAt")),                           # order_date
        _to_dt(o.get("processedAt")),                         # processed_at
        o.get("email"),                                       # email
        o.get("phone"),                                       # phone
        customer.get("phone"),                                # customer_phone
        customer.get("firstName"),                            # customer_first_name
        customer.get("lastName"),                             # customer_last_name
        total_units,                                          # total_units
        len(line_items_edges),                                # total_line_items
        _to_decimal(money.get("amount")),                     # total_price
        money.get("currencyCode"),                            # currency
        o.get("displayFinancialStatus"),                      # financial_status
        o.get("displayFulfillmentStatus"),                    # fulfillment_status
        json.dumps(o),                                        # raw
    )


async def _persist_batch(orders: list[dict[str, Any]]) -> int:
    if not orders:
        return 0
    rows = [_flatten_order(o) for o in orders]
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(UPSERT_ORDER_SQL, rows)
    return len(rows)


async def _save_state(
    redis: aioredis.Redis,
    cursor: Optional[str],
    processed: int,
    last_order_date: Optional[str],
    phase: str,
) -> None:
    if cursor is not None:
        await redis.set(REDIS_CURSOR_KEY, cursor)
    await redis.set(
        REDIS_STATE_KEY,
        json.dumps(
            {
                "phase": phase,
                "processed": processed,
                "last_order_date": last_order_date,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
    )


async def run() -> None:
    s = get_settings()
    logging.basicConfig(
        level=s.log_level,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    log.info("Puller starting. graphql=%s window=%s..%s",
             s.shopify_graphql_url,
             s.shopify_start_date or "<beginning-of-time>",
             s.shopify_end_date)

    await init_pool()
    redis = aioredis.from_url(s.redis_url, decode_responses=True)

    done = await redis.get(REDIS_DONE_KEY)
    cursor = None if done else await redis.get(REDIS_CURSOR_KEY)
    state_raw = await redis.get(REDIS_STATE_KEY)
    state = json.loads(state_raw) if state_raw else {}
    processed = int(state.get("processed") or 0)

    if done:
        log.info("Backfill marked done; entering tail mode.")
    elif cursor:
        log.info("Resuming backfill from cursor=%s processed=%s",
                 cursor[:24] + "…", processed)
    else:
        log.info("Starting fresh backfill.")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(*_a: Any) -> None:
        log.info("Signal received, finishing current page then exiting.")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    last_order_date: Optional[str] = state.get("last_order_date")

    async with ShopifyClient(s) as client:
        # ---- phase 1: backfill ------------------------------------------------
        if not done:
            query_filter = _build_query_filter()
            async for nodes, end_cursor, has_next in client.iter_orders(
                query_filter, starting_cursor=cursor
            ):
                inserted = await _persist_batch(nodes)
                processed += inserted

                if nodes:
                    last_order_date = nodes[-1].get("createdAt")

                cost = client.last_cost
                log.info(
                    "backfill batch=%d total=%d last=%s next=%s cost=%s/%s avail=%.0f",
                    inserted,
                    processed,
                    last_order_date,
                    "yes" if has_next else "no",
                    int(cost.actual) if cost else "?",
                    int(cost.requested) if cost else "?",
                    cost.available if cost else -1,
                )

                await _save_state(
                    redis, end_cursor, processed, last_order_date,
                    phase="backfill" if has_next else "backfill-complete",
                )

                if stop.is_set():
                    log.info("Stop requested; exiting after %d orders.", processed)
                    await redis.close()
                    await close_pool()
                    return

                if not has_next:
                    log.info("Backfill complete. Total orders: %d", processed)
                    await redis.set(REDIS_DONE_KEY, "1")
                    if last_order_date:
                        await redis.set(REDIS_TAIL_HIGH_WATERMARK, last_order_date)
                    break

        # ---- phase 2: tail (poll for new orders until end_date) --------------
        await _tail_loop(client, redis, processed, last_order_date, stop)

    await redis.close()
    await close_pool()


async def _tail_loop(
    client: ShopifyClient,
    redis: aioredis.Redis,
    processed: int,
    last_order_date: Optional[str],
    stop: asyncio.Event,
) -> None:
    """After backfill, keep sweeping for orders newer than the high-watermark.

    Runs forever (until the container is stopped). Stops calling Shopify
    once the configured end_date is in the past.
    """
    s = get_settings()
    end_dt = _to_dt(s.shopify_end_date)

    high = await redis.get(REDIS_TAIL_HIGH_WATERMARK)
    if high:
        last_order_date = high

    while not stop.is_set():
        now_utc = datetime.now(timezone.utc)
        if end_dt and now_utc > end_dt:
            log.info("Past end_date (%s); tail loop idling.", s.shopify_end_date)
            await _sleep_or_stop(stop, 3600)
            continue

        if not last_order_date:
            # No watermark yet (empty shop?). Sweep the whole window.
            tail_filter = _build_query_filter()
        else:
            tail_filter = f"created_at:>{last_order_date} created_at:<={s.shopify_end_date} status:any"

        log.info("tail sweep filter=%r", tail_filter)
        new_in_sweep = 0
        async for nodes, _cursor, has_next in client.iter_orders(tail_filter):
            inserted = await _persist_batch(nodes)
            processed += inserted
            new_in_sweep += inserted
            if nodes:
                last_order_date = nodes[-1].get("createdAt")
            await _save_state(
                redis, None, processed, last_order_date, phase="tail",
            )
            if last_order_date:
                await redis.set(REDIS_TAIL_HIGH_WATERMARK, last_order_date)
            if stop.is_set():
                return
            if not has_next:
                break

        log.info("tail sweep done: %d new orders, sleeping %ds",
                 new_in_sweep, TAIL_POLL_SECONDS)
        await _sleep_or_stop(stop, TAIL_POLL_SECONDS)


async def _sleep_or_stop(stop: asyncio.Event, seconds: int) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


if __name__ == "__main__":
    asyncio.run(run())
