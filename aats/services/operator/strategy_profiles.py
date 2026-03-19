from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.operator import AuthSource, OperatorActionRecord, OperatorRole
from aats.schemas.strategy_profiles import (
    StrategyProfileComparisonReport,
    StrategyProfileComparisonRow,
    StrategyProfileActivationRecord,
    StrategyProfileActivationState,
    StrategyProfileEvaluationRecord,
    StrategyProfileAxes,
    StrategyProfileGuardrails,
    StrategyProfilePayload,
    StrategyProfileRecommendation,
    StrategyProfileRecommendationOutput,
    StrategyProfileRejectionRecord,
    StrategyProfileRevision,
    apply_strategy_profile_payload,
    diff_strategy_profile_payload,
    strategy_profile_axes_from_payload,
    strategy_profile_payload_from_settings,
    summarize_strategy_profile_payload,
)
from aats.schemas.strategy_profile_reports import (
    StrategyProfileActivationPolicyConfig,
    StrategyProfileAutoRollbackPolicyConfig,
    StrategyProfileOptimizationCandidate,
    StrategyProfileOptimizationReport,
    StrategyProfileSelectionDecision,
)
from aats.services.ai_service.openai_provider import OpenAIProvider
from aats.services.ai_service.provider import AIProviderError, AIProviderTimeoutError
from aats.services.runtime_scope import (
    fills_for_scope,
    latest_reconciliation_for_scope,
    runtime_state_scope,
    snapshots_for_scope,
)
from aats.storage.base import EventStore, StrategyProfileRepository

if TYPE_CHECKING:
    from aats.bootstrap.config import ApplicationRuntime


_RISK_LEVEL_ORDER = {
    "conservative": 0,
    "normal": 1,
    "aggressive": 2,
}
_PROFILE_AXIS_LEVEL_ORDER = {
    "relaxed": 0,
    "balanced": 1,
    "strict": 2,
    "defensive": 3,
}

_AUTO_SWITCH_CONFIDENCE_MIN_CONSERVATIVE = 0.75
_AUTO_SWITCH_CONFIDENCE_MIN_SAME_RISK = 0.80
_AUTO_SWITCH_CONFIDENCE_MIN_AGGRESSIVE = 0.88
_EVALUATION_WINDOW_LIMIT = 50
_PROFILE_COMPARISON_EVALUATION_LIMIT = 100
_RESERVED_EXECUTION_PARAMETER_FIELDS = (
    "passive_bias",
    "maker_taker_bias",
    "max_cross_spread_bps",
    "slice_count",
    "max_participation_rate",
    "cancel_replace_patience_ms",
)

def _copy_payload(payload: StrategyProfilePayload, **updates: Any) -> StrategyProfilePayload:
    raw = payload.model_dump(mode="python")
    raw.update(updates)
    return StrategyProfilePayload.model_validate(raw)


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


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
                    "auto_switch_enabled": True,
                    "last_activation_result": "activation_succeeded",
                    "last_activation_at": utc_now(),
                    "last_switch_reason": "initial_seed",
                    "last_switch_actor": "system_seed",
                }
            )
        )
        return
    if not state.auto_switch_enabled:
        repo.save_activation_state(
            state.model_copy(
                update={
                    "auto_switch_enabled": True,
                }
            )
        )


