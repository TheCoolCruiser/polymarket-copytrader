"""Kalshi public API client (read-only) + naive Polymarket-to-Kalshi matcher.

Kalshi's public market list endpoint returns thousands of markets. We do a
local fuzzy-match on titles to find the most likely Kalshi market for a given
Polymarket market.

No auth needed for the read endpoints we use. For order placement (out of
scope here), Kalshi requires Ed25519-signed requests with an API key.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from rapidfuzz import fuzz, process

log = logging.getLogger(__name__)


@dataclass
class KalshiMarket:
    ticker: str
    title: str
    status: str
    yes_bid: float | None
    yes_ask: float | None
    close_ts: int | None
    volume: float

    @property
    def url(self) -> str:
        return f"https://kalshi.com/markets/{self.ticker}"


class KalshiClient:
    def __init__(self, base: str, *, timeout: float = 30.0):
        self._base = base.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_open_markets(self, *, limit: int = 1000) -> list[KalshiMarket]:
        """List open Kalshi markets. Paginated; we follow cursors until done or limit."""
        out: list[KalshiMarket] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {"status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                r = await self._client.get(f"{self._base}/markets", params=params)
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("kalshi list failed: %s", e)
                break
            data = r.json()
            for m in data.get("markets", []):
                out.append(
                    KalshiMarket(
                        ticker=m.get("ticker", ""),
                        title=m.get("title") or m.get("subtitle") or "",
                        status=m.get("status", ""),
                        yes_bid=_to_cents(m.get("yes_bid")),
                        yes_ask=_to_cents(m.get("yes_ask")),
                        close_ts=m.get("close_time") and _iso_to_ts(m["close_time"]),
                        volume=float(m.get("volume", 0) or 0),
                    )
                )
            cursor = data.get("cursor") or ""
            if not cursor or len(out) >= limit:
                break
        return out


def _to_cents(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v) / 100.0  # Kalshi prices come as cents
    except (TypeError, ValueError):
        return None


def _iso_to_ts(s: str) -> int | None:
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def fuzzy_match(
    polymarket_title: str,
    kalshi_markets: list[KalshiMarket],
    *,
    score_cutoff: int = 75,
) -> KalshiMarket | None:
    """Find the best Kalshi market by title fuzzy-match. Returns None below cutoff."""
    if not kalshi_markets:
        return None
    choices = {m.ticker: m.title for m in kalshi_markets}
    best = process.extractOne(
        polymarket_title,
        choices,
        scorer=fuzz.token_set_ratio,
        score_cutoff=score_cutoff,
    )
    if best is None:
        return None
    _title, _score, ticker = best
    return next((m for m in kalshi_markets if m.ticker == ticker), None)
