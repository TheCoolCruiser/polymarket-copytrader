"""Environment-driven configuration. Loads .env if present."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    return int(raw) if raw and raw.strip() else default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    return float(raw) if raw and raw.strip() else default


@dataclass(frozen=True)
class Config:
    data_api: str
    gamma_api: str
    kalshi_api: str
    discord_webhook: str
    discord_digest_webhook: str
    top_n_traders: int
    time_period: str
    order_by: str
    min_traders_per_market: int
    min_consensus_score: float
    min_market_volume_usd: float
    min_minutes_to_resolution: int
    edge_threshold: float
    bankroll_usd: float
    kelly_fraction: float
    max_bet_pct: float
    daily_exposure_cap: float
    db_path: Path


def load() -> Config:
    return Config(
        data_api=_env_str("POLYMARKET_DATA_API", "https://data-api.polymarket.com"),
        gamma_api=_env_str("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com"),
        kalshi_api=_env_str("KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2"),
        discord_webhook=_env_str("DISCORD_WEBHOOK_URL", ""),
        discord_digest_webhook=_env_str("DISCORD_DIGEST_WEBHOOK_URL", ""),
        top_n_traders=_env_int("TOP_N_TRADERS", 50),
        time_period=_env_str("LEADERBOARD_TIME_PERIOD", "MONTH"),
        order_by=_env_str("LEADERBOARD_ORDER_BY", "PNL"),
        min_traders_per_market=_env_int("MIN_TRADERS_PER_MARKET", 4),
        min_consensus_score=_env_float("MIN_CONSENSUS_SCORE", 0.15),
        min_market_volume_usd=_env_float("MIN_MARKET_VOLUME_USD", 10000),
        min_minutes_to_resolution=_env_int("MIN_MINUTES_TO_RESOLUTION", 60),
        edge_threshold=_env_float("EDGE_THRESHOLD", 0.08),
        bankroll_usd=_env_float("BANKROLL_USD", 1000.0),
        kelly_fraction=_env_float("KELLY_FRACTION", 0.25),
        max_bet_pct=_env_float("MAX_BET_PCT", 0.05),
        daily_exposure_cap=_env_float("DAILY_EXPOSURE_CAP", 0.20),
        db_path=Path(_env_str("DB_PATH", "data/copytrader.db")),
    )