@dataclass
class StrategyProfileControlService:
    runtime: ApplicationRuntime

    def __post_init__(self) -> None:
        self.repo = self.runtime.strategy_profile_repo
        self.settings = self.runtime.settings
        self.event_store: EventStore = self.runtime.event_store
        self.provider = OpenAIProvider(settings=self.settings) if self.settings.ai_provider_configured else None
        self.prompt_version = "strategy_tuning_v1"
        self.logger = get_logger("aats.strategy_profiles")

    def ensure_seed_profiles(self) -> None:
        seed_strategy_profiles(settings=self.settings, repo=self.repo)

    @property
    def runtime_state_scope(self):
        from aats.services.runtime_scope import runtime_state_scope

        return runtime_state_scope(self.settings)

    def snapshot(self) -> dict[str, Any]:
        self.ensure_seed_profiles()
        state = self._activation_state()
        active = self._revision(state.active_revision_id)
        pending = self._revision(state.pending_revision_id)
        latest_recommendation = self.repo.latest_recommendation(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
        )
        revisions = self.repo.list_revisions(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
        )
        comparison_report = self._comparison_report(revisions=revisions, state=state)
        return {
            "scope": self._scope(),
            "safety_state": self._safety_state(),
            "activation": state.model_dump(mode="json"),
            "active_revision": self._revision_view(active),
            "pending_revision": self._revision_view(pending),
            "latest_recommendation": latest_recommendation.model_dump(mode="json") if latest_recommendation else None,
            "revisions": [self._revision_view(item) for item in revisions],
            "profile_space": self._profile_space(revisions=revisions),
            "comparison_report": comparison_report.model_dump(mode="json"),
            "latest_optimization_report": self._latest_optimization_report_payload(),
            "latest_selection_decision": self._latest_selection_decision_payload(),
            "auto_rollback_policy": self._auto_rollback_policy_view(),
            "auto_rollback_policy_staged": self._staged_auto_rollback_policy_view(),
            "auto_rollback_policy_history": self._auto_rollback_policy_history_payload(limit=10),
            "activation_policy": self._activation_policy_view(),
            "activation_policy_staged": self._staged_activation_policy_view(),
            "activation_policy_history": self._activation_policy_history_payload(limit=10),
            "execution_parameter_suggestion_capability": self._execution_parameter_suggestion_capability(),
            "activation_history": [
                item.model_dump(mode="json")
                for item in self.repo.list_activation_history(
                    product_type=self.settings.trading_product_type,
                    margin_mode=self.settings.margin_mode,
                )[:20]
            ],
            "rejections": [
                item.model_dump(mode="json")
                for item in self.repo.list_rejections(
                    product_type=self.settings.trading_product_type,
                    margin_mode=self.settings.margin_mode,
                )[:20]
            ],
            "evaluations": [
                item.model_dump(mode="json")
                for item in self.repo.list_evaluations(
                    product_type=self.settings.trading_product_type,
                    margin_mode=self.settings.margin_mode,
                )[:20]
            ],
        }

    async def evaluate_now(self, *, allow_auto_activation: bool = True) -> dict[str, Any]:
        self.ensure_seed_profiles()
        context = self._tuning_context()
        state = self._activation_state()
        revisions = self.repo.list_revisions(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
        )
        evaluations = self._build_evaluation_pipeline(
            revisions=revisions,
            state=state,
            context=context,
        )
        current_evaluation = next(
            (
                item
                for item in evaluations
                if item.revision_id == state.active_revision_id or item.profile_id == state.active_profile_id
            ),
            None,
        )
        for evaluation in evaluations:
            self.repo.save_evaluation(evaluation)
            self.event_store.append(
                build_envelope(
                    topic=topics.STRATEGY_PROFILE_EVALUATIONS,
                    key=evaluation.profile_id,
                    payload_model=evaluation,
                    source_component="strategy_profile_service",
                )
            )
        comparison_report = self._comparison_report(
            revisions=revisions,
            state=state,
            evaluations=evaluations,
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_COMPARISON_REPORTS,
                key=state.active_profile_id or "strategy_profiles",
                payload_model=comparison_report,
                source_component="strategy_profile_service",
            )
        )
        optimization_report = self._build_optimization_report(
            state=state,
            comparison_report=comparison_report,
            evaluations=evaluations,
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS,
                key=state.active_profile_id or "strategy_profiles",
                payload_model=optimization_report,
                source_component="strategy_profile_service",
            )
        )
        selection_decision = self._build_selection_decision(
            state=state,
            optimization_report=optimization_report,
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
                key=selection_decision.candidate_profile_id or state.active_profile_id or "strategy_profiles",
                payload_model=selection_decision,
                source_component="strategy_profile_service",
            )
        )
        recommendation = await self._generate_recommendation(context=context)
        self.repo.save_recommendation(recommendation)
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_RECOMMENDATIONS,
                key=recommendation.recommended_profile_id,
                payload_model=recommendation,
                source_component="strategy_profile_service",
            )
        )
        validation = self._recommendation_validation(recommendation)
        result = {
            "recommendation": recommendation.model_dump(mode="json"),
            "validation": validation,
            "safety_state": self._safety_state(),
            "comparison_report": comparison_report.model_dump(mode="json"),
            "optimization_report": optimization_report.model_dump(mode="json"),
            "selection_decision": selection_decision.model_dump(mode="json"),
            "evaluation_pipeline": [item.model_dump(mode="json") for item in evaluations],
        }
        if current_evaluation is not None:
            result["current_evaluation"] = current_evaluation.model_dump(mode="json")
        if allow_auto_activation and validation["auto_apply_allowed"]:
            activation = self._auto_apply_recommendation(recommendation=recommendation)
            result["auto_activation"] = activation
            latest_selection = self._latest_selection_decision_payload()
            if latest_selection is not None:
                result["selection_decision"] = latest_selection
        elif allow_auto_activation and not result.get("auto_activation"):
            policy_activation = self._maybe_auto_execute_activation_policy(
                optimization_report=optimization_report,
                selection_decision=selection_decision,
            )
            if policy_activation is not None:
                result["profile_activation_policy"] = policy_activation
                latest_selection = self._latest_selection_decision_payload()
                if latest_selection is not None:
                    result["selection_decision"] = latest_selection
        outcome_decision = self._write_back_selection_outcome(
            state=self._activation_state(),
            evaluations=evaluations,
            optimization_report=optimization_report,
        )
        if outcome_decision is not None:
            result["selection_decision"] = outcome_decision.model_dump(mode="json")
            if allow_auto_activation:
                auto_rollback = self._maybe_auto_execute_rollback(decision=outcome_decision)
                if auto_rollback is not None:
                    result["auto_rollback"] = auto_rollback
                    latest_selection = self._latest_selection_decision_payload()
                    if latest_selection is not None:
                        result["selection_decision"] = latest_selection
        return result

    def accept_recommendation(
        self,
        *,
        recommendation_id: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        activation_mode: str,
        reason: str,
    ) -> dict[str, Any]:
        self.ensure_seed_profiles()
        recommendation = self.repo.get_recommendation(recommendation_id)
        if recommendation is None:
            raise KeyError("strategy_profile_recommendation_not_found")
        if recommendation.expires_at <= utc_now():
            raise ValueError("strategy_profile_recommendation_expired")
        revision = self._revision_for_profile(recommendation.recommended_profile_id)
        if revision is None:
            raise ValueError("strategy_profile_revision_missing")
        state = self._activation_state()

        if activation_mode == "stage_only":
            state = state.model_copy(
                update={
                    "pending_revision_id": revision.revision_id,
                    "pending_profile_id": revision.profile_id,
                    "activation_mode": "staged",
                    "last_switch_reason": reason,
                    "last_switch_actor": actor_identity,
                }
            )
            self.repo.save_activation_state(state)
            updated = recommendation.model_copy(
                update={
                    "decision_status": "accepted",
                    "decision_reason_code": "staged_for_manual_activation",
                    "decision_reason_detail": reason,
                }
            )
            self.repo.save_recommendation(updated)
            self._append_selection_decision_transition(
                status="staged_for_activation",
                candidate_profile_id=revision.profile_id,
                rollback_profile_id=state.active_profile_id,
                execution_state="staged",
                recommended_action="activate_or_reject",
                rationale=["operator_staged_recommendation"],
                notes=[reason],
            )
            return {
                "status": "accepted_and_staged",
                "recommendation": updated.model_dump(mode="json"),
                "activation": state.model_dump(mode="json"),
            }

        blockers = self._activation_blockers()
        if blockers:
            self._reject_recommendation_record(
                recommendation=recommendation,
                source="local_guard",
                reason_code=blockers[0],
                reason_detail=";".join(blockers),
                actor_identity=actor_identity,
                actor_role=actor_role,
            )
            raise ValueError(blockers[0])

        record = self._activate_revision(
            target=revision,
            state=state,
            trigger_type="manual",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=recommendation.recommendation_id,
            reason_code="operator_accept_recommendation",
            reason_detail=reason,
        )
        updated = recommendation.model_copy(
            update={
                "decision_status": "accepted",
                "decision_reason_code": "operator_accepted",
                "decision_reason_detail": reason,
            }
        )
        self.repo.save_recommendation(updated)
        self._append_selection_decision_transition(
            status="manual_activation_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="executed",
            recommended_action="observe_outcome",
            rationale=["operator_accepted_recommendation"],
            execution_outcome=self._activation_outcome_payload(record=record),
            notes=[reason],
        )
        return {
            "status": "accepted_and_activated",
            "recommendation": updated.model_dump(mode="json"),
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self._revision_view(revision),
        }

    def reject_recommendation(
        self,
        *,
        recommendation_id: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        reason_code: str,
        reason_detail: str | None,
    ) -> dict[str, Any]:
        self.ensure_seed_profiles()
        recommendation = self.repo.get_recommendation(recommendation_id)
        if recommendation is None:
            raise KeyError("strategy_profile_recommendation_not_found")
        updated = recommendation.model_copy(
            update={
                "decision_status": "rejected",
                "decision_reason_code": reason_code,
                "decision_reason_detail": reason_detail,
            }
        )
        self.repo.save_recommendation(updated)
        rejection = self._reject_recommendation_record(
            recommendation=updated,
            source="operator",
            reason_code=reason_code,
            reason_detail=reason_detail,
            actor_identity=actor_identity,
            actor_role=actor_role,
        )
        self._append_selection_decision_transition(
            status="recommendation_rejected",
            candidate_profile_id=updated.recommended_profile_id,
            rollback_profile_id=self._activation_state().active_profile_id,
            execution_state="not_executed",
            recommended_action="keep_current_profile",
            rationale=["operator_rejected_recommendation", reason_code],
            notes=[] if reason_detail is None else [reason_detail],
        )
        return {
            "status": "rejected",
            "recommendation": updated.model_dump(mode="json"),
            "rejection": rejection.model_dump(mode="json"),
        }

    def activate_pending(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        reason: str,
    ) -> dict[str, Any]:
        state = self._activation_state()
        if state.pending_revision_id is None:
            raise ValueError("strategy_profile_pending_revision_missing")
        blockers = self._activation_blockers()
        if blockers:
            raise ValueError(blockers[0])
        revision = self._revision(state.pending_revision_id)
        if revision is None:
            raise ValueError("strategy_profile_pending_revision_missing")
        record = self._activate_revision(
            target=revision,
            state=state,
            trigger_type="manual",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=revision.source_recommendation_id,
            reason_code="operator_activate_pending_profile",
            reason_detail=reason,
        )
        self._append_selection_decision_transition(
            status="pending_activation_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="executed",
            recommended_action="observe_outcome",
            rationale=["operator_activated_pending_profile"],
            execution_outcome=self._activation_outcome_payload(record=record),
            notes=[reason],
        )
        return {
            "status": "activated",
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self._revision_view(revision),
        }

    def activate_profile(
        self,
        *,
        profile_id: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        reason: str,
    ) -> dict[str, Any]:
        state = self._activation_state()
        if profile_id == state.active_profile_id:
            raise ValueError("strategy_profile_already_active")
        blockers = self._activation_blockers()
        if blockers:
            raise ValueError(blockers[0])
        revision = self._revision_for_profile(profile_id)
        if revision is None:
            raise ValueError("strategy_profile_profile_not_found")
        record = self._activate_revision(
            target=revision,
            state=state,
            trigger_type="manual",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=revision.source_recommendation_id,
            reason_code="operator_manual_profile_activation",
            reason_detail=reason,
        )
        self._append_selection_decision_transition(
            status="manual_profile_activation_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="executed",
            recommended_action="observe_outcome",
            rationale=["operator_manual_profile_activation"],
            execution_outcome=self._activation_outcome_payload(record=record),
            notes=[reason],
        )
        return {
            "status": "manually_activated",
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self._revision_view(revision),
        }

    def rollback(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        reason: str,
    ) -> dict[str, Any]:
        state = self._activation_state()
        if state.previous_active_revision_id is None:
            raise ValueError("strategy_profile_no_previous_revision")
        blockers = self._activation_blockers()
        if blockers:
            raise ValueError(blockers[0])
        revision = self._revision(state.previous_active_revision_id)
        if revision is None:
            raise ValueError("strategy_profile_previous_revision_missing")
        record = self._activate_revision(
            target=revision,
            state=state,
            trigger_type="rollback",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=revision.source_recommendation_id,
            reason_code="operator_manual_rollback",
            reason_detail=reason,
        )
        self._append_selection_decision_transition(
            status="rollback_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="rolled_back",
            recommended_action="observe_after_rollback",
            rationale=["operator_manual_rollback"],
            execution_outcome=self._activation_outcome_payload(record=record),
            notes=[reason],
        )
        return {
            "status": "rolled_back",
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self._revision_view(revision),
        }

    def audit_payload(
        self,
        *,
        action: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> OperatorActionRecord:
        return OperatorActionRecord(
            action=action,  # type: ignore[arg-type]
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason="strategy_profile_control",
            status=status,
            details=details or {},
        )

    def _tuning_context(self) -> dict[str, Any]:
        baseline_event = self.event_store.latest(topics.BASELINE_ASSESSMENTS)
        feature_event = self.event_store.latest(topics.FEATURE_SNAPSHOTS)
        latest_portfolio = self.runtime.portfolio_repo.latest()
        activation = self._activation_state()
        safety_state = self._safety_state()
        performance = self._performance_summary()
        revisions = self.repo.list_revisions(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
        )
        return {
            "scope": self._scope(),
            "baseline": baseline_event.payload if baseline_event is not None else None,
            "features": feature_event.payload if feature_event is not None else None,
            "portfolio": latest_portfolio.model_dump(mode="json") if latest_portfolio is not None else None,
            "safety_state": safety_state,
            "execution_health": {
                "open_order_count": len(
                    self.runtime.execution_repo.order_states_for_scope(scope=self.runtime_state_scope, open_only=True)
                ),
                "recent_execution_error_count": len(
                    self.runtime.event_store.recent_by_topic(topics.EXECUTION_ERROR_SUMMARIES, limit=20)
                ),
            },
            "performance": performance,
            "current_profile_id": activation.active_profile_id,
            "profile_selection_policy": {
                "selection_mode": "registered_profile_only",
                "free_form_parameter_generation_enabled": False,
                "execution_parameter_suggestions_enabled": False,
            },
            "candidate_profiles": [
                {
                    "profile_id": item.profile_id,
                    "profile_label": item.profile_label,
                    "risk_level": item.risk_level,
                    "market_intent": item.market_intent,
                    "status": item.status,
                    "axes": strategy_profile_axes_from_payload(item.payload).model_dump(mode="json"),
                    "payload_summary": summarize_strategy_profile_payload(item.payload),
                    "expected_behavior": list(item.expected_behavior),
                    "description": item.description,
                }
                for item in revisions
            ],
        }

    def _performance_summary(self) -> dict[str, Any]:
        fills = fills_for_scope(
            self.runtime.execution_repo,
            self.runtime_state_scope,
            limit=_EVALUATION_WINDOW_LIMIT,
        )
        snapshots = snapshots_for_scope(
            self.runtime.portfolio_repo,
            self.runtime_state_scope,
            limit=_EVALUATION_WINDOW_LIMIT,
        )
        fee_total = sum(Decimal(str(item.fee_amount)) for item in fills)
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
        fee_ratio = fee_total / gross_realized if gross_realized > 0 else (Decimal("1") if fee_total > 0 else Decimal("0"))
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

    def _safety_state(self) -> dict[str, Any]:
        scope = runtime_state_scope(self.settings)
        recovery = self.runtime.recovery_status
        market_status = self.runtime.market_gateway.status()
        account_status = self.runtime.account_service.status()
        latest_reconciliation = latest_reconciliation_for_scope(self.runtime.reconciliation_repo, scope)
        activation = self._activation_state()
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
        }

    def _build_evaluation_record(self) -> StrategyProfileEvaluationRecord | None:
        state = self._activation_state()
        active = self._revision(state.active_revision_id)
        if active is None:
            return None
        performance = self._performance_summary()
        safety_state = self._safety_state()
        execution_error_rate = (
            float(self.runtime.event_store.recent_by_topic(topics.EXECUTION_ERROR_SUMMARIES, limit=20).__len__())
            / float(max(performance["trade_count"], 1))
        )
        reconciliation_issue_count = 0
        reconciliation_severity = str(safety_state.get("reconciliation_severity") or "unknown").upper()
        if reconciliation_severity not in {"CLEAN", "UNKNOWN"}:
            reconciliation_issue_count = 1
        if performance["trade_count"] < 3:
            status = "observing"
        elif (
            safety_state["review_required"]
            or safety_state["reconciliation_halt_required"]
            or not safety_state["market_snapshot_fresh"]
            or not safety_state["account_snapshot_fresh"]
        ):
            status = "degraded"
        else:
            status = "healthy"
        return StrategyProfileEvaluationRecord(
            revision_id=active.revision_id,
            profile_id=active.profile_id,
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
            window_start=utc_now() if performance["window_start"] is None else datetime.fromisoformat(performance["window_start"]),
            window_end=utc_now() if performance["window_end"] is None else datetime.fromisoformat(performance["window_end"]),
            trade_count=int(performance["trade_count"]),
            win_rate=float(performance["win_rate"]),
            gross_realized_pnl=float(performance["gross_realized_pnl"]),
            net_realized_pnl=float(performance["net_realized_pnl"]),
            fee_total=float(performance["fee_total"]),
            fee_to_gross_pnl_ratio=float(performance["fee_to_gross_pnl_ratio"]),
            small_pnl_churn_ratio=float(performance["small_pnl_churn_ratio"]),
            execution_error_rate=execution_error_rate,
            reconciliation_issue_count=reconciliation_issue_count,
            status=status,
            summary={
                "safe_to_trade": safety_state["safe_to_trade"],
                "review_required": safety_state["review_required"],
                "reconciliation_severity": safety_state["reconciliation_severity"],
                "market_snapshot_fresh": safety_state["market_snapshot_fresh"],
                "account_snapshot_fresh": safety_state["account_snapshot_fresh"],
                "evaluation_mode": "actual_runtime_window",
            },
        )

    def _build_evaluation_pipeline(
        self,
        *,
        revisions: list[StrategyProfileRevision],
        state: StrategyProfileActivationState,
        context: dict[str, Any],
    ) -> list[StrategyProfileEvaluationRecord]:
        active_revision = self._revision(state.active_revision_id)
        actual = self._build_evaluation_record()
        performance = self._performance_summary()
        safety_state = self._safety_state()
        shadow_summary = self._shadow_summary_for_profiles()
        rows: list[StrategyProfileEvaluationRecord] = []
        for revision in revisions:
            if actual is not None and revision.revision_id == actual.revision_id:
                rows.append(actual)
                continue
            rows.append(
                self._projected_evaluation_record(
                    revision=revision,
                    active_revision=active_revision,
                    performance=performance,
                    safety_state=safety_state,
                    shadow_summary=shadow_summary,
                    context=context,
                )
            )
        return rows

    def _projected_evaluation_record(
        self,
        *,
        revision: StrategyProfileRevision,
        active_revision: StrategyProfileRevision | None,
        performance: dict[str, Any],
        safety_state: dict[str, Any],
        shadow_summary: dict[str, Any],
        context: dict[str, Any],
    ) -> StrategyProfileEvaluationRecord:
        active_payload = (
            active_revision.payload
            if active_revision is not None
            else strategy_profile_payload_from_settings(self.settings)
        )
        active_axes = strategy_profile_axes_from_payload(active_payload)
        candidate_axes = strategy_profile_axes_from_payload(revision.payload)
        frequency_delta = self._axis_rank(candidate_axes.frequency) - self._axis_rank(active_axes.frequency)
        entry_delta = self._axis_rank(candidate_axes.entry_threshold) - self._axis_rank(active_axes.entry_threshold)
        reversal_delta = self._axis_rank(candidate_axes.reversal_threshold) - self._axis_rank(active_axes.reversal_threshold)
        cost_delta = self._axis_rank(candidate_axes.cost_protection) - self._axis_rank(active_axes.cost_protection)
        cooldown_delta = self._axis_rank(candidate_axes.cooldown_fuse) - self._axis_rank(active_axes.cooldown_fuse)
        fit = self._profile_intent_fit_score(revision=revision, context=context, performance=performance, shadow_summary=shadow_summary)

        base_trade_count = int(performance["trade_count"])
        base_win_rate = float(performance["win_rate"])
        base_net_pnl = float(performance["net_realized_pnl"])
        base_gross_pnl = float(performance["gross_realized_pnl"])
        base_fee_total = float(performance["fee_total"])
        base_fee_ratio = float(performance["fee_to_gross_pnl_ratio"])
        base_churn = float(performance["small_pnl_churn_ratio"])
        error_rate = float(context.get("execution_health", {}).get("recent_execution_error_count", 0) or 0) / float(max(base_trade_count, 1))

        activity_factor = self._clamp_float(
            1.0 - 0.10 * frequency_delta - 0.05 * entry_delta - 0.03 * reversal_delta + 0.03 * min(fit, 1.5),
            0.20,
            1.35,
        )
        projected_trade_count = max(int(round(base_trade_count * activity_factor)), 0)
        projected_fee_ratio = self._clamp_float(
            base_fee_ratio - 0.035 * cost_delta - 0.02 * frequency_delta - 0.015 * cooldown_delta - 0.01 * fit,
            0.0,
            5.0,
        )
        projected_churn_ratio = self._clamp_float(
            base_churn - 0.08 * cost_delta - 0.06 * frequency_delta - 0.04 * reversal_delta - 0.02 * fit,
            0.0,
            1.0,
        )
        projected_win_rate = self._clamp_float(
            base_win_rate + 0.025 * entry_delta + 0.02 * cost_delta + 0.015 * max(fit, 0.0),
            0.0,
            1.0,
        )
        gross_scale = max(abs(base_gross_pnl), abs(base_net_pnl), 1.0)
        net_adjustment = gross_scale * (
            0.07 * fit
            + 0.025 * cost_delta
            + 0.015 * entry_delta
            + 0.01 * reversal_delta
            - 0.03 * max(-fit, 0.0)
        )
        projected_net_pnl = round(base_net_pnl + net_adjustment, 6)
        projected_fee_total = round(max(base_fee_total * activity_factor * max(projected_fee_ratio / max(base_fee_ratio, 0.01), 0.25), 0.0), 6)
        projected_gross_pnl = round(projected_net_pnl + projected_fee_total, 6)

        if projected_trade_count < 3:
            status = "observing"
        elif (
            safety_state["review_required"]
            or safety_state["reconciliation_halt_required"]
            or not safety_state["market_snapshot_fresh"]
            or not safety_state["account_snapshot_fresh"]
        ):
            status = "degraded"
        elif fit < -0.25:
            status = "rollback_recommended"
        else:
            status = "healthy"

        return StrategyProfileEvaluationRecord(
            revision_id=revision.revision_id,
            profile_id=revision.profile_id,
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
            window_start=utc_now(),
            window_end=utc_now(),
            trade_count=projected_trade_count,
            win_rate=projected_win_rate,
            gross_realized_pnl=projected_gross_pnl,
            net_realized_pnl=projected_net_pnl,
            fee_total=projected_fee_total,
            fee_to_gross_pnl_ratio=projected_fee_ratio,
            small_pnl_churn_ratio=projected_churn_ratio,
            execution_error_rate=round(error_rate, 6),
            reconciliation_issue_count=0 if str(safety_state.get("reconciliation_severity") or "unknown").upper() in {"CLEAN", "UNKNOWN"} else 1,
            status=status,
            summary={
                "evaluation_mode": "heuristic_projection_v1",
                "active_profile_id": active_revision.profile_id if active_revision is not None else None,
                "market_intent": revision.market_intent,
                "intent_fit_score": round(fit, 6),
                "activity_factor": round(activity_factor, 6),
                "market_snapshot_fresh": safety_state["market_snapshot_fresh"],
                "account_snapshot_fresh": safety_state["account_snapshot_fresh"],
                "shadow_window_count": shadow_summary.get("window_count", 0),
                "shadow_latest_net_pnl_delta": shadow_summary.get("latest_net_pnl_delta"),
            },
        )

    async def _generate_recommendation(self, *, context: dict[str, Any]) -> StrategyProfileRecommendation:
        active_state = self._activation_state()
        registered_profile_ids = self._registered_profile_ids()
        if self.provider is not None:
            try:
                response = await self.provider.generate_assessment(
                    prompt=self._prompt(context),
                    response_schema=StrategyProfileRecommendationOutput.model_json_schema(),
                )
                output = StrategyProfileRecommendationOutput.model_validate(response.payload)
                if output.recommended_profile_id not in registered_profile_ids:
                    raise ValueError("strategy_profile_provider_recommended_unregistered_profile")
                if (
                    output.fallback_profile_id is not None
                    and output.fallback_profile_id not in registered_profile_ids
                ):
                    raise ValueError("strategy_profile_provider_fallback_unregistered_profile")
                return StrategyProfileRecommendation(
                    product_type=self.settings.trading_product_type,
                    margin_mode=self.settings.margin_mode,
                    allowed_symbols=self.settings.allowed_symbols,
                    active_profile_id=active_state.active_profile_id,
                    recommended_profile_id=output.recommended_profile_id,
                    fallback_profile_id=output.fallback_profile_id,
                    confidence=output.confidence,
                    market_regime_assessment=output.market_regime_assessment,
                    reason_codes=output.reason_codes,
                    human_summary=output.human_summary,
                    risk_notes=output.risk_notes,
                    valid_for_minutes=output.valid_for_minutes,
                    generated_by=response.provider_name,
                    model_name=self.settings.ai_model_name,
                    prompt_version=self.prompt_version,
                    input_digest=self._input_digest(context),
                    input_snapshot=context,
                    expires_at=utc_now() + timedelta(minutes=output.valid_for_minutes),
                )
            except (AIProviderError, AIProviderTimeoutError, ValueError) as exc:
                fallback_reason_code = self._fallback_reason_code(exc)
                fallback_reason_detail = str(exc)
                log_event(
                    self.logger,
                    "strategy_profile_recommendation_fallback",
                    level="warning",
                    active_profile_id=active_state.active_profile_id,
                    provider=self.settings.ai_provider,
                    fallback_reason_code=fallback_reason_code,
                    fallback_reason_detail=fallback_reason_detail,
                )
                return self._fallback_recommendation(
                    context=context,
                    active_profile_id=active_state.active_profile_id,
                    fallback_reason_code=fallback_reason_code,
                    fallback_reason_detail=fallback_reason_detail,
                )
        return self._fallback_recommendation(
            context=context,
            active_profile_id=active_state.active_profile_id,
            fallback_reason_code="strategy_profile_provider_not_configured",
            fallback_reason_detail="strategy profile recommendation provider is not configured",
        )

    @staticmethod
    def _fallback_reason_code(exc: Exception) -> str:
        text = str(exc).strip()
        return text or type(exc).__name__

    def _fallback_recommendation(
        self,
        *,
        context: dict[str, Any],
        active_profile_id: str | None,
        fallback_reason_code: str,
        fallback_reason_detail: str | None,
    ) -> StrategyProfileRecommendation:
        baseline = context.get("baseline") or {}
        features = context.get("features") or {}
        regime = baseline.get("regime") or features.get("regime_indicator") or "uncertain"
        volatility_state = baseline.get("volatility_state") or features.get("volatility_state") or "unknown"
        composite_alpha = float(baseline.get("composite_alpha_score", features.get("composite_alpha_score", 0.0)) or 0.0)
        fee_ratio = float(context.get("performance", {}).get("fee_to_gross_pnl_ratio", 0.0) or 0.0)
        execution_errors = int(context.get("execution_health", {}).get("recent_execution_error_count", 0) or 0)

        if execution_errors >= 3:
            recommended = "execution_degraded_safe"
            summary = "Execution errors are elevated; switch to the safety profile to reduce decision frequency."
            reasons = ["execution_errors_elevated"]
        elif volatility_state == "high":
            recommended = "high_volatility_defensive"
            summary = "Volatility is elevated; move to the high-volatility defensive profile."
            reasons = ["high_volatility_detected"]
        elif regime in {"range", "uncertain"} or fee_ratio >= 0.5 or abs(composite_alpha) < 0.12:
            recommended = "range_defensive"
            summary = "Range behavior or fee compression is dominant; reduce churn with the range defensive profile."
            reasons = ["range_regime_detected", "fee_churn_elevated"]
        elif abs(composite_alpha) >= 0.22:
            recommended = "trend_normal"
            summary = "Trend signal remains healthy; keep the standard trend profile."
            reasons = ["trend_signal_supported"]
        else:
            recommended = "trend_strict"
            summary = "Trend exists but edge is moderate; prefer the stricter trend profile."
            reasons = ["trend_signal_moderate"]
        confidence = 0.82 if recommended in {"range_defensive", "high_volatility_defensive", "execution_degraded_safe"} else 0.72

        return StrategyProfileRecommendation(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
            active_profile_id=active_profile_id,
            recommended_profile_id=recommended,
            fallback_profile_id="trend_normal",
            confidence=confidence,
            market_regime_assessment={
                "regime": str(regime),
                "volatility_state": str(volatility_state),
                "execution_condition": "degraded" if execution_errors >= 3 else "normal",
            },
            reason_codes=reasons,
            human_summary=summary,
            risk_notes=["fallback_rule_based_recommendation"],
            valid_for_minutes=120,
            generated_by="rule_fallback",
            model_name="rule_fallback",
            prompt_version=self.prompt_version,
            fallback_reason_code=fallback_reason_code,
            fallback_reason_detail=fallback_reason_detail,
            input_digest=self._input_digest(context),
            input_snapshot=context,
            expires_at=utc_now() + timedelta(minutes=120),
        )

    def _prompt(self, context: dict[str, Any]) -> str:
        return "\n".join(
            [
                "You are a strategy tuning advisor for a cryptocurrency trading system.",
                "Select exactly one approved profile from candidate_profiles.",
                "Prioritize: financial safety, lower fee churn, lower low-edge trading, and execution reliability.",
                "Do not invent profiles. Do not return markdown. Return only JSON matching the provided schema.",
                f"prompt_version={self.prompt_version}",
                "Context JSON:",
                json.dumps(context, ensure_ascii=False, sort_keys=True, indent=2),
            ]
        )

    @staticmethod
    def _input_digest(context: dict[str, Any]) -> str:
        serialized = json.dumps(context, ensure_ascii=False, sort_keys=True)
        return sha256(serialized.encode("utf-8")).hexdigest()

    def _activation_state(self) -> StrategyProfileActivationState:
        return self.repo.activation_state(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
        )

    def _scope(self) -> dict[str, Any]:
        return {
            "product_type": self.settings.trading_product_type,
            "margin_mode": self.settings.margin_mode,
            "allowed_symbols": list(self.settings.allowed_symbols),
        }

    def _registered_profile_ids(self) -> set[str]:
        return {
            item.profile_id
            for item in self.repo.list_revisions(
                product_type=self.settings.trading_product_type,
                margin_mode=self.settings.margin_mode,
            )
        }

    def _revision(self, revision_id: str | None) -> StrategyProfileRevision | None:
        return self.repo.get_revision(revision_id) if revision_id is not None else None

    def _revision_for_profile(self, profile_id: str) -> StrategyProfileRevision | None:
        rows = self.repo.list_revisions(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            profile_id=profile_id,
        )
        return rows[0] if rows else None

    def _revision_view(self, revision: StrategyProfileRevision | None) -> dict[str, Any] | None:
        if revision is None:
            return None
        return {
            **revision.model_dump(mode="json"),
            "axes": strategy_profile_axes_from_payload(revision.payload).model_dump(mode="json"),
            "payload_summary": summarize_strategy_profile_payload(revision.payload),
            "summary": diff_strategy_profile_payload(
                strategy_profile_payload_from_settings(self.settings),
                revision.payload,
            ),
        }

    def _profile_space(self, *, revisions: list[StrategyProfileRevision]) -> dict[str, Any]:
        return {
            "selection_mode": "registered_profile_only",
            "free_form_parameter_generation_enabled": False,
            "execution_parameter_suggestions_enabled": False,
            "axes": [
                "frequency",
                "entry_threshold",
                "scale_in_threshold",
                "reversal_threshold",
                "cost_protection",
                "cooldown_fuse",
            ],
            "registered_profiles": [
                {
                    "profile_id": item.profile_id,
                    "profile_label": item.profile_label,
                    "risk_level": item.risk_level,
                    "market_intent": item.market_intent,
                    "status": item.status,
                    "axes": strategy_profile_axes_from_payload(item.payload).model_dump(mode="json"),
                    "payload_summary": summarize_strategy_profile_payload(item.payload),
                    "expected_behavior": list(item.expected_behavior),
                    "description": item.description,
                }
                for item in revisions
            ],
        }

    def _execution_parameter_suggestion_capability(self) -> dict[str, Any]:
        return {
            "status": "reserved_not_enabled",
            "enabled": False,
            "diagnostic_only": True,
            "accepted_by_execution_planner": False,
            "selection_boundary": "ai_may_only_select_registered_profiles",
            "supported_fields": list(_RESERVED_EXECUTION_PARAMETER_FIELDS),
            "summary": "Reserved schema only. The deterministic execution planner still owns all live order construction.",
        }

    def _auto_rollback_policy_view(self) -> dict[str, Any]:
        return self._resolved_auto_rollback_policy().model_dump(mode="json")

    def _staged_auto_rollback_policy_view(self) -> dict[str, Any] | None:
        staged = next(
            (item for item in self._auto_rollback_policy_history(limit=50) if item.policy_status == "staged"),
            None,
        )
        return staged.model_dump(mode="json") if staged is not None else None

    def _stored_auto_rollback_policy(self) -> StrategyProfileAutoRollbackPolicyConfig | None:
        history = self._auto_rollback_policy_history(limit=50)
        return next((item for item in history if item.policy_status == "approved"), None)

    def _auto_rollback_policy_history(self, *, limit: int | None = None) -> list[StrategyProfileAutoRollbackPolicyConfig]:
        rows = [
            StrategyProfileAutoRollbackPolicyConfig.model_validate(item.payload)
            for item in reversed(
                self.event_store.by_topic_scoped(
                    topics.STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES,
                    scope=self.runtime_state_scope,
                )
            )
            if isinstance(item.payload, dict)
        ]
        if limit is not None:
            rows = rows[:limit]
        return rows

    def _auto_rollback_policy_history_payload(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self._auto_rollback_policy_history(limit=limit)]

    def _resolved_auto_rollback_policy(self) -> StrategyProfileAutoRollbackPolicyConfig:
        stored = self._stored_auto_rollback_policy()
        if stored is not None:
            return stored
        return StrategyProfileAutoRollbackPolicyConfig(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            policy_status="settings_fallback",
            effective=True,
            enabled=bool(self.settings.strategy_profile_auto_rollback_enabled),
            review_required_only=bool(self.settings.strategy_profile_auto_rollback_review_required_only),
            min_trade_count=int(self.settings.strategy_profile_auto_rollback_min_trade_count),
            cooldown_seconds=float(self.settings.strategy_profile_auto_rollback_cooldown_seconds),
            matrix_allowed_symbols=tuple(self.settings.strategy_profile_auto_rollback_allowed_symbols),
            matrix_allowed_regimes=tuple(self.settings.strategy_profile_auto_rollback_allowed_regimes),
            matrix_allowed_profiles=tuple(self.settings.strategy_profile_auto_rollback_allowed_profiles),
            updated_by="runtime_settings_default",
            update_reason="settings_fallback",
        )

    def _activation_policy_view(self) -> dict[str, Any]:
        return self._resolved_activation_policy().model_dump(mode="json")

    def _staged_activation_policy_view(self) -> dict[str, Any] | None:
        staged = next(
            (item for item in self._activation_policy_history(limit=50) if item.policy_status == "staged"),
            None,
        )
        return staged.model_dump(mode="json") if staged is not None else None

    def _stored_activation_policy(self) -> StrategyProfileActivationPolicyConfig | None:
        history = self._activation_policy_history(limit=50)
        return next((item for item in history if item.policy_status == "approved"), None)

    def _activation_policy_history(self, *, limit: int | None = None) -> list[StrategyProfileActivationPolicyConfig]:
        rows = [
            StrategyProfileActivationPolicyConfig.model_validate(item.payload)
            for item in reversed(
                self.event_store.by_topic_scoped(
                    topics.STRATEGY_PROFILE_ACTIVATION_POLICIES,
                    scope=self.runtime_state_scope,
                )
            )
            if isinstance(item.payload, dict)
        ]
        if limit is not None:
            rows = rows[:limit]
        return rows

    def _activation_policy_history_payload(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self._activation_policy_history(limit=limit)]

    def _resolved_activation_policy(self) -> StrategyProfileActivationPolicyConfig:
        stored = self._stored_activation_policy()
        if stored is not None:
            return stored
        return StrategyProfileActivationPolicyConfig(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            policy_status="settings_fallback",
            effective=True,
            enabled=bool(self.settings.strategy_profile_activation_policy_enabled),
            min_composite_score=float(self.settings.strategy_profile_auto_activation_min_composite_score),
            min_offline_replay_score=float(self.settings.strategy_profile_auto_activation_min_offline_replay_score),
            min_recommendation_strength=float(self.settings.strategy_profile_auto_activation_min_recommendation_strength),
            require_positive_replay_consensus=bool(
                self.settings.strategy_profile_auto_activation_require_positive_replay_consensus
            ),
            disallow_when_shadow_review_required=bool(
                self.settings.strategy_profile_auto_activation_disallow_when_shadow_review_required
            ),
            matrix_allowed_symbols=tuple(self.settings.strategy_profile_activation_policy_allowed_symbols),
            matrix_allowed_regimes=tuple(self.settings.strategy_profile_activation_policy_allowed_regimes),
            matrix_allowed_profiles=tuple(self.settings.strategy_profile_activation_policy_allowed_profiles),
            updated_by="runtime_settings_default",
            update_reason="settings_fallback",
        )

    def update_activation_policy(
        self,
        *,
        enabled: bool,
        min_composite_score: float,
        min_offline_replay_score: float,
        min_recommendation_strength: float,
        require_positive_replay_consensus: bool,
        disallow_when_shadow_review_required: bool,
        matrix_allowed_symbols: tuple[str, ...],
        matrix_allowed_regimes: tuple[str, ...],
        matrix_allowed_profiles: tuple[str, ...],
        reason: str,
        actor_identity: str | None,
    ) -> dict[str, Any]:
        previous = self._activation_policy_history(limit=1)
        previous_policy = previous[0] if previous else None
        policy = StrategyProfileActivationPolicyConfig(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            previous_policy_id=previous_policy.policy_id if previous_policy is not None else None,
            policy_status="staged",
            effective=False,
            enabled=enabled,
            min_composite_score=float(min_composite_score),
            min_offline_replay_score=float(min_offline_replay_score),
            min_recommendation_strength=float(min_recommendation_strength),
            require_positive_replay_consensus=bool(require_positive_replay_consensus),
            disallow_when_shadow_review_required=bool(disallow_when_shadow_review_required),
            matrix_allowed_symbols=tuple(matrix_allowed_symbols),
            matrix_allowed_regimes=tuple(matrix_allowed_regimes),
            matrix_allowed_profiles=tuple(matrix_allowed_profiles),
            updated_by=actor_identity,
            update_reason=reason,
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_ACTIVATION_POLICIES,
                key=self.settings.default_symbol,
                payload_model=policy,
                source_component="strategy_profile_service",
            )
        )
        return policy.model_dump(mode="json")

    def approve_activation_policy(
        self,
        *,
        policy_id: str | None,
        actor_identity: str | None,
        reason: str,
    ) -> dict[str, Any]:
        history = self._activation_policy_history(limit=50)
        target = next((item for item in history if policy_id is None or item.policy_id == policy_id), None)
        if target is None:
            raise KeyError("strategy_profile_activation_policy_not_found")
        approved = target.model_copy(
            update={
                "policy_status": "approved",
                "effective": not bool(target.frozen),
                "approved_by": actor_identity,
                "approved_at": utc_now(),
                "update_reason": reason,
            }
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_ACTIVATION_POLICIES,
                key=self.settings.default_symbol,
                payload_model=approved,
                source_component="strategy_profile_service",
            )
        )
        return approved.model_dump(mode="json")

    def freeze_activation_policy(
        self,
        *,
        frozen: bool,
        actor_identity: str | None,
        reason: str,
    ) -> dict[str, Any]:
        current = self._resolved_activation_policy()
        updated = current.model_copy(
            update={
                "policy_status": "approved",
                "effective": False if frozen else True,
                "frozen": frozen,
                "frozen_by": actor_identity if frozen else None,
                "frozen_at": utc_now() if frozen else None,
                "freeze_reason": reason if frozen else None,
                "update_reason": reason,
            }
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_ACTIVATION_POLICIES,
                key=self.settings.default_symbol,
                payload_model=updated,
                source_component="strategy_profile_service",
            )
        )
        return updated.model_dump(mode="json")

    def update_auto_rollback_policy(
        self,
        *,
        enabled: bool,
        review_required_only: bool,
        min_trade_count: int,
        cooldown_seconds: float,
        matrix_allowed_symbols: tuple[str, ...],
        matrix_allowed_regimes: tuple[str, ...],
        matrix_allowed_profiles: tuple[str, ...],
        reason: str,
        actor_identity: str | None,
    ) -> dict[str, Any]:
        previous = self._auto_rollback_policy_history(limit=1)
        previous_policy = previous[0] if previous else None
        policy = StrategyProfileAutoRollbackPolicyConfig(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            previous_policy_id=previous_policy.policy_id if previous_policy is not None else None,
            policy_status="staged",
            effective=False,
            enabled=enabled,
            review_required_only=review_required_only,
            min_trade_count=max(int(min_trade_count), 0),
            cooldown_seconds=max(float(cooldown_seconds), 0.0),
            matrix_allowed_symbols=tuple(matrix_allowed_symbols),
            matrix_allowed_regimes=tuple(matrix_allowed_regimes),
            matrix_allowed_profiles=tuple(matrix_allowed_profiles),
            updated_by=actor_identity,
            update_reason=reason,
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES,
                key=self.settings.default_symbol,
                payload_model=policy,
                source_component="strategy_profile_service",
            )
        )
        return policy.model_dump(mode="json")

    def approve_auto_rollback_policy(
        self,
        *,
        policy_id: str | None,
        actor_identity: str | None,
        reason: str,
    ) -> dict[str, Any]:
        history = self._auto_rollback_policy_history(limit=50)
        target = next((item for item in history if policy_id is None or item.policy_id == policy_id), None)
        if target is None:
            raise KeyError("strategy_profile_auto_rollback_policy_not_found")
        approved = target.model_copy(
            update={
                "policy_status": "approved",
                "effective": not bool(target.frozen),
                "approved_by": actor_identity,
                "approved_at": utc_now(),
                "update_reason": reason,
            }
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES,
                key=self.settings.default_symbol,
                payload_model=approved,
                source_component="strategy_profile_service",
            )
        )
        return approved.model_dump(mode="json")

    def freeze_auto_rollback_policy(
        self,
        *,
        frozen: bool,
        actor_identity: str | None,
        reason: str,
    ) -> dict[str, Any]:
        current = self._resolved_auto_rollback_policy()
        updated = current.model_copy(
            update={
                "policy_status": "approved",
                "effective": False if frozen else True,
                "frozen": frozen,
                "frozen_by": actor_identity if frozen else None,
                "frozen_at": utc_now() if frozen else None,
                "freeze_reason": reason if frozen else None,
                "update_reason": reason,
            }
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_AUTO_ROLLBACK_POLICIES,
                key=self.settings.default_symbol,
                payload_model=updated,
                source_component="strategy_profile_service",
            )
        )
        return updated.model_dump(mode="json")

    def _comparison_report(
        self,
        *,
        revisions: list[StrategyProfileRevision],
        state: StrategyProfileActivationState,
        evaluations: list[StrategyProfileEvaluationRecord] | None = None,
    ) -> StrategyProfileComparisonReport:
        if evaluations is None:
            evaluations = self.repo.list_evaluations(
                product_type=self.settings.trading_product_type,
                margin_mode=self.settings.margin_mode,
            )[:_PROFILE_COMPARISON_EVALUATION_LIMIT]
        by_profile: dict[str, list[StrategyProfileEvaluationRecord]] = {}
        for item in evaluations:
            by_profile.setdefault(item.profile_id, []).append(item)

        rows: list[StrategyProfileComparisonRow] = []
        for revision in revisions:
            profile_evaluations = by_profile.get(revision.profile_id, [])
            trade_count = sum(item.trade_count for item in profile_evaluations)
            evaluation_count = len(profile_evaluations)
            avg_net_realized_pnl = (
                sum(item.net_realized_pnl for item in profile_evaluations) / evaluation_count
                if evaluation_count
                else 0.0
            )
            avg_fee_ratio = (
                sum(item.fee_to_gross_pnl_ratio for item in profile_evaluations) / evaluation_count
                if evaluation_count
                else 0.0
            )
            avg_churn_ratio = (
                sum(item.small_pnl_churn_ratio for item in profile_evaluations) / evaluation_count
                if evaluation_count
                else 0.0
            )
            avg_win_rate = (
                sum(item.win_rate for item in profile_evaluations) / evaluation_count
                if evaluation_count
                else 0.0
            )
            latest_status = profile_evaluations[0].status if profile_evaluations else None
            score_breakdown = {
                "net_realized_pnl": round(avg_net_realized_pnl, 6),
                "win_rate": round(avg_win_rate * 100.0, 6),
                "fee_penalty": round(avg_fee_ratio * -25.0, 6),
                "churn_penalty": round(avg_churn_ratio * -20.0, 6),
                "status_penalty": (
                    -15.0
                    if latest_status in {"degraded", "rollback_recommended", "rollback_executed"}
                    else 0.0
                ),
            }
            score = round(sum(score_breakdown.values()), 6)
            rows.append(
                StrategyProfileComparisonRow(
                    profile_id=revision.profile_id,
                    profile_label=revision.profile_label,
                    risk_level=revision.risk_level,
                    market_intent=revision.market_intent,
                    axes=strategy_profile_axes_from_payload(revision.payload),
                    evaluation_count=evaluation_count,
                    total_trade_count=trade_count,
                    avg_net_realized_pnl=avg_net_realized_pnl,
                    avg_fee_to_gross_pnl_ratio=avg_fee_ratio,
                    avg_small_pnl_churn_ratio=avg_churn_ratio,
                    avg_win_rate=avg_win_rate,
                    latest_status=latest_status,
                    active=revision.profile_id == state.active_profile_id,
                    pending=revision.profile_id == state.pending_profile_id,
                    score=score,
                    score_breakdown=score_breakdown,
                    expected_behavior=list(revision.expected_behavior),
                    summary=summarize_strategy_profile_payload(revision.payload),
                )
            )
        rows.sort(key=lambda item: (-item.score, item.profile_id))
        return StrategyProfileComparisonReport(
            scope=self._scope(),
            shadow_summary=self._shadow_summary_for_profiles(),
            active_profile_id=state.active_profile_id,
            rows=rows,
        )

    def _build_optimization_report(
        self,
        *,
        state: StrategyProfileActivationState,
        comparison_report: StrategyProfileComparisonReport,
        evaluations: list[StrategyProfileEvaluationRecord],
    ) -> StrategyProfileOptimizationReport:
        ai_performance_summary = self._shadow_summary_for_profiles()
        context = self._tuning_context()
        replay_summary = self._recent_replay_summary(
            symbol=self.settings.default_symbol,
            regime=(context.get("baseline") or {}).get("regime"),
            active_profile_id=state.active_profile_id,
        )
        replay_pipeline = self._build_offline_replay_pipeline(
            comparison_rows=comparison_report.rows,
            symbol=self.settings.default_symbol,
            regime=(context.get("baseline") or {}).get("regime"),
            active_profile_id=state.active_profile_id,
        )
        replay_summary = replay_pipeline.get("primary_summary") or replay_summary
        previous_report = self._latest_optimization_report()
        evaluation_refs_by_profile: dict[str, list[str]] = {}
        for evaluation in evaluations:
            evaluation_refs_by_profile.setdefault(evaluation.profile_id, []).append(evaluation.evaluation_id)

        candidates: list[StrategyProfileOptimizationCandidate] = []
        for row in comparison_report.rows:
            shadow_adjustment = self._shadow_adjustment_for_profile(row=row, ai_performance_summary=ai_performance_summary)
            replay_scorecard = (replay_pipeline.get("candidate_scores") or {}).get(row.profile_id) or {}
            replay_adjustment = float(replay_scorecard.get("aggregate_adjustment") or 0.0)
            stability_adjustment = self._stability_adjustment_for_profile(row=row, replay_summary=replay_summary)
            composite_score = round(row.score + shadow_adjustment + replay_adjustment + stability_adjustment, 6)
            reasons = self._optimization_reasons(
                row=row,
                shadow_adjustment=shadow_adjustment,
                replay_adjustment=replay_adjustment,
                stability_adjustment=stability_adjustment,
                ai_performance_summary=ai_performance_summary,
                replay_summary=replay_summary,
            )
            candidates.append(
                StrategyProfileOptimizationCandidate(
                    profile_id=row.profile_id,
                    profile_label=row.profile_label,
                    risk_level=row.risk_level,
                    market_intent=row.market_intent,
                    base_score=row.score,
                    shadow_adjustment=shadow_adjustment,
                    replay_adjustment=replay_adjustment,
                    stability_adjustment=stability_adjustment,
                    composite_score=composite_score,
                    recommendation_strength=round(max(composite_score, 0.0), 6),
                    offline_replay_score=replay_adjustment,
                    offline_replay_breakdown=replay_scorecard,
                    reasons=reasons,
                    evaluation_refs=evaluation_refs_by_profile.get(row.profile_id, []),
                    metrics={
                        "evaluation_count": row.evaluation_count,
                        "avg_net_realized_pnl": row.avg_net_realized_pnl,
                        "avg_fee_to_gross_pnl_ratio": row.avg_fee_to_gross_pnl_ratio,
                        "avg_small_pnl_churn_ratio": row.avg_small_pnl_churn_ratio,
                        "avg_win_rate": row.avg_win_rate,
                        "latest_status": row.latest_status,
                        "shadow_summary": ai_performance_summary,
                    },
                )
            )
        candidates.sort(key=lambda item: (-item.composite_score, item.profile_id))
        recommended = candidates[0].profile_id if candidates else None
        notes = [
            "offline_optimization_uses_historical_profile_evaluations",
            "shadow_guard_adjustment_derived_from_latest_ai_performance_reports",
            "replay_adjustment_derived_from_recent_replay_validations",
            "replay_cross_bucket_scoring_enabled_for_symbol_regime_profile",
        ]
        winner_selection_policy = self._winner_selection_policy(
            candidates=candidates,
            ai_performance_summary=ai_performance_summary,
        )
        version_experiments = self._profile_version_experiments(
            revisions=self.repo.list_revisions(
                product_type=self.settings.trading_product_type,
                margin_mode=self.settings.margin_mode,
            ),
            replay_pipeline=replay_pipeline,
        )
        return StrategyProfileOptimizationReport(
            version=1 if previous_report is None else previous_report.version + 1,
            parent_report_id=None if previous_report is None else previous_report.report_id,
            scope=self._scope(),
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            active_profile_id=state.active_profile_id,
            recommended_profile_id=recommended,
            replay_summary=replay_summary,
            offline_replay_pipeline=replay_pipeline,
            ai_performance_summary=ai_performance_summary,
            winner_selection_policy=winner_selection_policy,
            version_experiments=version_experiments,
            candidates=candidates,
            notes=notes,
        )

    def _shadow_summary_for_profiles(self) -> dict[str, Any]:
        latest_report = self.event_store.latest_by_topic_scoped(
            topics.AI_PERFORMANCE_REPORTS,
            scope=self.runtime_state_scope,
        )
        if latest_report is not None and isinstance(latest_report.payload, dict):
            payload = latest_report.payload
            summary = payload.get("summary") or {}
            return {
                "window_count": payload.get("window_count", 0),
                "outperformed_count": summary.get("outperformed_count", 0),
                "underperformed_count": summary.get("underperformed_count", 0),
                "latest_net_pnl_delta": summary.get("latest_net_pnl_delta", 0.0),
                "latest_fee_ratio_delta": summary.get("latest_fee_ratio_delta", 0.0),
                "latest_churn_ratio_delta": summary.get("latest_churn_ratio_delta", 0.0),
                "review_required": payload.get("review_required", False),
                "latest_status": payload.get("latest_status", "insufficient_data"),
            }
        rows = list(
            reversed(
                self.event_store.by_topic_scoped(
                    topics.AI_SHADOW_EVALUATIONS,
                    scope=self.runtime_state_scope,
                )
            )
        )[:10]
        payloads = [item.payload for item in rows if isinstance(item.payload, dict)]
        if not payloads:
            return {
                "window_count": 0,
                "outperformed_count": 0,
                "underperformed_count": 0,
                "latest_net_pnl_delta": 0.0,
                "latest_fee_ratio_delta": 0.0,
                "latest_churn_ratio_delta": 0.0,
            }
        latest = payloads[0]
        return {
            "window_count": len(payloads),
            "outperformed_count": sum(1 for item in payloads if item.get("shadow_outperformed") is True),
            "underperformed_count": sum(1 for item in payloads if item.get("shadow_outperformed") is False),
            "latest_net_pnl_delta": round(
                float(Decimal(str(latest.get("shadow_net_pnl") or "0")) - Decimal(str(latest.get("baseline_net_pnl") or "0"))),
                6,
            ),
            "latest_fee_ratio_delta": round(
                float(latest.get("shadow_fee_ratio") or 0.0) - float(latest.get("baseline_fee_ratio") or 0.0),
                6,
            ),
            "latest_churn_ratio_delta": round(
                float(latest.get("shadow_churn_ratio") or 0.0) - float(latest.get("baseline_churn_ratio") or 0.0),
                6,
            ),
        }

    def _recent_replay_summary(
        self,
        *,
        symbol: str | None = None,
        regime: str | None = None,
        active_profile_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        rows = list(reversed(self.event_store.by_topic(topics.REPLAY_VALIDATIONS)))
        scoped_rows: list[dict[str, Any]] = []
        for event in rows:
            payload = event.payload if isinstance(event.payload, dict) else None
            if payload is None:
                continue
            product_type = payload.get("product_type")
            margin_mode = payload.get("margin_mode")
            allowed_symbols = tuple(payload.get("allowed_symbols") or ())
            if product_type and product_type != self.settings.trading_product_type:
                continue
            if margin_mode and margin_mode != self.settings.margin_mode:
                continue
            if allowed_symbols and allowed_symbols != tuple(self.settings.allowed_symbols):
                continue
            scoped_rows.append(payload)
            if len(scoped_rows) >= limit:
                break
        matched_symbol_rows = [item for item in scoped_rows if symbol and item.get("symbol") == symbol]
        matched_regime_rows = [item for item in scoped_rows if regime and item.get("regime") == regime]
        matched_profile_rows = [
            item for item in scoped_rows if active_profile_id and item.get("active_profile_id") == active_profile_id
        ]
        matched_cross_rows = [
            item
            for item in scoped_rows
            if (symbol is None or item.get("symbol") == symbol)
            and (regime is None or item.get("regime") == regime)
            and (active_profile_id is None or item.get("active_profile_id") == active_profile_id)
        ]
        if not scoped_rows:
            return {
                "validation_count": 0,
                "healthy_rate": 0.0,
                "avg_divergence_count": None,
                "avg_divergence_density": None,
                "avg_chain_health_score": None,
                "avg_portfolio_issue_count": None,
                "avg_decision_chain_issue_count": None,
                "avg_execution_chain_issue_count": None,
                "avg_audit_issue_count": None,
                "avg_baseline_switch_issue_count": None,
                "latest_validation": None,
                "target_symbol": symbol,
                "target_regime": regime,
            "target_profile_id": active_profile_id,
            "bucket_scores": {},
            "cross_bucket_scores": [],
            "current_cross_bucket": {"count": 0, "healthy_rate": 0.0, "avg_chain_health_score": None, "avg_divergence_density": None},
        }
        def _bucket(rows_subset: list[dict[str, Any]]) -> dict[str, Any]:
            if not rows_subset:
                return {"count": 0, "healthy_rate": 0.0, "avg_chain_health_score": None, "avg_divergence_density": None}
            return {
                "count": len(rows_subset),
                "healthy_rate": round(sum(1 for item in rows_subset if item.get("healthy")) / len(rows_subset), 6),
                "avg_chain_health_score": round(
                    sum(float(item.get("chain_health_score") or 0.0) for item in rows_subset) / len(rows_subset),
                    6,
                ),
                "avg_divergence_density": round(
                    sum(float(item.get("divergence_density") or 0.0) for item in rows_subset) / len(rows_subset),
                    6,
                ),
            }
        cross_bucket_map: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for item in scoped_rows:
            cross_key = (
                str(item.get("symbol") or "unknown"),
                str(item.get("regime") or "unknown"),
                str(item.get("active_profile_id") or "unknown"),
            )
            cross_bucket_map.setdefault(cross_key, []).append(item)
        cross_bucket_scores = [
            {
                "symbol": cross_key[0],
                "regime": cross_key[1],
                "profile_id": cross_key[2],
                **_bucket(bucket_rows),
            }
            for cross_key, bucket_rows in cross_bucket_map.items()
        ]
        cross_bucket_scores.sort(
            key=lambda item: (
                -int(item.get("count") or 0),
                -float(item.get("avg_chain_health_score") or 0.0),
                str(item.get("symbol") or ""),
                str(item.get("regime") or ""),
                str(item.get("profile_id") or ""),
            )
        )
        return {
            "validation_count": len(scoped_rows),
            "healthy_rate": round(sum(1 for item in scoped_rows if item.get("healthy")) / len(scoped_rows), 6),
            "avg_divergence_count": round(
                sum(float(item.get("divergence_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
                6,
            ),
            "avg_divergence_density": round(
                sum(float(item.get("divergence_density") or 0.0) for item in scoped_rows) / len(scoped_rows),
                6,
            ),
            "avg_chain_health_score": round(
                sum(float(item.get("chain_health_score") or 0.0) for item in scoped_rows) / len(scoped_rows),
                6,
            ),
            "avg_portfolio_issue_count": round(
                sum(float(item.get("portfolio_issue_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
                6,
            ),
            "avg_decision_chain_issue_count": round(
                sum(float(item.get("decision_chain_issue_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
                6,
            ),
            "avg_execution_chain_issue_count": round(
                sum(float(item.get("execution_chain_issue_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
                6,
            ),
            "avg_audit_issue_count": round(
                sum(float(item.get("audit_issue_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
                6,
            ),
            "avg_baseline_switch_issue_count": round(
                sum(float(item.get("baseline_switch_issue_count") or 0.0) for item in scoped_rows) / len(scoped_rows),
                6,
            ),
            "latest_validation": scoped_rows[0],
            "target_symbol": symbol,
            "target_regime": regime,
            "target_profile_id": active_profile_id,
            "bucket_scores": {
                "symbol": _bucket(matched_symbol_rows),
                "regime": _bucket(matched_regime_rows),
                "profile": _bucket(matched_profile_rows),
            },
            "cross_bucket_scores": cross_bucket_scores[:12],
            "current_cross_bucket": _bucket(matched_cross_rows),
        }

    def _build_offline_replay_pipeline(
        self,
        *,
        comparison_rows: list[StrategyProfileComparisonRow],
        symbol: str | None,
        regime: str | None,
        active_profile_id: str | None,
    ) -> dict[str, Any]:
        windows = sorted({int(item) for item in self.settings.strategy_profile_offline_replay_windows if int(item) > 0})
        if not windows:
            windows = [20]
        window_reports: list[dict[str, Any]] = []
        primary_summary: dict[str, Any] | None = None
        for index, window in enumerate(windows):
            summary = self._recent_replay_summary(
                symbol=symbol,
                regime=regime,
                active_profile_id=active_profile_id,
                limit=window,
            )
            if index == 0:
                primary_summary = summary
            window_reports.append(
                {
                    "window": window,
                    "summary": summary,
                }
            )
        candidate_scores: dict[str, dict[str, Any]] = {}
        for row in comparison_rows:
            experiments: list[dict[str, Any]] = []
            for window_report in window_reports:
                summary = window_report["summary"]
                scorecard = self._offline_replay_scorecard_for_row(row=row, replay_summary=summary)
                scorecard["window"] = window_report["window"]
                experiments.append(scorecard)
            aggregate_adjustment = round(
                sum(float(item.get("final_adjustment") or 0.0) for item in experiments) / len(experiments),
                6,
            ) if experiments else 0.0
            aggregate_confidence = round(
                sum(float(item.get("confidence_weight") or 0.0) for item in experiments) / len(experiments),
                6,
            ) if experiments else 0.0
            consensus = "positive" if aggregate_adjustment > 0 else "negative" if aggregate_adjustment < 0 else "neutral"
            candidate_scores[row.profile_id] = {
                "aggregate_adjustment": aggregate_adjustment,
                "aggregate_confidence": aggregate_confidence,
                "consensus": consensus,
                "experiments": experiments,
            }
        return {
            "pipeline_version": self.settings.strategy_profile_offline_replay_pipeline_version,
            "history_window": {
                "window_sizes": windows,
                "target_symbol": symbol,
                "target_regime": regime,
                "active_profile_id": active_profile_id,
            },
            "stages": [
                "scope_history",
                "run_multi_window_replay_scoring",
                "score_symbol_bucket",
                "score_regime_bucket",
                "score_profile_bucket",
                "score_symbol_regime_profile_cross_bucket",
                "aggregate_window_experiments",
                "emit_candidate_adjustments",
            ],
            "candidate_scores": candidate_scores,
            "window_reports": window_reports,
            "primary_summary": primary_summary or {},
        }

    def _winner_selection_policy(
        self,
        *,
        candidates: list[StrategyProfileOptimizationCandidate],
        ai_performance_summary: dict[str, Any],
    ) -> dict[str, Any]:
        activation_policy = self._resolved_activation_policy()
        winner = candidates[0] if candidates else None
        blocked_reasons: list[str] = []
        if winner is not None:
            if float(winner.composite_score or 0.0) < float(activation_policy.min_composite_score):
                blocked_reasons.append("winner_composite_score_below_threshold")
            if float(winner.offline_replay_score or 0.0) < float(activation_policy.min_offline_replay_score):
                blocked_reasons.append("winner_offline_replay_score_below_threshold")
            if float(winner.recommendation_strength or 0.0) < float(activation_policy.min_recommendation_strength):
                blocked_reasons.append("winner_recommendation_strength_below_threshold")
            if activation_policy.require_positive_replay_consensus and (
                (winner.offline_replay_breakdown or {}).get("consensus") != "positive"
            ):
                blocked_reasons.append("winner_replay_consensus_not_positive")
            if activation_policy.disallow_when_shadow_review_required and bool(
                ai_performance_summary.get("review_required")
            ):
                blocked_reasons.append("winner_shadow_review_required")
        return {
            "policy_version": "winner_selection_v1",
            "primary_metric": "composite_score",
            "tie_breakers": [
                "higher_offline_replay_score",
                "higher_recommendation_strength",
                "lexicographic_profile_id",
            ],
            "thresholds": {
                "min_composite_score": float(activation_policy.min_composite_score),
                "min_offline_replay_score": float(activation_policy.min_offline_replay_score),
                "min_recommendation_strength": float(activation_policy.min_recommendation_strength),
                "require_positive_replay_consensus": bool(activation_policy.require_positive_replay_consensus),
                "disallow_when_shadow_review_required": bool(activation_policy.disallow_when_shadow_review_required),
            },
            "activation_policy": {
                "policy_id": activation_policy.policy_id,
                "policy_status": activation_policy.policy_status,
                "effective": activation_policy.effective,
                "enabled": activation_policy.enabled,
                "frozen": activation_policy.frozen,
                "matrix_allowed_symbols": list(activation_policy.matrix_allowed_symbols),
                "matrix_allowed_regimes": list(activation_policy.matrix_allowed_regimes),
                "matrix_allowed_profiles": list(activation_policy.matrix_allowed_profiles),
            },
            "failure_rollback_rules": {
                "on_degraded_evaluation": bool(self.settings.strategy_profile_failure_rollback_on_degraded_evaluation),
                "on_shadow_review_required": bool(self.settings.strategy_profile_failure_rollback_on_shadow_review_required),
                "on_alternative_winner": bool(self.settings.strategy_profile_failure_rollback_on_alternative_winner),
            },
            "winner_profile_id": winner.profile_id if winner else None,
            "auto_activation": {
                "eligible": not blocked_reasons and winner is not None,
                "blocked_reasons": blocked_reasons,
            },
        }

    def _profile_version_experiments(
        self,
        *,
        revisions: list[StrategyProfileRevision],
        replay_pipeline: dict[str, Any],
    ) -> list[dict[str, Any]]:
        by_profile: dict[str, list[StrategyProfileRevision]] = {}
        candidate_scores = replay_pipeline.get("candidate_scores") or {}
        for revision in revisions:
            by_profile.setdefault(revision.profile_id, []).append(revision)
        experiments: list[dict[str, Any]] = []
        for profile_id, rows in by_profile.items():
            rows = sorted(rows, key=lambda item: (-int(item.version), item.updated_at), reverse=False)
            experiments.append(
                {
                    "profile_id": profile_id,
                    "versions": [
                        {
                            "revision_id": row.revision_id,
                            "version": row.version,
                            "status": row.status,
                            "profile_label": row.profile_label,
                            "updated_at": row.updated_at.isoformat(),
                            "candidate_score": candidate_scores.get(profile_id, {}),
                        }
                        for row in rows
                    ],
                }
            )
        experiments.sort(key=lambda item: item["profile_id"])
        return experiments

    def _offline_replay_scorecard_for_row(
        self,
        *,
        row: StrategyProfileComparisonRow,
        replay_summary: dict[str, Any],
    ) -> dict[str, Any]:
        bucket_scores = replay_summary.get("bucket_scores") or {}
        symbol_bucket = bucket_scores.get("symbol") or {}
        regime_bucket = bucket_scores.get("regime") or {}
        profile_bucket = bucket_scores.get("profile") or {}
        target_symbol = replay_summary.get("target_symbol")
        target_regime = replay_summary.get("target_regime")
        cross_bucket = next(
            (
                item
                for item in replay_summary.get("cross_bucket_scores") or []
                if item.get("symbol") == target_symbol
                and item.get("regime") == target_regime
                and item.get("profile_id") == row.profile_id
            ),
            {},
        )
        scorecard = {
            "global_health_adjustment": 0.0,
            "global_divergence_adjustment": 0.0,
            "execution_chain_adjustment": 0.0,
            "decision_chain_adjustment": 0.0,
            "symbol_bucket_adjustment": 0.0,
            "regime_bucket_adjustment": 0.0,
            "profile_bucket_adjustment": 0.0,
            "cross_bucket_adjustment": 0.0,
            "evidence_counts": {
                "global_validations": int(replay_summary.get("validation_count") or 0),
                "symbol_bucket": int(symbol_bucket.get("count") or 0),
                "regime_bucket": int(regime_bucket.get("count") or 0),
                "profile_bucket": int(profile_bucket.get("count") or 0),
                "cross_bucket": int(cross_bucket.get("count") or 0),
            },
        }
        healthy_rate = float(replay_summary.get("healthy_rate") or 0.0)
        avg_divergence = float(replay_summary.get("avg_divergence_count") or 0.0)
        avg_execution_issues = float(replay_summary.get("avg_execution_chain_issue_count") or 0.0)
        avg_decision_issues = float(replay_summary.get("avg_decision_chain_issue_count") or 0.0)
        if healthy_rate < 0.8:
            scorecard["global_health_adjustment"] = 2.5 if row.risk_level == "conservative" else -2.0
        if avg_divergence > 0:
            scorecard["global_divergence_adjustment"] = 1.5 if row.market_intent == "execution_degraded" else -1.0
        if avg_execution_issues > 0:
            scorecard["execution_chain_adjustment"] = 1.5 if row.market_intent == "execution_degraded" else -0.75
        if avg_decision_issues > 0:
            scorecard["decision_chain_adjustment"] = 1.0 if row.market_intent in {"range", "high_volatility"} else -0.5
        if float(symbol_bucket.get("avg_chain_health_score") or 0.0) >= 0.95 and row.latest_status == "healthy":
            scorecard["symbol_bucket_adjustment"] = 0.75
        if float(regime_bucket.get("avg_divergence_density") or 0.0) > 0.0:
            scorecard["regime_bucket_adjustment"] = (
                1.0 if row.market_intent in {"range", "high_volatility", "execution_degraded"} else -0.75
            )
        if profile_bucket.get("count", 0) > 0:
            scorecard["profile_bucket_adjustment"] = (
                1.0 if float(profile_bucket.get("healthy_rate") or 0.0) >= 0.8 else -1.0
            )
        if cross_bucket.get("count", 0) > 0:
            scorecard["cross_bucket_adjustment"] = (
                1.5
                if float(cross_bucket.get("healthy_rate") or 0.0) >= 0.8
                and float(cross_bucket.get("avg_divergence_density") or 0.0) <= 0.0
                else -1.5
            )
        scorecard["final_adjustment"] = round(
            sum(
                float(scorecard[key] or 0.0)
                for key in (
                    "global_health_adjustment",
                    "global_divergence_adjustment",
                    "execution_chain_adjustment",
                    "decision_chain_adjustment",
                    "symbol_bucket_adjustment",
                    "regime_bucket_adjustment",
                    "profile_bucket_adjustment",
                    "cross_bucket_adjustment",
                )
            ),
            6,
        )
        scorecard["confidence_weight"] = round(
            min(1.0, float(scorecard["evidence_counts"]["global_validations"]) / 20.0),
            6,
        )
        scorecard["target"] = {
            "symbol": target_symbol,
            "regime": target_regime,
            "profile_id": row.profile_id,
        }
        return scorecard

    @staticmethod
    def _shadow_adjustment_for_profile(
        *,
        row: StrategyProfileComparisonRow,
        ai_performance_summary: dict[str, Any],
    ) -> float:
        latest_net_delta = float(ai_performance_summary.get("latest_net_pnl_delta") or 0.0)
        latest_fee_delta = float(ai_performance_summary.get("latest_fee_ratio_delta") or 0.0)
        latest_churn_delta = float(ai_performance_summary.get("latest_churn_ratio_delta") or 0.0)
        review_required = bool(ai_performance_summary.get("review_required"))
        adjustment = 0.0
        if latest_net_delta < 0 or review_required:
            adjustment += 4.0 if row.market_intent in {"range", "high_volatility", "execution_degraded"} else -4.0
        if latest_fee_delta > 0 or latest_churn_delta > 0:
            adjustment += 2.5 if row.risk_level == "conservative" else -1.5
        return round(adjustment, 6)

    @staticmethod
    def _replay_adjustment_for_profile(
        *,
        row: StrategyProfileComparisonRow,
        replay_summary: dict[str, Any],
    ) -> float:
        healthy_rate = float(replay_summary.get("healthy_rate") or 0.0)
        avg_divergence = float(replay_summary.get("avg_divergence_count") or 0.0)
        avg_execution_issues = float(replay_summary.get("avg_execution_chain_issue_count") or 0.0)
        avg_decision_issues = float(replay_summary.get("avg_decision_chain_issue_count") or 0.0)
        bucket_scores = replay_summary.get("bucket_scores") or {}
        symbol_bucket = bucket_scores.get("symbol") or {}
        regime_bucket = bucket_scores.get("regime") or {}
        profile_bucket = bucket_scores.get("profile") or {}
        target_symbol = replay_summary.get("target_symbol")
        target_regime = replay_summary.get("target_regime")
        cross_bucket = next(
            (
                item
                for item in replay_summary.get("cross_bucket_scores") or []
                if item.get("symbol") == target_symbol
                and item.get("regime") == target_regime
                and item.get("profile_id") == row.profile_id
            ),
            {},
        )
        if replay_summary.get("validation_count", 0) == 0:
            return 0.0
        adjustment = 0.0
        if healthy_rate < 0.8:
            adjustment += 2.5 if row.risk_level == "conservative" else -2.0
        if avg_divergence > 0:
            adjustment += 1.5 if row.market_intent == "execution_degraded" else -1.0
        if avg_execution_issues > 0:
            adjustment += 1.5 if row.market_intent == "execution_degraded" else -0.75
        if avg_decision_issues > 0:
            adjustment += 1.0 if row.market_intent in {"range", "high_volatility"} else -0.5
        if float(symbol_bucket.get("avg_chain_health_score") or 0.0) >= 0.95:
            adjustment += 0.75 if row.latest_status == "healthy" else 0.0
        if float(regime_bucket.get("avg_divergence_density") or 0.0) > 0.0:
            adjustment += 1.0 if row.market_intent in {"range", "high_volatility", "execution_degraded"} else -0.75
        if profile_bucket.get("count", 0) > 0:
            adjustment += 1.0 if float(profile_bucket.get("healthy_rate") or 0.0) >= 0.8 else -1.0
        if cross_bucket.get("count", 0) > 0:
            if float(cross_bucket.get("healthy_rate") or 0.0) >= 0.8 and float(cross_bucket.get("avg_divergence_density") or 0.0) <= 0.0:
                adjustment += 1.5
            else:
                adjustment -= 1.5
        return round(adjustment, 6)

    @staticmethod
    def _stability_adjustment_for_profile(
        *,
        row: StrategyProfileComparisonRow,
        replay_summary: dict[str, Any],
    ) -> float:
        latest_validation = replay_summary.get("latest_validation") or {}
        divergence_count = float(latest_validation.get("divergence_count") or 0.0)
        chain_health_score = float(replay_summary.get("avg_chain_health_score") or 0.0)
        if divergence_count <= 0:
            return 1.0 if chain_health_score >= 0.95 and row.latest_status == "healthy" else 0.0
        if row.latest_status in {"degraded", "rollback_recommended", "rollback_executed"}:
            return -3.0
        if row.risk_level == "conservative":
            return 1.0
        return -1.5

    @staticmethod
    def _optimization_reasons(
        *,
        row: StrategyProfileComparisonRow,
        shadow_adjustment: float,
        replay_adjustment: float,
        stability_adjustment: float,
        ai_performance_summary: dict[str, Any],
        replay_summary: dict[str, Any],
    ) -> list[str]:
        reasons: list[str] = []
        if row.score >= 0:
            reasons.append("historical_profile_score_positive")
        if shadow_adjustment > 0:
            reasons.append("shadow_guard_prefers_more_defensive_profile")
        elif shadow_adjustment < 0:
            reasons.append("shadow_guard_penalizes_profile")
        if replay_adjustment > 0:
            reasons.append("replay_health_prefers_profile")
        elif replay_adjustment < 0:
            reasons.append("replay_health_penalizes_profile")
        bucket_scores = replay_summary.get("bucket_scores") or {}
        if (bucket_scores.get("symbol") or {}).get("count", 0) > 0:
            reasons.append("replay_symbol_bucket_available")
        if (bucket_scores.get("regime") or {}).get("count", 0) > 0:
            reasons.append("replay_regime_bucket_available")
        if (bucket_scores.get("profile") or {}).get("count", 0) > 0:
            reasons.append("replay_profile_bucket_available")
        cross_bucket = next(
            (
                item
                for item in replay_summary.get("cross_bucket_scores") or []
                if item.get("symbol") == replay_summary.get("target_symbol")
                and item.get("regime") == replay_summary.get("target_regime")
                and item.get("profile_id") == row.profile_id
            ),
            None,
        )
        if cross_bucket and cross_bucket.get("count", 0) > 0:
            reasons.append("replay_symbol_regime_profile_bucket_available")
        if stability_adjustment < 0:
            reasons.append("recent_replay_divergence_penalty")
        if ai_performance_summary.get("review_required"):
            reasons.append("ai_performance_review_required")
        if replay_summary.get("validation_count", 0) == 0:
            reasons.append("replay_history_insufficient")
        return reasons

    def _latest_optimization_report_payload(self) -> dict[str, Any] | None:
        latest = self._latest_optimization_report_event()
        if latest is None or not isinstance(latest.payload, dict):
            return None
        return latest.payload

    def _latest_selection_decision_payload(self) -> dict[str, Any] | None:
        latest = self._latest_selection_decision_event()
        if latest is None or not isinstance(latest.payload, dict):
            return None
        return latest.payload

    def _latest_optimization_report_event(self):
        return self.event_store.latest_by_topic_scoped(
            topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS,
            scope=self.runtime_state_scope,
        )

    def _latest_selection_decision_event(self):
        return self.event_store.latest_by_topic_scoped(
            topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
            scope=self.runtime_state_scope,
        )

    def _latest_optimization_report(self) -> StrategyProfileOptimizationReport | None:
        latest = self._latest_optimization_report_event()
        if latest is None:
            return None
        return StrategyProfileOptimizationReport.model_validate(latest.payload)

    def _latest_selection_decision(self) -> StrategyProfileSelectionDecision | None:
        latest = self._latest_selection_decision_event()
        if latest is None:
            return None
        return StrategyProfileSelectionDecision.model_validate(latest.payload)

    def _build_selection_decision(
        self,
        *,
        state: StrategyProfileActivationState,
        optimization_report: StrategyProfileOptimizationReport,
    ) -> StrategyProfileSelectionDecision:
        previous = self._latest_selection_decision()
        candidate = optimization_report.recommended_profile_id
        if candidate is None:
            status = "insufficient_data"
            execution_state = "not_executed"
            recommended_action = "collect_more_data"
        elif candidate == state.active_profile_id:
            status = "stable_keep_active"
            execution_state = "already_active"
            recommended_action = "keep_current_profile"
        else:
            status = "recommended_not_executed"
            execution_state = "not_executed"
            recommended_action = "review_activate"
        return StrategyProfileSelectionDecision(
            version=1 if previous is None else previous.version + 1,
            report_id=optimization_report.report_id,
            parent_decision_id=None if previous is None else previous.selection_decision_id,
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            active_profile_id=state.active_profile_id,
            candidate_profile_id=candidate,
            rollback_profile_id=state.active_profile_id,
            decision_status=status,
            execution_state=execution_state,
            recommended_action=recommended_action,
            rationale=[
                "selection_based_on_optimization_report",
                "rollback_target_preserved_as_previous_active_profile",
                *optimization_report.notes,
            ],
            replay_guard=optimization_report.replay_summary,
            shadow_guard=optimization_report.ai_performance_summary,
            notes=[
                f"optimization_version={optimization_report.version}",
                f"report_parent={optimization_report.parent_report_id or 'none'}",
            ],
        )

    def _append_selection_decision_transition(
        self,
        *,
        status: str,
        candidate_profile_id: str | None,
        rollback_profile_id: str | None,
        execution_state: str | None = None,
        recommended_action: str | None = None,
        rationale: list[str],
        execution_outcome: dict[str, Any] | None = None,
        auto_rollback_recommendation: dict[str, Any] | None = None,
        notes: list[str],
    ) -> StrategyProfileSelectionDecision:
        previous = self._latest_selection_decision()
        latest_report = self._latest_optimization_report()
        decision = StrategyProfileSelectionDecision(
            version=1 if previous is None else previous.version + 1,
            report_id=(
                latest_report.report_id
                if latest_report is not None
                else (previous.report_id if previous is not None else "unknown")
            ),
            parent_decision_id=None if previous is None else previous.selection_decision_id,
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            active_profile_id=self._activation_state().active_profile_id,
            candidate_profile_id=candidate_profile_id,
            rollback_profile_id=rollback_profile_id,
            decision_status=status,
            execution_state=execution_state or (previous.execution_state if previous is not None else "not_executed"),
            recommended_action=(
                recommended_action
                if recommended_action is not None
                else (previous.recommended_action if previous is not None else None)
            ),
            rationale=rationale,
            replay_guard=(
                latest_report.replay_summary
                if latest_report is not None
                else (previous.replay_guard if previous is not None else {})
            ),
            shadow_guard=(
                latest_report.ai_performance_summary
                if latest_report is not None
                else (previous.shadow_guard if previous is not None else {})
            ),
            execution_outcome=(
                execution_outcome
                if execution_outcome is not None
                else (previous.execution_outcome if previous is not None else {})
            ),
            auto_rollback_recommendation=(
                auto_rollback_recommendation
                if auto_rollback_recommendation is not None
                else (previous.auto_rollback_recommendation if previous is not None else {})
            ),
            notes=notes,
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
                key=decision.candidate_profile_id or decision.active_profile_id or "strategy_profiles",
                payload_model=decision,
                source_component="strategy_profile_service",
            )
        )
        return decision

    @staticmethod
    def _activation_outcome_payload(*, record) -> dict[str, Any]:
        return {
            "activation_event_id": record.activation_event_id,
            "trigger_type": record.trigger_type,
            "result": record.result,
            "reason_code": record.reason_code,
            "executed_at": record.executed_at.isoformat(),
            "from_profile_id": record.from_profile_id,
            "to_profile_id": record.to_profile_id,
        }

    def _write_back_selection_outcome(
        self,
        *,
        state: StrategyProfileActivationState,
        evaluations: list[StrategyProfileEvaluationRecord],
        optimization_report: StrategyProfileOptimizationReport,
    ) -> StrategyProfileSelectionDecision | None:
        active_profile_id = state.active_profile_id
        if not active_profile_id:
            return None
        decision_history = [
            StrategyProfileSelectionDecision.model_validate(item.payload)
            for item in reversed(
                self.event_store.by_topic_scoped(
                    topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
                    scope=self.runtime_state_scope,
                )
            )
            if isinstance(item.payload, dict)
        ]
        latest_decision = next(
            (
                item
                for item in decision_history
                if item.execution_state in {"executed", "rolled_back", "already_active"}
                and (item.candidate_profile_id or item.active_profile_id) == active_profile_id
            ),
            None,
        )
        if latest_decision is None:
            return None
        tracked_profile_id = latest_decision.candidate_profile_id or latest_decision.active_profile_id
        if tracked_profile_id != active_profile_id:
            return None
        current_evaluation = next((item for item in evaluations if item.profile_id == active_profile_id), None)
        if current_evaluation is None:
            return None
        activation_history = self.repo.list_activation_history(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
        )
        latest_activation = next(
            (item for item in reversed(activation_history) if item.to_profile_id == active_profile_id),
            None,
        )
        execution_outcome = {
            "evaluation_id": current_evaluation.evaluation_id,
            "evaluation_status": current_evaluation.status,
            "net_realized_pnl": current_evaluation.net_realized_pnl,
            "fee_to_gross_pnl_ratio": current_evaluation.fee_to_gross_pnl_ratio,
            "small_pnl_churn_ratio": current_evaluation.small_pnl_churn_ratio,
            "trade_count": current_evaluation.trade_count,
            "window_end": current_evaluation.window_end.isoformat() if current_evaluation.window_end else None,
        }
        if latest_activation is not None:
            execution_outcome["activation_event_id"] = latest_activation.activation_event_id
            execution_outcome["trigger_type"] = latest_activation.trigger_type
            execution_outcome["activation_result"] = latest_activation.result
        rollback_target = latest_decision.rollback_profile_id or optimization_report.active_profile_id
        rollback_reasons: list[str] = []
        failure_rules = (optimization_report.winner_selection_policy or {}).get("failure_rollback_rules") or {}
        if bool(failure_rules.get("on_degraded_evaluation", True)) and current_evaluation.status in {"degraded", "rollback_recommended"}:
            rollback_reasons.append(f"evaluation_status_{current_evaluation.status}")
        if bool(failure_rules.get("on_alternative_winner", True)) and optimization_report.recommended_profile_id and optimization_report.recommended_profile_id != active_profile_id:
            rollback_reasons.append("optimization_report_prefers_alternative_profile")
        if bool(failure_rules.get("on_shadow_review_required", True)) and bool(optimization_report.ai_performance_summary.get("review_required")):
            rollback_reasons.append("ai_shadow_guard_review_required")
        auto_rollback_recommendation = {
            "recommended": bool(rollback_reasons and rollback_target and rollback_target != active_profile_id),
            "target_profile_id": rollback_target if rollback_target != active_profile_id else None,
            "reason_codes": rollback_reasons,
            "symbol": self.settings.default_symbol,
            "regime": replay_summary.get("target_regime") if (replay_summary := optimization_report.replay_summary) else None,
            "active_profile_id": active_profile_id,
        }
        next_status = (
            "auto_rollback_recommended"
            if auto_rollback_recommendation["recommended"]
            else "execution_outcome_recorded"
        )
        if (
            latest_decision.decision_status == next_status
            and latest_decision.execution_outcome.get("evaluation_id") == current_evaluation.evaluation_id
            and latest_decision.auto_rollback_recommendation == auto_rollback_recommendation
        ):
            return None
        rationale = ["selection_execution_outcome_written_back", f"evaluation_status_{current_evaluation.status}"]
        recommended_action = "review_rollback" if auto_rollback_recommendation["recommended"] else "keep_observing"
        if auto_rollback_recommendation["recommended"]:
            rationale.append("auto_rollback_advice_generated")
        return self._append_selection_decision_transition(
            status=next_status,
            candidate_profile_id=active_profile_id,
            rollback_profile_id=rollback_target,
            execution_state="rolled_back" if latest_decision.execution_state == "rolled_back" else "executed",
            recommended_action=recommended_action,
            rationale=rationale,
            execution_outcome=execution_outcome,
            auto_rollback_recommendation=auto_rollback_recommendation,
            notes=[f"evaluation_ref={current_evaluation.evaluation_id}"],
        )

    def _maybe_auto_execute_rollback(self, *, decision: StrategyProfileSelectionDecision) -> dict[str, Any] | None:
        recommendation = decision.auto_rollback_recommendation or {}
        policy = self._resolved_auto_rollback_policy()
        if not policy.enabled or not policy.effective or policy.frozen:
            return None
        if not recommendation.get("recommended"):
            return None
        allowed_symbols = set(policy.matrix_allowed_symbols)
        if allowed_symbols and str(recommendation.get("symbol") or self.settings.default_symbol) not in allowed_symbols:
            return None
        allowed_regimes = set(policy.matrix_allowed_regimes)
        if allowed_regimes and str(recommendation.get("regime") or "unknown") not in allowed_regimes:
            return None
        allowed_profiles = set(policy.matrix_allowed_profiles)
        if allowed_profiles and str(recommendation.get("active_profile_id") or "") not in allowed_profiles:
            return None
        if policy.review_required_only and "ai_shadow_guard_review_required" not in (
            recommendation.get("reason_codes") or []
        ):
            return None
        target_profile_id = recommendation.get("target_profile_id")
        if not target_profile_id:
            return None
        state = self._activation_state()
        if state.active_profile_id == target_profile_id:
            return None
        latest_activation = next(
            (
                item
                for item in reversed(
                    self.repo.list_activation_history(
                        product_type=self.settings.trading_product_type,
                        margin_mode=self.settings.margin_mode,
                    )
                )
                if item.to_profile_id == state.active_profile_id
            ),
            None,
        )
        if latest_activation is not None and (
            utc_now() - latest_activation.executed_at
        ).total_seconds() < float(policy.cooldown_seconds):
            return None
        trade_count = int((decision.execution_outcome or {}).get("trade_count") or 0)
        if trade_count < int(policy.min_trade_count):
            return None
        blockers = self._activation_blockers()
        if blockers:
            return None
        revision = self._revision_for_profile(target_profile_id)
        if revision is None:
            return None
        record = self._activate_revision(
            target=revision,
            state=state,
            trigger_type="rollback",
            actor_role="system",
            actor_identity="system_strategy_guard",
            auth_source="local_config",
            recommendation_id=None,
            reason_code="system_auto_rollback_recommendation_executed",
            reason_detail=";".join(recommendation.get("reason_codes") or []),
        )
        self._append_selection_decision_transition(
            status="auto_rollback_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="rolled_back",
            recommended_action="observe_after_rollback",
            rationale=["system_auto_rollback_executed", *(recommendation.get("reason_codes") or [])],
            execution_outcome=self._activation_outcome_payload(record=record),
            auto_rollback_recommendation={
                **recommendation,
                "executed": True,
                "executed_at": record.executed_at.isoformat(),
            },
            notes=["auto_rollback_policy_applied"],
        )
        return {
            "status": "auto_rollback_executed",
            "activation_record": record.model_dump(mode="json"),
            "target_profile_id": revision.profile_id,
            "reason_codes": recommendation.get("reason_codes") or [],
        }

    def _maybe_auto_execute_activation_policy(
        self,
        *,
        optimization_report: StrategyProfileOptimizationReport,
        selection_decision: StrategyProfileSelectionDecision,
    ) -> dict[str, Any] | None:
        policy = self._resolved_activation_policy()
        winner_policy = optimization_report.winner_selection_policy or {}
        candidate_profile_id = optimization_report.recommended_profile_id
        if candidate_profile_id is None:
            return None
        revision = self._revision_for_profile(candidate_profile_id)
        if revision is None:
            return None
        if not policy.enabled or not policy.effective or policy.frozen:
            return None
        state = self._activation_state()
        if state.active_profile_id == candidate_profile_id:
            return None
        blocked_reasons = list(((winner_policy.get("auto_activation") or {}).get("blocked_reasons")) or [])
        if revision.manual_approval_required:
            blocked_reasons.append("strategy_profile_manual_approval_required")
        if not revision.auto_switch_allowed:
            blocked_reasons.append("strategy_profile_auto_switch_not_allowed")
        replay_summary = optimization_report.replay_summary or {}
        target_symbol = str(replay_summary.get("target_symbol") or self.settings.default_symbol)
        target_regime = str(replay_summary.get("target_regime") or "unknown")
        if policy.matrix_allowed_symbols and target_symbol not in set(policy.matrix_allowed_symbols):
            blocked_reasons.append("activation_policy_symbol_not_allowed")
        if policy.matrix_allowed_regimes and target_regime not in set(policy.matrix_allowed_regimes):
            blocked_reasons.append("activation_policy_regime_not_allowed")
        if policy.matrix_allowed_profiles and candidate_profile_id not in set(policy.matrix_allowed_profiles):
            blocked_reasons.append("activation_policy_profile_not_allowed")
        blockers = self._activation_blockers()
        blocked_reasons.extend(blockers)
        if blocked_reasons:
            if selection_decision.candidate_profile_id != candidate_profile_id or (
                selection_decision.decision_status != "winner_policy_recommended_not_executed"
            ):
                self._append_selection_decision_transition(
                    status="winner_policy_recommended_not_executed",
                    candidate_profile_id=candidate_profile_id,
                    rollback_profile_id=state.active_profile_id,
                    execution_state="not_executed",
                    recommended_action="review_activation_policy",
                    rationale=["winner_selection_policy_not_executed", *blocked_reasons],
                    notes=[f"activation_policy_id={policy.policy_id}"],
                )
            return {
                "status": "blocked",
                "candidate_profile_id": candidate_profile_id,
                "policy_id": policy.policy_id,
                "blocked_reasons": blocked_reasons,
            }
        record = self._activate_revision(
            target=revision,
            state=state,
            trigger_type="system_guard",
            actor_role="system",
            actor_identity="system_profile_activation_policy",
            auth_source="local_config",
            recommendation_id=None,
            reason_code="winner_selection_policy_auto_activation",
            reason_detail=f"winner={candidate_profile_id};policy={policy.policy_id}",
        )
        self._append_selection_decision_transition(
            status="winner_policy_auto_activation_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="executed",
            recommended_action="observe_outcome",
            rationale=["winner_selection_policy_auto_activated", f"policy_id={policy.policy_id}"],
            execution_outcome=self._activation_outcome_payload(record=record),
            notes=[f"winner_selection_policy={winner_policy.get('policy_version') or 'unknown'}"],
        )
        return {
            "status": "winner_policy_auto_activation_executed",
            "policy_id": policy.policy_id,
            "activation_record": record.model_dump(mode="json"),
            "candidate_profile_id": revision.profile_id,
        }

    @staticmethod
    def _axis_rank(level: str | None) -> int:
        return _PROFILE_AXIS_LEVEL_ORDER.get(str(level or "").lower(), 0)

    @staticmethod
    def _clamp_float(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))

    def _profile_intent_fit_score(
        self,
        *,
        revision: StrategyProfileRevision,
        context: dict[str, Any],
        performance: dict[str, Any],
        shadow_summary: dict[str, Any],
    ) -> float:
        baseline = context.get("baseline") or {}
        features = context.get("features") or {}
        execution_health = context.get("execution_health") or {}
        regime = str(baseline.get("regime") or features.get("regime_indicator") or "uncertain")
        volatility_state = str(baseline.get("volatility_state") or features.get("volatility_state") or "unknown")
        composite_alpha = float(baseline.get("composite_alpha_score", features.get("composite_alpha_score", 0.0)) or 0.0)
        fee_ratio = float(performance.get("fee_to_gross_pnl_ratio", 0.0) or 0.0)
        execution_errors = int(execution_health.get("recent_execution_error_count", 0) or 0)
        latest_net_delta = float(shadow_summary.get("latest_net_pnl_delta") or 0.0)
        latest_fee_delta = float(shadow_summary.get("latest_fee_ratio_delta") or 0.0)
        latest_churn_delta = float(shadow_summary.get("latest_churn_ratio_delta") or 0.0)

        score = 0.0
        if execution_errors >= 3:
            score += 1.5 if revision.market_intent == "execution_degraded" else -0.6
        if volatility_state == "high":
            score += 1.2 if revision.market_intent == "high_volatility" else (-0.3 if revision.market_intent == "trend" else 0.0)
        elif regime in {"range", "uncertain"}:
            score += 1.0 if revision.market_intent == "range" else (-0.25 if revision.market_intent == "trend" else 0.0)
        else:
            score += 1.0 if revision.market_intent == "trend" else 0.0
            if revision.profile_id == "trend_strict" and 0.12 <= abs(composite_alpha) <= 0.22:
                score += 0.35
            if revision.profile_id == "trend_normal" and abs(composite_alpha) >= 0.22:
                score += 0.2

        if fee_ratio >= 0.5 or latest_fee_delta > 0:
            score += 0.35 if revision.market_intent in {"range", "execution_degraded", "high_volatility"} else -0.15
        if latest_churn_delta > 0 or latest_net_delta < 0:
            score += 0.45 if revision.market_intent in {"range", "execution_degraded", "high_volatility"} else -0.2
        return round(score, 6)

    @staticmethod
    def _risk_rank(level: str | None) -> int:
        return _RISK_LEVEL_ORDER.get(str(level or "").lower(), 99)

    def _transition_risk_direction(
        self,
        *,
        current: StrategyProfileRevision | None,
        target: StrategyProfileRevision,
    ) -> str:
        if current is None:
            return "same_risk"
        current_rank = self._risk_rank(current.risk_level)
        target_rank = self._risk_rank(target.risk_level)
        if target_rank < current_rank:
            return "more_conservative"
        if target_rank > current_rank:
            return "more_aggressive"
        return "same_risk"

    def _auto_switch_confidence_min(
        self,
        *,
        current: StrategyProfileRevision | None,
        target: StrategyProfileRevision,
    ) -> float:
        transition = self._transition_risk_direction(current=current, target=target)
        if transition == "more_conservative":
            return _AUTO_SWITCH_CONFIDENCE_MIN_CONSERVATIVE
        if transition == "more_aggressive":
            return _AUTO_SWITCH_CONFIDENCE_MIN_AGGRESSIVE
        return _AUTO_SWITCH_CONFIDENCE_MIN_SAME_RISK

    def _activation_blockers(self) -> list[str]:
        open_orders = self.runtime.execution_repo.order_states_for_scope(
            scope=self.runtime_state_scope,
            open_only=True,
        )
        return ["strategy_profile_open_orders_present"] if open_orders else []

    def _recommendation_validation(self, recommendation: StrategyProfileRecommendation) -> dict[str, Any]:
        active = self._activation_state()
        revision = self._revision_for_profile(recommendation.recommended_profile_id)
        current = self._revision(active.active_revision_id)
        safety_state = self._safety_state()
        blocked_reasons: list[str] = []
        transition = "unknown"
        if revision is None:
            blocked_reasons.append("strategy_profile_revision_missing")
        else:
            transition = self._transition_risk_direction(current=current, target=revision)
        if recommendation.expires_at <= utc_now():
            blocked_reasons.append("strategy_profile_recommendation_expired")
        if recommendation.recommended_profile_id == active.active_profile_id:
            blocked_reasons.append("strategy_profile_already_active")
        if active.frozen_until is not None and active.frozen_until > utc_now():
            blocked_reasons.append("strategy_profile_auto_switch_frozen")
        if active.cooldown_until is not None and active.cooldown_until > utc_now():
            blocked_reasons.append("strategy_profile_switch_cooldown_active")
        if not active.auto_switch_enabled:
            blocked_reasons.append("strategy_profile_auto_switch_disabled")
        if revision is not None:
            confidence_floor = self._auto_switch_confidence_min(current=current, target=revision)
            if recommendation.confidence < confidence_floor:
                if transition == "more_aggressive":
                    blocked_reasons.append("strategy_profile_auto_switch_aggressive_confidence_too_low")
                elif transition == "same_risk":
                    blocked_reasons.append("strategy_profile_auto_switch_same_risk_confidence_too_low")
                else:
                    blocked_reasons.append("strategy_profile_auto_switch_confidence_too_low")
        if not safety_state["safe_to_trade"]:
            blocked_reasons.append("strategy_profile_runtime_not_safe_to_trade")
        if safety_state["review_required"]:
            blocked_reasons.append("strategy_profile_review_required")
        if not safety_state["market_snapshot_fresh"]:
            blocked_reasons.append("strategy_profile_market_data_stale")
        if not safety_state["account_snapshot_fresh"]:
            blocked_reasons.append("strategy_profile_account_state_stale")
        if safety_state["reconciliation_halt_required"] or safety_state["reconciliation_review_required"]:
            blocked_reasons.append("strategy_profile_reconciliation_not_clean")
        elif str(safety_state["reconciliation_severity"]).upper() not in {"CLEAN", "UNKNOWN"}:
            blocked_reasons.append("strategy_profile_reconciliation_not_clean")
        if revision is not None and not revision.auto_switch_allowed:
            blocked_reasons.append("strategy_profile_auto_switch_not_allowed")
        if revision is not None and revision.manual_approval_required:
            blocked_reasons.append("strategy_profile_manual_approval_required")
        blocked_reasons.extend(self._activation_blockers())
        return {
            "auto_apply_allowed": not blocked_reasons,
            "blocked_reasons": blocked_reasons,
            "requires_manual_approval": bool(blocked_reasons),
            "transition_risk_direction": transition,
        }

    def _auto_apply_recommendation(self, *, recommendation: StrategyProfileRecommendation) -> dict[str, Any]:
        state = self._activation_state()
        revision = self._revision_for_profile(recommendation.recommended_profile_id)
        current = self._revision(state.active_revision_id)
        if revision is None:
            raise ValueError("strategy_profile_revision_missing")
        transition = self._transition_risk_direction(current=current, target=revision)
        if transition == "more_aggressive":
            reason_code = "ai_recommended_more_aggressive_profile"
        elif transition == "same_risk":
            reason_code = "ai_recommended_same_risk_profile"
        else:
            reason_code = "ai_recommended_more_conservative_profile"
        record = self._activate_revision(
            target=revision,
            state=state,
            trigger_type="ai_auto",
            actor_role="system",
            actor_identity="system_ai",
            auth_source="local_config",
            recommendation_id=recommendation.recommendation_id,
            reason_code=reason_code,
            reason_detail=recommendation.human_summary,
        )
        updated = recommendation.model_copy(
            update={
                "decision_status": "accepted",
                "decision_reason_code": "auto_applied",
                "decision_reason_detail": record.reason_code,
            }
        )
        self.repo.save_recommendation(updated)
        self._append_selection_decision_transition(
            status="auto_activation_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="executed",
            recommended_action="observe_outcome",
            rationale=["ai_auto_applied_recommendation", reason_code],
            execution_outcome=self._activation_outcome_payload(record=record),
            notes=[recommendation.human_summary],
        )
        return {
            "status": "auto_applied",
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self._revision_view(revision),
            "recommendation": updated.model_dump(mode="json"),
        }

    def _activate_revision(
        self,
        *,
        target: StrategyProfileRevision,
        state: StrategyProfileActivationState,
        trigger_type: str,
        actor_role: str,
        actor_identity: str | None,
        auth_source: str,
        recommendation_id: str | None,
        reason_code: str,
        reason_detail: str,
    ) -> StrategyProfileActivationRecord:
        previous = self._revision(state.active_revision_id)
        previous_payload = previous.payload if previous is not None else strategy_profile_payload_from_settings(self.settings)
        if previous is not None and previous.revision_id != target.revision_id:
            self.repo.save_revision(previous.model_copy(update={"status": "superseded", "updated_at": utc_now()}))
        target = target.model_copy(update={"status": "active", "updated_at": utc_now()})
        self.repo.save_revision(target)
        apply_strategy_profile_payload(self.settings, target.payload)
        next_state = state.model_copy(
            update={
                "previous_active_revision_id": state.active_revision_id,
                "active_revision_id": target.revision_id,
                "active_profile_id": target.profile_id,
                "pending_revision_id": None,
                "pending_profile_id": None,
                "activation_mode": (
                    "manual"
                    if trigger_type == "manual"
                    else "auto" if trigger_type == "ai_auto" else "rollback"
                ),
                "last_activation_result": (
                    "rollback_succeeded" if trigger_type == "rollback" else "activation_succeeded"
                ),
                "last_activation_at": utc_now(),
                "last_activation_error": None,
                "last_switch_reason": reason_code,
                "last_switch_actor": actor_identity,
                "cooldown_until": utc_now() + timedelta(minutes=120),
            }
        )
        self.repo.save_activation_state(next_state)
        record = StrategyProfileActivationRecord(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
            from_revision_id=previous.revision_id if previous is not None else None,
            to_revision_id=target.revision_id,
            from_profile_id=previous.profile_id if previous is not None else None,
            to_profile_id=target.profile_id,
            trigger_type=trigger_type,  # type: ignore[arg-type]
            actor_identity=actor_identity,
            actor_role=actor_role,
            auth_source=auth_source,
            recommendation_id=recommendation_id,
            result="rolled_back" if trigger_type == "rollback" else "succeeded",
            reason_code=reason_code,
            reason_detail=reason_detail,
            hot_safe=True,
            restart_required=False,
            diff=diff_strategy_profile_payload(previous_payload, target.payload),
        )
        self.repo.save_activation_record(record)
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_ACTIVATIONS,
                key=target.profile_id,
                payload_model=record,
                source_component="strategy_profile_service",
            )
        )
        return record

    def _reject_recommendation_record(
        self,
        *,
        recommendation: StrategyProfileRecommendation,
        source: str,
        reason_code: str,
        reason_detail: str | None,
        actor_identity: str | None,
        actor_role: str,
    ) -> StrategyProfileRejectionRecord:
        record = StrategyProfileRejectionRecord(
            product_type=self.settings.trading_product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.allowed_symbols,
            recommendation_id=recommendation.recommendation_id,
            recommended_profile_id=recommendation.recommended_profile_id,
            rejection_source=source,
            rejection_reason_code=reason_code,
            rejection_reason_detail=reason_detail,
            actor_identity=actor_identity,
            actor_role=actor_role,
        )
        self.repo.save_rejection(record)
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_REJECTIONS,
                key=recommendation.recommended_profile_id,
                payload_model=record,
                source_component="strategy_profile_service",
            )
        )
        return record
