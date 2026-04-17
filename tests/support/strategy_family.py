from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext


def make_derivatives_hedge_settings(**overrides: object) -> AATSSettings:
    return AATSSettings.model_validate(
        {
            "default_order_qty": 0.01,
            "trading_product_type": "derivatives",
            "margin_mode": "cross",
            "derivatives_position_mode": "hedge",
            "strategy_short_bias_enabled": True,
            "strategy_hedge_overlay_enabled": True,
            "strategy_cost_guard_enabled": False,
            "strategy_entry_min_signal_edge_bps": 0.0,
            "strategy_entry_alpha_min": 0.0,
            "strategy_entry_confidence_min": 0.0,
            "strategy_reversal_min_signal_edge_bps": 0.0,
            "strategy_reversal_alpha_min": 0.0,
            "strategy_reversal_confidence_min": 0.0,
            "strategy_short_reversal_min_signal_edge_bps": 0.0,
            "strategy_short_reversal_alpha_min": 0.0,
            "strategy_short_reversal_confidence_min": 0.0,
            "strategy_edge_noise_buffer_bps": 0.0,
            **overrides,
        }
    )


def make_context(
    *,
    as_of_ts: datetime | None = None,
    current_position_qty: float = 0.0,
    current_long_position_qty: float | None = None,
    current_short_position_qty: float | None = None,
    product_type: str = "spot",
    current_exposure_side: str = "flat",
    current_position_opened_seconds_ago: int | None = None,
    last_position_closed_seconds_ago: int | None = None,
    current_long_leg_opened_seconds_ago: int | None = None,
    current_short_leg_opened_seconds_ago: int | None = None,
    last_long_leg_closed_seconds_ago: int | None = None,
    last_short_leg_closed_seconds_ago: int | None = None,
    latest_long_leg_fill_seconds_ago: int | None = None,
    latest_short_leg_fill_seconds_ago: int | None = None,
    recent_low_edge_trade_streak: int = 0,
    recent_low_edge_trade_seconds_ago: int | None = None,
    recent_closed_trade_count: int = 0,
    recent_fee_drag_ratio: float = 0.0,
    recent_churn_ratio: float = 0.0,
    recent_guard_eligible_closed_trade_count: int | None = None,
    recent_guard_eligible_win_rate: float | None = None,
    recent_guard_eligible_fee_drag_ratio: float | None = None,
    recent_guard_eligible_churn_ratio: float | None = None,
    recent_guard_eligible_low_edge_trade_streak: int | None = None,
    recent_guard_eligible_low_edge_trade_seconds_ago: int | None = None,
    recent_guard_eligible_net_realized_pnl: Decimal = Decimal("0"),
    leg_strategy_health: dict[str, dict[str, object]] | None = None,
    market_last_price: float = 0.0,
    available_trading_equity: float = 0.0,
) -> DecisionContext:
    now = as_of_ts or utc_now()
    derived_long_qty = (
        current_position_qty
        if current_long_position_qty is None and current_position_qty > 0
        else (0.0 if current_long_position_qty is None else current_long_position_qty)
    )
    derived_short_qty = (
        abs(current_position_qty)
        if current_short_position_qty is None and current_position_qty < 0
        else (0.0 if current_short_position_qty is None else current_short_position_qty)
    )
    health_payload = leg_strategy_health or {
            "long": {
                "recent_closed_trade_count": 0,
                "recent_win_rate": 0.0,
                "recent_fee_drag_ratio": 0.0,
                "recent_churn_ratio": 0.0,
                "recent_low_edge_trade_streak": 0,
                "recent_low_edge_trade_at": None,
                "recent_guard_eligible_closed_trade_count": 0,
                "recent_guard_eligible_win_rate": 0.0,
                "recent_guard_eligible_fee_drag_ratio": 0.0,
                "recent_guard_eligible_churn_ratio": 0.0,
                "recent_guard_eligible_low_edge_trade_streak": 0,
                "recent_guard_eligible_low_edge_trade_at": None,
                "recent_guard_eligible_net_realized_pnl": Decimal("0"),
                "recent_net_realized_pnl": Decimal("0"),
            },
            "short": {
                "recent_closed_trade_count": 0,
                "recent_win_rate": 0.0,
                "recent_fee_drag_ratio": 0.0,
                "recent_churn_ratio": 0.0,
                "recent_low_edge_trade_streak": 0,
                "recent_low_edge_trade_at": None,
                "recent_guard_eligible_closed_trade_count": 0,
                "recent_guard_eligible_win_rate": 0.0,
                "recent_guard_eligible_fee_drag_ratio": 0.0,
                "recent_guard_eligible_churn_ratio": 0.0,
                "recent_guard_eligible_low_edge_trade_streak": 0,
                "recent_guard_eligible_low_edge_trade_at": None,
                "recent_guard_eligible_net_realized_pnl": Decimal("0"),
                "recent_net_realized_pnl": Decimal("0"),
            },
        }
    return DecisionContext(
        decision_id="decision_target_test",
        symbol="BTC-USDT",
        timeframe="15m",
        as_of_ts=now,
        market_snapshot_ref="evt_market",
        feature_snapshot_ref="evt_feature",
        portfolio_snapshot_ref="evt_portfolio",
        health_snapshot_ref="evt_health",
        mode="paper_live",
        current_position_qty=Decimal(str(current_position_qty)),
        current_net_position_qty=Decimal(str(current_position_qty)),
        current_long_position_qty=Decimal(str(derived_long_qty)),
        current_short_position_qty=Decimal(str(derived_short_qty)),
        product_type=product_type,  # type: ignore[arg-type]
        current_exposure_side=current_exposure_side,  # type: ignore[arg-type]
        current_target_leverage=1.0,
        current_position_opened_at=(
            now - timedelta(seconds=current_position_opened_seconds_ago)
            if current_position_opened_seconds_ago is not None
            else None
        ),
        last_position_closed_at=(
            now - timedelta(seconds=last_position_closed_seconds_ago)
            if last_position_closed_seconds_ago is not None
            else None
        ),
        current_long_leg_opened_at=(
            now - timedelta(seconds=current_long_leg_opened_seconds_ago)
            if current_long_leg_opened_seconds_ago is not None
            else None
        ),
        current_short_leg_opened_at=(
            now - timedelta(seconds=current_short_leg_opened_seconds_ago)
            if current_short_leg_opened_seconds_ago is not None
            else None
        ),
        last_long_leg_closed_at=(
            now - timedelta(seconds=last_long_leg_closed_seconds_ago)
            if last_long_leg_closed_seconds_ago is not None
            else None
        ),
        last_short_leg_closed_at=(
            now - timedelta(seconds=last_short_leg_closed_seconds_ago)
            if last_short_leg_closed_seconds_ago is not None
            else None
        ),
        latest_long_leg_fill_timestamp=(
            now - timedelta(seconds=latest_long_leg_fill_seconds_ago)
            if latest_long_leg_fill_seconds_ago is not None
            else None
        ),
        latest_short_leg_fill_timestamp=(
            now - timedelta(seconds=latest_short_leg_fill_seconds_ago)
            if latest_short_leg_fill_seconds_ago is not None
            else None
        ),
        recent_low_edge_trade_streak=recent_low_edge_trade_streak,
        recent_low_edge_trade_at=(
            now - timedelta(seconds=recent_low_edge_trade_seconds_ago)
            if recent_low_edge_trade_seconds_ago is not None
            else None
        ),
        recent_closed_trade_count=recent_closed_trade_count,
        recent_fee_drag_ratio=recent_fee_drag_ratio,
        recent_churn_ratio=recent_churn_ratio,
        recent_guard_eligible_closed_trade_count=(
            recent_closed_trade_count
            if recent_guard_eligible_closed_trade_count is None
            else recent_guard_eligible_closed_trade_count
        ),
        recent_guard_eligible_win_rate=(
            0.0 if recent_guard_eligible_win_rate is None else recent_guard_eligible_win_rate
        ),
        recent_guard_eligible_fee_drag_ratio=(
            recent_fee_drag_ratio
            if recent_guard_eligible_fee_drag_ratio is None
            else recent_guard_eligible_fee_drag_ratio
        ),
        recent_guard_eligible_churn_ratio=(
            recent_churn_ratio
            if recent_guard_eligible_churn_ratio is None
            else recent_guard_eligible_churn_ratio
        ),
        recent_guard_eligible_low_edge_trade_streak=(
            recent_low_edge_trade_streak
            if recent_guard_eligible_low_edge_trade_streak is None
            else recent_guard_eligible_low_edge_trade_streak
        ),
        recent_guard_eligible_low_edge_trade_at=(
            now - timedelta(seconds=recent_guard_eligible_low_edge_trade_seconds_ago)
            if recent_guard_eligible_low_edge_trade_seconds_ago is not None
            else (
                now - timedelta(seconds=recent_low_edge_trade_seconds_ago)
                if recent_low_edge_trade_seconds_ago is not None
                else None
            )
        ),
        recent_guard_eligible_net_realized_pnl=recent_guard_eligible_net_realized_pnl,
        leg_strategy_health=health_payload,
        strategy_guardrail_flags=[],
        strategy_cooldowns={},
        market_last_price=Decimal(str(market_last_price)),
        available_trading_equity=Decimal(str(available_trading_equity)),
    )


