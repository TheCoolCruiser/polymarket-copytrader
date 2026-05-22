"""Pydantic models for Polymarket API responses + internal types."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Trader(BaseModel):
    model_config = ConfigDict(extra="ignore")
    rank: int
    proxy_wallet: str = Field(alias="proxyWallet")
    user_name: str = Field(alias="userName")
    x_username: str = Field(default="", alias="xUsername")
    verified_badge: bool = Field(default=False, alias="verifiedBadge")
    vol: float = 0.0
    pnl: float = 0.0

    @field_validator("rank", mode="before")
    @classmethod
    def _coerce_rank(cls, v: Any) -> int:
        return int(v) if v is not None else 0


class Position(BaseModel):
    model_config = ConfigDict(extra="ignore")
    proxy_wallet: str = Field(alias="proxyWallet")
    asset: str
    condition_id: str = Field(alias="conditionId")
    size: float = 0.0
    avg_price: float = Field(default=0.0, alias="avgPrice")
    initial_value: float = Field(default=0.0, alias="initialValue")
    current_value: float = Field(default=0.0, alias="currentValue")
    cur_price: float = Field(default=0.0, alias="curPrice")
    cash_pnl: float = Field(default=0.0, alias="cashPnl")
    percent_pnl: float = Field(default=0.0, alias="percentPnl")
    redeemable: bool = False
    title: str = ""
    slug: str = ""
    event_slug: str = Field(default="", alias="eventSlug")
    event_id: str = Field(default="", alias="eventId")
    outcome: str = ""
    outcome_index: int = Field(default=0, alias="outcomeIndex")
    end_date: str = Field(default="", alias="endDate")
    negative_risk: bool = Field(default=False, alias="negativeRisk")

    @property
    def is_open(self) -> bool:
        return self.current_value > 0

    @property
    def yes_side(self) -> bool:
        return self.outcome == "Yes"

    @property
    def yes_price(self) -> float | None:
        if self.cur_price <= 0:
            return None
        return self.cur_price if self.yes_side else 1.0 - self.cur_price


class GammaMarket(BaseModel):
    """Subset of the Gamma /markets payload we actually use."""
    model_config = ConfigDict(extra="ignore")
    id: str
    question: str = ""
    slug: str = ""
    condition_id: str = Field(default="", alias="conditionId")
    end_date: str = Field(default="", alias="endDate")
    end_date_iso: str = Field(default="", alias="endDateIso")
    outcomes: str = "[]"
    outcome_prices: str = Field(default="[]", alias="outcomePrices")
    volume: str = "0"
    volume_num: float = Field(default=0.0, alias="volumeNum")
    liquidity_num: float = Field(default=0.0, alias="liquidityNum")
    active: bool = False
    closed: bool = True
    accepting_orders: bool = Field(default=False, alias="acceptingOrders")
    clob_token_ids: str = Field(default="[]", alias="clobTokenIds")

    @property
    def end_dt(self) -> datetime | None:
        for candidate in (self.end_date_iso, self.end_date):
            if not candidate:
                continue
            try:
                if candidate.endswith("Z"):
                    return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
                return datetime.fromisoformat(candidate)
            except ValueError:
                continue
        return None


class MarketScore(BaseModel):
    """A consensus-scored market — internal type produced by signals.py."""
    condition_id: str
    title: str
    event_slug: str
    end_date: str
    score: float
    n_traders: int
    yes_dollars: float
    no_dollars: float
    yes_price: float | None
    consensus_side: str
    market_implied_side: str | None
    edge: float | None
    top_trader_names: list[str]
    has_edge: bool

    @property
    def url(self) -> str:
        return f"https://polymarket.com/event/{self.event_slug}" if self.event_slug else ""
