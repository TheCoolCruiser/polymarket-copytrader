"""copytrader CLI."""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from pathlib import Path

from . import analyze, config as cfg_mod
from . import export as export_mod
from . import paper, research, signals, storage
from .kalshi import KalshiClient, fuzzy_match
from .notifier import send_digest
from .polymarket import PolymarketClient

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False)
console = Console()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _color_for(s) -> str:
    if s.has_edge:
        return "bold green"
    return "white"


def _print_bet_table(scores, recs: dict) -> None:
    table = Table(
        title="Edge candidates + bet recommendations",
        header_style="bold", expand=True,
    )
    table.add_column("Market", overflow="fold")
    table.add_column("Side", width=4, justify="center")
    table.add_column("Price", width=6, justify="right")
    table.add_column("Win %", width=7, justify="right")
    table.add_column("EV", width=7, justify="right")
    table.add_column("Bet $", width=8, justify="right")
    table.add_column("If win", width=8, justify="right")
    table.add_column("If lose", width=8, justify="right")
    for s in scores:
        rec = recs.get(s.condition_id)
        if rec is None:
            table.add_row(s.title[:55], s.consensus_side, "-", "-", "-", "(no bet)", "-", "-")
            continue
        table.add_row(
            f"[bold green]{s.title[:55]}[/bold green]",
            rec.side,
            f"{rec.side_price:.2f}",
            f"{rec.p_blended:.0%}",
            f"{rec.ev_pct:+.0%}",
            f"${rec.bet_size_usd:,.0f}",
            f"+${rec.profit_if_win_usd:,.0f}",
            f"-${-rec.loss_if_wrong_usd:,.0f}",
        )
    console.print(table)


def _print_table(scores, *, title: str = "Markets") -> None:
    table = Table(title=title, header_style="bold", expand=True)
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("Market", overflow="fold")
    table.add_column("Side", width=4, justify="center")
    table.add_column("Score", width=8, justify="right")
    table.add_column("N", width=4, justify="right")
    table.add_column("YES px", width=7, justify="right")
    table.add_column("Edge", width=8, justify="right")
    table.add_column("Ends", width=11)
    for i, s in enumerate(scores, 1):
        edge_str = f"{s.edge:+.3f}" if s.edge is not None else "-"
        price_str = f"{s.yes_price:.2f}" if s.yes_price is not None else "?"
        style = _color_for(s)
        table.add_row(
            str(i),
            f"[{style}]{s.title[:70]}[/{style}]",
            s.consensus_side,
            f"{s.score:+.3f}",
            str(s.n_traders),
            price_str,
            edge_str,
            s.end_date[:10],
        )
    console.print(table)


CATEGORIES = [
    "OVERALL", "POLITICS", "SPORTS", "CRYPTO",
    "CULTURE", "WEATHER", "ECONOMICS", "TECH", "FINANCE",
]


def _dedupe_traders(by_category: dict) -> list:
    """Merge per-category leaderboards into unique traders with their best rank."""
    by_wallet = {}
    for cat, traders in by_category.items():
        for t in traders:
            existing = by_wallet.get(t.proxy_wallet)
            if existing is None or t.rank < existing.rank:
                by_wallet[t.proxy_wallet] = t
    return list(by_wallet.values())


