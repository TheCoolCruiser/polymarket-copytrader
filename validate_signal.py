"""Phase 0 signal validation for the Polymarket copy-trader idea.

Pulls today's top traders, fetches their open positions in parallel, scores
each market by rank-weighted, portfolio-normalized consensus, and prints the
top markets so we can eyeball whether the signal looks meaningful.

Usage:
    pip install httpx
    python validate_signal.py
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

import httpx

DATA_API = "https://data-api.polymarket.com"
TOP_N_TRADERS = 50
TIME_PERIOD = "MONTH"      # DAY | WEEK | MONTH | ALL
ORDER_BY = "PNL"           # PNL | VOL
CATEGORY = "OVERALL"
SIZE_THRESHOLD_USD = 100   # ignore dust positions
MIN_TRADERS_PER_MARKET = 3 # require this many top traders before scoring
CONCURRENCY = 10           # parallel position fetches
TOP_K_TO_PRINT = 20


def rank_weight(rank: int) -> float:
    return 1.0 / (rank ** 0.5)


async def fetch_leaderboard(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get(
        f"{DATA_API}/v1/leaderboard",
        params={
            "category": CATEGORY,
            "timePeriod": TIME_PERIOD,
            "orderBy": ORDER_BY,
            "limit": TOP_N_TRADERS,
        },
    )
    r.raise_for_status()
    return r.json()


async def fetch_positions(
    client: httpx.AsyncClient, wallet: str, sem: asyncio.Semaphore
) -> list[dict[str, Any]]:
    async with sem:
        r = await client.get(
            f"{DATA_API}/positions",
            params={"user": wallet, "sizeThreshold": SIZE_THRESHOLD_USD, "limit": 500},
        )
        r.raise_for_status()
        return r.json()


def yes_price(pos: dict[str, Any]) -> float | None:
    cp = pos.get("curPrice")
    if cp in (None, 0):
        return None
    return cp if pos["outcome"] == "Yes" else 1.0 - cp


def score_markets(
    traders: list[dict[str, Any]], all_positions: list[list[dict[str, Any]]]
):
    scores: dict[str, float] = defaultdict(float)
    meta: dict[str, dict[str, Any]] = {}
    traders_in: dict[str, list[tuple[str, int, str, float]]] = defaultdict(list)
    yes_dollars: dict[str, float] = defaultdict(float)
    no_dollars: dict[str, float] = defaultdict(float)

    for trader, positions in zip(traders, all_positions):
        rank = int(trader["rank"])
        rw = rank_weight(rank)
        open_positions = [
            p for p in positions
            if p.get("currentValue", 0) > 0 and p.get("conditionId")
        ]
        portfolio = sum(p["currentValue"] for p in open_positions)
        if portfolio <= 0:
            continue

        for pos in open_positions:
            cid = pos["conditionId"]
            direction = 1 if pos["outcome"] == "Yes" else -1
            size_pct = pos["currentValue"] / portfolio
            scores[cid] += rw * size_pct * direction
            traders_in[cid].append(
                (trader["userName"], rank, pos["outcome"], pos["currentValue"])
            )
            if direction > 0:
                yes_dollars[cid] += pos["currentValue"]
            else:
                no_dollars[cid] += pos["currentValue"]

            if cid not in meta:
                meta[cid] = {
                    "title": pos.get("title", "?"),
                    "eventSlug": pos.get("eventSlug", ""),
                    "endDate": pos.get("endDate", "?"),
                    "yes_price": yes_price(pos),
                }
            elif meta[cid]["yes_price"] is None:
                meta[cid]["yes_price"] = yes_price(pos)

    return scores, meta, traders_in, yes_dollars, no_dollars


def format_row(
    rank_idx: int,
    cid: str,
    score: float,
    meta: dict[str, Any],
    traders_in: list[tuple[str, int, str, float]],
    yes_d: float,
    no_d: float,
) -> str:
    side = "YES" if score > 0 else "NO"
    yes_n = sum(1 for _, _, o, _ in traders_in if o == "Yes")
    no_n = len(traders_in) - yes_n
    price = meta.get("yes_price")
    price_str = f"{price:.2f}" if price is not None else "  ? "
    if price is None:
        edge = ""
    else:
        implied_market = "YES" if price > 0.5 else "NO"
        if implied_market == side:
            edge = "(market agrees)"
        else:
            edge = "(market disagrees - possible edge)"

    url = f"https://polymarket.com/event/{meta['eventSlug']}" if meta.get("eventSlug") else ""
    title = meta["title"][:80]
    top_named = ", ".join(
        f"{name}(#{r})" for name, r, _, _ in sorted(traders_in, key=lambda x: x[1])[:5]
    )
    return (
        f"  [{rank_idx:>2}] {title}\n"
        f"       side={side}  score={score:+.3f}  traders: {yes_n} Yes / {no_n} No"
        f"  |  $Yes={yes_d:,.0f}  $No={no_d:,.0f}\n"
        f"       yes_price={price_str}  {edge}\n"
        f"       top in market: {top_named}\n"
        f"       ends {meta['endDate']}  {url}"
    )


async def main() -> None:
    print(
        f"Fetching top {TOP_N_TRADERS} traders "
        f"(category={CATEGORY}, period={TIME_PERIOD}, by={ORDER_BY})..."
    )
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=30.0) as client:
        leaderboard = await fetch_leaderboard(client)
        print(f"  got {len(leaderboard)} traders")
        print("Fetching open positions in parallel...")
        all_positions = await asyncio.gather(
            *(fetch_positions(client, t["proxyWallet"], sem) for t in leaderboard)
        )

    n_pos = sum(len(p) for p in all_positions)
    open_n = sum(1 for ps in all_positions for p in ps if p.get("currentValue", 0) > 0)
    print(f"  got {n_pos} total positions ({open_n} open with currentValue>0)")

    scores, meta, traders_in, yes_d, no_d = score_markets(leaderboard, all_positions)
    ranked = [
        (cid, s) for cid, s in scores.items()
        if len(traders_in[cid]) >= MIN_TRADERS_PER_MARKET
    ]
    print(
        f"  {len(scores)} unique markets among top traders; "
        f"{len(ranked)} have >={MIN_TRADERS_PER_MARKET} top traders"
    )

    if not ranked:
        print("\nNo markets meet the minimum-trader threshold. Lower it and retry.")
        return

    ranked.sort(key=lambda x: abs(x[1]), reverse=True)
    print(f"\n=== Top {min(TOP_K_TO_PRINT, len(ranked))} markets by |consensus score| ===\n")
    for i, (cid, s) in enumerate(ranked[:TOP_K_TO_PRINT], 1):
        print(format_row(i, cid, s, meta[cid], traders_in[cid], yes_d[cid], no_d[cid]))
        print()

    # Quick "edge" view: filter to markets where consensus disagrees with current price
    edge_candidates = []
    for cid, s in ranked:
        price = meta[cid].get("yes_price")
        if price is None:
            continue
        side_consensus = "YES" if s > 0 else "NO"
        side_market = "YES" if price > 0.5 else "NO"
        if side_consensus != side_market:
            disagreement = abs(price - 0.5) + abs(s)
            edge_candidates.append((cid, s, price, disagreement))
    edge_candidates.sort(key=lambda x: x[3], reverse=True)

    print(f"=== Edge candidates (smart money disagrees with market): {len(edge_candidates)} found ===\n")
    for i, (cid, s, price, _) in enumerate(edge_candidates[:10], 1):
        side = "YES" if s > 0 else "NO"
        print(
            f"  [{i:>2}] {meta[cid]['title'][:75]}\n"
            f"       consensus {side}  (score {s:+.3f})  vs  market yes_price={price:.2f}\n"
            f"       ends {meta[cid]['endDate']}  "
            f"https://polymarket.com/event/{meta[cid]['eventSlug']}\n"
        )


if __name__ == "__main__":
    asyncio.run(main())
