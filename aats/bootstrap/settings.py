from __future__ import annotations

from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from aats.schemas.decision import AIOperatingMode


RuntimeMode = Literal["backtest", "paper_live", "guarded_live", "autonomous_live"]
EnvironmentName = Literal["dev", "staging", "prod"]
SupportedTimeframe = Literal["15m", "1h"]
StorageMode = Literal["memory", "postgres"]
PersistenceMode = Literal["strict", "permissive"]
ConfigProfile = Literal[
    "local_demo",
    "real_market_paper",
    "guarded_simulated_submit_dry_run",
    "guarded_simulated_submit_enabled",
    "guarded_simulated_dry_run",
    "guarded_simulated_enabled",
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
    bootstrap_portfolio_from_exchange: bool = False
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
    ai_operating_mode: AIOperatingMode = "baseline_only"
    ai_provider: Literal["disabled", "openai"] = "disabled"
    ai_model_name: str = "gpt-4o-mini"
    ai_prompt_version: str = "0.2.0"
    ai_model_version: str = "1.0.0"
    ai_timeout_seconds: float = 5.0
    ai_max_retries: int = 0
    ai_degrade_after_failures: int = 3
    ai_recover_after_successes: int = 2
    ai_primary_min_confidence: float = 0.6
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com"
    market_data_stale_after_seconds: float = 30.0
    account_state_stale_after_seconds: float = 60.0
    reconciliation_stale_after_seconds: float = 300.0
    okx_rest_url: str = "https://www.okx.com"
    okx_public_ws_url: str = "wss://ws.okx.com:8443/ws/v5/public"
    okx_business_ws_url: str = "wss://ws.okx.com:8443/ws/v5/business"
    okx_api_key: str | None = None
    okx_api_secret: str | None = None
    okx_api_passphrase: str | None = None
    okx_simulated_trading: bool = False
    okx_timeout_seconds: float = 10.0
    okx_market_reconnect_delay_seconds: float = 2.0
    okx_account_refresh_interval_seconds: float = 15.0
    okx_execution_sync_interval_seconds: float = 5.0
    okx_fill_fetch_limit: int = 100
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    operator_auth_enabled: bool = False
    operator_read_api_key: str | None = None
    operator_write_api_key: str | None = None
    operator_unsafe_write_without_auth: bool = False
    operator_session_secret: str | None = None
    operator_session_cookie_name: str = "aats_operator_session"
    operator_session_max_age_seconds: int = 43_200
    operator_session_cookie_secure: bool = False
    operator_viewer_username: str | None = None
    operator_viewer_password: str | None = None
    operator_operator_username: str | None = None
    operator_operator_password: str | None = None
    operator_admin_username: str | None = None
    operator_admin_password: str | None = None
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_rotate_max_bytes: int = 5_242_880
    log_backup_count: int = 7
    exchange_name: str = "PAPER"
    allowed_symbols: tuple[str, ...] = Field(default=("BTC-USDT",))

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> "AATSSettings":
        # BaseSettings.model_validate() will still consult .env sources. When callers
        # pass an explicit dict, treat it as a concrete override set instead.
        if isinstance(obj, dict):
            return cls(_env_file=None, **obj)
        return super().model_validate(obj, *args, **kwargs)

    @property
    def supported_timeframes(self) -> tuple[SupportedTimeframe, SupportedTimeframe]:
        return (self.primary_timeframe, self.secondary_timeframe)

    @property
    def okx_credentials_configured(self) -> bool:
        return all((self.okx_api_key, self.okx_api_secret, self.okx_api_passphrase))

    @property
    def ai_provider_configured(self) -> bool:
        if self.ai_provider == "disabled":
            return False
        if self.ai_provider == "openai":
            return bool(self.openai_api_key)
        return False

    @property
    def operator_session_configured(self) -> bool:
        return bool(
            self.operator_session_secret
            and (
                (self.operator_viewer_username and self.operator_viewer_password)
                or (self.operator_operator_username and self.operator_operator_password)
                or (self.operator_admin_username and self.operator_admin_password)
            )
        )
