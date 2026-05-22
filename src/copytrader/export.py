"""CSV export for weekly tracking spreadsheet.

Joins market_scores with paper_trades into a single per-snapshot, per-market
view: what we detected, what we recommended, and (if resolved) what happened.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

EXPORT_HEADERS = [
    "snapshot_ts",
    "market_title",
    "end_date",
    "condition_id",
    "consensus_side",
    "smart_money_dollars",
    "n_traders",
    "market_yes_price",
    "consensus_score",
    "edge",
    "was_edge_candidate",
    "suggested_bet_side",
    "suggested_bet_usd",
    "resolved_outcome",
    "we_predicted_correctly",
    "realized_pnl",
    "resolved_at",
    "polymarket_url",
]


def export(db_path: Path, out_path: Path) -> int:
    """Write a per-snapshot, per-market CSV. Returns row count."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        # Left join: every scored market gets a row; paper_trade row is optional.
        cur = conn.execute(
            """
            SELECT
                ms.snapshot_ts,
                ms.title,
                ms.end_date,
                ms.condition_id,
                ms.consensus_side,
                ms.yes_dollars,
                ms.no_dollars,
                ms.n_traders,
                ms.yes_price,
                ms.score,
                ms.edge,
                ms.has_edge,
                ms.event_slug,
                pt.side AS bet_side,
                pt.notional_usd,
                pt.resolved_outcome,
                pt.realized_pnl,
                pt.resolved_at
            FROM market_scores ms
            LEFT JOIN paper_trades pt
              ON ms.snapshot_ts = pt.snapshot_ts
             AND ms.condition_id = pt.condition_id
            ORDER BY ms.snapshot_ts DESC, ABS(ms.score) DESC
            """
        )

        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(EXPORT_HEADERS)
            for r in cur:
                consensus_side = r["consensus_side"]
                smart_money_dollars = (
                    r["no_dollars"] if consensus_side == "NO" else r["yes_dollars"]
                )
                outcome = r["resolved_outcome"]
                we_correct = ""
                if outcome and r["bet_side"]:
                    we_correct = "YES" if outcome == r["bet_side"] else "NO"
                url = (
                    f"https://polymarket.com/event/{r['event_slug']}"
                    if r["event_slug"] else ""
                )
                writer.writerow([
                    r["snapshot_ts"],
                    r["title"],
                    r["end_date"],
                    r["condition_id"],
                    consensus_side,
                    f"{smart_money_dollars:.0f}",
                    r["n_traders"],
                    f"{r['yes_price']:.4f}" if r["yes_price"] is not None else "",
                    f"{r['score']:.4f}",
                    f"{r['edge']:.4f}" if r["edge"] is not None else "",
                    "yes" if r["has_edge"] else "no",
                    r["bet_side"] or "",
                    f"{r['notional_usd']:.2f}" if r["notional_usd"] is not None else "",
                    outcome or "",
                    we_correct,
                    f"{r['realized_pnl']:.2f}" if r["realized_pnl"] is not None else "",
                    r["resolved_at"] or "",
                    url,
                ])
                rows_written += 1
    return rows_written
