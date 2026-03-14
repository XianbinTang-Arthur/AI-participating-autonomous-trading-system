from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


RuntimeMode = Literal["backtest", "paper_live", "guarded_live", "autonomous_live"]
EnvironmentName = Literal["dev", "staging", "prod"]
SupportedTimeframe = Literal["15m", "1h"]
StorageMode = Literal["memory", "postgres"]


class AATSSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AATS_",
        extra="ignore",
    )

    environment: EnvironmentName = "dev"
    mode: RuntimeMode = "paper_live"
    default_symbol: str = "BTC-USDT"
    primary_timeframe: SupportedTimeframe = "15m"
    secondary_timeframe: SupportedTimeframe = "1h"
    initial_usdt_balance: float = 10_000.0
    storage_mode: StorageMode = "memory"
    database_url: str | None = None
    database_auto_create_schema: bool = True
    max_abs_position_qty: float = 0.01
    default_order_qty: float = 0.001
    local_publish_iterations: int = 6
    local_publish_interval_seconds: float = 0.0
    paper_taker_fee_bps: float = 5.0
    max_slippage_tolerance_bps: int = 20
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    exchange_name: str = "PAPER"
    allowed_symbols: tuple[str, ...] = Field(default=("BTC-USDT",))

    @property
    def supported_timeframes(self) -> tuple[SupportedTimeframe, SupportedTimeframe]:
        return (self.primary_timeframe, self.secondary_timeframe)
