"""Discord webhook notifier. Rich embeds for alerts and daily digests."""
from __future__ import annotations

import logging
from typing import Iterable

import httpx

from .analyze import BetRecommendation
from .models import MarketScore
from .research import ResearchResult

log = logging.getLogger(__name__)

COLOR_EDGE = 0x10B981   # green: smart money disagrees with market
COLOR_NORMAL = 0x3B82F6  # blue: high consensus, market agrees
COLOR_DIGEST = 0x6366F1  # indigo: daily digest
COLOR_ERROR = 0xEF4444   # red: errors

MAX_FIELDS_PER_EMBED = 25  # Discord limit
MAX_EMBEDS_PER_MESSAGE = 10  # Discord limit


def _fmt_money(x: float) -> str:
    if abs(x) >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if abs(x) >= 1_000:
        return f"${x/1_000:.1f}k"
    return f"${x:,.0f}"


def _final_take(s: MarketScore, rec: BetRecommendation | None, research: ResearchResult | None) -> str:
    """Build a one-paragraph plain-English verdict combining signal + research."""
    bits: list[str] = []
    if rec is None:
        bits.append("No actionable bet (Kelly says skip).")
    else:
        win_str = f"{rec.p_blended:.0%}"
        bits.append(
            f"Bet {rec.side} (~{win_str} estimated win prob). "
            f"Smart money agrees with this side; "
        )
        if rec.ev_pct >= 0.30:
            bits.append("EV looks attractive but the underlying prob is uncertain — small bet recommended.")
        else:
            bits.append("modest edge — proceed with sizing as shown.")
    if research is not None:
        bits.append(f" Research ({research.confidence} confidence): {research.reasoning}")
    return " ".join(bits).strip()


def _market_embed(
    s: MarketScore,
    rec: BetRecommendation | None = None,
    *,
    research: ResearchResult | None = None,
    color: int | None = None,
) -> dict:
    if color is None:
        color = COLOR_EDGE if s.has_edge else COLOR_NORMAL

    smart_dollars = s.no_dollars if s.consensus_side == "NO" else s.yes_dollars
    price_str = f"{s.yes_price:.2f}" if s.yes_price is not None else "?"

    lines = [
        f"**Smart money:** {s.consensus_side}  ·  {s.n_traders} traders  ·  {_fmt_money(smart_dollars)}",
        f"**Market price:** YES {price_str}",
    ]
    if rec is not None:
        lines.append(
            f"**Est. win prob:** {rec.p_blended:.0%}  ·  EV {rec.ev_pct:+.0%}"
        )
        lines.append(
            f"**Suggested bet:** ${rec.bet_size_usd:,.0f} on {rec.side} "
            f"→ win ${rec.profit_if_win_usd:,.0f} / lose ${-rec.loss_if_wrong_usd:,.0f}"
        )

    final_take = _final_take(s, rec, research)
    if final_take:
        # Discord embed field value limit is 1024 chars.
        lines.append(f"\n**Final take:** {final_take[:900]}")

    return {
        "title": (s.title or "(untitled)")[:250],
        "url": s.url or None,
        "color": color,
        "description": "\n".join(lines)[:4096],
        "footer": {"text": f"Resolves {s.end_date}" if s.end_date else "Polymarket"},
    }


async def send_embeds(webhook_url: str, embeds: list[dict], *, content: str = "") -> None:
    if not webhook_url:
        log.warning("no webhook url configured, skipping Discord send")
        return
    for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
        chunk = embeds[i : i + MAX_EMBEDS_PER_MESSAGE]
        payload = {"embeds": chunk}
        if content and i == 0:
            payload["content"] = content[:1900]
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(webhook_url, json=payload)
            if r.status_code >= 300:
                log.error("Discord webhook %d: %s", r.status_code, r.text[:300])
                r.raise_for_status()


async def send_alert(webhook_url: str, market: MarketScore) -> None:
    """Send a single high-priority alert for a market with a fresh edge signal."""
    embed = _market_embed(market, color=COLOR_EDGE)
    content = (
        f"**New edge candidate**: smart money on {market.consensus_side} "
        f"vs market {market.market_implied_side or '?'}"
    )
    await send_embeds(webhook_url, [embed], content=content)


async def send_digest(
    webhook_url: str,
    top: Iterable[MarketScore],
    edge_candidates: Iterable[MarketScore],
    *,
    title: str = "Polymarket digest",
    recommendations: dict[str, BetRecommendation] | None = None,
    research_results: dict[str, ResearchResult] | None = None,
) -> None:
    top = list(top)
    edge_candidates = list(edge_candidates)
    recs = recommendations or {}
    research = research_results or {}
    n_total = len(top)
    n_edge = len(edge_candidates)

    parts = [f"**{title}**", f"{n_total} markets · {n_edge} edge candidate{'s' if n_edge != 1 else ''}"]
    if edge_candidates:
        best = edge_candidates[0]
        best_rec = recs.get(best.condition_id)
        if best_rec is not None:
            parts.append(
                f"Top pick: **{best.title[:80]}** → bet **${best_rec.bet_size_usd:,.0f} on {best_rec.side}**"
            )
        else:
            parts.append(f"Top pick: **{best.title[:80]}** → bet **{best.consensus_side}**")
    total_recommended = sum(r.bet_size_usd for r in recs.values())
    if total_recommended > 0:
        parts.append(f"Total recommended exposure today: ${total_recommended:,.0f}")
    content = "\n".join(parts)

    embeds: list[dict] = []
    for m in edge_candidates[:5]:
        embeds.append(_market_embed(
            m, recs.get(m.condition_id),
            research=research.get(m.condition_id), color=COLOR_EDGE,
        ))
    for m in top[: max(0, 10 - len(embeds))]:
        if any(e.get("url") == m.url for e in embeds):
            continue
        embeds.append(_market_embed(
            m, recs.get(m.condition_id),
            research=research.get(m.condition_id), color=COLOR_NORMAL,
        ))

    await send_embeds(webhook_url, embeds, content=content)