def make_baseline(
    *,
    volatility_target_scale: float,
    suggested_position_scale: float,
    direction_bias: str = "long",
    confidence: float = 0.8,
    volatility_state: str = "medium",
    factor_scores: dict[str, float] | None = None,
) -> BaselineAssessment:
    return BaselineAssessment(
        decision_id="decision_target_test",
        symbol="BTC-USDT",
        regime="trend",
        direction_bias=direction_bias,  # type: ignore[arg-type]
        trend_strength=0.7,
        volatility_state=volatility_state,
        confidence=confidence,
        composite_alpha_score=0.45,
        suggested_position_scale=suggested_position_scale,
        volatility_target_scale=volatility_target_scale,
        factor_scores=factor_scores or {"momentum_alpha": 0.4},
        holding_horizon="15m",
        invalidation_conditions=[],
        reason_codes=["test"],
        engine_version="test",
    )


def make_ai_assessment(
    *,
    direction: float = 0.1,
    confidence: float = 0.7,
    fallback_used: bool = True,
    override: bool = False,
    actionable: bool = False,
) -> AIMarketAssessment:
    return AIMarketAssessment(
        decision_id="decision_target_test",
        symbol="BTC-USDT",
        regime="trend",
        directional_edge=direction,
        expected_volatility=0.02,
        confidence=confidence,
        uncertainty=0.2,
        expected_holding_horizon="15m",
        invalidation_conditions=[],
        risk_tags=[],
        rationale_summary="test",
        operating_mode="baseline_only",
        provider_name="baseline_fallback",
        output_valid=True,
        fallback_used=fallback_used,
        fallback_reason="baseline_only_mode",
        degraded=False,
        calibrated_confidence=confidence,
        baseline_override_recommended=override,
        override_reason_codes=["ai_override"] if override else [],
        economically_actionable=actionable,
        estimated_edge_bps=45.0 if actionable else 4.0,
        estimated_cost_bps=12.0,
        estimated_net_edge_bps=33.0 if actionable else -8.0,
        source_mode="provider" if not fallback_used else "fallback",
        execution_condition="normal",
        model_name="none",
        model_version="1",
        prompt_version="1",
    )
