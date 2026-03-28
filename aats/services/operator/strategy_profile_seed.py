from __future__ import annotations

from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.strategy_profiles import (
    StrategyProfileGuardrails,
    StrategyProfilePayload,
    StrategyProfileRevision,
    normalize_strategy_profile_payload_for_product_type,
    strategy_profile_payload_from_settings,
)
from aats.storage.base import StrategyProfileRepository


def _copy_payload(payload: StrategyProfilePayload, **updates: Any) -> StrategyProfilePayload:
    raw = payload.model_dump(mode="python")
    raw.update(updates)
    return StrategyProfilePayload.model_validate(raw)


def _clamp_float(value: float, *, lower: float, upper: float) -> float:
    return min(max(float(value), lower), upper)


def _clamp_int(value: int, *, lower: int, upper: int) -> int:
    return min(max(int(value), lower), upper)


def _balanced_profile_payload(payload: StrategyProfilePayload, *, product_type: str) -> StrategyProfilePayload:
    derivatives_runtime = product_type == "derivatives"
    return _copy_payload(
        payload,
        strategy_flat_signal_hold_enabled=False,
        strategy_expected_slippage_bps_fraction=_clamp_float(
            payload.strategy_expected_slippage_bps_fraction,
            lower=0.27,
            upper=0.30,
        ),
        strategy_min_net_edge_bps=_clamp_float(payload.strategy_min_net_edge_bps, lower=4.5, upper=6.0),
        strategy_entry_min_signal_edge_bps=_clamp_float(
            payload.strategy_entry_min_signal_edge_bps,
            lower=13.0,
            upper=16.0,
        ),
        strategy_entry_alpha_min=_clamp_float(payload.strategy_entry_alpha_min, lower=0.17, upper=0.19),
        strategy_entry_confidence_min=_clamp_float(payload.strategy_entry_confidence_min, lower=0.63, upper=0.67),
        strategy_short_entry_min_signal_edge_bps=_clamp_float(
            payload.strategy_short_entry_min_signal_edge_bps,
            lower=11.0,
            upper=13.0,
        ),
        strategy_short_entry_alpha_min=_clamp_float(payload.strategy_short_entry_alpha_min, lower=0.15, upper=0.18),
        strategy_short_entry_confidence_min=_clamp_float(
            payload.strategy_short_entry_confidence_min,
            lower=0.58,
            upper=0.62,
        ),
        strategy_scale_in_min_signal_edge_bps=_clamp_float(
            payload.strategy_scale_in_min_signal_edge_bps,
            lower=16.0 if derivatives_runtime else 17.0,
            upper=20.0,
        ),
        strategy_scale_in_alpha_min=_clamp_float(
            payload.strategy_scale_in_alpha_min,
            lower=0.22 if derivatives_runtime else 0.23,
            upper=0.26,
        ),
        strategy_scale_in_confidence_min=_clamp_float(
            payload.strategy_scale_in_confidence_min,
            lower=0.68 if derivatives_runtime else 0.70,
            upper=0.74,
        ),
        strategy_short_scale_in_min_signal_edge_bps=_clamp_float(
            payload.strategy_short_scale_in_min_signal_edge_bps,
            lower=15.0,
            upper=17.0,
        ),
        strategy_short_scale_in_alpha_min=_clamp_float(
            payload.strategy_short_scale_in_alpha_min,
            lower=0.20,
            upper=0.23,
        ),
        strategy_short_scale_in_confidence_min=_clamp_float(
            payload.strategy_short_scale_in_confidence_min,
            lower=0.64,
            upper=0.69,
        ),
        strategy_reversal_min_signal_edge_bps=_clamp_float(
            payload.strategy_reversal_min_signal_edge_bps,
            lower=20.0 if derivatives_runtime else 22.0,
            upper=26.0,
        ),
        strategy_reversal_alpha_min=_clamp_float(
            payload.strategy_reversal_alpha_min,
            lower=0.28 if derivatives_runtime else 0.32,
            upper=0.36,
        ),
        strategy_reversal_confidence_min=_clamp_float(
            payload.strategy_reversal_confidence_min,
            lower=0.72 if derivatives_runtime else 0.78,
            upper=0.82,
        ),
        strategy_short_reversal_min_signal_edge_bps=_clamp_float(
            payload.strategy_short_reversal_min_signal_edge_bps,
            lower=18.0,
            upper=22.0,
        ),
        strategy_short_reversal_alpha_min=_clamp_float(
            payload.strategy_short_reversal_alpha_min,
            lower=0.20,
            upper=0.26,
        ),
        strategy_short_reversal_confidence_min=_clamp_float(
            payload.strategy_short_reversal_confidence_min,
            lower=0.60,
            upper=0.68,
        ),
        strategy_min_hold_seconds=_clamp_float(payload.strategy_min_hold_seconds, lower=600.0, upper=900.0),
        strategy_post_close_cooldown_seconds=_clamp_float(
            payload.strategy_post_close_cooldown_seconds,
            lower=240.0,
            upper=360.0,
        ),
        strategy_max_fee_drag_ratio=_clamp_float(
            payload.strategy_max_fee_drag_ratio,
            lower=0.36,
            upper=0.48 if derivatives_runtime else 0.42,
        ),
        strategy_max_churn_ratio=_clamp_float(
            payload.strategy_max_churn_ratio,
            lower=0.30,
            upper=0.42 if derivatives_runtime else 0.38,
        ),
        strategy_low_edge_threshold_bps=_clamp_float(payload.strategy_low_edge_threshold_bps, lower=4.0, upper=5.0),
        strategy_low_edge_streak_limit=_clamp_int(payload.strategy_low_edge_streak_limit, lower=3, upper=4),
        strategy_low_edge_cooldown_seconds=_clamp_float(
            payload.strategy_low_edge_cooldown_seconds,
            lower=900.0 if derivatives_runtime else 1_500.0,
            upper=2_400.0,
        ),
    )


