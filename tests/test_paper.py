"""Paper-trade resolution math."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from copytrader.models import GammaMarket, MarketScore
from copytrader.paper import resolve_paper_trades, summary
from copytrader.storage import Storage


def _resolved_market(cid: str, winning_side: str) -> GammaMarket:
    prices = '["1","0"]' if winning_side == "YES" else '["0","1"]'
    return GammaMarket.model_validate({
        "id": "1", "conditionId": cid, "question": "test",
        "outcomes": '["Yes","No"]',
        "outcomePrices": prices,
        "active": False, "closed": True, "acceptingOrders": False,
        "volumeNum": 100000.0, "endDateIso": "2020-01-01",
    })


def _unresolved_market(cid: str) -> GammaMarket:
    return GammaMarket.model_validate({
        "id": "1", "conditionId": cid, "question": "test",
        "outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]',
        "active": True, "closed": False, "acceptingOrders": True,
        "volumeNum": 100000.0, "endDateIso": "2099-01-01",
    })


def _score(cid: str, side: str, yes_price: float) -> MarketScore:
    return MarketScore(
        condition_id=cid, title="t", event_slug="", end_date="2099-01-01",
        score=0.5 if side == "YES" else -0.5,
        n_traders=4, yes_dollars=100, no_dollars=0,
        yes_price=yes_price, consensus_side=side, market_implied_side="NO" if side == "YES" else "YES",
        edge=0.1, top_trader_names=["a"], has_edge=True,
    )


@pytest.mark.asyncio
async def test_resolve_winning_trade(tmp_path: Path):
    store = Storage(tmp_path / "test.db")
    ts = store.write_snapshot([], {}, [])
    # Paper-bet YES at 0.40, market resolves YES -> win
    s = _score("cid1", "YES", 0.40)
    store.log_paper_trades(ts, [s], notional_by_cid={"cid1": 100.0})

    client = AsyncMock()
    client.fetch_markets_by_condition_ids = AsyncMock(
        return_value={"cid1": _resolved_market("cid1", "YES")}
    )
    result = await resolve_paper_trades(store.path, client)

    assert result.n_resolved == 1
    assert result.n_wins == 1
    # 100/0.40 = 250 shares; payout=$250; gross profit=$150; net (2% fee on profit) = $147
    assert abs(result.total_pnl - 147.0) < 0.01

    s2 = summary(store.path)
    assert s2["resolved"] == 1 and s2["pending"] == 0
    assert s2["wins"] == 1 and s2["losses"] == 0


@pytest.mark.asyncio
async def test_resolve_losing_trade(tmp_path: Path):
    store = Storage(tmp_path / "test.db")
    ts = store.write_snapshot([], {}, [])
    s = _score("cid1", "NO", 0.40)  # paper-bet NO; if YES wins we lose
    store.log_paper_trades(ts, [s], notional_by_cid={"cid1": 100.0})

    client = AsyncMock()
    client.fetch_markets_by_condition_ids = AsyncMock(
        return_value={"cid1": _resolved_market("cid1", "YES")}
    )
    result = await resolve_paper_trades(store.path, client)
    assert result.n_resolved == 1 and result.n_losses == 1
    assert abs(result.total_pnl + 100.0) < 0.01  # lost entire notional


@pytest.mark.asyncio
async def test_resolve_skips_unresolved(tmp_path: Path):
    store = Storage(tmp_path / "test.db")
    ts = store.write_snapshot([], {}, [])
    s = _score("cid1", "YES", 0.40)
    store.log_paper_trades(ts, [s], notional_by_cid={"cid1": 100.0})

    client = AsyncMock()
    client.fetch_markets_by_condition_ids = AsyncMock(
        return_value={"cid1": _unresolved_market("cid1")}
    )
    result = await resolve_paper_trades(store.path, client)
    assert result.n_checked == 1
    assert result.n_resolved == 0

    with sqlite3.connect(store.path) as conn:
        outcome = conn.execute("SELECT resolved_outcome FROM paper_trades").fetchone()[0]
    assert outcome is None
