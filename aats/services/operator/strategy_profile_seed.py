from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.strategy_profiles import (
    StrategyProfileGuardrails,
    StrategyProfilePayload,
    StrategyProfileRevision,
    strategy_profile_payload_from_settings,
)
from aats.storage.base import StrategyProfileRepository


def _copy_payload(payload: StrategyProfilePayload, **updates: Any) -> StrategyProfilePayload:
    raw = payload.model_dump(mode="python")
    raw.update(updates)
    return StrategyProfilePayload.model_validate(raw)


def _seed_revisions(*, settings: AATSSettings, payload: StrategyProfilePayload) -> list[StrategyProfileRevision]:
    common = {
        "product_type": settings.trading_product_type,
        "margin_mode": settings.margin_mode,
        "allowed_symbols": settings.allowed_symbols,
        "guardrails": StrategyProfileGuardrails(),
        "created_by": "system_seed",
        "created_reason": "initial_seed",
    }
    return [
        StrategyProfileRevision(
            profile_id="trend_aggressive",
            profile_label="Trend Aggressive",
            risk_level="aggressive",
            market_intent="trend",
            payload=_copy_payload(
                payload,
                decision_min_interval_seconds_15m=min(payload.decision_min_interval_seconds_15m, 45.0),
                max_decisions_per_minute=max(payload.max_decisions_per_minute, 4),
                decision_min_price_move_bps=min(payload.decision_min_price_move_bps, 3.0),
                decision_min_momentum_delta=min(payload.decision_min_momentum_delta, 0.00025),
                strategy_min_net_edge_bps=min(payload.strategy_min_net_edge_bps, 4.0),
                strategy_entry_alpha_min=min(payload.strategy_entry_alpha_min, 0.16),
                strategy_entry_confidence_min=min(payload.strategy_entry_confidence_min, 0.60),
                strategy_scale_in_alpha_min=min(payload.strategy_scale_in_alpha_min, 0.22),
                strategy_scale_in_confidence_min=min(payload.strategy_scale_in_confidence_min, 0.68),
                strategy_reversal_alpha_min=min(payload.strategy_reversal_alpha_min, 0.30),
                strategy_reversal_confidence_min=min(payload.strategy_reversal_confidence_min, 0.78),
                strategy_transient_close_retry_cooldown_seconds=min(
                    payload.strategy_transient_close_retry_cooldown_seconds, 90.0
                ),
            ),
            description="Use when trend quality is strong and the system can tolerate a more proactive posture.",
            expected_behavior=["enter earlier on clean trend signals", "accept more trend continuation trades"],
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
            payload=payload,
            description="Baseline trend profile derived from current runtime settings.",
            expected_behavior=["preserve current trend thresholds", "serve as rollback baseline"],
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
                payload,
                decision_min_interval_seconds_15m=max(payload.decision_min_interval_seconds_15m, 75.0),
                max_decisions_per_minute=min(payload.max_decisions_per_minute, 2),
                decision_min_price_move_bps=max(payload.decision_min_price_move_bps, 5.0),
                decision_min_momentum_delta=max(payload.decision_min_momentum_delta, 0.00045),
                strategy_min_net_edge_bps=max(payload.strategy_min_net_edge_bps, 8.0),
                strategy_entry_alpha_min=max(payload.strategy_entry_alpha_min, 0.22),
                strategy_entry_confidence_min=max(payload.strategy_entry_confidence_min, 0.70),
                strategy_scale_in_alpha_min=max(payload.strategy_scale_in_alpha_min, 0.28),
                strategy_scale_in_confidence_min=max(payload.strategy_scale_in_confidence_min, 0.76),
                strategy_reversal_alpha_min=max(payload.strategy_reversal_alpha_min, 0.36),
                strategy_reversal_confidence_min=max(payload.strategy_reversal_confidence_min, 0.84),
                strategy_transient_close_retry_cooldown_seconds=max(
                    payload.strategy_transient_close_retry_cooldown_seconds, 120.0
                ),
            ),
            description="Keep trend trading enabled while raising entry, scale-in, and reversal thresholds.",
            expected_behavior=["reduce weak-trend entries", "preserve trend-following capability"],
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
                payload,
                decision_min_interval_seconds_15m=max(payload.decision_min_interval_seconds_15m, 105.0),
                max_decisions_per_minute=min(payload.max_decisions_per_minute, 2),
                decision_min_price_move_bps=max(payload.decision_min_price_move_bps, 6.0),
                decision_min_momentum_delta=max(payload.decision_min_momentum_delta, 0.0007),
                strategy_min_net_edge_bps=max(payload.strategy_min_net_edge_bps, 10.0),
                strategy_entry_allowed_regimes=("breakout",),
                strategy_entry_alpha_min=max(payload.strategy_entry_alpha_min, 0.24),
                strategy_entry_confidence_min=max(payload.strategy_entry_confidence_min, 0.72),
                strategy_scale_in_alpha_min=max(payload.strategy_scale_in_alpha_min, 0.30),
                strategy_scale_in_confidence_min=max(payload.strategy_scale_in_confidence_min, 0.80),
                strategy_reversal_alpha_min=max(payload.strategy_reversal_alpha_min, 0.40),
                strategy_reversal_confidence_min=max(payload.strategy_reversal_confidence_min, 0.86),
                strategy_transient_close_retry_cooldown_seconds=max(
                    payload.strategy_transient_close_retry_cooldown_seconds, 180.0
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
                payload,
                decision_min_interval_seconds_15m=max(payload.decision_min_interval_seconds_15m, 120.0),
                max_decisions_per_minute=min(payload.max_decisions_per_minute, 1),
                decision_min_price_move_bps=max(payload.decision_min_price_move_bps, 8.0),
                decision_min_momentum_delta=max(payload.decision_min_momentum_delta, 0.0009),
                strategy_min_net_edge_bps=max(payload.strategy_min_net_edge_bps, 12.0),
                strategy_entry_allowed_regimes=("breakout",),
                strategy_entry_alpha_min=max(payload.strategy_entry_alpha_min, 0.28),
                strategy_entry_confidence_min=max(payload.strategy_entry_confidence_min, 0.78),
                strategy_scale_in_alpha_min=max(payload.strategy_scale_in_alpha_min, 0.34),
                strategy_scale_in_confidence_min=max(payload.strategy_scale_in_confidence_min, 0.84),
                strategy_reversal_alpha_min=max(payload.strategy_reversal_alpha_min, 0.44),
                strategy_reversal_confidence_min=max(payload.strategy_reversal_confidence_min, 0.90),
                strategy_transient_close_retry_cooldown_seconds=max(
                    payload.strategy_transient_close_retry_cooldown_seconds, 240.0
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
                payload,
                decision_min_interval_seconds_15m=max(payload.decision_min_interval_seconds_15m, 180.0),
                max_decisions_per_minute=1,
                decision_min_price_move_bps=max(payload.decision_min_price_move_bps, 10.0),
                decision_min_momentum_delta=max(payload.decision_min_momentum_delta, 0.0011),
                strategy_min_net_edge_bps=max(payload.strategy_min_net_edge_bps, 14.0),
                strategy_entry_allowed_regimes=("breakout",),
                strategy_entry_alpha_min=max(payload.strategy_entry_alpha_min, 0.32),
                strategy_entry_confidence_min=max(payload.strategy_entry_confidence_min, 0.82),
                strategy_scale_in_alpha_min=max(payload.strategy_scale_in_alpha_min, 0.38),
                strategy_scale_in_confidence_min=max(payload.strategy_scale_in_confidence_min, 0.88),
                strategy_reversal_alpha_min=max(payload.strategy_reversal_alpha_min, 0.48),
                strategy_reversal_confidence_min=max(payload.strategy_reversal_confidence_min, 0.92),
                strategy_transient_close_retry_cooldown_seconds=max(
                    payload.strategy_transient_close_retry_cooldown_seconds, 300.0
                ),
            ),
            description="Use when exchange busy responses or execution jitter increase.",
            expected_behavior=["cut decision frequency sharply", "avoid repeated submit and reversal loops"],
            auto_switch_allowed=True,
            manual_approval_required=False,
            **common,
        ),
    ]


def seed_strategy_profiles(*, settings: AATSSettings, repo: StrategyProfileRepository) -> None:
    payload = strategy_profile_payload_from_settings(settings)
    existing = repo.list_revisions(
        product_type=settings.trading_product_type,
        margin_mode=settings.margin_mode,
    )
    existing_profile_ids = {item.profile_id for item in existing}
    for revision in _seed_revisions(settings=settings, payload=payload):
        if revision.profile_id not in existing_profile_ids:
            repo.save_revision(revision)
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
