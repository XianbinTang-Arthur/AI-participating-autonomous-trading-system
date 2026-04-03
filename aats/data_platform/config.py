"""Research Data Platform configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ResearchPlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RDP_", env_file=".env", extra="ignore")

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://postgres:123456@localhost:5432/aats_research",
        description="PostgreSQL DSN for the research database",
    )

    # Historical file directory
    historical_download_dir: str = Field(
        default="./data/historical",
        description="Root directory for OKX historical download files",
    )

    # OKX API
    okx_rest_url: str = "https://www.okx.com"
    okx_timeout_seconds: float = 15.0
    okx_rate_limit_sleep: float = 0.12  # ~8 req/s conservative

    # Candles rolling
    rolling_candles_enabled: bool = True
    rolling_candles_symbols: list[str] = Field(
        default=["BTC-USDT", "ETH-USDT", "BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    )
    rolling_candles_timeframes: list[str] = Field(default=["1m", "5m", "15m", "1H"])

    # Funding rolling
    rolling_funding_enabled: bool = True
    rolling_funding_symbols: list[str] = Field(
        default=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    )

    # Gap repair
    auto_gap_repair_enabled: bool = True
    max_gap_repair_window_hours: int = 72

    # Gold build
    gold_replay_build_enabled: bool = True

    # Dataset version prefix
    dataset_version_prefix: str = "v1"


def get_settings() -> ResearchPlatformSettings:
    return ResearchPlatformSettings()
