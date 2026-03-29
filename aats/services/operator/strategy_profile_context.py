from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.schemas.common import utc_now
from aats.schemas.strategy_profiles import (
    StrategyProfileEvaluationContextSnapshot,
    strategy_profile_axes_from_payload,
    summarize_strategy_profile_payload,
)
from aats.services.accounting import fill_fee_cost_in_quote
from aats.services.governance_engine.recovery_posture import RecoveryPostureEvaluator
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12
from aats.services.runtime_scope import fills_for_scope, latest_reconciliation_for_scope, runtime_state_scope, snapshots_for_scope

if TYPE_CHECKING:
    from aats.services.operator.strategy_profiles import StrategyProfileControlService


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


class StrategyProfileContextFacade:
    def __init__(self, owner: "StrategyProfileControlService") -> None:
        self.owner = owner

    def tuning_context(self) -> StrategyProfileEvaluationContextSnapshot:
        baseline_event = self.owner.event_store.latest(topics.BASELINE_ASSESSMENTS)
        feature_event = self.owner.event_store.latest(topics.FEATURE_SNAPSHOTS)
        latest_portfolio = self.owner.runtime.portfolio_repo.latest()
        activation = self.owner._activation_state()
        safety_state = self.safety_state()
        performance = self.performance_summary()
        revisions = self.owner.repo.list_revisions(
            product_type=self.owner.settings.trading_product_type,
            margin_mode=self.owner.settings.margin_mode,
        )
        return StrategyProfileEvaluationContextSnapshot(
            snapshot_ts=utc_now(),
            scope=self.owner._scope(),
            baseline=baseline_event.payload if baseline_event is not None else None,
            features=feature_event.payload if feature_event is not None else None,
            portfolio=latest_portfolio.model_dump(mode="json") if latest_portfolio is not None else None,
            safety_state=safety_state,
            execution_health={
                "open_order_count": len(
                    self.owner.runtime.execution_repo.order_states_for_scope(
                        scope=self.owner.runtime_state_scope,
                        open_only=True,
                    )
                ),
                "recent_execution_error_count": len(
                    self.owner.runtime.event_store.recent_by_topic(topics.EXECUTION_ERROR_SUMMARIES, limit=20)
                ),
            },
            performance=performance,
            current_profile_id=activation.active_profile_id,
            profile_selection_policy={
                "selection_mode": "registered_profile_only",
                "free_form_parameter_generation_enabled": False,
                "execution_parameter_suggestions_enabled": False,
            },
            candidate_profiles=[
                {
                    "profile_id": item.profile_id,
                    "profile_label": item.profile_label,
                    "risk_level": item.risk_level,
                    "market_intent": item.market_intent,
                    "status": item.status,
                    "axes": strategy_profile_axes_from_payload(
                        item.payload,
                        product_type=item.product_type,
                    ).model_dump(mode="json"),
                    "payload_summary": summarize_strategy_profile_payload(
                        item.payload,
                        product_type=item.product_type,
                    ),
                    "expected_behavior": list(item.expected_behavior),
                    "description": item.description,
                }
                for item in revisions
            ],
        )

    @staticmethod
    def context_payload(snapshot: StrategyProfileEvaluationContextSnapshot) -> dict[str, Any]:
        return snapshot.model_dump(mode="json")

    def resolved_context_signals(self, context: dict[str, Any]) -> dict[str, Any]:
        baseline = context.get("baseline") or {}
        features = context.get("features") or {}
        return {
            "regime": str(features.get("regime_indicator") or baseline.get("regime") or "uncertain"),
            "volatility_state": str(features.get("volatility_state") or baseline.get("volatility_state") or "unknown"),
            "confidence": float(features.get("regime_confidence", baseline.get("confidence", 0.0)) or 0.0),
            "composite_alpha_score": float(
                features.get("composite_alpha_score", baseline.get("composite_alpha_score", 0.0)) or 0.0
            ),
            "direction_bias": str(baseline.get("direction_bias") or features.get("direction_bias") or "flat"),
            "suggested_position_scale": float(features.get("suggested_position_scale", 0.0) or 0.0),
        }

    def performance_summary(self) -> dict[str, Any]:
        fills = fills_for_scope(
            self.owner.runtime.execution_repo,
            self.owner.runtime_state_scope,
            limit=self.owner.evaluation_window_limit,
        )
        snapshots = snapshots_for_scope(
            self.owner.runtime.portfolio_repo,
            self.owner.runtime_state_scope,
            limit=self.owner.evaluation_window_limit,
        )
        fee_total = sum((fill_fee_cost_in_quote(item) for item in fills), start=Decimal("0"))
        latest_snapshot = snapshots[-1] if snapshots else None
        earliest_snapshot = snapshots[0] if len(snapshots) > 1 else None
        latest_realized = latest_snapshot.realized_pnl if latest_snapshot is not None else Decimal("0")
        earliest_realized = earliest_snapshot.realized_pnl if earliest_snapshot is not None else Decimal("0")
        net_realized = latest_realized - earliest_realized if earliest_snapshot is not None else Decimal("0")
        gross_realized = net_realized + fee_total

        pnl_deltas: list[Decimal] = []
        for previous, current in zip(snapshots, snapshots[1:]):
            delta = current.realized_pnl - previous.realized_pnl
            if delta != 0:
                pnl_deltas.append(delta)
        avg_fee = fee_total / Decimal(len(fills)) if fills else Decimal("0")
        material_moves = [delta for delta in pnl_deltas if abs(delta) > Decimal("0")]
        small_pnl_cutoff = avg_fee * Decimal("1.25")
        small_moves = [delta for delta in material_moves if abs(delta) <= small_pnl_cutoff] if material_moves else []
        fee_ratio = (
            fee_total / abs(gross_realized)
            if abs(gross_realized) > EPSILON_DECIMAL_12
            else (Decimal("1") if fee_total > 0 else Decimal("0"))
        )
        win_rate = (
            Decimal(sum(1 for delta in material_moves if delta > 0)) / Decimal(len(material_moves))
            if material_moves
            else Decimal("0")
        )
        small_pnl_churn_ratio = (
            Decimal(len(small_moves)) / Decimal(len(material_moves))
            if material_moves
            else Decimal("0")
        )
        return {
            "trade_count": len(fills),
            "gross_realized_pnl": float(gross_realized),
            "net_realized_pnl": float(net_realized),
            "fee_total": float(fee_total),
            "fee_to_gross_pnl_ratio": float(fee_ratio),
            "win_rate": float(win_rate),
            "small_pnl_churn_ratio": float(small_pnl_churn_ratio),
            "window_start": snapshots[0].snapshot_ts.isoformat() if snapshots else None,
            "window_end": snapshots[-1].snapshot_ts.isoformat() if snapshots else None,
        }

    def safety_state(self) -> dict[str, Any]:
        scope = runtime_state_scope(self.owner.settings)
        market_status = self.owner.runtime.market_gateway.status()
        account_status = self.owner.runtime.account_service.status()
        latest_reconciliation = latest_reconciliation_for_scope(self.owner.runtime.reconciliation_repo, scope)
        recovery = RecoveryPostureEvaluator(self.owner.runtime).finalize_status(
            base_status=self.owner.runtime.recovery_status,
            latest_reconciliation=latest_reconciliation,
        )
        activation = self.owner._activation_state()
        live_guard_service = getattr(self.owner.runtime, "derivatives_live_guard_service", None)
        trial_guard_service = getattr(self.owner.runtime, "trial_guard_service", None)
        live_guard = (
            live_guard_service.snapshot()
            if live_guard_service is not None and hasattr(live_guard_service, "snapshot")
            else {}
        )
        trial_guard = (
            trial_guard_service.snapshot()
            if trial_guard_service is not None and hasattr(trial_guard_service, "snapshot")
            else {}
        )
        now = utc_now()
        return {
            "safe_to_trade": recovery.safe_to_trade,
            "review_required": recovery.review_required,
            "halted": recovery.halted,
            "recovery_state": recovery.recovery_state,
            "resume_blocked_reasons": list(recovery.resume_blocked_reasons),
            "market_snapshot_fresh": bool(market_status.get("fresh")),
            "account_snapshot_fresh": bool(account_status.get("fresh")),
            "market_status": _json_safe(market_status),
            "account_status": _json_safe(account_status),
            "reconciliation_id": latest_reconciliation.reconciliation_id if latest_reconciliation else None,
            "reconciliation_severity": latest_reconciliation.severity if latest_reconciliation else "unknown",
            "reconciliation_halt_required": bool(latest_reconciliation.halt_required) if latest_reconciliation else False,
            "reconciliation_review_required": bool(latest_reconciliation.review_required) if latest_reconciliation else False,
            "auto_switch_frozen": bool(activation.frozen_until and activation.frozen_until > now),
            "auto_switch_cooldown_active": bool(activation.cooldown_until and activation.cooldown_until > now),
            "live_guard": _json_safe(live_guard),
            "trial_guard": _json_safe(trial_guard),
            "only_reduce_required": bool(live_guard.get("only_reduce_required")),
            "auto_halt_required": bool(live_guard.get("auto_halt_required")),
            "trial_guard_breached": str(trial_guard.get("status") or "").lower() == "breached",
        }
