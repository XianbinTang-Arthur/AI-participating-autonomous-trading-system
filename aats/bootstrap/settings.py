from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from aats.schemas.decision import AIOperatingMode, CanonicalAIOperatingMode, normalize_ai_operating_mode


RuntimeMode = Literal["backtest", "paper_live", "guarded_live", "autonomous_live"]
EnvironmentName = Literal["dev", "staging", "prod"]
SupportedTimeframe = Literal["15m", "1h"]
StorageMode = Literal["memory", "postgres"]
PersistenceMode = Literal["strict", "permissive"]
TradingProductType = Literal["spot", "derivatives"]
MarginMode = Literal["cash", "cross", "isolated"]
ConfigProfile = Literal[
    "local_demo",
    "real_market_paper",
    "forward_test_small_capital",
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
AIExecutionSuggestionMode = Literal["disabled", "diagnostic_only", "shadow_translation", "enabled_live"]


class AATSSettings(BaseSettings):
    model_config = SettingsConfigDict(
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
    database_single_runtime_guard_enabled: bool = True
    database_runtime_lock_key: int = 42_420_001
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
    ai_auto_downgrade_enabled: bool = True
    ai_recovery_probe_interval_seconds: float = 300.0
    ai_decision_min_confidence: float = Field(
        default=0.6,
        validation_alias=AliasChoices("ai_decision_min_confidence", "ai_primary_min_confidence"),
    )
    ai_decision_max_uncertainty: float = Field(
        default=0.35,
        validation_alias=AliasChoices("ai_decision_max_uncertainty", "ai_primary_max_uncertainty"),
    )
    ai_decision_min_directional_edge: float = Field(
        default=0.2,
        validation_alias=AliasChoices("ai_decision_min_directional_edge", "ai_primary_min_directional_edge"),
    )
    ai_shadow_mode_enabled: bool = True
    ai_shadow_evaluation_window: int = 50
    ai_outcome_review_bad_window_threshold: int = 2
    ai_outcome_review_warmup_evaluations: int = 10
    ai_outcome_review_min_trade_count: int = 3
    ai_outcome_max_fee_ratio_delta: float = 0.05
    ai_outcome_max_churn_ratio_delta: float = 0.08
    ai_execution_suggestion_mode: AIExecutionSuggestionMode = "disabled"
    ai_execution_max_passive_bias: float = 1.0
    ai_execution_max_maker_taker_bias: float = 1.0
    ai_execution_max_cross_spread_bps: float = 6.0
    ai_execution_max_slice_count: int = 4
    ai_execution_max_participation_rate: float = 0.35
    ai_execution_max_cancel_replace_patience_ms: int = 4_000
    ai_execution_live_limit_offset_fraction_of_slippage: float = 0.5
    ai_profile_control_freeze_after_admin_override_seconds: float = 3_600.0
    ai_manual_operating_mode_override_freeze_seconds: float = 3_600.0
    strategy_profile_auto_control_enabled: bool = False
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com"
    market_data_stale_after_seconds: float = 30.0
    account_state_stale_after_seconds: float = 60.0
    reconciliation_stale_after_seconds: float = 300.0
    okx_rest_url: str = "https://www.okx.com"
    okx_public_ws_url: str = "wss://ws.okx.com:8443/ws/v5/public"
    okx_business_ws_url: str = "wss://ws.okx.com:8443/ws/v5/business"
    okx_private_ws_url: str = "wss://ws.okx.com:8443/ws/v5/private"
    okx_api_key: str | None = None
    okx_api_secret: str | None = None
    okx_api_passphrase: str | None = None
    okx_simulated_trading: bool = False
    okx_timeout_seconds: float = 10.0
    okx_market_reconnect_delay_seconds: float = 2.0
    okx_market_rest_fallback_enabled: bool = True
    okx_market_rest_fallback_poll_interval_seconds: float = 5.0
    okx_account_refresh_interval_seconds: float = 15.0
    okx_execution_sync_interval_seconds: float = 5.0
    execution_command_flow_enabled: bool = False
    execution_command_poll_interval_seconds: float = 1.0
    portfolio_ledger_truth_enabled: bool = False
    recovery_reconciliation_execution_ledger_enabled: bool = False
    operator_control_plane_execution_ledger_enabled: bool = False
    financial_convergence_mode_enabled: bool = False
    okx_fill_fetch_limit: int = 100
    okx_bills_fetch_limit: int = 100
    okx_trade_fee_refresh_interval_seconds: float = 300.0
    okx_system_status_refresh_interval_seconds: float = 60.0
    okx_max_order_quantity_precheck_enabled: bool = True
    okx_account_config_validation_enabled: bool = True
    okx_system_status_gate_enabled: bool = True
    okx_private_balance_position_ws_enabled: bool = True
    strategy_profile_auto_rollback_enabled: bool = False
    strategy_profile_auto_rollback_review_required_only: bool = True
    strategy_profile_auto_rollback_min_trade_count: int = 3
    strategy_profile_auto_rollback_cooldown_seconds: float = 1_800.0
    strategy_profile_auto_rollback_allowed_symbols: tuple[str, ...] = Field(default=tuple())
    strategy_profile_auto_rollback_allowed_regimes: tuple[str, ...] = Field(default=tuple())
    strategy_profile_auto_rollback_allowed_profiles: tuple[str, ...] = Field(default=tuple())
    strategy_profile_offline_replay_windows: tuple[int, ...] = Field(default=(10, 20, 50))
    strategy_profile_offline_replay_pipeline_version: str = "offline_replay_pipeline_v2"
    strategy_profile_auto_activation_min_composite_score: float = 0.0
    strategy_profile_auto_activation_min_offline_replay_score: float = -10.0
    strategy_profile_auto_activation_min_recommendation_strength: float = 0.0
    strategy_profile_auto_activation_require_positive_replay_consensus: bool = False
    strategy_profile_auto_activation_disallow_when_shadow_review_required: bool = False
    strategy_profile_activation_policy_enabled: bool = False
    strategy_profile_activation_policy_allowed_symbols: tuple[str, ...] = Field(default=tuple())
    strategy_profile_activation_policy_allowed_regimes: tuple[str, ...] = Field(default=tuple())
    strategy_profile_activation_policy_allowed_profiles: tuple[str, ...] = Field(default=tuple())
    strategy_profile_failure_rollback_on_degraded_evaluation: bool = True
    strategy_profile_failure_rollback_on_shadow_review_required: bool = True
    strategy_profile_failure_rollback_on_alternative_winner: bool = True
    strategy_profile_activation_min_active_minutes: int = 240
    strategy_profile_activation_min_score_delta: float = 0.5
    strategy_profile_activation_required_consecutive_wins: int = 3
    strategy_profile_auto_switch_min_closed_trades: int = 6
    strategy_profile_auto_switch_min_replay_validations: int = 5
    strategy_profile_cold_start_lock_enabled: bool = True
    strategy_profile_safety_profiles: tuple[str, ...] = Field(default=("execution_degraded_safe",))
    strategy_profile_safety_trigger_execution_error_count: int = 3
    strategy_profile_score_fee_penalty_weight: float = -25.0
    strategy_profile_score_churn_penalty_weight: float = -20.0
    strategy_profile_score_degraded_status_penalty: float = -15.0
    strategy_profile_score_low_health_conservative_bonus: float = 2.5
    strategy_profile_score_low_health_non_conservative_penalty: float = -2.0
    strategy_profile_score_divergence_execution_bonus: float = 1.5
    strategy_profile_score_divergence_other_penalty: float = -1.0
    trading_product_type: TradingProductType = "spot"
    margin_mode: MarginMode = "cash"
    max_target_leverage: float = 1.0
    default_target_leverage: float = 1.0
    strategy_short_bias_enabled: bool = False
    strategy_dynamic_leverage_enabled: bool = False
    strategy_flat_signal_hold_enabled: bool = True
    strategy_flat_exit_microstructure_threshold: float = 0.12
    strategy_flat_exit_factor_threshold: float = 0.18
    strategy_flat_exit_ai_edge_threshold: float = 0.22
    strategy_cost_guard_enabled: bool = True
    strategy_alpha_edge_bps_scale: float = 100.0
    strategy_expected_slippage_bps_fraction: float = 0.25
    strategy_edge_noise_buffer_bps: float = 3.0
    strategy_min_net_edge_bps: float = 2.0
    strategy_entry_allowed_regimes: tuple[str, ...] = Field(default=("trend", "breakout"))
    strategy_entry_min_signal_edge_bps: float = 10.0
    strategy_entry_alpha_min: float = 0.18
    strategy_entry_confidence_min: float = 0.62
    strategy_scale_in_min_signal_edge_bps: float = 12.0
    strategy_scale_in_alpha_min: float = 0.24
    strategy_scale_in_confidence_min: float = 0.68
    strategy_reversal_min_signal_edge_bps: float = 15.0
    strategy_reversal_alpha_min: float = 0.3
    strategy_reversal_confidence_min: float = 0.75
    strategy_min_hold_seconds: float = 900.0
    strategy_post_close_cooldown_seconds: float = 600.0
    strategy_health_lookback_trades: int = 12
    strategy_performance_guard_min_closed_trades: int = 4
    strategy_max_fee_drag_ratio: float = 0.55
    strategy_max_churn_ratio: float = 0.6
    strategy_low_edge_threshold_bps: float = 3.0
    strategy_low_edge_streak_limit: int = 3
    strategy_low_edge_cooldown_seconds: float = 1_800.0
    strategy_transient_close_retry_cooldown_seconds: float = 90.0
    trial_guard_enabled: bool = False
    trial_guard_poll_interval_seconds: float = 15.0
    trial_guard_lookback_fills: int = 30
    trial_guard_min_closed_fills: int = 5
    trial_guard_max_daily_loss_usdt: float = 25.0
    trial_guard_max_consecutive_losses: int = 4
    trial_guard_max_fee_to_notional_ratio: float = 0.0012
    trial_guard_max_high_slippage_ratio: float = 0.35
    trial_guard_max_slow_submit_to_fill_ratio: float = 0.35
    max_margin_usage_fraction: float = 0.85
    liquidation_buffer_fraction: float = 0.15
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
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_rotate_max_bytes: int = 5_242_880
    log_backup_count: int = 7
    exchange_name: str = "PAPER"
    allowed_symbols: tuple[str, ...] = Field(default=("BTC-USDT",))

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> "AATSSettings":
        # BaseSettings instances still consult process environment unless we neutralize
        # the env prefix as well as the env file. Callers that pass an explicit dict
        # expect deterministic defaults plus their overrides, not the operator shell's
        # live AATS_* values leaking into tests or runtime-built settings objects.
        if isinstance(obj, dict):
            return cls(_env_file=None, _env_prefix="__AATS_EXPLICIT_CONFIG__", **obj)
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
    def canonical_ai_operating_mode(self) -> CanonicalAIOperatingMode:
        return normalize_ai_operating_mode(self.ai_operating_mode)

    @property
    def strategy_profile_auto_control_configured(self) -> bool:
        return bool(self.strategy_profile_auto_control_enabled)

    def strategy_profile_auto_control_is_enabled_for_mode(self, operating_mode: str | None) -> bool:
        _ = operating_mode
        return bool(self.strategy_profile_auto_control_enabled)

    @property
    def operator_session_configured(self) -> bool:
        return bool(self.operator_session_secret)
