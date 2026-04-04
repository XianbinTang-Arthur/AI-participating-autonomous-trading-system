"""Research Data Platform configuration.

All sensitive or environment-specific values are loaded from ``.env.research``
(project root).  Code defaults are intentionally set to non-functional
placeholders so the system will not accidentally connect with hardcoded
credentials.

The ``.env.research`` file is covered by ``.gitignore`` (matches ``.env.*``).
A template with documentation is provided at
``configs/templates/.env.research.example``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env.research relative to the project root
# (config.py lives at aats/data_platform/config.py → project root is 3 levels up)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env.research"


class ResearchPlatformSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RDP_",
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+psycopg://localhost:5432/aats_research",
        description="PostgreSQL DSN for the research database. "
                    "Set via RDP_DATABASE_URL in .env.research.",
    )

    # Historical file directories (daemon mode)
    historical_download_dir: str = Field(
        default="./data/historical",
        description="Root directory for OKX historical download files (legacy)",
    )
    historical_incoming_dir: str = Field(
        default="./data/historical/incoming",
        description="Incoming directory — place ZIP files here for auto-consumption",
    )
    historical_completed_dir: str = Field(
        default="./data/historical/completed",
        description="Completed directory — successfully consumed files are moved here",
    )
    historical_failed_dir: str = Field(
        default="./data/historical/failed",
        description="Failed directory — files that failed processing are moved here",
    )
    historical_scan_interval_seconds: int = Field(
        default=30,
        description="How often the historical daemon scans for new ZIP files",
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
    gold_auto_build_interval_cycles: int = Field(
        default=60,
        description="In realtime daemon: rebuild Gold every N rolling cycles",
    )

    # Gap repair
    gap_auto_detect_interval_cycles: int = Field(
        default=120,
        description="In realtime daemon: run gap detection every N rolling cycles",
    )
    gap_auto_detect_window_hours: int = Field(
        default=24,
        description="Gap detection lookback window in hours",
    )

    # Dataset version prefix
    dataset_version_prefix: str = "v1"


def get_settings() -> ResearchPlatformSettings:
    return ResearchPlatformSettings()
