"""SQLite persistence for daily leaderboard + position + market snapshots.

The schema is intentionally append-only: every snapshot run inserts new rows
keyed by (snapshot_ts, ...). This builds up an honest forward-only history
that the backtest module can replay later.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import GammaMarket, MarketScore, Position, Trader

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_ts TEXT PRIMARY KEY,
    n_traders INTEGER NOT NULL,
    n_positions INTEGER NOT NULL,
    n_markets_scored INTEGER NOT NULL,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS leaderboard (
    snapshot_ts TEXT NOT NULL,
    rank INTEGER NOT NULL,
    proxy_wallet TEXT NOT NULL,
    user_name TEXT NOT NULL,
    vol REAL NOT NULL,
    pnl REAL NOT NULL,
    PRIMARY KEY (snapshot_ts, proxy_wallet),
    FOREIGN KEY (snapshot_ts) REFERENCES snapshots(snapshot_ts)
);

CREATE TABLE IF NOT EXISTS positions (
    snapshot_ts TEXT NOT NULL,
    proxy_wallet TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    outcome TEXT NOT NULL,
    size REAL NOT NULL,
    avg_price REAL NOT NULL,
    current_value REAL NOT NULL,
    cur_price REAL NOT NULL,
    title TEXT NOT NULL,
    event_slug TEXT NOT NULL,
    end_date TEXT NOT NULL,
    PRIMARY KEY (snapshot_ts, proxy_wallet, asset),
    FOREIGN KEY (snapshot_ts) REFERENCES snapshots(snapshot_ts)
);
CREATE INDEX IF NOT EXISTS idx_positions_cid ON positions(condition_id);

CREATE TABLE IF NOT EXISTS market_scores (
    snapshot_ts TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    title TEXT NOT NULL,
    event_slug TEXT NOT NULL,
    end_date TEXT NOT NULL,
    score REAL NOT NULL,
    n_traders INTEGER NOT NULL,
    yes_dollars REAL NOT NULL,
    no_dollars REAL NOT NULL,
    yes_price REAL,
    consensus_side TEXT NOT NULL,
    market_implied_side TEXT,
    edge REAL,
    has_edge INTEGER NOT NULL,
    top_trader_names_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_ts, condition_id),
    FOREIGN KEY (snapshot_ts) REFERENCES snapshots(snapshot_ts)
);
CREATE INDEX IF NOT EXISTS idx_market_scores_edge ON market_scores(has_edge, snapshot_ts);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    consensus_score REAL NOT NULL,
    notional_usd REAL NOT NULL,
    end_date TEXT NOT NULL,
    resolved_outcome TEXT,
    realized_pnl REAL,
    resolved_at TEXT
);
"""


class Storage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def write_snapshot(
        self,
        traders: list[Trader],
        positions_by_wallet: dict[str, list[Position]],
        scores: list[MarketScore],
        *,
        ts: datetime | None = None,
        notes: str = "",
    ) -> str:
        ts = ts or datetime.now(timezone.utc)
        snapshot_ts = ts.isoformat()
        n_pos = sum(len(v) for v in positions_by_wallet.values())
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)",
                (snapshot_ts, len(traders), n_pos, len(scores), notes),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO leaderboard VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (snapshot_ts, t.rank, t.proxy_wallet, t.user_name, t.vol, t.pnl)
                    for t in traders
                ],
            )
            pos_rows = []
            for wallet, positions in positions_by_wallet.items():
                for p in positions:
                    pos_rows.append((
                        snapshot_ts, wallet, p.condition_id, p.asset, p.outcome,
                        p.size, p.avg_price, p.current_value, p.cur_price,
                        p.title, p.event_slug, p.end_date,
                    ))
            if pos_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    pos_rows,
                )
            score_rows = [
                (
                    snapshot_ts, s.condition_id, s.title, s.event_slug, s.end_date,
                    s.score, s.n_traders, s.yes_dollars, s.no_dollars, s.yes_price,
                    s.consensus_side, s.market_implied_side, s.edge,
                    1 if s.has_edge else 0, json.dumps(s.top_trader_names),
                )
                for s in scores
            ]
            if score_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO market_scores VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    score_rows,
                )
        return snapshot_ts

    def log_paper_trades(
        self,
        snapshot_ts: str,
        scores: Iterable[MarketScore],
        notional_by_cid: dict[str, float] | None = None,
    ) -> int:
        """Log a row for every scored market in this snapshot.

        - notional_usd > 0 when Kelly produced an actionable bet (edge candidate)
        - notional_usd = 0 means "prediction logged, no bet placed"

        Either way, side = consensus_side and we'll compare against the resolved
        outcome later — giving us a smart-money win-rate stat across the whole
        sample, not just the subset where we'd have bet.
        """
        notional_by_cid = notional_by_cid or {}
        rows = []
        for s in scores:
            if s.yes_price is None:
                continue
            entry_price = s.yes_price if s.consensus_side == "YES" else 1.0 - s.yes_price
            notional = notional_by_cid.get(s.condition_id, 0.0)
            rows.append((
                snapshot_ts, s.condition_id, s.consensus_side,
                entry_price, s.score, notional, s.end_date,
            ))
        if not rows:
            return 0
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO paper_trades "
                "(snapshot_ts, condition_id, side, entry_price, consensus_score, notional_usd, end_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)