async def _do_snapshot(
    cfg, write_db: bool, send_to_discord: bool, persist_paper: bool, do_research: bool
):
    async with PolymarketClient(cfg) as pm:
        console.print(
            f"Fetching top {cfg.top_n_traders} traders across {len(CATEGORIES)} categories "
            f"({cfg.time_period}/{cfg.order_by})..."
        )
        by_category = await pm.fetch_leaderboards(CATEGORIES, limit=cfg.top_n_traders)
        per_cat_counts = ", ".join(f"{c}={len(v)}" for c, v in by_category.items() if v)
        console.print(f"  per-category counts: {per_cat_counts}")
        traders = _dedupe_traders(by_category)
        console.print(f"  {len(traders)} unique traders after dedupe")

        console.print("Fetching open positions in parallel...")
        positions_by_wallet = await pm.fetch_positions_for(traders)
        total_pos = sum(len(p) for p in positions_by_wallet.values())
        console.print(f"  got {total_pos} positions")

        condition_ids = sorted({
            p.condition_id
            for ps in positions_by_wallet.values()
            for p in ps
            if p.is_open and p.condition_id
        })
        console.print(f"Fetching gamma metadata for {len(condition_ids)} unique markets...")
        markets = await pm.fetch_markets_by_condition_ids(condition_ids)
        console.print(f"  got {len(markets)} markets")

    scored = signals.score(cfg, traders, positions_by_wallet, markets)
    edge = signals.edge_only(scored)
    console.print(
        f"\nScored {len(scored)} markets after filters; {len(edge)} flagged as edge candidates."
    )

    research_results: dict[str, research.ResearchResult] = {}
    if do_research and edge and research.is_available():
        console.print(f"Researching {len(edge)} edge candidates via headless claude (~30-90s each)...")
        for m in edge:
            r = await asyncio.to_thread(research.research_market, m.title, m.end_date)
            if r is not None:
                research_results[m.condition_id] = r
                console.print(
                    f"  {m.title[:50]}: research prob_yes={r.prob_yes:.2f} ({r.confidence})"
                )
            else:
                console.print(f"  {m.title[:50]}: research failed (continuing without)")
    elif do_research and not research.is_available():
        console.print("[yellow]claude CLI not on PATH; skipping research.[/yellow]")

    raw_recs: list = []
    edge_by_rec_index: dict = {}
    for m in edge:
        research_prob = None
        rr = research_results.get(m.condition_id)
        if rr is not None:
            research_prob = rr.prob_yes
        rec = analyze.recommend(
            m,
            bankroll_usd=cfg.bankroll_usd,
            kelly_fraction_mult=cfg.kelly_fraction,
            max_bet_pct=cfg.max_bet_pct,
            research_prob=research_prob,
        )
        if rec is not None:
            edge_by_rec_index[len(raw_recs)] = m.condition_id
            raw_recs.append(rec)

    scaled_recs, was_scaled = analyze.scale_to_daily_cap(
        raw_recs, cfg.bankroll_usd, cfg.daily_exposure_cap
    )
    recs: dict = {edge_by_rec_index[i]: r for i, r in enumerate(scaled_recs)}
    if was_scaled:
        total = sum(r.bet_size_usd for r in scaled_recs)
        console.print(
            f"[yellow]Total raw exposure exceeded {cfg.daily_exposure_cap:.0%} cap "
            f"(${cfg.bankroll_usd * cfg.daily_exposure_cap:,.0f}); "
            f"scaled all bets to total ${total:,.0f}.[/yellow]"
        )

    if scored:
        _print_table(scored[:20], title="Top by |consensus score|")
    if edge:
        _print_bet_table(edge[:10], recs)

    if write_db:
        store = storage.Storage(cfg.db_path)
        ts = store.write_snapshot(traders, positions_by_wallet, scored)
        if persist_paper:
            n = store.log_paper_trades(ts, edge)
            console.print(f"Wrote snapshot {ts} (logged {n} paper trades).")
        else:
            console.print(f"Wrote snapshot {ts}.")

    if send_to_discord and cfg.discord_webhook:
        webhook = cfg.discord_digest_webhook or cfg.discord_webhook
        ts_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        await send_digest(
            webhook,
            top=scored[:10],
            edge_candidates=edge[:5],
            title=f"Polymarket digest — {ts_label}",
            recommendations=recs,
            research_results=research_results,
        )
        console.print("Sent digest to Discord.")
    elif send_to_discord:
        console.print("[yellow]No DISCORD_WEBHOOK_URL set, skipping send.[/yellow]")


@app.command()
def snapshot(
    write: Annotated[bool, typer.Option("--write/--no-write", help="Persist snapshot to SQLite")] = True,
    notify: Annotated[bool, typer.Option("--notify/--no-notify", help="Send to Discord")] = False,
    paper: Annotated[bool, typer.Option("--paper/--no-paper", help="Log paper trades for edge candidates")] = True,
    do_research: Annotated[bool, typer.Option("--research/--no-research", help="Auto-research via headless claude CLI")] = True,
):
    """Fetch leaderboard + positions, score markets, persist + optionally notify."""
    cfg = cfg_mod.load()
    asyncio.run(_do_snapshot(
        cfg, write_db=write, send_to_discord=notify, persist_paper=paper, do_research=do_research,
    ))


