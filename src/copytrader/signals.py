"""Consensus scoring + filtering for Polymarket top-trader signals.

Score per market =
    sum over traders of [ rank_weight(rank) * (position_value / trader_portfolio) * direction ]

Direction = +1 for Yes, -1 for No. Higher |score| means stronger consensus.
Sign of score indicates which side the smart money is on.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .config import Config
from .models import GammaMarket, MarketScore, Position, Trader


def rank_weight(rank: int) -> float:
    return 1.0 / (rank ** 0.5) if rank > 0 else 0.0


@dataclass
class _Aggregate:
    score: float = 0.0
    yes_dollars: float = 0.0
    no_dollars: float = 0.0
    traders: list[tuple[str, int, str, float]] = None  # (name, rank, outcome, $ value)

    def __post_init__(self):
        if self.traders is None:
            self.traders = []


def aggregate(
    traders: list[Trader],
    positions_by_wallet: dict[str, list[Position]],
) -> dict[str, _Aggregate]:
    by_market: dict[str, _Aggregate] = defaultdict(_Aggregate)
    for trader in traders:
        positions = positions_by_wallet.get(trader.proxy_wallet, [])
        open_positions = [p for p in positions if p.is_open and p.condition_id]
        portfolio = sum(p.current_value for p in open_positions)
        if portfolio <= 0:
            continue

        rw = rank_weight(trader.rank)
        for pos in open_positions:
            direction = 1 if pos.yes_side else -1
            size_pct = pos.current_value / portfolio
            agg = by_market[pos.condition_id]
            agg.score += rw * size_pct * direction
            agg.traders.append((trader.user_name, trader.rank, pos.outcome, pos.current_value))
            if direction > 0:
                agg.yes_dollars += pos.current_value
            else:
                agg.no_dollars += pos.current_value
    return by_market


def _parse_outcome_price(market: GammaMarket) -> float | None:
    """Return the price at outcome index 0.

    For Yes/No markets this is literally the YES price. For sports/multi-outcome
    markets index 0 corresponds to outcomeIndex=0 in positions, which the Data
    API reports as outcome="Yes" — so it's still the right quantity to compare
    against position-side direction.
    """
    try:
        prices = json.loads(market.outcome_prices or "[]")
    except json.JSONDecodeError:
        return None
    if not prices:
        return None
    try:
        return float(prices[0])
    except (TypeError, ValueError):
        return None


def minutes_to_resolution(market: GammaMarket, now: datetime | None = None) -> float | None:
    end = market.end_dt
    if end is None:
        return None
    now = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (end - now).total_seconds() / 60.0


def score(
    cfg: Config,
    traders: list[Trader],
    positions_by_wallet: dict[str, list[Position]],
    markets_by_condition_id: dict[str, GammaMarket],
    *,
    now: datetime | None = None,
) -> list[MarketScore]:
    """Return MarketScore rows, filtered + sorted by |score| desc."""
    by_market = aggregate(traders, positions_by_wallet)
    out: list[MarketScore] = []
    for cid, agg in by_market.items():
        unique_traders = {name for name, _, _, _ in agg.traders}
        if len(unique_traders) < cfg.min_traders_per_market:
            continue
        if abs(agg.score) < cfg.min_consensus_score:
            continue

        market = markets_by_condition_id.get(cid)
        title = event_slug = end_date = ""
        yes_price: float | None = None
        if market is not None:
            if market.closed or not market.active or not market.accepting_orders:
                continue
            if market.volume_num < cfg.min_market_volume_usd:
                continue
            mins = minutes_to_resolution(market, now=now)
            if mins is not None:
                if mins < cfg.min_minutes_to_resolution:
                    continue
                if mins > cfg.max_days_to_resolution * 24 * 60:
                    continue
            yes_price = _parse_outcome_price(market)
            # Skip markets whose price is pinned at the edge — gamma sometimes
            # leaves active=true on already-resolved markets, and a price like
            # 0.99 / 0.01 means we couldn't actually fill at it anyway.
            if yes_price is not None and not 0.02 <= yes_price <= 0.98:
                continue
            title = market.question
            event_slug = market.slug
            end_date = market.end_date_iso or market.end_date
        else:
            # Fall back to position metadata if gamma lookup failed
            first = agg.traders[0] if agg.traders else None
            title = "(market not found in gamma)"
            end_date = ""
            event_slug = ""

        consensus_side = "YES" if agg.score > 0 else "NO"
        market_implied_side: str | None
        edge: float | None
        if yes_price is None:
            market_implied_side = None
            edge = None
        else:
            market_implied_side = "YES" if yes_price > 0.5 else "NO"
            # Treat the absolute consensus score (after the threshold) as a
            # heuristic probability shift away from 0.5. We don't claim it
            # *is* a probability; it's a comparable magnitude.
            consensus_implied_prob = 0.5 + (agg.score / 4.0)
            consensus_implied_prob = max(0.0, min(1.0, consensus_implied_prob))
            edge = consensus_implied_prob - yes_price  # positive => bet YES, negative => bet NO

        has_edge = (
            edge is not None
            and market_implied_side is not None
            and market_implied_side != consensus_side
            and abs(edge) >= cfg.edge_threshold
        )

        # De-duplicate trader names while preserving best (lowest) rank
        seen: dict[str, int] = {}
        for name, rank, _, _ in agg.traders:
            if name not in seen or rank < seen[name]:
                seen[name] = rank
        top_names = [n for n, _ in sorted(seen.items(), key=lambda x: x[1])[:5]]

        out.append(
            MarketScore(
                condition_id=cid,
                title=title or "(no title)",
                event_slug=event_slug,
                end_date=end_date,
                score=agg.score,
                n_traders=len(unique_traders),
                yes_dollars=agg.yes_dollars,
                no_dollars=agg.no_dollars,
                yes_price=yes_price,
                consensus_side=consensus_side,
                market_implied_side=market_implied_side,
                edge=edge,
                top_trader_names=top_names,
                has_edge=has_edge,
            )
        )

    out.sort(key=lambda m: abs(m.score), reverse=True)
    return out


def edge_only(scores: Iterable[MarketScore]) -> list[MarketScore]:
    return sorted(
        (s for s in scores if s.has_edge),
        key=lambda s: abs(s.edge or 0.0),
        reverse=True,
    )