def _sync_system_seed_revision(
    *,
    existing: StrategyProfileRevision,
    desired: StrategyProfileRevision,
) -> StrategyProfileRevision:
    return desired.model_copy(
        update={
            "revision_id": existing.revision_id,
            "version": existing.version,
            "status": existing.status,
            "created_by": existing.created_by,
            "created_reason": existing.created_reason,
            "source_recommendation_id": existing.source_recommendation_id,
            "updated_at": utc_now(),
        }
    )


def _seed_revisions(*, settings: AATSSettings, payload: StrategyProfilePayload) -> list[StrategyProfileRevision]:
    balanced_payload = _balanced_profile_payload(payload, product_type=settings.trading_product_type)
    common = {
        "product_type": settings.trading_product_type,
        "margin_mode": settings.margin_mode,
        "allowed_symbols": settings.allowed_symbols,
        "guardrails": StrategyProfileGuardrails(),
        "created_by": "system_seed",
        "created_reason": "initial_seed",
    }
    revisions = [
        StrategyProfileRevision(
            profile_id="trend_aggressive",
            profile_label="Trend Aggressive",
            risk_level="aggressive",
            market_intent="trend",
            payload=_copy_payload(
                balanced_payload,
                decision_min_interval_seconds_15m=min(balanced_payload.decision_min_interval_seconds_15m, 45.0),
                max_decisions_per_minute=max(balanced_payload.max_decisions_per_minute, 6),
                decision_min_price_move_bps=min(balanced_payload.decision_min_price_move_bps, 3.5),
                decision_min_momentum_delta=min(balanced_payload.decision_min_momentum_delta, 0.00028),
                strategy_expected_slippage_bps_fraction=min(
                    balanced_payload.strategy_expected_slippage_bps_fraction,
                    0.25,
                ),
                strategy_min_net_edge_bps=min(balanced_payload.strategy_min_net_edge_bps, 4.0),
                strategy_entry_min_signal_edge_bps=min(
                    balanced_payload.strategy_entry_min_signal_edge_bps,
                    12.0,
                ),
                strategy_entry_alpha_min=min(balanced_payload.strategy_entry_alpha_min, 0.15),
                strategy_entry_confidence_min=min(balanced_payload.strategy_entry_confidence_min, 0.60),
                strategy_short_entry_min_signal_edge_bps=min(
                    balanced_payload.strategy_short_entry_min_signal_edge_bps,
                    10.0,
                ),
                strategy_short_entry_alpha_min=min(balanced_payload.strategy_short_entry_alpha_min, 0.14),
                strategy_short_entry_confidence_min=min(
                    balanced_payload.strategy_short_entry_confidence_min,
                    0.56,
                ),
                strategy_scale_in_min_signal_edge_bps=min(
                    balanced_payload.strategy_scale_in_min_signal_edge_bps,
                    15.0,
                ),
                strategy_scale_in_alpha_min=min(balanced_payload.strategy_scale_in_alpha_min, 0.20),
                strategy_scale_in_confidence_min=min(balanced_payload.strategy_scale_in_confidence_min, 0.66),
                strategy_short_scale_in_min_signal_edge_bps=min(
                    balanced_payload.strategy_short_scale_in_min_signal_edge_bps,
                    13.0,
                ),
                strategy_short_scale_in_alpha_min=min(
                    balanced_payload.strategy_short_scale_in_alpha_min,
                    0.18,
                ),
                strategy_short_scale_in_confidence_min=min(
                    balanced_payload.strategy_short_scale_in_confidence_min,
                    0.62,
                ),
                strategy_reversal_min_signal_edge_bps=max(
                    balanced_payload.strategy_reversal_min_signal_edge_bps,
                    22.0,
                ),
                strategy_reversal_alpha_min=max(balanced_payload.strategy_reversal_alpha_min, 0.32),
                strategy_reversal_confidence_min=max(balanced_payload.strategy_reversal_confidence_min, 0.78),
                strategy_short_reversal_min_signal_edge_bps=min(
                    balanced_payload.strategy_short_reversal_min_signal_edge_bps,
                    17.0,
                ),
                strategy_short_reversal_alpha_min=min(
                    balanced_payload.strategy_short_reversal_alpha_min,
                    0.18,
                ),
                strategy_short_reversal_confidence_min=min(
                    balanced_payload.strategy_short_reversal_confidence_min,
                    0.58,
                ),
                strategy_min_hold_seconds=min(balanced_payload.strategy_min_hold_seconds, 480.0),
                strategy_post_close_cooldown_seconds=min(
                    balanced_payload.strategy_post_close_cooldown_seconds,
                    180.0,
                ),
                strategy_max_fee_drag_ratio=max(balanced_payload.strategy_max_fee_drag_ratio, 0.44),
                strategy_max_churn_ratio=max(balanced_payload.strategy_max_churn_ratio, 0.42),
                strategy_low_edge_threshold_bps=min(balanced_payload.strategy_low_edge_threshold_bps, 4.0),
                strategy_low_edge_streak_limit=max(balanced_payload.strategy_low_edge_streak_limit, 4),
                strategy_low_edge_cooldown_seconds=min(balanced_payload.strategy_low_edge_cooldown_seconds, 900.0),
                strategy_transient_close_retry_cooldown_seconds=min(
                    balanced_payload.strategy_transient_close_retry_cooldown_seconds,
                    90.0,
                ),
            ),
            description="Use when trend quality is strong and the system can tolerate a more proactive posture.",
            expected_behavior=[
                "enter earlier on clean trend signals",
                "favor faster add-on decisions but keep reversal relatively strict",
            ],
            auto_switch_allowed=False,
            manual_approval_required=True,
            **common,
        ),
        StrategyProfileRevision(
            profile_id="trend_normal",
            profile_label="Trend Normal",
            status="active",
            risk_level="normal",
            market_intent="trend",
            payload=balanced_payload,
            description="Balanced baseline for live trend trading with faster exits and moderate churn control.",
            expected_behavior=["react faster than the legacy baseline", "avoid turning every flat patch into a reversal"],
            manual_approval_required=False,
            auto_switch_allowed=True,
            **common,
        ),
        StrategyProfileRevision(
            profile_id="trend_strict",
            profile_label="Trend Strict",
            risk_level="normal",
            market_intent="trend",
            payload=_copy_payload(
                balanced_payload,
                decision_min_interval_seconds_15m=max(balanced_payload.decision_min_interval_seconds_15m, 75.0),
                max_decisions_per_minute=min(balanced_payload.max_decisions_per_minute, 3),
                decision_min_price_move_bps=max(balanced_payload.decision_min_price_move_bps, 5.0),
                decision_min_momentum_delta=max(balanced_payload.decision_min_momentum_delta, 0.00045),
                strategy_expected_slippage_bps_fraction=max(
                    balanced_payload.strategy_expected_slippage_bps_fraction,
                    0.30,
                ),
                strategy_min_net_edge_bps=max(balanced_payload.strategy_min_net_edge_bps, 6.5),
                strategy_entry_min_signal_edge_bps=max(
                    balanced_payload.strategy_entry_min_signal_edge_bps,
                    16.0,
                ),
                strategy_entry_alpha_min=max(balanced_payload.strategy_entry_alpha_min, 0.20),
                strategy_entry_confidence_min=max(balanced_payload.strategy_entry_confidence_min, 0.69),
                strategy_short_entry_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_entry_min_signal_edge_bps,
                    14.0,
                ),
                strategy_short_entry_alpha_min=max(balanced_payload.strategy_short_entry_alpha_min, 0.18),
                strategy_short_entry_confidence_min=max(
                    balanced_payload.strategy_short_entry_confidence_min,
                    0.64,
                ),
                strategy_scale_in_min_signal_edge_bps=max(
                    balanced_payload.strategy_scale_in_min_signal_edge_bps,
                    20.0,
                ),
                strategy_scale_in_alpha_min=max(balanced_payload.strategy_scale_in_alpha_min, 0.27),
                strategy_scale_in_confidence_min=max(balanced_payload.strategy_scale_in_confidence_min, 0.76),
                strategy_short_scale_in_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_scale_in_min_signal_edge_bps,
                    18.0,
                ),
                strategy_short_scale_in_alpha_min=max(
                    balanced_payload.strategy_short_scale_in_alpha_min,
                    0.24,
                ),
                strategy_short_scale_in_confidence_min=max(
                    balanced_payload.strategy_short_scale_in_confidence_min,
                    0.72,
                ),
                strategy_reversal_min_signal_edge_bps=max(
                    balanced_payload.strategy_reversal_min_signal_edge_bps,
                    26.0,
                ),
                strategy_reversal_alpha_min=max(balanced_payload.strategy_reversal_alpha_min, 0.38),
                strategy_reversal_confidence_min=max(balanced_payload.strategy_reversal_confidence_min, 0.83),
                strategy_short_reversal_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_reversal_min_signal_edge_bps,
                    22.0,
                ),
                strategy_short_reversal_alpha_min=max(
                    balanced_payload.strategy_short_reversal_alpha_min,
                    0.28,
                ),
                strategy_short_reversal_confidence_min=max(
                    balanced_payload.strategy_short_reversal_confidence_min,
                    0.70,
                ),
                strategy_min_hold_seconds=max(balanced_payload.strategy_min_hold_seconds, 960.0),
                strategy_post_close_cooldown_seconds=max(
                    balanced_payload.strategy_post_close_cooldown_seconds,
                    480.0,
                ),
                strategy_max_fee_drag_ratio=min(balanced_payload.strategy_max_fee_drag_ratio, 0.36),
                strategy_max_churn_ratio=min(balanced_payload.strategy_max_churn_ratio, 0.30),
                strategy_low_edge_threshold_bps=max(balanced_payload.strategy_low_edge_threshold_bps, 5.0),
                strategy_low_edge_streak_limit=min(balanced_payload.strategy_low_edge_streak_limit, 3),
                strategy_low_edge_cooldown_seconds=max(balanced_payload.strategy_low_edge_cooldown_seconds, 3_000.0),
                strategy_transient_close_retry_cooldown_seconds=max(
                    balanced_payload.strategy_transient_close_retry_cooldown_seconds,
                    120.0,
                ),
            ),
            description="Tighten trend entries and reversals without falling back to the legacy ultra-slow posture.",
            expected_behavior=["reduce weak-trend entries", "require clearer follow-through before reversing"],
            auto_switch_allowed=True,
            manual_approval_required=False,
            **common,
        ),
        StrategyProfileRevision(
            profile_id="range_defensive",
            profile_label="Range Defensive",
            risk_level="conservative",
            market_intent="range",
            payload=_copy_payload(
                balanced_payload,
                decision_min_interval_seconds_15m=max(balanced_payload.decision_min_interval_seconds_15m, 120.0),
                max_decisions_per_minute=min(balanced_payload.max_decisions_per_minute, 2),
                decision_min_price_move_bps=max(balanced_payload.decision_min_price_move_bps, 6.0),
                decision_min_momentum_delta=max(balanced_payload.decision_min_momentum_delta, 0.00065),
                strategy_expected_slippage_bps_fraction=max(
                    balanced_payload.strategy_expected_slippage_bps_fraction,
                    0.32,
                ),
                strategy_min_net_edge_bps=max(balanced_payload.strategy_min_net_edge_bps, 8.0),
                strategy_entry_allowed_regimes=("breakout",),
                strategy_short_entry_allowed_regimes=("breakout",),
                strategy_entry_min_signal_edge_bps=max(
                    balanced_payload.strategy_entry_min_signal_edge_bps,
                    18.0,
                ),
                strategy_entry_alpha_min=max(balanced_payload.strategy_entry_alpha_min, 0.23),
                strategy_entry_confidence_min=max(balanced_payload.strategy_entry_confidence_min, 0.72),
                strategy_short_entry_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_entry_min_signal_edge_bps,
                    16.0,
                ),
                strategy_short_entry_alpha_min=max(balanced_payload.strategy_short_entry_alpha_min, 0.20),
                strategy_short_entry_confidence_min=max(
                    balanced_payload.strategy_short_entry_confidence_min,
                    0.68,
                ),
                strategy_scale_in_min_signal_edge_bps=max(
                    balanced_payload.strategy_scale_in_min_signal_edge_bps,
                    22.0,
                ),
                strategy_scale_in_alpha_min=max(balanced_payload.strategy_scale_in_alpha_min, 0.30),
                strategy_scale_in_confidence_min=max(balanced_payload.strategy_scale_in_confidence_min, 0.80),
                strategy_short_scale_in_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_scale_in_min_signal_edge_bps,
                    20.0,
                ),
                strategy_short_scale_in_alpha_min=max(
                    balanced_payload.strategy_short_scale_in_alpha_min,
                    0.26,
                ),
                strategy_short_scale_in_confidence_min=max(
                    balanced_payload.strategy_short_scale_in_confidence_min,
                    0.76,
                ),
                strategy_reversal_min_signal_edge_bps=max(
                    balanced_payload.strategy_reversal_min_signal_edge_bps,
                    28.0,
                ),
                strategy_reversal_alpha_min=max(balanced_payload.strategy_reversal_alpha_min, 0.42),
                strategy_reversal_confidence_min=max(balanced_payload.strategy_reversal_confidence_min, 0.86),
                strategy_short_reversal_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_reversal_min_signal_edge_bps,
                    24.0,
                ),
                strategy_short_reversal_alpha_min=max(
                    balanced_payload.strategy_short_reversal_alpha_min,
                    0.32,
                ),
                strategy_short_reversal_confidence_min=max(
                    balanced_payload.strategy_short_reversal_confidence_min,
                    0.78,
                ),
                strategy_min_hold_seconds=max(balanced_payload.strategy_min_hold_seconds, 1_320.0),
                strategy_post_close_cooldown_seconds=max(
                    balanced_payload.strategy_post_close_cooldown_seconds,
                    900.0,
                ),
                strategy_max_fee_drag_ratio=min(balanced_payload.strategy_max_fee_drag_ratio, 0.32),
                strategy_max_churn_ratio=min(balanced_payload.strategy_max_churn_ratio, 0.24),
                strategy_low_edge_threshold_bps=max(balanced_payload.strategy_low_edge_threshold_bps, 5.5),
                strategy_low_edge_streak_limit=min(balanced_payload.strategy_low_edge_streak_limit, 3),
                strategy_low_edge_cooldown_seconds=max(balanced_payload.strategy_low_edge_cooldown_seconds, 4_200.0),
                strategy_transient_close_retry_cooldown_seconds=max(
                    balanced_payload.strategy_transient_close_retry_cooldown_seconds,
                    180.0,
                ),
            ),
            description="Use in range markets or when fee pressure is elevated.",
            expected_behavior=["reduce churn", "raise net-edge threshold"],
            auto_switch_allowed=True,
            manual_approval_required=False,
            **common,
        ),
        StrategyProfileRevision(
            profile_id="high_volatility_defensive",
            profile_label="High Vol Defensive",
            risk_level="conservative",
            market_intent="high_volatility",
            payload=_copy_payload(
                balanced_payload,
                decision_min_interval_seconds_15m=max(balanced_payload.decision_min_interval_seconds_15m, 150.0),
                max_decisions_per_minute=min(balanced_payload.max_decisions_per_minute, 1),
                decision_min_price_move_bps=max(balanced_payload.decision_min_price_move_bps, 8.0),
                decision_min_momentum_delta=max(balanced_payload.decision_min_momentum_delta, 0.0009),
                strategy_expected_slippage_bps_fraction=max(
                    balanced_payload.strategy_expected_slippage_bps_fraction,
                    0.34,
                ),
                strategy_min_net_edge_bps=max(balanced_payload.strategy_min_net_edge_bps, 10.0),
                strategy_entry_allowed_regimes=("breakout",),
                strategy_short_entry_allowed_regimes=("breakout",),
                strategy_entry_min_signal_edge_bps=max(
                    balanced_payload.strategy_entry_min_signal_edge_bps,
                    20.0,
                ),
                strategy_entry_alpha_min=max(balanced_payload.strategy_entry_alpha_min, 0.27),
                strategy_entry_confidence_min=max(balanced_payload.strategy_entry_confidence_min, 0.78),
                strategy_short_entry_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_entry_min_signal_edge_bps,
                    18.0,
                ),
                strategy_short_entry_alpha_min=max(balanced_payload.strategy_short_entry_alpha_min, 0.24),
                strategy_short_entry_confidence_min=max(
                    balanced_payload.strategy_short_entry_confidence_min,
                    0.74,
                ),
                strategy_scale_in_min_signal_edge_bps=max(
                    balanced_payload.strategy_scale_in_min_signal_edge_bps,
                    24.0,
                ),
                strategy_scale_in_alpha_min=max(balanced_payload.strategy_scale_in_alpha_min, 0.34),
                strategy_scale_in_confidence_min=max(balanced_payload.strategy_scale_in_confidence_min, 0.84),
                strategy_short_scale_in_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_scale_in_min_signal_edge_bps,
                    22.0,
                ),
                strategy_short_scale_in_alpha_min=max(
                    balanced_payload.strategy_short_scale_in_alpha_min,
                    0.30,
                ),
                strategy_short_scale_in_confidence_min=max(
                    balanced_payload.strategy_short_scale_in_confidence_min,
                    0.82,
                ),
                strategy_reversal_min_signal_edge_bps=max(
                    balanced_payload.strategy_reversal_min_signal_edge_bps,
                    30.0,
                ),
                strategy_reversal_alpha_min=max(balanced_payload.strategy_reversal_alpha_min, 0.44),
                strategy_reversal_confidence_min=max(balanced_payload.strategy_reversal_confidence_min, 0.88),
                strategy_short_reversal_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_reversal_min_signal_edge_bps,
                    28.0,
                ),
                strategy_short_reversal_alpha_min=max(
                    balanced_payload.strategy_short_reversal_alpha_min,
                    0.38,
                ),
                strategy_short_reversal_confidence_min=max(
                    balanced_payload.strategy_short_reversal_confidence_min,
                    0.84,
                ),
                strategy_min_hold_seconds=max(balanced_payload.strategy_min_hold_seconds, 1_500.0),
                strategy_post_close_cooldown_seconds=max(
                    balanced_payload.strategy_post_close_cooldown_seconds,
                    1_200.0,
                ),
                strategy_max_fee_drag_ratio=min(balanced_payload.strategy_max_fee_drag_ratio, 0.30),
                strategy_max_churn_ratio=min(balanced_payload.strategy_max_churn_ratio, 0.20),
                strategy_low_edge_threshold_bps=max(balanced_payload.strategy_low_edge_threshold_bps, 6.0),
                strategy_low_edge_streak_limit=min(balanced_payload.strategy_low_edge_streak_limit, 2),
                strategy_low_edge_cooldown_seconds=max(balanced_payload.strategy_low_edge_cooldown_seconds, 5_400.0),
                strategy_transient_close_retry_cooldown_seconds=max(
                    balanced_payload.strategy_transient_close_retry_cooldown_seconds,
                    240.0,
                ),
            ),
            description="Use when volatility expands materially and execution risk rises.",
            expected_behavior=["reduce false triggers in high vol", "avoid overtrading when execution degrades"],
            auto_switch_allowed=True,
            manual_approval_required=False,
            **common,
        ),
        StrategyProfileRevision(
            profile_id="execution_degraded_safe",
            profile_label="Execution Safe",
            risk_level="conservative",
            market_intent="execution_degraded",
            payload=_copy_payload(
                balanced_payload,
                strategy_flat_signal_hold_enabled=True,
                decision_min_interval_seconds_15m=max(balanced_payload.decision_min_interval_seconds_15m, 150.0),
                max_decisions_per_minute=1,
                decision_min_price_move_bps=max(balanced_payload.decision_min_price_move_bps, 10.0),
                decision_min_momentum_delta=max(balanced_payload.decision_min_momentum_delta, 0.001),
                strategy_expected_slippage_bps_fraction=max(
                    balanced_payload.strategy_expected_slippage_bps_fraction,
                    0.36,
                ),
                strategy_min_net_edge_bps=max(balanced_payload.strategy_min_net_edge_bps, 12.0),
                strategy_entry_allowed_regimes=("breakout",),
                strategy_short_entry_allowed_regimes=("breakout",),
                strategy_entry_min_signal_edge_bps=max(
                    balanced_payload.strategy_entry_min_signal_edge_bps,
                    22.0,
                ),
                strategy_entry_alpha_min=max(balanced_payload.strategy_entry_alpha_min, 0.30),
                strategy_entry_confidence_min=max(balanced_payload.strategy_entry_confidence_min, 0.82),
                strategy_short_entry_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_entry_min_signal_edge_bps,
                    20.0,
                ),
                strategy_short_entry_alpha_min=max(balanced_payload.strategy_short_entry_alpha_min, 0.26),
                strategy_short_entry_confidence_min=max(
                    balanced_payload.strategy_short_entry_confidence_min,
                    0.78,
                ),
                strategy_scale_in_min_signal_edge_bps=max(
                    balanced_payload.strategy_scale_in_min_signal_edge_bps,
                    26.0,
                ),
                strategy_scale_in_alpha_min=max(balanced_payload.strategy_scale_in_alpha_min, 0.36),
                strategy_scale_in_confidence_min=max(balanced_payload.strategy_scale_in_confidence_min, 0.88),
                strategy_short_scale_in_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_scale_in_min_signal_edge_bps,
                    24.0,
                ),
                strategy_short_scale_in_alpha_min=max(
                    balanced_payload.strategy_short_scale_in_alpha_min,
                    0.32,
                ),
                strategy_short_scale_in_confidence_min=max(
                    balanced_payload.strategy_short_scale_in_confidence_min,
                    0.86,
                ),
                strategy_reversal_min_signal_edge_bps=max(
                    balanced_payload.strategy_reversal_min_signal_edge_bps,
                    32.0,
                ),
                strategy_reversal_alpha_min=max(balanced_payload.strategy_reversal_alpha_min, 0.46),
                strategy_reversal_confidence_min=max(balanced_payload.strategy_reversal_confidence_min, 0.90),
                strategy_short_reversal_min_signal_edge_bps=max(
                    balanced_payload.strategy_short_reversal_min_signal_edge_bps,
                    30.0,
                ),
                strategy_short_reversal_alpha_min=max(
                    balanced_payload.strategy_short_reversal_alpha_min,
                    0.40,
                ),
                strategy_short_reversal_confidence_min=max(
                    balanced_payload.strategy_short_reversal_confidence_min,
                    0.88,
                ),
                strategy_min_hold_seconds=max(balanced_payload.strategy_min_hold_seconds, 2_100.0),
                strategy_post_close_cooldown_seconds=max(
                    balanced_payload.strategy_post_close_cooldown_seconds,
                    1_500.0,
                ),
                strategy_max_fee_drag_ratio=min(balanced_payload.strategy_max_fee_drag_ratio, 0.26),
                strategy_max_churn_ratio=min(balanced_payload.strategy_max_churn_ratio, 0.16),
                strategy_low_edge_threshold_bps=max(balanced_payload.strategy_low_edge_threshold_bps, 6.0),
                strategy_low_edge_streak_limit=min(balanced_payload.strategy_low_edge_streak_limit, 2),
                strategy_low_edge_cooldown_seconds=max(balanced_payload.strategy_low_edge_cooldown_seconds, 6_000.0),
                strategy_transient_close_retry_cooldown_seconds=max(
                    balanced_payload.strategy_transient_close_retry_cooldown_seconds,
                    300.0,
                ),
            ),
            description="Use when exchange busy responses or execution jitter increase.",
            expected_behavior=["cut decision frequency sharply", "avoid repeated submit and reversal loops"],
            auto_switch_allowed=True,
            manual_approval_required=False,
            **common,
        ),
    ]
    if settings.trading_product_type != "derivatives":
        revisions = [
            revision.model_copy(
                update={
                    "payload": normalize_strategy_profile_payload_for_product_type(
                        revision.payload,
                        product_type=settings.trading_product_type,
                    )
                }
            )
            for revision in revisions
        ]
    return revisions


