"""Tests for the bet-size calculator."""
from __future__ import annotations

from copytrader.analyze import (
    composite_prob,
    kelly_fraction,
    recommend,
    signal_implied_prob,
)
from copytrader.models import MarketScore


def _score(side: str, score: float, yes_price: float, edge: float, n_traders: int = 5) -> MarketScore:
    return MarketScore(
        condition_id="cid", title="t", event_slug="e", end_date="2099-01-01",
        score=score, n_traders=n_traders, yes_dollars=100, no_dollars=100,
        yes_price=yes_price, consensus_side=side,
        market_implied_side="YES" if yes_price > 0.5 else "NO",
        edge=edge, top_trader_names=["a"], has_edge=True,
    )


def test_signal_implied_prob_clamps():
    assert signal_implied_prob(0.0) == 0.5
    assert signal_implied_prob(-10.0) == 0.05
    assert signal_implied_prob(10.0) == 0.95


def test_composite_prob_drops_research_when_missing():
    # signal+market only, weights normalized
    p = composite_prob(0.6, 0.4, p_research=None)
    assert abs(p - (0.45 * 0.6 + 0.35 * 0.4) / (0.45 + 0.35)) < 1e-9


def test_kelly_zero_when_no_edge():
    # true_prob = market_price -> no edge
    assert kelly_fraction(0.5, 0.5) == 0.0
    # true_prob < market_price -> negative edge -> clamped to 0
    assert kelly_fraction(0.3, 0.5) == 0.0


def test_kelly_positive_when_edge():
    # true_prob 0.6, market 0.5 -> f = 0.1/0.5 = 0.2
    assert abs(kelly_fraction(0.6, 0.5) - 0.2) < 1e-9


def test_recommend_returns_none_when_no_edge():
    s = _score("YES", 0.0, 0.5, edge=0.0)
    rec = recommend(s, bankroll_usd=1000, kelly_fraction_mult=0.25, max_bet_pct=0.1)
    assert rec is None


def test_recommend_caps_at_max_bet_pct():
    # Strong NO consensus, low NO price -> potentially huge Kelly
    s = _score("NO", -0.8, yes_price=0.9, edge=-0.4)
    rec = recommend(s, bankroll_usd=1000, kelly_fraction_mult=0.5, max_bet_pct=0.05)
    assert rec is not None
    assert rec.side == "NO"
    assert rec.bet_size_usd <= 1000 * 0.05 + 1e-6


def test_scale_to_daily_cap_no_op_when_under_cap():
    from copytrader.analyze import scale_to_daily_cap

    s = _score("YES", 0.3, yes_price=0.4, edge=0.1)
    rec = recommend(s, bankroll_usd=1000, kelly_fraction_mult=0.25, max_bet_pct=0.5)
    assert rec is not None
    out, scaled = scale_to_daily_cap([rec], bankroll_usd=1000, daily_exposure_cap=0.5)
    assert scaled is False
    assert out[0].bet_size_usd == rec.bet_size_usd


def test_scale_to_daily_cap_halves_when_double():
    from copytrader.analyze import scale_to_daily_cap

    s1 = _score("NO", -0.8, yes_price=0.9, edge=-0.4)
    s2 = _score("NO", -0.8, yes_price=0.9, edge=-0.4)
    r1 = recommend(s1, bankroll_usd=1000, kelly_fraction_mult=1.0, max_bet_pct=0.10)
    r2 = recommend(s2, bankroll_usd=1000, kelly_fraction_mult=1.0, max_bet_pct=0.10)
    assert r1 is not None and r2 is not None
    # Each at $100, total $200 = 20% of bankroll
    # Cap at 10% (= $100) -> each should be scaled to $50
    out, scaled = scale_to_daily_cap([r1, r2], bankroll_usd=1000, daily_exposure_cap=0.10)
    assert scaled is True
    assert abs(out[0].bet_size_usd - 50.0) < 0.01
    assert abs(out[1].bet_size_usd - 50.0) < 0.01
    # EV recomputed against the smaller bet
    assert out[0].loss_if_wrong_usd == -out[0].bet_size_usd


def test_recommend_uses_consensus_side():
    s = _score("NO", -0.5, yes_price=0.55, edge=-0.15)
    rec = recommend(s, bankroll_usd=1000, kelly_fraction_mult=0.25, max_bet_pct=0.1)
    assert rec is not None
    assert rec.side == "NO"
    assert abs(rec.side_price - 0.45) < 1e-9
    # Loss is bounded by bet size
    assert rec.loss_if_wrong_usd == -rec.bet_size_usd
