"""Polymarket Data API + Gamma API async client."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

import httpx

QueryParams = dict[str, Any] | Sequence[tuple[str, Any]] | None

from .config import Config
from .models import GammaMarket, Position, Trader

log = logging.getLogger(__name__)

_RETRIABLE_STATUS = {429, 500, 502, 503, 504}


class PolymarketClient:
    def __init__(self, cfg: Config, *, concurrency: int = 10, timeout: float = 30.0):
        self._cfg = cfg
        self._sem = asyncio.Semaphore(concurrency)
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "PolymarketClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: QueryParams = None) -> Any:
        backoff = 1.0
        for attempt in range(5):
            async with self._sem:
                try:
                    r = await self._client.get(url, params=params)
                except httpx.RequestError as e:
                    log.warning("request error on %s (attempt %d): %s", url, attempt + 1, e)
                    if attempt == 4:
                        raise
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue

            if r.status_code in _RETRIABLE_STATUS:
                log.warning("HTTP %d on %s (attempt %d), backing off %.1fs",
                            r.status_code, url, attempt + 1, backoff)
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            r.raise_for_status()
            return r.json()

        raise RuntimeError(f"max retries exceeded for {url}")

    async def fetch_leaderboard(
        self,
        *,
        limit: int | None = None,
        time_period: str | None = None,
        order_by: str | None = None,
        category: str = "OVERALL",
    ) -> list[Trader]:
        params = {
            "category": category,
            "timePeriod": time_period or self._cfg.time_period,
            "orderBy": order_by or self._cfg.order_by,
            "limit": limit or self._cfg.top_n_traders,
        }
        raw = await self._get(f"{self._cfg.data_api}/v1/leaderboard", params)
        return [Trader.model_validate(r) for r in raw]

    async def fetch_leaderboards(
        self,
        categories: list[str],
        *,
        limit: int | None = None,
    ) -> dict[str, list[Trader]]:
        """Fetch multiple leaderboards in parallel. Failed ones return empty lists."""
        results = await asyncio.gather(
            *(self.fetch_leaderboard(limit=limit, category=c) for c in categories),
            return_exceptions=True,
        )
        out: dict[str, list[Trader]] = {}
        for category, res in zip(categories, results):
            if isinstance(res, Exception):
                log.warning("leaderboard fetch failed for category=%s: %s", category, res)
                out[category] = []
            else:
                out[category] = res
        return out

    async def fetch_positions(
        self,
        wallet: str,
        *,
        size_threshold: float = 100.0,
        limit: int = 500,
    ) -> list[Position]:
        params = {"user": wallet, "sizeThreshold": size_threshold, "limit": limit}
        raw = await self._get(f"{self._cfg.data_api}/positions", params)
        return [Position.model_validate(r) for r in raw]

    async def fetch_positions_for(
        self,
        traders: list[Trader],
        *,
        size_threshold: float = 100.0,
    ) -> dict[str, list[Position]]:
        tasks = [self.fetch_positions(t.proxy_wallet, size_threshold=size_threshold) for t in traders]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: dict[str, list[Position]] = {}
        for trader, res in zip(traders, results):
            if isinstance(res, Exception):
                log.warning("failed positions for %s (%s): %s", trader.user_name, trader.proxy_wallet, res)
                out[trader.proxy_wallet] = []
            else:
                out[trader.proxy_wallet] = res
        return out

    async def fetch_market_by_condition_id(self, condition_id: str) -> GammaMarket | None:
        """Look up a gamma market by conditionId. Returns None if not found."""
        raw = await self._get(
            f"{self._cfg.gamma_api}/markets",
            params={"condition_ids": condition_id, "limit": 1},
        )
        if not raw:
            return None
        return GammaMarket.model_validate(raw[0])

    async def fetch_markets_by_condition_ids(
        self,
        condition_ids: list[str],
        *,
        batch_size: int = 30,
        include_closed: bool = False,
    ) -> dict[str, GammaMarket]:
        """Batch-fetch gamma markets. Gamma defaults to closed=false, so we
        explicitly query open then closed (when include_closed=True) and merge.
        """
        out: dict[str, GammaMarket] = {}
        closed_filters: list[str | None] = ["false"]
        if include_closed:
            closed_filters.append("true")
        for closed in closed_filters:
            for i in range(0, len(condition_ids), batch_size):
                chunk = condition_ids[i : i + batch_size]
                params = [("condition_ids", c) for c in chunk] + [("limit", str(len(chunk)))]
                if closed is not None:
                    params.append(("closed", closed))
                raw = await self._get(f"{self._cfg.gamma_api}/markets", params=params)
                for r in raw:
                    try:
                        m = GammaMarket.model_validate(r)
                        if m.condition_id and m.condition_id not in out:
                            out[m.condition_id] = m
                    except Exception as e:
                        log.warning("failed to parse gamma market: %s", e)
        return out