def seed_strategy_profiles(*, settings: AATSSettings, repo: StrategyProfileRepository) -> None:
    payload = strategy_profile_payload_from_settings(settings)
    existing = repo.list_revisions(
        product_type=settings.trading_product_type,
        margin_mode=settings.margin_mode,
    )
    existing_by_profile_id: dict[str, StrategyProfileRevision] = {}
    for item in existing:
        existing_by_profile_id.setdefault(item.profile_id, item)
    for revision in _seed_revisions(settings=settings, payload=payload):
        existing_revision = existing_by_profile_id.get(revision.profile_id)
        if existing_revision is None:
            repo.save_revision(revision)
            continue
        if existing_revision.created_by != "system_seed" or existing_revision.source_recommendation_id is not None:
            continue
        repo.save_revision(_sync_system_seed_revision(existing=existing_revision, desired=revision))
    state = repo.activation_state(
        product_type=settings.trading_product_type,
        margin_mode=settings.margin_mode,
        allowed_symbols=settings.allowed_symbols,
    )
    if state.active_revision_id is None:
        active = repo.list_revisions(
            product_type=settings.trading_product_type,
            margin_mode=settings.margin_mode,
            profile_id="trend_normal",
        )[0]
        repo.save_revision(active.model_copy(update={"status": "active", "updated_at": utc_now()}))
        repo.save_activation_state(
            state.model_copy(
                update={
                    "active_revision_id": active.revision_id,
                    "active_profile_id": active.profile_id,
                    "auto_switch_enabled": settings.strategy_profile_auto_control_enabled,
                    "last_activation_result": "activation_succeeded",
                    "last_activation_at": utc_now(),
                    "last_switch_reason": "initial_seed",
                    "last_switch_actor": "system_seed",
                }
            )
        )
        return
