"""Tests for the consensus scoring math."""
from __future__ import annotations

from copytrader.config import Config
from copytrader.models import Position, Trader
from copytrader.signals import aggregate, rank_weight, score
from pathlib import Path


def _cfg(**overrides) -> Config:
    base = dict(
        data_api="x", gamma_api="x", kalshi_api="x",
        discord_webhook="", discord_digest_webhook="",
        top_n_traders=50, time_period="MONTH", order_by="PNL",
        min_traders_per_market=2, min_consensus_score=0.0,
        min_market_volume_usd=0.0, min_minutes_to_resolution=0,
        edge_threshold=0.05,
        bankroll_usd=1000.0, kelly_fraction=0.25, max_bet_pct=0.05,
        daily_exposure_cap=0.20,
        db_path=Path("x.db"),
    )
    base.update(overrides)
    return Config(**base)


def _trader(rank: int, name: str | None = None, wallet: str | None = None) -> Trader:
    return Trader.model_validate({
        "rank": rank,
        "proxyWallet": wallet or f"0x{rank:040x}",
        "userName": name or f"trader{rank}",
        "vol": 0.0,
        "pnl": 0.0,
    })


def _position(cid: str, outcome: str, value: float, wallet: str = "0x1") -> Position:
    return Position.model_validate({
        "proxyWallet": wallet,
        "asset": f"asset-{cid}-{outcome}",
        "conditionId": cid,
        "outcome": outcome,
        "size": value,
        "currentValue": value,
        "curPrice": 0.5,
        "title": "market " + cid,
    })


def test_rank_weight_decays():
    assert rank_weight(1) > rank_weight(2) > rank_weight(10)
    assert abs(rank_weight(1) - 1.0) < 1e-9
    assert abs(rank_weight(4) - 0.5) < 1e-9


def test_aggregate_sums_signed_score():
    traders = [_trader(1, "a"), _trader(4, "b")]
    positions = {
        traders[0].proxy_wallet: [_position("cid1", "Yes", 100, traders[0].proxy_wallet)],
        traders[1].proxy_wallet: [_position("cid1", "No", 50, traders[1].proxy_wallet)],
    }
    agg = aggregate(traders, positions)
    # trader a: rank_weight(1)=1.0, size_pct=1.0, dir=+1 -> +1.0
    # trader b: rank_weight(4)=0.5, size_pct=1.0, dir=-1 -> -0.5
    assert abs(agg["cid1"].score - 0.5) < 1e-9
    assert agg["cid1"].yes_dollars == 100
    assert agg["cid1"].no_dollars == 50


def test_score_respects_min_traders():
    traders = [_trader(1, "a")]
    positions = {traders[0].proxy_wallet: [_position("cid1", "Yes", 100, traders[0].proxy_wallet)]}
    cfg = _cfg(min_traders_per_market=2)
    out = score(cfg, traders, positions, markets_by_condition_id={})
    assert out == []


def test_score_marks_edge_when_market_disagrees():
    """Smart money strongly on NO, market price says YES -> edge flagged."""
    traders = [_trader(r) for r in (1, 2, 3, 4, 5)]
    positions = {
        t.proxy_wallet: [_position("cid1", "No", 1000, t.proxy_wallet)] for t in traders
    }
    # Build a fake gamma market with YES priced at 0.7
    from copytrader.models import GammaMarket
    market = GammaMarket.model_validate({
        "id": "1",
        "conditionId": "cid1",
        "question": "test",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.7","0.3"]',
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "volumeNum": 100000.0,
        "endDateIso": "2099-01-01",
    })
    cfg = _cfg(min_traders_per_market=2, edge_threshold=0.05)
    out = score(cfg, traders, positions, markets_by_condition_id={"cid1": market})
    assert len(out) == 1
    s = out[0]
    assert s.consensus_side == "NO"
    assert s.market_implied_side == "YES"
    assert s.has_edge is True
    assert s.edge is not None and s.edge < 0  # negative edge => bet NO


def test_n_traders_counts_unique_wallets_not_positions():
    """One wallet holding two tranches of the same market counts as one trader."""
    t = _trader(1, "alice")
    positions = {
        t.proxy_wallet: [
            _position("cid1", "Yes", 100, t.proxy_wallet),
            _position("cid1", "Yes", 200, t.proxy_wallet),
        ],
    }
    cfg = _cfg(min_traders_per_market=2)
    out = score(cfg, [t], positions, markets_by_condition_id={})
    # Only one unique trader -> drops below min_traders_per_market=2
    assert out == []

    cfg2 = _cfg(min_traders_per_market=1)
    out2 = score(cfg2, [t], positions, markets_by_condition_id={})
    assert len(out2) == 1
    assert out2[0].n_traders == 1


def test_score_filters_pinned_price_market():
    """A market whose yes_price is 0.99 is effectively resolved — drop it."""
    traders = [_trader(r) for r in (1, 2, 3, 4)]
    positions = {
        t.proxy_wallet: [_position("cid1", "No", 1000, t.proxy_wallet)] for t in traders
    }
    from copytrader.models import GammaMarket
    market = GammaMarket.model_validate({
        "id": "1", "conditionId": "cid1", "question": "test",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.99","0.01"]',
        "active": True, "closed": False, "acceptingOrders": True,
        "volumeNum": 100000.0, "endDateIso": "2099-01-01",
    })
    cfg = _cfg(min_traders_per_market=2)
    out = score(cfg, traders, positions, markets_by_condition_id={"cid1": market})
    assert out == []


def test_yes_price_works_for_sports_outcomes():
    """Sports markets have outcomes like ['Team A', 'Team B'], not ['Yes','No']."""
    from copytrader.models import GammaMarket
    from copytrader.signals import _parse_outcome_price

    market = GammaMarket.model_validate({
        "id": "1", "conditionId": "cid1", "question": "Toronto vs Yankees",
        "outcomes": '["Toronto Blue Jays", "New York Yankees"]',
        "outcomePrices": '["0.54", "0.46"]',
        "active": True, "closed": False, "acceptingOrders": True,
        "volumeNum": 100000.0, "endDateIso": "2099-01-01",
    })
    assert abs(_parse_outcome_price(market) - 0.54) < 1e-9


def test_score_filters_resolved_market():
    traders = [_trader(r) for r in (1, 2, 3)]
    positions = {
        t.proxy_wallet: [_position("cid1", "Yes", 1000, t.proxy_wallet)] for t in traders
    }
    from copytrader.models import GammaMarket
    market = GammaMarket.model_validate({
        "id": "1",
        "conditionId": "cid1",
        "question": "test",
        "outcomes": '["Yes","No"]',
        "outcomePrices": '["1.0","0.0"]',
        "active": False,
        "closed": True,
        "acceptingOrders": False,
        "volumeNum": 100000.0,
        "endDateIso": "2099-01-01",
    })
    cfg = _cfg(min_traders_per_market=2)
    out = score(cfg, traders, positions, markets_by_condition_id={"cid1": market})
    assert out == []
