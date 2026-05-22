"""Win-probability calculator + Kelly bet sizing.

Combines:
1. Smart-money signal (consensus score → implied probability)
2. Market price (current YES price = market's implied probability)
3. (Optional) external research probability — caller passes it in

Produces a blended "true" probability estimate and a fractional-Kelly bet size,
capped at a configurable max % of bankroll.

This is a research tool. Profit is not guaranteed; we have no backtest yet.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from .models import MarketScore

SIGNAL_SCALING = 0.25  # how much consensus score tilts the implied probability
POLYMARKET_TAKER_FEE = 0.02


def signal_implied_prob(score: float) -> float:
    """Map a consensus score (theoretically -1..+1) into a YES probability.

    Clamped to (0.05, 0.95) so we never assume certainty.
    """
    return max(0.05, min(0.95, 0.5 + SIGNAL_SCALING * score))


def composite_prob(
    p_signal: float,
    p_market: float,
    p_research: float | None = None,
    *,
    w_signal: float = 0.45,
    w_market: float = 0.35,
    w_research: float = 0.20,
) -> float:
    """Blend the inputs into a single YES probability.

    If research is missing, signal and market are re-weighted to sum to 1.
    """
    if p_research is None:
        total = w_signal + w_market
        return (w_signal * p_signal + w_market * p_market) / total
    return w_signal * p_signal + w_market * p_market + w_research * p_research


def kelly_fraction(true_prob: float, market_price: float) -> float:
    """Full-Kelly fraction for a bet at `market_price` with `true_prob` of winning.

    Returns 0 (no bet) when there is no positive edge.
    """
    if not 0 < market_price < 1 or not 0 <= true_prob <= 1:
        return 0.0
    f = (true_prob - market_price) / (1.0 - market_price)
    return max(0.0, f)


@dataclass
class BetRecommendation:
    side: str
    side_price: float
    p_signal: float
    p_blended: float
    p_research: float | None
    full_kelly: float
    fractional_kelly: float
    bet_size_usd: float
    payout_if_win_usd: float
    profit_if_win_usd: float
    loss_if_wrong_usd: float
    ev_usd: float
    ev_pct: float
    reasoning: str


def recommend(
    m: MarketScore,
    *,
    bankroll_usd: float,
    kelly_fraction_mult: float = 0.25,
    max_bet_pct: float = 0.05,
    research_prob: float | None = None,
) -> BetRecommendation | None:
    """Compute a bet recommendation for the consensus side of `m`.

    `research_prob` is an externally-derived YES probability (e.g. from web
    research / LLM). Pass None to skip.
    """
    if m.yes_price is None or not 0 < m.yes_price < 1:
        return None

    p_yes_signal = signal_implied_prob(m.score)
    p_yes_market = m.yes_price
    p_yes_blended = composite_prob(p_yes_signal, p_yes_market, research_prob)

    if m.consensus_side == "YES":
        side, side_price, p_side, p_signal_side = "YES", m.yes_price, p_yes_blended, p_yes_signal
        p_side_research = research_prob
    else:
        side, side_price = "NO", 1.0 - m.yes_price
        p_side = 1.0 - p_yes_blended
        p_signal_side = 1.0 - p_yes_signal
        p_side_research = None if research_prob is None else 1.0 - research_prob

    full_kelly = kelly_fraction(p_side, side_price)
    bet_fraction = min(full_kelly * kelly_fraction_mult, max_bet_pct)
    if bet_fraction <= 0:
        return None

    bet = bankroll_usd * bet_fraction
    payout_if_win = bet / side_price if side_price > 0 else 0
    gross_profit = payout_if_win - bet
    net_profit_if_win = gross_profit * (1.0 - POLYMARKET_TAKER_FEE)
    loss_if_wrong = -bet
    ev = p_side * net_profit_if_win + (1.0 - p_side) * loss_if_wrong
    ev_pct = ev / bet if bet > 0 else 0.0

    reasoning_bits = [
        f"signal {p_signal_side:.0%}",
        f"market {1 - side_price:.0%}" if side == "YES" else f"market {side_price:.0%}",
    ]
    if p_side_research is not None:
        reasoning_bits.append(f"research {p_side_research:.0%}")
    reasoning_bits.append(f"blended {p_side:.0%}")
    reasoning = " · ".join(reasoning_bits)

    return BetRecommendation(
        side=side,
        side_price=side_price,
        p_signal=p_signal_side,
        p_blended=p_side,
        p_research=p_side_research,
        full_kelly=full_kelly,
        fractional_kelly=bet_fraction,
        bet_size_usd=bet,
        payout_if_win_usd=payout_if_win,
        profit_if_win_usd=net_profit_if_win,
        loss_if_wrong_usd=loss_if_wrong,
        ev_usd=ev,
        ev_pct=ev_pct,
        reasoning=reasoning,
    )


def scale_to_daily_cap(
    recs: Iterable[BetRecommendation],
    bankroll_usd: float,
    daily_exposure_cap: float,
) -> tuple[list[BetRecommendation], bool]:
    """Cap the total of all bets at `bankroll * daily_exposure_cap`.

    If the sum exceeds the cap, scale each bet down by the same factor and
    recompute its expected outcomes. Returns (scaled_list, was_scaled).
    """
    recs_list = list(recs)
    total = sum(r.bet_size_usd for r in recs_list)
    cap = bankroll_usd * daily_exposure_cap
    if total <= cap or total <= 0:
        return recs_list, False
    factor = cap / total
    scaled: list[BetRecommendation] = []
    for r in recs_list:
        new_bet = r.bet_size_usd * factor
        new_payout = new_bet / r.side_price if r.side_price > 0 else 0
        new_profit = (new_payout - new_bet) * (1.0 - POLYMARKET_TAKER_FEE)
        new_loss = -new_bet
        new_ev = r.p_blended * new_profit + (1.0 - r.p_blended) * new_loss
        new_ev_pct = new_ev / new_bet if new_bet > 0 else 0.0
        scaled.append(replace(
            r,
            fractional_kelly=r.fractional_kelly * factor,
            bet_size_usd=new_bet,
            payout_if_win_usd=new_payout,
            profit_if_win_usd=new_profit,
            loss_if_wrong_usd=new_loss,
            ev_usd=new_ev,
            ev_pct=new_ev_pct,
        ))
    return scaled, True
