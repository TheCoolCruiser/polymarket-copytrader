"""Smoke test for SQLite storage roundtrip."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from copytrader.models import MarketScore, Position, Trader
from copytrader.storage import Storage


@pytest.fixture
def store(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "test.db")


def test_schema_creates(store: Storage):
    with sqlite3.connect(store.path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"snapshots", "leaderboard", "positions", "market_scores", "paper_trades"} <= names


def test_roundtrip_snapshot(store: Storage):
    t = Trader.model_validate({
        "rank": 1, "proxyWallet": "0xabc", "userName": "alice",
        "vol": 1000.0, "pnl": 500.0,
    })
    p = Position.model_validate({
        "proxyWallet": "0xabc",
        "asset": "tok1",
        "conditionId": "cid1",
        "outcome": "Yes",
        "size": 100.0,
        "currentValue": 50.0,
        "curPrice": 0.5,
        "title": "test market",
        "eventSlug": "test-event",
        "endDate": "2099-01-01",
    })
    s = MarketScore(
        condition_id="cid1",
        title="test market",
        event_slug="test-event",
        end_date="2099-01-01",
        score=0.7,
        n_traders=4,
        yes_dollars=200.0,
        no_dollars=0.0,
        yes_price=0.4,
        consensus_side="YES",
        market_implied_side="NO",
        edge=0.2,
        top_trader_names=["alice"],
        has_edge=True,
    )
    ts = store.write_snapshot([t], {"0xabc": [p]}, [s])
    n = store.log_paper_trades(ts, [s], notional=100.0)
    assert n == 1

    with sqlite3.connect(store.path) as conn:
        snap_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        score_count = conn.execute("SELECT COUNT(*) FROM market_scores").fetchone()[0]
        paper_count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert snap_count == 1
    assert score_count == 1
    assert paper_count == 1
