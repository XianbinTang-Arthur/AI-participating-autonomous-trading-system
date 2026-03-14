from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


RuntimeMode = Literal["backtest", "paper_live", "guarded_live", "autonomous_live"]
EnvironmentName = Literal["dev", "staging", "prod"]
SupportedTimeframe = Literal["15m", "1h"]
StorageMode = Literal["memory", "postgres"]
PersistenceMode = Literal["strict", "permissive"]
ConfigProfile = Literal[
    "local_demo",
    "real_market_paper",
    "guarded_live_blocked",
    "guarded_live_enabled",
]
MarketDataBackend = Literal["demo", "okx"]
ExecutionBackend = Literal["paper", "okx"]
AccountBackend = Literal["disabled", "okx"]


class AATSSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AATS_",
        extra="ignore",
    )

    environment: EnvironmentName = "dev"
    config_profile: ConfigProfile = "local_demo"
    mode: RuntimeMode = "paper_live"
    default_symbol: str = "BTC-USDT"
    primary_timeframe: SupportedTimeframe = "15m"
    secondary_timeframe: SupportedTimeframe = "1h"
    enabled_decision_timeframes: tuple[SupportedTimeframe, ...] = Field(default=("15m",))
    initial_usdt_balance: float = 10_000.0
    storage_mode: StorageMode = "memory"
    event_persistence_mode: PersistenceMode = "strict"
    market_data_backend: MarketDataBackend = "demo"
    execution_backend: ExecutionBackend = "paper"
    account_backend: AccountBackend = "disabled"
    account_read_enabled: bool = False
    live_submit_enabled: bool = False
    guarded_execution_dry_run: bool = True
    database_url: str | None = None
    database_auto_create_schema: bool = True
    max_abs_position_qty: float = 0.01
    max_notional_per_symbol: float = 1_000.0
    max_open_orders: int = 5
    max_decisions_per_minute: int = 6
    default_order_qty: float = 0.001
    local_publish_iterations: int = 6
    local_publish_interval_seconds: float = 0.0
    decision_min_interval_seconds_15m: float = 0.0
    decision_min_interval_seconds_1h: float = 0.0
    decision_min_price_move_bps: float = 0.0
    decision_min_momentum_delta: float = 0.0
    paper_taker_fee_bps: float = 5.0
    max_slippage_tolerance_bps: int = 20
    market_data_stale_after_seconds: float = 30.0
    account_state_stale_after_seconds: float = 60.0
    reconciliation_stale_after_seconds: float = 300.0
    okx_rest_url: str = "https://us.okx.com"
    okx_public_ws_url: str = "wss://wsus.okx.com:8443/ws/v5/public"
    okx_business_ws_url: str = "wss://wsus.okx.com:8443/ws/v5/business"
    okx_api_key: str | None = None
    okx_api_secret: str | None = None
    okx_api_passphrase: str | None = None
    okx_simulated_trading: bool = False
    okx_timeout_seconds: float = 10.0
    okx_market_reconnect_delay_seconds: float = 2.0
    okx_account_refresh_interval_seconds: float = 15.0
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"
    exchange_name: str = "PAPER"
    allowed_symbols: tuple[str, ...] = Field(default=("BTC-USDT",))

    @property
    def supported_timeframes(self) -> tuple[SupportedTimeframe, SupportedTimeframe]:
        return (self.primary_timeframe, self.secondary_timeframe)

    @property
    def okx_credentials_configured(self) -> bool:
        return all((self.okx_api_key, self.okx_api_secret, self.okx_api_passphrase))