@app.command()
def show(top_k: Annotated[int, typer.Option(help="How many top markets to print")] = 20):
    """Run a snapshot and print to terminal only (no DB write, no Discord, no research)."""
    cfg = cfg_mod.load()
    asyncio.run(_do_snapshot(
        cfg, write_db=False, send_to_discord=False, persist_paper=False, do_research=False,
    ))


async def _do_kalshi_match(cfg, top_k: int):
    async with PolymarketClient(cfg) as pm:
        traders = await pm.fetch_leaderboard()
        positions = await pm.fetch_positions_for(traders)
        cids = sorted({
            p.condition_id for ps in positions.values() for p in ps if p.is_open and p.condition_id
        })
        markets = await pm.fetch_markets_by_condition_ids(cids)
    scored = signals.score(cfg, traders, positions, markets)
    edge = signals.edge_only(scored)[:top_k]

    if not edge:
        console.print("No edge candidates to match.")
        return

    console.print(f"Fetching Kalshi open markets...")
    async with KalshiClient(cfg.kalshi_api) as k:
        kalshi_markets = await k.list_open_markets()
    console.print(f"  got {len(kalshi_markets)} Kalshi markets")

    table = Table(title=f"Top {len(edge)} edge candidates with Kalshi matches", expand=True)
    table.add_column("Polymarket")
    table.add_column("PM side", justify="center")
    table.add_column("PM yes px", justify="right")
    table.add_column("Kalshi match")
    table.add_column("K yes ask", justify="right")
    for s in edge:
        m = fuzzy_match(s.title, kalshi_markets)
        kalshi_str = "(no match)" if m is None else f"{m.title[:50]} ({m.ticker})"
        k_ask = f"{m.yes_ask:.2f}" if m and m.yes_ask is not None else "-"
        table.add_row(
            s.title[:55],
            s.consensus_side,
            f"{s.yes_price:.2f}" if s.yes_price is not None else "?",
            kalshi_str,
            k_ask,
        )
    console.print(table)


@app.command("kalshi-match")
def kalshi_match(top_k: Annotated[int, typer.Option(help="How many edge candidates to match")] = 10):
    """For top edge candidates, look up the most similar Kalshi market by title."""
    cfg = cfg_mod.load()
    asyncio.run(_do_kalshi_match(cfg, top_k))


async def _do_resolve(cfg):
    async with PolymarketClient(cfg) as pm:
        result = await paper.resolve_paper_trades(cfg.db_path, pm)
    console.print(
        f"Checked {result.n_checked} pending paper trades, "
        f"resolved {result.n_resolved} ({result.n_wins}W / {result.n_losses}L), "
        f"total P&L = ${result.total_pnl:,.2f}"
    )
    s = paper.summary(cfg.db_path)
    table = Table(title="Cumulative paper-trade P&L", show_header=False)
    table.add_row("Resolved trades", f"{s['resolved']}")
    table.add_row("Pending trades", f"{s['pending']}")
    table.add_row("Wins / Losses", f"{s['wins']} / {s['losses']}")
    table.add_row("Win rate", f"{s['win_rate']:.1%}")
    table.add_row("Total notional", f"${s['total_notional']:,.0f}")
    table.add_row("Total P&L", f"${s['total_pnl']:,.2f}")
    table.add_row("ROI", f"{s['roi']:.2%}")
    console.print(table)


@app.command()
def resolve():
    """Resolve any paper trades whose underlying market has closed; compute realized P&L."""
    cfg = cfg_mod.load()
    asyncio.run(_do_resolve(cfg))


@app.command("export-csv")
def export_csv(
    out: Annotated[Path, typer.Option(help="Output CSV path")] = Path("data/predictions.csv"),
):
    """Dump per-snapshot, per-market data into a spreadsheet for weekly tracking."""
    cfg = cfg_mod.load()
    n = export_mod.export(cfg.db_path, out)
    console.print(f"Wrote {n} rows to {out}")


@app.command()
def init_db():
    """Initialize the SQLite database (idempotent)."""
    cfg = cfg_mod.load()
    storage.Storage(cfg.db_path)
    console.print(f"Initialized {cfg.db_path}")


if __name__ == "__main__":
    app()
