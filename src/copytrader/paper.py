"""Paper-trade P&L resolution.

For every paper trade logged in `paper_trades` with no resolved_outcome, look
up the market via gamma. If the market has closed and resolved, compute
realized P&L (net of a configurable taker fee) and write the result back.
Idempotent: rows already resolved are skipped.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .polymarket import PolymarketClient

log = logging.getLogger(__name__)

TAKER_FEE = 0.02  # Polymarket charges ~2% on the winning side


@dataclass
class ResolutionResult:
    n_checked: int
    n_resolved: int
    total_pnl: float
    n_wins: int
    n_losses: int

    @property
    def win_rate(self) -> float:
        decided = self.n_wins + self.n_losses
        return self.n_wins / decided if decided else 0.0


def _winning_outcome(market) -> str | None:
    """Return 'YES' or 'NO' if the market has resolved, else None.

    Polymarket resolves by setting outcomePrices to ['1','0'] (YES wins) or
    ['0','1'] (NO wins), and closed=True. If both still nonzero, market hasn't
    paid out yet even if past end date.
    """
    if not market.closed:
        return None
    try:
        prices = json.loads(market.outcome_prices or "[]")
    except json.JSONDecodeError:
        return None
    if len(prices) < 2:
        return None
    try:
        p_yes = float(prices[0])
        p_no = float(prices[1])
    except (TypeError, ValueError):
        return None
    if p_yes >= 0.99 and p_no <= 0.01:
        return "YES"
    if p_no >= 0.99 and p_yes <= 0.01:
        return "NO"
    return None


async def resolve_paper_trades(db_path, client: PolymarketClient) -> ResolutionResult:
    """Walk unresolved paper trades, look up market state, compute P&L."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, condition_id, side, entry_price, notional_usd "
            "FROM paper_trades WHERE resolved_outcome IS NULL"
        ).fetchall()

    n_checked = len(rows)
    if not n_checked:
        return ResolutionResult(0, 0, 0.0, 0, 0)

    cids = list({r["condition_id"] for r in rows})
    markets = await client.fetch_markets_by_condition_ids(cids)

    updates = []
    total_pnl = 0.0
    n_wins = n_losses = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for r in rows:
        market = markets.get(r["condition_id"])
        if market is None:
            continue
        winner = _winning_outcome(market)
        if winner is None:
            continue
        # Side we paper-bet vs. who actually won
        side = r["side"]
        entry = float(r["entry_price"])
        notional = float(r["notional_usd"])
        shares = notional / entry if entry > 0 else 0.0
        if side == winner:
            payout = shares * 1.0
            payout_after_fee = notional + (payout - notional) * (1.0 - TAKER_FEE)
            pnl = payout_after_fee - notional
            n_wins += 1
        else:
            pnl = -notional
            n_losses += 1
        total_pnl += pnl
        updates.append((winner, pnl, now_iso, r["id"]))

    if updates:
        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                "UPDATE paper_trades SET resolved_outcome=?, realized_pnl=?, resolved_at=? "
                "WHERE id=?",
                updates,
            )
            conn.commit()

    return ResolutionResult(n_checked, len(updates), total_pnl, n_wins, n_losses)


def summary(db_path) -> dict:
    """Aggregate stats across all resolved paper trades."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS resolved,
                SUM(realized_pnl) AS total_pnl,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                SUM(notional_usd) AS total_notional
            FROM paper_trades
            WHERE resolved_outcome IS NOT NULL
            """
        ).fetchone()
        n_pending = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE resolved_outcome IS NULL"
        ).fetchone()[0]
    return {
        "resolved": row["resolved"] or 0,
        "pending": n_pending,
        "wins": row["wins"] or 0,
        "losses": row["losses"] or 0,
        "total_pnl": row["total_pnl"] or 0.0,
        "total_notional": row["total_notional"] or 0.0,
        "win_rate": (row["wins"] or 0) / (row["resolved"] or 1),
        "roi": (row["total_pnl"] or 0.0) / (row["total_notional"] or 1),
    }
