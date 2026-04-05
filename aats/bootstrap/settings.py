from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from aats.schemas.decision import AIOperatingMode, CanonicalAIOperatingMode, normalize_ai_operating_mode


RuntimeMode = Literal["backtest", "paper_live", "guarded_live", "autonomous_live"]
EnvironmentName = Literal["dev", "staging", "prod"]
SupportedTimeframe = Literal["15m", "1h"]
StorageMode = Literal["memory", "postgres"]
PersistenceMode = Literal["strict", "permissive"]
TradingProductType = Literal["spot", "derivatives"]
StartupProfile = Literal["spot", "derivatives"]
EnvTemplateProfile = Literal["spot", "derivatives", "spot_live", "derivatives_live"]
MarginMode = Literal["cash", "cross", "isolated"]
DerivativesPositionMode = Literal["net", "hedge"]
DerivativesHedgeTransitionMode = Literal["close_then_open", "overlap_then_reduce", "independent_books"]
StrategyHedgeOverlayMode = Literal["protective", "opportunistic", "independent"]
StrategyHedgeOverlayRolloutStage = Literal["replay_only", "dry_run", "live"]
IndependentWeakEdgeExecutionMode = Literal["block", "report_only"]
IndependentExecutionPolicyMode = Literal[
    "adaptive",
    "passive_first",
    "bounded_limit",
    "bounded_taker",
    "aggressive_bounded_taker",
]
ConfigProfile = Literal[
    "local_demo",
    "real_market_paper",
    "forward_test_small_capital",
    "guarded_spot_dry_run",
    "guarded_spot_enabled",
    "guarded_derivatives_dry_run",
    "guarded_derivatives_enabled",
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
StrategyFamily = Literal[
    "directional",
    "smart_arbitrage",
    "spot_grid",
    "dca",
    "protective",
    "opportunistic",
    "independent",
]
SmartArbitrageNegativeBasisMode = Literal["disabled", "advisory_only", "inventory_backed", "margin_backed"]
SmartArbitragePairPriorityMode = Literal["net_edge", "executable_edge", "ideal_edge", "basis_abs"]
SmartArbitrageSpotMarginMode = Literal["cross", "isolated"]
SmartArbitrageFeeSourceMode = Literal["configured", "account_schedule"]
SmartArbitrageFundingSourceMode = Literal["configured", "account_proxy"]
SmartArbitrageBorrowSourceMode = Literal["configured", "apr_window_model"]

PRIMARY_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY = "strategy_sleeve_auto_execution_enabled"
DEPRECATED_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY = "strategy_sleeve_auto_parallel_enabled"

_PLACEHOLDER_TOKENS = (
    "REPLACE_WITH_",
    "CHANGE_ME",
    "YOUR_",
    "SET_ME",
    "TODO",
    "<",
)


def is_placeholder_config_value(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip()
    if not normalized:
        return False
    upper = normalized.upper()
    return any(upper.startswith(token) for token in _PLACEHOLDER_TOKENS)


class AATSSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AATS_",
        extra="ignore",
    )

    environment: EnvironmentName = "dev"
    config_profile: ConfigProfile = "local_demo"
    startup_profile: StartupProfile | None = None
    env_template_profile: EnvTemplateProfile | None = None
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
    # ── Active Parameter Set (RDP 整合) ──────────────────────────────
    active_parameter_registry_path: str | None = Field(
        default=None,
        description="Path to active_parameter_registry.json. "
                    "If None, defaults to configs/active_parameter_sets/active_parameter_registry.json",
    )
    active_parameters_enabled: bool = Field(
        default=False,
        description="Master switch for loading active parameter set from RDP governance. "
                    "When True, active parameters override strategy profile defaults.",
    )
    database_single_runtime_guard_enabled: bool = True
    database_runtime_lock_key: int = 42_420_001
    max_abs_position_qty: float = 0.01
    max_notional_per_symbol: float = 1_000.0
    max_open_orders: int = 5
    max_decisions_per_minute: int = 6
    default_order_qty: float = 0.001
    local_publish_iterations: int = 6
    local_publish_interval_seconds: float = 0.0
    decision_min_interval_seconds_15m: float = 60.0
    decision_min_interval_seconds_1h: float = 240.0
    decision_min_price_move_bps: float = 4.0
    decision_min_momentum_delta: float = 0.0003
    paper_taker_fee_bps: float = 5.0
    trade_cost_spot_maker_fee_bps: float = 8.0
    trade_cost_spot_taker_fee_bps: float = 10.0
    trade_cost_margin_maker_fee_bps: float = 8.0
    trade_cost_margin_taker_fee_bps: float = 10.0
    trade_cost_derivatives_maker_fee_bps: float = 2.0
    trade_cost_derivatives_taker_fee_bps: float = 5.0
    trade_cost_delivery_settlement_fee_bps: float = 1.0
    trade_cost_spot_spread_bps: float = 1.0
    trade_cost_spot_slippage_bps: float = 1.5
    trade_cost_margin_spread_bps: float = 1.0
    trade_cost_margin_slippage_bps: float = 1.5
    trade_cost_derivatives_spread_bps: float = 0.5
    trade_cost_derivatives_slippage_bps: float = 1.0
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
    market_data_stale_after_seconds: float = 45.0
    account_state_stale_after_seconds: float = 120.0
    reconciliation_stale_after_seconds: float = 300.0
    okx_rest_url: str = "https://www.okx.com"
    okx_public_ws_url: str = "wss://ws.okx.com:8443/ws/v5/public"
    okx_business_ws_url: str = "wss://ws.okx.com:8443/ws/v5/business"
    okx_private_ws_url: str = "wss://ws.okx.com:8443/ws/v5/private"
    okx_api_key: str | None = None
    okx_api_secret: str | None = None
    okx_api_passphrase: str | None = None
    okx_simulated_trading: bool = False
    okx_timeout_seconds: float = 15.0
    okx_market_reconnect_delay_seconds: float = 4.0
    okx_market_reconnect_max_delay_seconds: float = 20.0
    okx_ws_ping_interval_seconds: float = 20.0
    okx_ws_ping_timeout_seconds: float = 20.0
    okx_ws_open_timeout_seconds: float = 20.0
    okx_private_ws_idle_ping_interval_seconds: float = 20.0
    okx_market_rest_fallback_enabled: bool = True
    okx_market_rest_fallback_poll_interval_seconds: float = 5.0
    okx_account_refresh_interval_seconds: float = 15.0
    okx_execution_sync_interval_seconds: float = 5.0
    execution_unknown_submit_review_after_seconds: float = 30.0
    execution_unknown_cancel_review_after_seconds: float = 300.0
    execution_command_flow_enabled: bool = False
    execution_command_poll_interval_seconds: float = 1.0
    execution_command_sent_retry_after_seconds: float = 30.0
    portfolio_ledger_truth_enabled: bool = False
    recovery_reconciliation_execution_ledger_enabled: bool = False
    operator_control_plane_execution_ledger_enabled: bool = False
    financial_convergence_mode_enabled: bool = False
    okx_fill_fetch_limit: int = 100
    okx_bills_fetch_limit: int = 100
    okx_instruments_refresh_interval_seconds: float = 300.0
    okx_account_config_refresh_interval_seconds: float = 300.0
    okx_account_position_risk_refresh_interval_seconds: float = 60.0
    okx_bills_refresh_interval_seconds: float = 60.0
    okx_funding_rate_refresh_interval_seconds: float = 60.0
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
    strategy_profile_emergency_safety_fast_track_enabled: bool = True
    strategy_profile_emergency_safety_confidence_min: float = 0.68
    strategy_profile_score_fee_penalty_weight: float = -25.0
    strategy_profile_score_churn_penalty_weight: float = -20.0
    strategy_profile_score_degraded_status_penalty: float = -15.0
    strategy_profile_score_low_health_conservative_bonus: float = 2.5
    strategy_profile_score_low_health_non_conservative_penalty: float = -2.0
    strategy_profile_score_divergence_execution_bonus: float = 1.5
    strategy_profile_score_divergence_other_penalty: float = -1.0
    strategy_family_active: StrategyFamily = "directional"
    strategy_family_auto_selection_enabled: bool = True
    strategy_family_protective_enabled: bool = False
    strategy_family_opportunistic_enabled: bool = False
    strategy_family_independent_enabled: bool = False
    strategy_family_protective_shadow_mode_enabled: bool = False
    strategy_family_opportunistic_shadow_mode_enabled: bool = False
    strategy_family_independent_shadow_mode_enabled: bool = False
    strategy_family_protective_live_execution_enabled: bool = False
    strategy_family_opportunistic_live_execution_enabled: bool = False
    strategy_family_independent_live_execution_enabled: bool = False
    strategy_sleeve_auto_execution_enabled: bool = True
    strategy_sleeve_auto_min_budget_multiplier: float = 0.35
    strategy_sleeve_auto_reconciliation_contraction_multiplier: float = 0.50
    strategy_sleeve_auto_soft_loss_usdt: float = 10.0
    strategy_sleeve_auto_hard_loss_usdt: float = 25.0
    strategy_sleeve_auto_volatility_cap_enabled: bool = True
    smart_arbitrage_enabled: bool = False
    smart_arbitrage_basis_entry_bps: float = 40.0
    smart_arbitrage_basis_exit_bps: float = 6.0
    smart_arbitrage_estimated_cost_bps: float = 34.0
    smart_arbitrage_quote_budget_per_trade: float = 200.0
    smart_arbitrage_max_pair_notional: float = 2_000.0
    smart_arbitrage_pair_definitions: tuple[dict[str, Any], ...] = Field(default=())
    smart_arbitrage_negative_basis_mode: SmartArbitrageNegativeBasisMode = "advisory_only"
    smart_arbitrage_cost_model_enabled: bool = True
    smart_arbitrage_funding_cost_enabled: bool = False
    smart_arbitrage_borrow_cost_enabled: bool = False
    smart_arbitrage_inventory_reservation_enabled: bool = False
    smart_arbitrage_margin_short_enabled: bool = False
    smart_arbitrage_margin_short_execution_ready: bool = False
    smart_arbitrage_margin_short_spot_margin_mode: SmartArbitrageSpotMarginMode = "cross"
    smart_arbitrage_margin_short_auto_repay_enabled: bool = False
    smart_arbitrage_max_concurrent_pairs: int = 1
    smart_arbitrage_pair_priority_mode: SmartArbitragePairPriorityMode = "net_edge"
    smart_arbitrage_min_inventory_backed_ratio: float = 1.0
    smart_arbitrage_fee_source_mode: SmartArbitrageFeeSourceMode = "account_schedule"
    smart_arbitrage_funding_source_mode: SmartArbitrageFundingSourceMode = "account_proxy"
    smart_arbitrage_borrow_source_mode: SmartArbitrageBorrowSourceMode = "apr_window_model"
    smart_arbitrage_expected_hold_hours: float = 8.0
    smart_arbitrage_funding_interval_hours: float = 8.0
    smart_arbitrage_expected_funding_events: int = 0
    smart_arbitrage_hedge_target_leverage: float = 3.0
    smart_arbitrage_estimated_execution_mismatch_bps: float = 0.0
    smart_arbitrage_estimated_transfer_cost_bps: float = 0.0
    smart_arbitrage_time_decay_bps_per_hour: float = 0.0
    smart_arbitrage_estimated_borrow_apr: float = 0.0
    smart_arbitrage_borrow_interest_free_ratio: float = 0.0
    smart_arbitrage_estimated_funding_bps: float = 0.0
    smart_arbitrage_estimated_borrow_bps: float = 0.0
    spot_grid_enabled: bool = False
    spot_grid_anchor_lookback_snapshots: int = 24
    spot_grid_band_bps: float = 150.0
    spot_grid_inventory_floor_fraction: float = 0.15
    spot_grid_inventory_ceiling_fraction: float = 1.0
    spot_grid_rebalance_min_fraction_of_max_qty: float = 0.08
    spot_grid_breakout_guard_enabled: bool = True
    dca_enabled: bool = False
    dca_interval_seconds: float = 86_400.0
    dca_quote_budget_per_cycle: float = 25.0
    dca_max_position_fraction_of_limit: float = 1.0
    dca_pullback_only_enabled: bool = False
    dca_pullback_entry_bps: float = 40.0
    trading_product_type: TradingProductType = "spot"
    margin_mode: MarginMode = "cash"
    derivatives_position_mode: DerivativesPositionMode = "net"
    derivatives_hedge_transition_mode: DerivativesHedgeTransitionMode = "close_then_open"
    derivatives_require_exchange_pos_mode_match: bool = True
    max_target_leverage: float = 1.0
    default_target_leverage: float = 1.0
    strategy_short_bias_enabled: bool = False
    strategy_dynamic_leverage_enabled: bool = False
    strategy_flat_signal_hold_enabled: bool = False
    strategy_flat_exit_microstructure_threshold: float = 0.12
    strategy_flat_exit_factor_threshold: float = 0.18
    strategy_flat_exit_ai_edge_threshold: float = 0.22
    strategy_position_alpha_decay_reduce_alpha: float = 0.12
    strategy_position_alpha_decay_reduce_confidence: float = 0.5
    strategy_position_alpha_decay_exit_alpha: float = 0.06
    strategy_position_high_volatility_reduce_fraction: float = 0.72
    strategy_position_range_reduce_fraction: float = 0.8
    strategy_position_uncertain_reduce_fraction: float = 0.65
    strategy_cost_guard_enabled: bool = True
    strategy_alpha_edge_bps_scale: float = 100.0
    strategy_signal_edge_scale_bps: float | None = Field(
        default=None,
        description=(
            "RDP 研究层 composite-score → signal_edge_bps 缩放系数。"
            "当设置为正数时，compute_signal_edge_bps() 会额外计算 "
            "score_based_edge = composite_score × scale，并取 max(component_edge, score_edge)。"
            "None 或 0 表示仅使用传统 alpha-based 路径。"
            "由 active_parameters 从 signal_edge_scale_bps 自动注入。"
        ),
    )
    strategy_expected_slippage_bps_fraction: float = 0.28
    strategy_edge_noise_buffer_bps: float = 4.0
    strategy_min_net_edge_bps: float = 4.0
    strategy_entry_allowed_regimes: tuple[str, ...] = Field(default=("trend", "breakout"))
    strategy_entry_min_signal_edge_bps: float = 12.0
    strategy_entry_alpha_min: float = 0.17
    strategy_entry_confidence_min: float = 0.64
    strategy_short_entry_allowed_regimes: tuple[str, ...] = Field(default=("trend", "breakout"))
    strategy_short_entry_min_signal_edge_bps: float = 11.0
    strategy_short_entry_alpha_min: float = 0.15
    strategy_short_entry_confidence_min: float = 0.55
    strategy_scale_in_min_signal_edge_bps: float = 16.0
    strategy_scale_in_alpha_min: float = 0.22
    strategy_scale_in_confidence_min: float = 0.70
    strategy_short_scale_in_min_signal_edge_bps: float = 14.0
    strategy_short_scale_in_alpha_min: float = 0.20
    strategy_short_scale_in_confidence_min: float = 0.64
    strategy_reversal_min_signal_edge_bps: float = 22.0
    strategy_reversal_alpha_min: float = 0.32
    strategy_reversal_confidence_min: float = 0.78
    strategy_short_reversal_min_signal_edge_bps: float = 14.0
    strategy_short_reversal_alpha_min: float = 0.18
    strategy_short_reversal_confidence_min: float = 0.55
    strategy_hedge_overlay_enabled: bool = False
    strategy_hedge_protective_enabled: bool = True
    strategy_hedge_overlay_mode: StrategyHedgeOverlayMode = "protective"
    strategy_hedge_open_threshold: float = 0.58
    strategy_hedge_close_threshold: float = 0.42
    strategy_hedge_max_ratio: float = 0.50
    strategy_hedge_min_hold_seconds: float = 300.0
    strategy_hedge_rebalance_cooldown_seconds: float = 120.0
    strategy_hedge_opportunistic_enabled: bool = False
    strategy_hedge_opportunistic_rollout_stage: StrategyHedgeOverlayRolloutStage = "dry_run"
    strategy_hedge_opportunistic_open_threshold: float = 0.62
    strategy_hedge_opportunistic_close_threshold: float = 0.46
    strategy_hedge_opportunistic_max_ratio: float = 0.35
    strategy_hedge_opportunistic_min_hold_seconds: float = 180.0
    strategy_hedge_opportunistic_rebalance_cooldown_seconds: float = 90.0
    strategy_hedge_opportunistic_max_fee_drag_ratio: float = 0.18
    strategy_hedge_opportunistic_max_churn_ratio: float = 0.22
    strategy_hedge_opportunistic_min_safe_net_edge_bps: float = 0.0
    strategy_hedge_opportunistic_expected_slippage_buffer_bps: float = 0.0
    strategy_hedge_opportunistic_expected_execution_buffer_bps: float = 0.0
    strategy_hedge_opportunistic_weak_edge_execution_mode: IndependentWeakEdgeExecutionMode = "block"
    strategy_hedge_opportunistic_max_acceptable_cost_bps: float = 0.0
    strategy_hedge_opportunistic_passive_first_enabled: bool = False
    strategy_hedge_independent_enabled: bool = False
    strategy_hedge_independent_rollout_stage: StrategyHedgeOverlayRolloutStage = "dry_run"
    strategy_hedge_independent_long_entry_threshold: float = 0.66
    strategy_hedge_independent_short_entry_threshold: float = 0.66
    strategy_hedge_independent_long_close_threshold: float = 0.66
    strategy_hedge_independent_short_close_threshold: float = 0.66
    strategy_hedge_independent_long_scale_in_threshold: float = 0.70
    strategy_hedge_independent_short_scale_in_threshold: float = 0.70
    strategy_hedge_independent_long_min_hold_seconds: float = 300.0
    strategy_hedge_independent_short_min_hold_seconds: float = 300.0
    strategy_hedge_independent_rebalance_cooldown_seconds: float = 120.0
    strategy_hedge_independent_trial_guard_enabled: bool = True
    strategy_hedge_independent_min_safe_net_edge_bps: float = 0.0
    strategy_hedge_independent_expected_slippage_buffer_bps: float = 0.0
    strategy_hedge_independent_expected_execution_buffer_bps: float = 0.0
    strategy_hedge_independent_weak_edge_execution_mode: IndependentWeakEdgeExecutionMode = "block"
    strategy_hedge_independent_max_acceptable_cost_bps: float = 0.0
    strategy_hedge_independent_passive_first_enabled: bool = False
    strategy_hedge_independent_min_confirm_ticks: int = 2
    strategy_hedge_independent_min_score_stability_bps: float = 2.0
    strategy_hedge_independent_min_score_drawdown_bps: float | None = None
    strategy_hedge_independent_min_liquidity_quality: float = 0.55
    strategy_hedge_independent_require_execution_health_ok: bool = True
    strategy_hedge_independent_max_thesis_age_seconds: int = 1_800
    strategy_hedge_independent_de_risk_net_edge_bps: float = 2.0
    strategy_hedge_independent_failed_thesis_net_edge_bps: float = -1.0
    strategy_hedge_independent_execution_health_de_risk_enabled: bool = True
    strategy_hedge_independent_liquidity_de_risk_enabled: bool = True
    strategy_hedge_independent_entry_execution_mode: IndependentExecutionPolicyMode = "adaptive"
    strategy_hedge_independent_scale_in_execution_mode: IndependentExecutionPolicyMode = "adaptive"
    strategy_hedge_independent_de_risk_execution_mode: IndependentExecutionPolicyMode = "adaptive"
    strategy_hedge_independent_close_failed_thesis_execution_mode: IndependentExecutionPolicyMode = "adaptive"
    strategy_hedge_independent_close_stale_execution_mode: IndependentExecutionPolicyMode = "adaptive"
    strategy_hedge_independent_limit_offset_bps_entry: float = 1.5
    strategy_hedge_independent_limit_offset_bps_scale_in: float = 1.0
    strategy_hedge_independent_limit_offset_bps_stale_close: float = 0.8
    strategy_hedge_independent_emit_book_level_metrics: bool = True
    strategy_hedge_independent_emit_expected_vs_realized_metrics: bool = True
    strategy_hedge_independent_emit_close_reason_metrics: bool = True
    strategy_hedge_independent_emit_execution_policy_metrics: bool = True
    strategy_hedge_independent_adaptive_rollout_enabled: bool = False
    strategy_hedge_independent_health_enforcement_enabled: bool = False
    strategy_hedge_independent_size_down_entry_enabled: bool = False
    strategy_hedge_independent_long_short_asymmetry_enabled: bool = False
    strategy_hedge_independent_short_asymmetry_penalty_multiplier: float = 0.85
    strategy_hedge_independent_entry_size_down_floor: float = 0.50
    strategy_min_hold_seconds: float = 720.0
    strategy_post_close_cooldown_seconds: float = 300.0
    strategy_health_lookback_trades: int = 12
    strategy_performance_guard_min_closed_trades: int = 4
    strategy_max_fee_drag_ratio: float = 0.40
    strategy_max_churn_ratio: float = 0.35
    strategy_low_edge_threshold_bps: float = 4.0
    strategy_low_edge_streak_limit: int = 3
    strategy_low_edge_cooldown_seconds: float = 1_800.0
    strategy_transient_close_retry_cooldown_seconds: float = 120.0
    strategy_volatility_target_scale_floor: float = 0.35
    strategy_volatility_target_scale_ceiling: float = 1.10
    strategy_risk_budget_multiplier_floor: float = 0.35
    strategy_execution_aggressiveness_multiplier_floor: float = 0.30
    strategy_risk_snapshot_missing_budget_multiplier: float = 0.7
    strategy_risk_snapshot_missing_execution_aggressiveness_multiplier: float = 0.55
    trial_guard_enabled: bool = False
    trial_guard_poll_interval_seconds: float = 15.0
    trial_guard_lookback_fills: int = 30
    trial_guard_min_closed_fills: int = 5
    trial_guard_max_daily_loss_usdt: float = 25.0
    trial_guard_max_consecutive_losses: int = 5
    trial_guard_max_fee_to_notional_ratio: float = 0.0012
    trial_guard_max_high_slippage_ratio: float = 0.35
    trial_guard_max_slow_submit_to_fill_ratio: float = 0.35
    max_gross_notional_per_symbol: float = 2_500.0
    max_pending_notional_per_symbol: float = 1_250.0
    max_total_open_notional: float = 5_000.0
    risk_max_long_notional: float = 0.0
    risk_max_short_notional: float = 0.0
    risk_max_gross_notional: float = 0.0
    risk_max_net_notional: float = 0.0
    max_daily_realized_loss_usdt: float = 100.0
    derivatives_only_reduce_trigger_margin_fraction: float = 0.7
    derivatives_runtime_guard_enabled: bool = True
    derivatives_risk_snapshot_grace_seconds: float = 45.0
    derivatives_risk_snapshot_auto_halt_after_seconds: float = 240.0
    derivatives_auto_halt_margin_usage_fraction: float = 0.85
    derivatives_auto_halt_liquidation_gap_fraction: float = 0.08
    max_margin_usage_fraction: float = 0.85
    liquidation_buffer_fraction: float = 0.15
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    operator_auth_enabled: bool = False
    operator_read_api_key: str | None = None
    operator_write_api_key: str | None = None
    operator_login_max_failed_attempts: int = 5
    operator_login_lockout_seconds: int = 900
    operator_exchange_refresh_max_attempts: int = 3
    operator_exchange_refresh_retry_delay_seconds: float = 1.0
    operator_unsafe_write_without_auth: bool = False
    operator_session_secret: str | None = None
    operator_session_cookie_name: str = "aats_operator_session"
    operator_session_max_age_seconds: int = 43_200
    operator_session_cookie_secure: bool = True
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_rotate_max_bytes: int = 5_242_880
    log_backup_count: int = 7
    exchange_name: str = "PAPER"
    allowed_symbols: tuple[str, ...] = Field(default=("BTC-USDT",))

    @field_validator("strategy_entry_allowed_regimes", "strategy_short_entry_allowed_regimes", mode="before")
    @classmethod
    def normalize_allowed_regimes(cls, value: Any) -> Any:
        if value is None:
            return tuple()
        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            return value
        normalized = []
        for item in items:
            text = "" if item is None else str(item).strip()
            if text:
                normalized.append(text)
        return tuple(normalized)

    @model_validator(mode="before")
    @classmethod
    def reject_removed_sleeve_auto_parallel_key(cls, value: Any) -> Any:
        if isinstance(value, dict) and DEPRECATED_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY in value:
            raise ValueError(
                "strategy_sleeve_auto_parallel_enabled_has_been_removed_use_strategy_sleeve_auto_execution_enabled"
            )
        return value

    @model_validator(mode="after")
    def validate_supported_runtime_overrides(self) -> "AATSSettings":
        # The current market/feature pipeline is still built around fixed
        # 15m + 1h snapshots. Exposing broader timeframe configurability in
        # env/YAML without enforcing this creates misleading configs.
        explicit_new_sleeve_auto_key = PRIMARY_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY in self.model_fields_set
        self.strategy_sleeve_auto_execution_enabled = bool(self.strategy_sleeve_auto_execution_enabled)
        if not explicit_new_sleeve_auto_key:
            self.__pydantic_fields_set__.discard(PRIMARY_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY)
        if "strategy_hedge_independent_long_close_threshold" not in self.model_fields_set:
            self.strategy_hedge_independent_long_close_threshold = float(
                self.strategy_hedge_independent_long_entry_threshold
            )
        if "strategy_hedge_independent_short_close_threshold" not in self.model_fields_set:
            self.strategy_hedge_independent_short_close_threshold = float(
                self.strategy_hedge_independent_short_entry_threshold
            )
        if self.primary_timeframe != "15m":
            raise ValueError("primary_timeframe_currently_must_be_15m")
        if self.secondary_timeframe != "1h":
            raise ValueError("secondary_timeframe_currently_must_be_1h")
        if self.trading_product_type != "derivatives" and self.derivatives_position_mode != "net":
            raise ValueError("non_derivatives_runtime_disallows_derivatives_hedge_position_mode")
        if self.derivatives_position_mode == "hedge":
            if self.trading_product_type != "derivatives":
                raise ValueError("derivatives_hedge_position_mode_requires_derivatives_product_type")
            if self.margin_mode == "cash":
                raise ValueError("derivatives_hedge_position_mode_requires_margin_runtime")
        if not 0.0 <= float(self.strategy_hedge_close_threshold) <= 1.0:
            raise ValueError("strategy_hedge_close_threshold_must_be_between_zero_and_one")
        if not 0.0 <= float(self.strategy_hedge_open_threshold) <= 1.0:
            raise ValueError("strategy_hedge_open_threshold_must_be_between_zero_and_one")
        if float(self.strategy_hedge_close_threshold) - float(self.strategy_hedge_open_threshold) > 1e-9:
            raise ValueError("strategy_hedge_close_threshold_must_not_exceed_open_threshold")
        if not 0.0 <= float(self.strategy_hedge_max_ratio) <= 1.0:
            raise ValueError("strategy_hedge_max_ratio_must_be_between_zero_and_one")
        if not 0.0 <= float(self.strategy_hedge_opportunistic_close_threshold) <= 1.0:
            raise ValueError("strategy_hedge_opportunistic_close_threshold_must_be_between_zero_and_one")
        if not 0.0 <= float(self.strategy_hedge_opportunistic_open_threshold) <= 1.0:
            raise ValueError("strategy_hedge_opportunistic_open_threshold_must_be_between_zero_and_one")
        if (
            float(self.strategy_hedge_opportunistic_close_threshold)
            - float(self.strategy_hedge_opportunistic_open_threshold)
            > 1e-9
        ):
            raise ValueError("strategy_hedge_opportunistic_close_threshold_must_not_exceed_open_threshold")
        if not 0.0 <= float(self.strategy_hedge_opportunistic_max_ratio) <= 1.0:
            raise ValueError("strategy_hedge_opportunistic_max_ratio_must_be_between_zero_and_one")
        if not 0.0 <= float(self.strategy_hedge_opportunistic_max_fee_drag_ratio) <= 1.0:
            raise ValueError("strategy_hedge_opportunistic_max_fee_drag_ratio_must_be_between_zero_and_one")
        if not 0.0 <= float(self.strategy_hedge_opportunistic_max_churn_ratio) <= 1.0:
            raise ValueError("strategy_hedge_opportunistic_max_churn_ratio_must_be_between_zero_and_one")
        if float(self.strategy_hedge_opportunistic_min_safe_net_edge_bps) < 0.0:
            raise ValueError("strategy_hedge_opportunistic_min_safe_net_edge_bps_must_be_non_negative")
        if float(self.strategy_hedge_opportunistic_expected_slippage_buffer_bps) < 0.0:
            raise ValueError("strategy_hedge_opportunistic_expected_slippage_buffer_bps_must_be_non_negative")
        if float(self.strategy_hedge_opportunistic_expected_execution_buffer_bps) < 0.0:
            raise ValueError("strategy_hedge_opportunistic_expected_execution_buffer_bps_must_be_non_negative")
        if float(self.strategy_hedge_opportunistic_max_acceptable_cost_bps) < 0.0:
            raise ValueError("strategy_hedge_opportunistic_max_acceptable_cost_bps_must_be_non_negative")
        if not 0.0 <= float(self.strategy_hedge_independent_long_entry_threshold) <= 1.0:
            raise ValueError("strategy_hedge_independent_long_entry_threshold_must_be_between_zero_and_one")
        if not 0.0 <= float(self.strategy_hedge_independent_short_entry_threshold) <= 1.0:
            raise ValueError("strategy_hedge_independent_short_entry_threshold_must_be_between_zero_and_one")
        if not 0.0 <= float(self.strategy_hedge_independent_long_close_threshold) <= 1.0:
            raise ValueError("strategy_hedge_independent_long_close_threshold_must_be_between_zero_and_one")
        if not 0.0 <= float(self.strategy_hedge_independent_short_close_threshold) <= 1.0:
            raise ValueError("strategy_hedge_independent_short_close_threshold_must_be_between_zero_and_one")
        if not 0.0 <= float(self.strategy_hedge_independent_long_scale_in_threshold) <= 1.0:
            raise ValueError("strategy_hedge_independent_long_scale_in_threshold_must_be_between_zero_and_one")
        if not 0.0 <= float(self.strategy_hedge_independent_short_scale_in_threshold) <= 1.0:
            raise ValueError("strategy_hedge_independent_short_scale_in_threshold_must_be_between_zero_and_one")
        if (
            float(self.strategy_hedge_independent_long_close_threshold)
            - float(self.strategy_hedge_independent_long_entry_threshold)
            > 1e-9
        ):
            raise ValueError("strategy_hedge_independent_long_close_threshold_must_not_exceed_entry_threshold")
        if (
            float(self.strategy_hedge_independent_short_close_threshold)
            - float(self.strategy_hedge_independent_short_entry_threshold)
            > 1e-9
        ):
            raise ValueError("strategy_hedge_independent_short_close_threshold_must_not_exceed_entry_threshold")
        if (
            float(self.strategy_hedge_independent_long_entry_threshold)
            - float(self.strategy_hedge_independent_long_scale_in_threshold)
            > 1e-9
        ):
            raise ValueError("strategy_hedge_independent_long_entry_threshold_must_not_exceed_scale_in_threshold")
        if (
            float(self.strategy_hedge_independent_short_entry_threshold)
            - float(self.strategy_hedge_independent_short_scale_in_threshold)
            > 1e-9
        ):
            raise ValueError("strategy_hedge_independent_short_entry_threshold_must_not_exceed_scale_in_threshold")
        if float(self.strategy_hedge_independent_min_safe_net_edge_bps) < 0.0:
            raise ValueError("strategy_hedge_independent_min_safe_net_edge_bps_must_be_non_negative")
        if float(self.strategy_hedge_independent_expected_slippage_buffer_bps) < 0.0:
            raise ValueError("strategy_hedge_independent_expected_slippage_buffer_bps_must_be_non_negative")
        if float(self.strategy_hedge_independent_expected_execution_buffer_bps) < 0.0:
            raise ValueError("strategy_hedge_independent_expected_execution_buffer_bps_must_be_non_negative")
        if float(self.strategy_hedge_independent_max_acceptable_cost_bps) < 0.0:
            raise ValueError("strategy_hedge_independent_max_acceptable_cost_bps_must_be_non_negative")
        if int(self.strategy_hedge_independent_min_confirm_ticks) < 1:
            raise ValueError("strategy_hedge_independent_min_confirm_ticks_must_be_positive")
        if float(self.strategy_hedge_independent_min_score_stability_bps) < 0.0:
            raise ValueError("strategy_hedge_independent_min_score_stability_bps_must_be_non_negative")
        if (
            self.strategy_hedge_independent_min_score_drawdown_bps is not None
            and float(self.strategy_hedge_independent_min_score_drawdown_bps) < 0.0
        ):
            raise ValueError("strategy_hedge_independent_min_score_drawdown_bps_must_be_non_negative")
        if not 0.0 <= float(self.strategy_hedge_independent_min_liquidity_quality) <= 1.0:
            raise ValueError("strategy_hedge_independent_min_liquidity_quality_must_be_between_zero_and_one")
        if int(self.strategy_hedge_independent_max_thesis_age_seconds) < 1:
            raise ValueError("strategy_hedge_independent_max_thesis_age_seconds_must_be_positive")
        if float(self.strategy_hedge_independent_de_risk_net_edge_bps) < 0.0:
            raise ValueError("strategy_hedge_independent_de_risk_net_edge_bps_must_be_non_negative")
        if (
            float(self.strategy_hedge_independent_failed_thesis_net_edge_bps)
            - float(self.strategy_hedge_independent_de_risk_net_edge_bps)
            > 1e-9
        ):
            raise ValueError(
                "strategy_hedge_independent_failed_thesis_net_edge_bps_must_not_exceed_de_risk_threshold"
            )
        if float(self.strategy_hedge_independent_limit_offset_bps_entry) < 0.0:
            raise ValueError("strategy_hedge_independent_limit_offset_bps_entry_must_be_non_negative")
        if float(self.strategy_hedge_independent_limit_offset_bps_scale_in) < 0.0:
            raise ValueError("strategy_hedge_independent_limit_offset_bps_scale_in_must_be_non_negative")
        if float(self.strategy_hedge_independent_limit_offset_bps_stale_close) < 0.0:
            raise ValueError("strategy_hedge_independent_limit_offset_bps_stale_close_must_be_non_negative")
        if self.trading_product_type == "spot" and self.margin_mode == "cash":
            if float(self.max_target_leverage) != 1.0 or float(self.default_target_leverage) != 1.0:
                raise ValueError("spot_cash_runtime_requires_unit_leverage")
        return self

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
        return all(
            value and not is_placeholder_config_value(value)
            for value in (self.okx_api_key, self.okx_api_secret, self.okx_api_passphrase)
        )

    @property
    def database_url_configured(self) -> bool:
        return bool(self.database_url and not is_placeholder_config_value(self.database_url))

    @property
    def ai_provider_configured(self) -> bool:
        if self.ai_provider == "disabled":
            return False
        if self.ai_provider == "openai":
            return bool(self.openai_api_key and not is_placeholder_config_value(self.openai_api_key))
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
    def effective_strategy_sleeve_auto_execution_enabled(self) -> bool:
        return bool(self.strategy_sleeve_auto_execution_enabled)

    @property
    def strategy_sleeve_auto_execution_uses_deprecated_key(self) -> bool:
        return False

    @property
    def strategy_sleeve_auto_execution_config_source(self) -> str:
        return PRIMARY_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY

    @property
    def strategy_sleeve_auto_execution_deprecated_key(self) -> str:
        return DEPRECATED_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY

    @property
    def strategy_sleeve_auto_execution_primary_key(self) -> str:
        return PRIMARY_STRATEGY_SLEEVE_AUTO_EXECUTION_KEY

    @property
    def strategy_sleeve_auto_execution_deprecated_value(self) -> bool | None:
        return None

    @property
    def operator_session_configured(self) -> bool:
        return bool(self.operator_session_secret and not is_placeholder_config_value(self.operator_session_secret))

    @staticmethod
    def _derived_spot_symbol(symbol: str | None) -> str | None:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return None
        if normalized.endswith("-SWAP"):
            return normalized[:-5]
        tail = normalized.rsplit("-", 1)[-1]
        if tail.isdigit():
            return normalized[: -(len(tail) + 1)]
        return normalized

    @staticmethod
    def _derived_derivatives_symbol(symbol: str | None) -> str | None:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return None
        if normalized.endswith("-SWAP"):
            return normalized
        tail = normalized.rsplit("-", 1)[-1]
        if tail.isdigit():
            return normalized
        return f"{normalized}-SWAP"

    def decision_cycle_symbols(self) -> tuple[str, ...]:
        symbols = list(dict.fromkeys(str(item).upper() for item in self.allowed_symbols if item))
        if self.default_symbol:
            default_symbol = str(self.default_symbol).upper()
            if default_symbol not in symbols:
                symbols.append(default_symbol)
        return tuple(symbols)

    def symbol_allowed_for_decision_cycle(self, symbol: str | None) -> bool:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return False
        return normalized in self.decision_cycle_symbols()

    def expanded_allowed_symbols(self) -> tuple[str, ...]:
        symbols = list(self.decision_cycle_symbols())
        if not self.smart_arbitrage_enabled:
            return tuple(symbols)
        for symbol in (
            self._derived_spot_symbol(self.default_symbol),
            self._derived_derivatives_symbol(self.default_symbol),
        ):
            normalized = str(symbol or "").strip().upper()
            if normalized and normalized not in symbols:
                symbols.append(normalized)
        for pair in self.smart_arbitrage_pair_definitions:
            if not isinstance(pair, dict):
                continue
            for key in ("spot_symbol", "hedge_symbol", "derivatives_symbol"):
                normalized = str(pair.get(key) or "").strip().upper()
                if normalized and normalized not in symbols:
                    symbols.append(normalized)
        return tuple(symbols)
