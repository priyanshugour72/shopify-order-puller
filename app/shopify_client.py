from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import Settings, get_settings

log = logging.getLogger(__name__)


# We pull a useful slice of fields per order. Keeping the query lean keeps the
# Shopify cost down so we can do many more requests/sec than the naive 2/sec.
ORDERS_QUERY = """
query Orders($first: Int!, $cursor: String, $query: String!) {
  orders(first: $first, after: $cursor, query: $query, sortKey: CREATED_AT) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      name
      createdAt
      processedAt
      email
      phone
      displayFinancialStatus
      displayFulfillmentStatus
      currentTotalPriceSet {
        shopMoney { amount currencyCode }
      }
      customer {
        firstName
        lastName
        phone
      }
      lineItems(first: 50) {
        edges {
          node {
            quantity
          }
        }
      }
    }
  }
}
"""


class ShopifyError(Exception):
    pass


class ShopifyThrottledError(ShopifyError):
    """Raised on a THROTTLED extension code so the caller can sleep + retry."""


@dataclass
class CostInfo:
    requested: float
    actual: float
    available: float
    maximum: float
    restore_rate: float


class ShopifyClient:
    """Async Shopify Admin GraphQL client with cost-aware adaptive throttling.

    Shopify returns `extensions.cost.throttleStatus` on every response. We
    use it to know how many points are left and how fast they refill, then
    sleep just enough between calls to stay safely below the bucket floor.
    Because our query is small, the bucket is rarely an issue — typical
    throughput is well above the 2 req/s a naive limiter would assume.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.s = settings or get_settings()
        self._client: Optional[httpx.AsyncClient] = None
        self._last_cost: Optional[CostInfo] = None

    async def __aenter__(self) -> "ShopifyClient":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={
                "X-Shopify-Access-Token": self.s.shopify_access_token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _execute(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        assert self._client is not None, "use `async with ShopifyClient()`"

        await self._maybe_sleep_for_cost()

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(6),
            wait=wait_exponential(multiplier=1.5, min=2, max=30),
            retry=retry_if_exception_type(
                (httpx.HTTPError, ShopifyThrottledError)
            ),
            reraise=True,
        ):
            with attempt:
                resp = await self._client.post(
                    self.s.shopify_graphql_url,
                    json={"query": query, "variables": variables},
                )
                if resp.status_code == 429:
                    # Shopify also signals throttling via plain 429 sometimes.
                    retry_after = float(resp.headers.get("Retry-After", "2"))
                    log.warning("HTTP 429, sleeping %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    raise ShopifyThrottledError("HTTP 429")
                resp.raise_for_status()
                payload = resp.json()

                if "errors" in payload:
                    if self._is_throttle_error(payload["errors"]):
                        await asyncio.sleep(2.0)
                        raise ShopifyThrottledError(str(payload["errors"]))
                    raise ShopifyError(f"GraphQL errors: {payload['errors']}")

                self._capture_cost(payload.get("extensions"))
                return payload["data"]

        raise ShopifyError("unreachable")  # pragma: no cover

    @staticmethod
    def _is_throttle_error(errors: list[dict[str, Any]]) -> bool:
        return any(
            (e.get("extensions") or {}).get("code") == "THROTTLED"
            for e in errors
        )

    def _capture_cost(self, extensions: Optional[dict[str, Any]]) -> None:
        if not extensions:
            return
        cost = extensions.get("cost") or {}
        ts = cost.get("throttleStatus") or {}
        try:
            self._last_cost = CostInfo(
                requested=float(cost.get("requestedQueryCost", 0)),
                actual=float(cost.get("actualQueryCost", 0)),
                available=float(ts.get("currentlyAvailable", 0)),
                maximum=float(ts.get("maximumAvailable", 0)),
                restore_rate=float(ts.get("restoreRate", 50)),
            )
        except (TypeError, ValueError):
            self._last_cost = None

    async def _maybe_sleep_for_cost(self) -> None:
        c = self._last_cost
        if c is None or c.restore_rate <= 0:
            return
        # Keep a safety margin: don't fire the next request unless we have
        # at least (last actual cost * multiplier) points in the bucket.
        target = max(c.actual, 50) * self.s.shopify_cost_safety_multiplier
        if c.available >= target:
            return
        deficit = target - c.available
        sleep_s = deficit / c.restore_rate
        # Hard cap so a misconfigured rate doesn't park us forever.
        sleep_s = min(sleep_s, 10.0)
        if sleep_s > 0.01:
            log.debug(
                "throttle wait %.2fs (available=%.0f target=%.0f rate=%.0f)",
                sleep_s, c.available, target, c.restore_rate,
            )
            await asyncio.sleep(sleep_s)

    @property
    def last_cost(self) -> Optional[CostInfo]:
        return self._last_cost

    async def iter_orders(
        self,
        query_filter: str,
        starting_cursor: Optional[str] = None,
    ) -> AsyncIterator[tuple[list[dict[str, Any]], Optional[str], bool]]:
        """Yield (page_nodes, end_cursor, has_next_page) tuples."""
        cursor = starting_cursor
        while True:
            data = await self._execute(
                ORDERS_QUERY,
                {
                    "first": self.s.shopify_page_size,
                    "cursor": cursor,
                    "query": query_filter,
                },
            )
            orders = data["orders"]
            nodes = orders["nodes"]
            page_info = orders["pageInfo"]
            end_cursor = page_info.get("endCursor")
            has_next = page_info.get("hasNextPage", False)

            yield nodes, end_cursor, has_next

            if not has_next:
                return
            cursor = end_cursor
