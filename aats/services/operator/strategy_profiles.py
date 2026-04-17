from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from aats.bootstrap.logging import get_logger
from aats.data_platform.governance._time_util import parse_iso_datetime_utc
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.decision import ProfileControlDecision
from aats.schemas.operator import AuthSource, OperatorActionRecord, OperatorRole
from aats.schemas.strategy_profiles import (
    StrategyProfileAIAdvice,
    StrategyProfileComparisonReport,
    StrategyProfileComparisonRow,
    StrategyProfileEvaluationContextSnapshot,
    StrategyProfileActivationRecord,
    StrategyProfileActivationState,
    StrategyProfileEvaluationRecord,
    StrategyProfileRecommendation,
    StrategyProfileRejectionRecord,
    StrategyProfileRevision,
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
from aats.services.operator.strategy_profile_optimization import (
    build_comparison_report,
    build_offline_replay_pipeline,
    build_optimization_report,
    recent_replay_summary,
    shadow_summary_for_profiles,
)
from aats.services.operator.strategy_profile_policies import (
    activation_policy_history,
    activation_policy_history_payload,
    activation_policy_view,
    approve_activation_policy as approve_activation_policy_helper,
    approve_auto_rollback_policy as approve_auto_rollback_policy_helper,
    auto_rollback_policy_history,
    auto_rollback_policy_history_payload,
    auto_rollback_policy_view,
    freeze_activation_policy as freeze_activation_policy_helper,
    freeze_auto_rollback_policy as freeze_auto_rollback_policy_helper,
    resolved_activation_policy,
    resolved_auto_rollback_policy,
    staged_activation_policy_view,
    staged_auto_rollback_policy_view,
    stored_activation_policy,
    stored_auto_rollback_policy,
    update_activation_policy as update_activation_policy_helper,
    update_auto_rollback_policy as update_auto_rollback_policy_helper,
)
from aats.services.operator.strategy_profile_seed import seed_strategy_profiles
from aats.services.operator.strategy_profile_snapshot import (
    build_strategy_profile_ai_config_snapshot,
    build_strategy_profile_snapshot,
)
from aats.services.operator.strategy_profile_context import StrategyProfileContextFacade
from aats.services.operator.strategy_profile_activation import StrategyProfileActivationFacade
from aats.services.operator.strategy_profile_recommendations import StrategyProfileRecommendationFacade
from aats.services.governance_engine.adaptive_controls import (
    reconciliation_clean_from_safety_state,
    resolve_execution_aggressiveness_state,
    resolve_risk_budget_state,
)
from aats.services.strategy_engines.sleeve_execution_permission import non_protective_entry_execution_guard
from aats.storage.base import EventStore

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
        self.evaluation_window_limit = _EVALUATION_WINDOW_LIMIT
        self.context = StrategyProfileContextFacade(self)
        self.activation = StrategyProfileActivationFacade(self)
        self.recommendations = StrategyProfileRecommendationFacade(self)

    def ensure_seed_profiles(self) -> None:
        seed_strategy_profiles(settings=self.settings, repo=self.repo)

    @property
    def runtime_state_scope(self):
        from aats.services.runtime_scope import runtime_state_scope

        return runtime_state_scope(self.settings)

    def snapshot(self) -> dict[str, Any]:
        return build_strategy_profile_snapshot(self)

    def ai_config_snapshot(self) -> dict[str, Any]:
        return build_strategy_profile_ai_config_snapshot(self)

    async def evaluate_now(self, *, allow_auto_activation: bool = True) -> dict[str, Any]:
        # 原本 evaluate_now 在 event loop 主线程上顺序跑：
        #   1) ensure_seed_profiles + repo.list_revisions
        #   2) N 次 repo.save_evaluation + event_store.append（N = revisions 数）
        #   3) 3 次 event_store.append（comparison / optimization / selection）
        #   4) 1 次 repo.save_recommendation
        #   5) await _generate_recommendation (async, OK)
        #   6) 再次 _activation_state() / _write_back_selection_outcome
        # 2)~4) 是 N+5 次同步 DB 写入（每次 SELECT + INSERT + COMMIT）。策略
        # profile 自动评估在 decision cycle 外也会被触发（manual + scheduled），
        # 单次 evaluate_now 在 event loop 里跑需要数百毫秒到秒级。全部丢线程池
        # 让 HTTP handler 可以在此期间被调度。
        #
        # 结构：拆成 phase1 (pre-AI sync DB) + AI 异步调用 + phase3 (post-AI sync DB)。
        # 两段 sync 各自包装一次 asyncio.to_thread。trigger.py 的
        # `_timeframe_locks` / operator 触发锁已经保证不会有同服务对同 key 并发
        # 跑 evaluate_now，所以这两段 thread 不会交叉，不存在 state 竞态。
        phase1 = await asyncio.to_thread(self._evaluate_now_phase1_sync)
        ai_recommendation = await self._generate_recommendation(context=phase1["context"])
        result = await asyncio.to_thread(
            self._evaluate_now_phase3_sync,
            phase1=phase1,
            ai_recommendation=ai_recommendation,
            allow_auto_activation=allow_auto_activation,
        )
        return result

    def _evaluate_now_phase1_sync(self) -> dict[str, Any]:
        """同步跑的 Phase 1：seed + context + evaluation pipeline + 3 份 report 入库。

        返回的 dict 把所有后续 phase 3 需要的对象一并带出来，避免跨线程再次读取
        self 里可能被改动的状态。
        """
        self.ensure_seed_profiles()
        context_snapshot = self._tuning_context()
        context = self._context_payload(context_snapshot)
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
            context_snapshot=context_snapshot,
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS,
                key=state.active_profile_id or "strategy_profiles",
                payload_model=optimization_report,
                source_component="strategy_profile_service",
            )
        )
        return {
            "context_snapshot": context_snapshot,
            "context": context,
            "state": state,
            "evaluations": evaluations,
            "current_evaluation": current_evaluation,
            "comparison_report": comparison_report,
            "optimization_report": optimization_report,
        }

    def _evaluate_now_phase3_sync(
        self,
        *,
        phase1: dict[str, Any],
        ai_recommendation: StrategyProfileRecommendation,
        allow_auto_activation: bool,
    ) -> dict[str, Any]:
        """同步跑的 Phase 3：归一化 recommendation + save + 自动激活 + 写回。

        所有 state 对象都由 phase1 的返回值传入，避免 thread 里再去读 self。
        """
        context_snapshot = phase1["context_snapshot"]
        state: StrategyProfileActivationState = phase1["state"]
        evaluations: list[StrategyProfileEvaluationRecord] = phase1["evaluations"]
        comparison_report: StrategyProfileComparisonReport = phase1["comparison_report"]
        optimization_report: StrategyProfileOptimizationReport = phase1["optimization_report"]
        current_evaluation = phase1["current_evaluation"]
        recommendation = self._build_normalized_recommendation(
            context_snapshot=context_snapshot,
            optimization_report=optimization_report,
            ai_recommendation=ai_recommendation,
        )
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
        selection_decision = self._build_selection_decision(
            state=state,
            optimization_report=optimization_report,
            activation_decision=validation,
        )
        self.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
                key=selection_decision.candidate_profile_id or state.active_profile_id or "strategy_profiles",
                payload_model=selection_decision,
                source_component="strategy_profile_service",
            )
        )
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
        outcome_decision = self._write_back_selection_outcome(
            state=self._activation_state(),
            evaluations=evaluations,
            optimization_report=optimization_report,
        )
        if outcome_decision is not None:
            result["selection_decision"] = outcome_decision.model_dump(mode="json")
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
        return self.activation.accept_recommendation(
            recommendation_id=recommendation_id,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            activation_mode=activation_mode,
            reason=reason,
        )

    def reject_recommendation(
        self,
        *,
        recommendation_id: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        reason_code: str,
        reason_detail: str | None,
    ) -> dict[str, Any]:
        return self.activation.reject_recommendation(
            recommendation_id=recommendation_id,
            actor_role=actor_role,
            actor_identity=actor_identity,
            reason_code=reason_code,
            reason_detail=reason_detail,
        )

    def activate_pending(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        reason: str,
    ) -> dict[str, Any]:
        return self.activation.activate_pending(
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
        )

    def activate_profile(
        self,
        *,
        profile_id: str,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        reason: str,
    ) -> dict[str, Any]:
        return self.activation.activate_profile(
            profile_id=profile_id,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
        )

    def restore_auto(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        reason: str,
    ) -> dict[str, Any]:
        return self.activation.restore_auto(
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
        )

    def pause_auto(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        reason: str,
    ) -> dict[str, Any]:
        return self.activation.pause_auto(
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
        )

    def rollback(
        self,
        *,
        actor_role: OperatorRole,
        actor_identity: str | None,
        auth_source: AuthSource,
        reason: str,
    ) -> dict[str, Any]:
        return self.activation.rollback(
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            reason=reason,
        )

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

    async def evaluate_mainline_profile_control(self, *, decision_id: str) -> ProfileControlDecision | None:
        return await self.activation.evaluate_mainline_profile_control(decision_id=decision_id)

    def _tuning_context(self) -> StrategyProfileEvaluationContextSnapshot:
        return self.context.tuning_context()

    @staticmethod
    def _context_payload(snapshot: StrategyProfileEvaluationContextSnapshot) -> dict[str, Any]:
        return StrategyProfileContextFacade.context_payload(snapshot)

    @staticmethod
    def _dedupe_items(items: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            normalized = str(item or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _resolved_context_signals(self, context: dict[str, Any]) -> dict[str, Any]:
        return self.context.resolved_context_signals(context)

    def _safety_profile_ids(self) -> set[str]:
        return {str(item).strip() for item in self.settings.strategy_profile_safety_profiles if str(item).strip()}

    def _is_safety_profile_id(self, profile_id: str | None) -> bool:
        return bool(profile_id and profile_id in self._safety_profile_ids())

    def _safety_profile_required(self, *, context: dict[str, Any]) -> bool:
        safety_state = context.get("safety_state") or {}
        execution_health = context.get("execution_health") or {}
        execution_errors = int(execution_health.get("recent_execution_error_count", 0) or 0)
        return bool(
            execution_errors >= int(self.settings.strategy_profile_safety_trigger_execution_error_count)
            or not bool(safety_state.get("safe_to_trade", True))
            or bool(safety_state.get("review_required", False))
            or bool(safety_state.get("reconciliation_halt_required", False))
            or bool(safety_state.get("reconciliation_review_required", False))
            or not bool(safety_state.get("market_snapshot_fresh", True))
            or not bool(safety_state.get("account_snapshot_fresh", True))
        )

    def _profile_control_evidence_state(
        self,
        *,
        context: dict[str, Any],
        replay_summary: dict[str, Any],
    ) -> dict[str, Any]:
        performance = context.get("performance") or {}
        closed_trades = int(performance.get("trade_count", 0) or 0)
        replay_validations = int(replay_summary.get("validation_count", 0) or 0)
        min_closed_trades = int(self.settings.strategy_profile_auto_switch_min_closed_trades)
        min_replay_validations = int(self.settings.strategy_profile_auto_switch_min_replay_validations)
        trade_requirement_met = closed_trades >= min_closed_trades
        replay_requirement_met = replay_validations >= min_replay_validations
        cold_start_active = bool(
            self.settings.strategy_profile_cold_start_lock_enabled
            and (not trade_requirement_met or not replay_requirement_met)
        )
        return {
            "closed_trades": closed_trades,
            "replay_validations": replay_validations,
            "min_closed_trades": min_closed_trades,
            "min_replay_validations": min_replay_validations,
            "trade_requirement_met": trade_requirement_met,
            "replay_requirement_met": replay_requirement_met,
            "cold_start_active": cold_start_active,
        }

    def _profile_control_summary(
        self,
        *,
        context: dict[str, Any],
        replay_summary: dict[str, Any],
        active_profile_id: str | None,
    ) -> dict[str, Any]:
        evidence = self._profile_control_evidence_state(context=context, replay_summary=replay_summary)
        adaptive_controls = self._adaptive_control_summary(context=context)
        return {
            "active_profile_id": active_profile_id,
            "safety_profile_ids": sorted(self._safety_profile_ids()),
            "safety_profile_required": self._safety_profile_required(context=context),
            "emergency_safety_fast_track_enabled": bool(
                self.settings.strategy_profile_emergency_safety_fast_track_enabled
            ),
            "evidence": evidence,
            "adaptive_controls": adaptive_controls,
            "entry_execution_guard": non_protective_entry_execution_guard(self.settings),
        }

    def _adaptive_control_summary(self, *, context: dict[str, Any]) -> dict[str, Any]:
        safety_state = context.get("safety_state") or {}
        execution_health = context.get("execution_health") or {}
        live_guard = safety_state.get("live_guard") or {}
        trial_guard = safety_state.get("trial_guard") or {}
        projected_margin_usage = live_guard.get("projected_margin_usage_fraction")
        if projected_margin_usage is None:
            projected_margin_usage = live_guard.get("projected_margin_usage")
        risk_budget = resolve_risk_budget_state(
            self.settings,
            execution_error_count=int(execution_health.get("recent_execution_error_count") or 0),
            safe_to_trade=bool(safety_state.get("safe_to_trade", True)),
            review_required=bool(safety_state.get("review_required", False)),
            market_snapshot_fresh=bool(safety_state.get("market_snapshot_fresh", True)),
            account_snapshot_fresh=bool(safety_state.get("account_snapshot_fresh", True)),
            reconciliation_clean=reconciliation_clean_from_safety_state(safety_state),
            only_reduce_required=bool(safety_state.get("only_reduce_required")),
            auto_halt_required=bool(safety_state.get("auto_halt_required")),
            trial_guard_breached=bool(safety_state.get("trial_guard_breached")),
            current_margin_usage_fraction=live_guard.get("current_initial_margin_usage_fraction"),
            projected_margin_usage_fraction=projected_margin_usage,
            nearest_liquidation_gap_ratio=live_guard.get("nearest_liquidation_gap_ratio"),
        )
        execution_aggressiveness = resolve_execution_aggressiveness_state(
            self.settings,
            execution_error_count=int(execution_health.get("recent_execution_error_count") or 0),
            safe_to_trade=bool(safety_state.get("safe_to_trade", True)),
            review_required=bool(safety_state.get("review_required", False)),
            market_snapshot_fresh=bool(safety_state.get("market_snapshot_fresh", True)),
            account_snapshot_fresh=bool(safety_state.get("account_snapshot_fresh", True)),
            reconciliation_clean=reconciliation_clean_from_safety_state(safety_state),
            only_reduce_required=bool(safety_state.get("only_reduce_required")),
            auto_halt_required=bool(safety_state.get("auto_halt_required")),
            trial_guard_breached=bool(safety_state.get("trial_guard_breached")),
            current_margin_usage_fraction=live_guard.get("current_initial_margin_usage_fraction"),
            projected_margin_usage_fraction=projected_margin_usage,
            nearest_liquidation_gap_ratio=live_guard.get("nearest_liquidation_gap_ratio"),
        )
        return {
            "risk_budget": risk_budget,
            "execution_aggressiveness": execution_aggressiveness,
            "live_guard_status": live_guard.get("status"),
            "trial_guard_status": trial_guard.get("status"),
        }

    def _candidate_for_profile(
        self,
        *,
        optimization_report: StrategyProfileOptimizationReport,
        profile_id: str | None,
    ) -> StrategyProfileOptimizationCandidate | None:
        if not profile_id:
            return None
        return next((item for item in optimization_report.candidates if item.profile_id == profile_id), None)

    def _ai_advice_from_recommendation(
        self,
        *,
        recommendation: StrategyProfileRecommendation,
        candidate_profile_id: str | None,
    ) -> StrategyProfileAIAdvice:
        agreement = bool(candidate_profile_id and recommendation.recommended_profile_id == candidate_profile_id)
        confidence_adjustment = 0.05 if agreement else -0.08
        return StrategyProfileAIAdvice(
            provider=recommendation.generated_by,
            model_name=recommendation.model_name,
            preferred_profile_id=recommendation.recommended_profile_id,
            confidence=float(recommendation.confidence),
            agreement_with_candidate=agreement,
            confidence_adjustment=confidence_adjustment,
            market_regime_assessment=recommendation.market_regime_assessment,
            reason_codes=list(recommendation.reason_codes),
            risk_notes=list(recommendation.risk_notes),
            summary=recommendation.human_summary,
            fallback_reason_code=recommendation.fallback_reason_code,
            fallback_reason_detail=recommendation.fallback_reason_detail,
            used_fallback=recommendation.generated_by == "rule_fallback",
        )

    def _normalized_candidate_confidence(
        self,
        *,
        candidate: StrategyProfileOptimizationCandidate | None,
        ai_advice: StrategyProfileAIAdvice,
        candidate_profile_id: str | None,
    ) -> float:
        if candidate_profile_id and ai_advice.preferred_profile_id == candidate_profile_id:
            return self._clamp_float(float(ai_advice.confidence), 0.0, 1.0)
        base = 0.68 if ai_advice.used_fallback else 0.74
        if candidate is not None and candidate.composite_score > 0:
            base += min(candidate.composite_score / 100.0, 0.12)
        base += ai_advice.confidence_adjustment
        return self._clamp_float(base, 0.0, 0.99)

    def _build_normalized_recommendation(
        self,
        *,
        context_snapshot: StrategyProfileEvaluationContextSnapshot,
        optimization_report: StrategyProfileOptimizationReport,
        ai_recommendation: StrategyProfileRecommendation,
    ) -> StrategyProfileRecommendation:
        return self.recommendations.build_normalized_recommendation(
            context_snapshot=context_snapshot,
            optimization_report=optimization_report,
            ai_recommendation=ai_recommendation,
        )

    def _performance_summary(self) -> dict[str, Any]:
        return self.context.performance_summary()

    def _safety_state(self) -> dict[str, Any]:
        return self.context.safety_state()

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
            window_start=utc_now() if performance["window_start"] is None else parse_iso_datetime_utc(
                performance["window_start"], context="strategy_profiles.performance.window_start"
            ),
            window_end=utc_now() if performance["window_end"] is None else parse_iso_datetime_utc(
                performance["window_end"], context="strategy_profiles.performance.window_end"
            ),
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
        active_axes = strategy_profile_axes_from_payload(
            active_payload,
            product_type=active_revision.product_type if active_revision is not None else self.settings.trading_product_type,
        )
        candidate_axes = strategy_profile_axes_from_payload(
            revision.payload,
            product_type=revision.product_type,
        )
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
        return await self.recommendations.generate_recommendation(context=context)

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
        signals = self._resolved_context_signals(context)
        regime = signals["regime"]
        volatility_state = signals["volatility_state"]
        composite_alpha = float(signals["composite_alpha_score"] or 0.0)
        regime_confidence = float(signals["confidence"] or 0.0)
        direction_bias = signals["direction_bias"]
        suggested_position_scale = float(signals["suggested_position_scale"] or 0.0)
        fee_ratio = float(context.get("performance", {}).get("fee_to_gross_pnl_ratio", 0.0) or 0.0)
        churn_ratio = float(context.get("performance", {}).get("small_pnl_churn_ratio", 0.0) or 0.0)
        execution_errors = int(context.get("execution_health", {}).get("recent_execution_error_count", 0) or 0)
        safety_state = context.get("safety_state") or {}
        safe_to_trade = bool(safety_state.get("safe_to_trade", True))
        review_required = bool(safety_state.get("review_required", False))
        fallback_rules = [
            {
                "profile_id": "execution_degraded_safe",
                "when": execution_errors >= 3 or not safe_to_trade or review_required,
                "summary": "Execution or runtime safety is degraded; use the safety profile.",
                "reasons": ["execution_errors_elevated", "runtime_safety_degraded"],
                "confidence": 0.9,
            },
            {
                "profile_id": "high_volatility_defensive",
                "when": volatility_state == "high" and (abs(composite_alpha) < 0.45 or regime in {"uncertain", "range"}),
                "summary": "Volatility is elevated; use the high-volatility defensive profile.",
                "reasons": ["high_volatility_detected"],
                "confidence": 0.84,
            },
            {
                "profile_id": "range_defensive",
                "when": regime in {"range", "uncertain"} or direction_bias == "flat" or fee_ratio >= 0.45 or churn_ratio >= 0.55 or abs(composite_alpha) < 0.14,
                "summary": "Range behavior or fee churn is dominant; reduce activity with the range defensive profile.",
                "reasons": ["range_regime_detected", "fee_churn_elevated"],
                "confidence": 0.82,
            },
            {
                "profile_id": "trend_aggressive",
                "when": regime in {"trend", "breakout"} and direction_bias != "flat" and composite_alpha >= 0.55 and regime_confidence >= 0.78 and fee_ratio <= 0.3 and execution_errors == 0 and suggested_position_scale >= 0.25,
                "summary": "Trend evidence is strong; the aggressive trend profile is the best fit.",
                "reasons": ["trend_signal_supported", "trend_signal_strong"],
                "confidence": 0.86,
            },
            {
                "profile_id": "trend_normal",
                "when": regime in {"trend", "breakout"} and direction_bias != "flat" and composite_alpha >= 0.24 and regime_confidence >= 0.62,
                "summary": "Trend evidence is healthy; use the standard trend profile.",
                "reasons": ["trend_signal_supported"],
                "confidence": 0.76,
            },
            {
                "profile_id": "trend_strict",
                "when": regime in {"trend", "breakout"},
                "summary": "Trend exists but conviction is moderate; use the stricter trend profile.",
                "reasons": ["trend_signal_moderate"],
                "confidence": 0.72,
            },
        ]
        selected_rule = next((item for item in fallback_rules if item["when"]), fallback_rules[2])
        recommended = str(selected_rule["profile_id"])
        summary = str(selected_rule["summary"])
        reasons = list(selected_rule["reasons"])
        confidence = float(selected_rule["confidence"])

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
            "axes": strategy_profile_axes_from_payload(
                revision.payload,
                product_type=revision.product_type,
            ).model_dump(mode="json"),
            "payload_summary": summarize_strategy_profile_payload(
                revision.payload,
                product_type=revision.product_type,
            ),
            "summary": diff_strategy_profile_payload(
                strategy_profile_payload_from_settings(self.settings),
                revision.payload,
                product_type=revision.product_type,
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
        return auto_rollback_policy_view(self)

    def _staged_auto_rollback_policy_view(self) -> dict[str, Any] | None:
        return staged_auto_rollback_policy_view(self)

    def _stored_auto_rollback_policy(self) -> StrategyProfileAutoRollbackPolicyConfig | None:
        return stored_auto_rollback_policy(self)

    def _auto_rollback_policy_history(self, *, limit: int | None = None) -> list[StrategyProfileAutoRollbackPolicyConfig]:
        return auto_rollback_policy_history(self, limit=limit)

    def _auto_rollback_policy_history_payload(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return auto_rollback_policy_history_payload(self, limit=limit)

    def _resolved_auto_rollback_policy(self) -> StrategyProfileAutoRollbackPolicyConfig:
        return resolved_auto_rollback_policy(self)

    def _activation_policy_view(self) -> dict[str, Any]:
        return activation_policy_view(self)

    def _staged_activation_policy_view(self) -> dict[str, Any] | None:
        return staged_activation_policy_view(self)

    def _stored_activation_policy(self) -> StrategyProfileActivationPolicyConfig | None:
        return stored_activation_policy(self)

    def _activation_policy_history(self, *, limit: int | None = None) -> list[StrategyProfileActivationPolicyConfig]:
        return activation_policy_history(self, limit=limit)

    def _activation_policy_history_payload(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        return activation_policy_history_payload(self, limit=limit)

    def _resolved_activation_policy(self) -> StrategyProfileActivationPolicyConfig:
        return resolved_activation_policy(self)

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
        return update_activation_policy_helper(
            self,
            enabled=enabled,
            min_composite_score=min_composite_score,
            min_offline_replay_score=min_offline_replay_score,
            min_recommendation_strength=min_recommendation_strength,
            require_positive_replay_consensus=require_positive_replay_consensus,
            disallow_when_shadow_review_required=disallow_when_shadow_review_required,
            matrix_allowed_symbols=matrix_allowed_symbols,
            matrix_allowed_regimes=matrix_allowed_regimes,
            matrix_allowed_profiles=matrix_allowed_profiles,
            reason=reason,
            actor_identity=actor_identity,
        )

    def approve_activation_policy(
        self,
        *,
        policy_id: str | None,
        actor_identity: str | None,
        reason: str,
    ) -> dict[str, Any]:
        return approve_activation_policy_helper(
            self,
            policy_id=policy_id,
            actor_identity=actor_identity,
            reason=reason,
        )

    def freeze_activation_policy(
        self,
        *,
        frozen: bool,
        actor_identity: str | None,
        reason: str,
    ) -> dict[str, Any]:
        return freeze_activation_policy_helper(
            self,
            frozen=frozen,
            actor_identity=actor_identity,
            reason=reason,
        )

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
        return update_auto_rollback_policy_helper(
            self,
            enabled=enabled,
            review_required_only=review_required_only,
            min_trade_count=min_trade_count,
            cooldown_seconds=cooldown_seconds,
            matrix_allowed_symbols=matrix_allowed_symbols,
            matrix_allowed_regimes=matrix_allowed_regimes,
            matrix_allowed_profiles=matrix_allowed_profiles,
            reason=reason,
            actor_identity=actor_identity,
        )

    def approve_auto_rollback_policy(
        self,
        *,
        policy_id: str | None,
        actor_identity: str | None,
        reason: str,
    ) -> dict[str, Any]:
        return approve_auto_rollback_policy_helper(
            self,
            policy_id=policy_id,
            actor_identity=actor_identity,
            reason=reason,
        )

    def freeze_auto_rollback_policy(
        self,
        *,
        frozen: bool,
        actor_identity: str | None,
        reason: str,
    ) -> dict[str, Any]:
        return freeze_auto_rollback_policy_helper(
            self,
            frozen=frozen,
            actor_identity=actor_identity,
            reason=reason,
        )

    def _comparison_report(
        self,
        *,
        revisions: list[StrategyProfileRevision],
        state: StrategyProfileActivationState,
        evaluations: list[StrategyProfileEvaluationRecord] | None = None,
    ) -> StrategyProfileComparisonReport:
        return build_comparison_report(
            self,
            revisions=revisions,
            state=state,
            evaluations=evaluations,
            evaluation_limit=_PROFILE_COMPARISON_EVALUATION_LIMIT,
        )

    def _build_optimization_report(
        self,
        *,
        state: StrategyProfileActivationState,
        comparison_report: StrategyProfileComparisonReport,
        evaluations: list[StrategyProfileEvaluationRecord],
        context_snapshot: StrategyProfileEvaluationContextSnapshot | None = None,
    ) -> StrategyProfileOptimizationReport:
        return build_optimization_report(
            self,
            state=state,
            comparison_report=comparison_report,
            evaluations=evaluations,
            context_snapshot=context_snapshot,
        )

    def _shadow_summary_for_profiles(self) -> dict[str, Any]:
        return shadow_summary_for_profiles(self)

    def _recent_replay_summary(
        self,
        *,
        symbol: str | None = None,
        regime: str | None = None,
        active_profile_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return recent_replay_summary(
            self,
            symbol=symbol,
            regime=regime,
            active_profile_id=active_profile_id,
            limit=limit,
        )

    def _build_offline_replay_pipeline(
        self,
        *,
        comparison_rows: list[StrategyProfileComparisonRow],
        symbol: str | None,
        regime: str | None,
        active_profile_id: str | None,
    ) -> dict[str, Any]:
        return build_offline_replay_pipeline(
            self,
            comparison_rows=comparison_rows,
            symbol=symbol,
            regime=regime,
            active_profile_id=active_profile_id,
        )

    def _winner_selection_policy(
        self,
        *,
        candidates: list[StrategyProfileOptimizationCandidate],
        ai_performance_summary: dict[str, Any],
    ) -> dict[str, Any]:
        activation_policy = self._resolved_activation_policy()
        eligible_candidates = [item for item in candidates if item.selection_eligible]
        winner = eligible_candidates[0] if eligible_candidates else (candidates[0] if candidates else None)
        blocked_reasons: list[str] = []
        if winner is not None:
            blocked_reasons.extend(list(winner.selection_blocked_reasons))
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
                "min_score_delta_vs_active": float(self.settings.strategy_profile_activation_min_score_delta),
                "required_consecutive_wins": int(self.settings.strategy_profile_activation_required_consecutive_wins),
                "min_active_minutes": int(self.settings.strategy_profile_activation_min_active_minutes),
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
            "eligible_candidate_profile_ids": [item.profile_id for item in eligible_candidates],
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
            "evidence_state": "sufficient",
        }
        if int(scorecard["evidence_counts"]["global_validations"]) <= 0:
            scorecard["final_adjustment"] = 0.0
            scorecard["confidence_weight"] = 0.0
            scorecard["evidence_state"] = "insufficient"
            scorecard["target"] = {
                "symbol": target_symbol,
                "regime": target_regime,
                "profile_id": row.profile_id,
            }
            return scorecard
        healthy_rate = float(replay_summary.get("healthy_rate") or 0.0)
        avg_divergence = float(replay_summary.get("avg_divergence_count") or 0.0)
        avg_execution_issues = float(replay_summary.get("avg_execution_chain_issue_count") or 0.0)
        avg_decision_issues = float(replay_summary.get("avg_decision_chain_issue_count") or 0.0)
        if healthy_rate < 0.8:
            scorecard["global_health_adjustment"] = (
                float(self.settings.strategy_profile_score_low_health_conservative_bonus)
                if row.risk_level == "conservative"
                else float(self.settings.strategy_profile_score_low_health_non_conservative_penalty)
            )
        if avg_divergence > 0:
            scorecard["global_divergence_adjustment"] = (
                float(self.settings.strategy_profile_score_divergence_execution_bonus)
                if row.market_intent == "execution_degraded"
                else float(self.settings.strategy_profile_score_divergence_other_penalty)
            )
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

    def _replay_adjustment_for_profile(
        self,
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
            adjustment += (
                float(self.settings.strategy_profile_score_low_health_conservative_bonus)
                if row.risk_level == "conservative"
                else float(self.settings.strategy_profile_score_low_health_non_conservative_penalty)
            )
        if avg_divergence > 0:
            adjustment += (
                float(self.settings.strategy_profile_score_divergence_execution_bonus)
                if row.market_intent == "execution_degraded"
                else float(self.settings.strategy_profile_score_divergence_other_penalty)
            )
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
            reasons.append("replay_history_neutralized")
        return reasons

    def _latest_optimization_report_payload(self) -> dict[str, Any] | None:
        return self.recommendations.latest_optimization_report_payload()

    def _latest_selection_decision_payload(self) -> dict[str, Any] | None:
        return self.recommendations.latest_selection_decision_payload()

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
        return self.recommendations.latest_optimization_report()

    def _latest_selection_decision(self) -> StrategyProfileSelectionDecision | None:
        return self.recommendations.latest_selection_decision()

    def _build_selection_decision(
        self,
        *,
        state: StrategyProfileActivationState,
        optimization_report: StrategyProfileOptimizationReport,
        activation_decision: dict[str, Any],
    ) -> StrategyProfileSelectionDecision:
        return self.recommendations.build_selection_decision(
            state=state,
            optimization_report=optimization_report,
            activation_decision=activation_decision,
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
        blocked_reasons: list[str] | None = None,
        execution_outcome: dict[str, Any] | None = None,
        auto_rollback_recommendation: dict[str, Any] | None = None,
        notes: list[str],
        transition_class: str | None = None,
        transition_risk_direction: str | None = None,
        fast_track_eligible: bool | None = None,
        fast_track_applied: bool | None = None,
        operator_summary: str | None = None,
        gating_state: dict[str, Any] | None = None,
    ) -> StrategyProfileSelectionDecision:
        return self.recommendations.append_selection_decision_transition(
            status=status,
            candidate_profile_id=candidate_profile_id,
            rollback_profile_id=rollback_profile_id,
            execution_state=execution_state,
            recommended_action=recommended_action,
            rationale=rationale,
            blocked_reasons=blocked_reasons,
            execution_outcome=execution_outcome,
            auto_rollback_recommendation=auto_rollback_recommendation,
            notes=notes,
            transition_class=transition_class,
            transition_risk_direction=transition_risk_direction,
            fast_track_eligible=fast_track_eligible,
            fast_track_applied=fast_track_applied,
            operator_summary=operator_summary,
            gating_state=gating_state,
        )

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
        return self.recommendations.write_back_selection_outcome(
            state=state,
            evaluations=evaluations,
            optimization_report=optimization_report,
        )

    def _maybe_auto_execute_rollback(self, *, decision: StrategyProfileSelectionDecision) -> dict[str, Any] | None:
        return self.activation.maybe_auto_execute_rollback(decision=decision)

    def _consecutive_candidate_win_count(self, *, candidate_profile_id: str | None) -> int:
        if not candidate_profile_id:
            return 0
        reports = [
            StrategyProfileOptimizationReport.model_validate(item.payload)
            for item in reversed(
                self.event_store.by_topic_scoped(
                    topics.STRATEGY_PROFILE_OPTIMIZATION_REPORTS,
                    scope=self.runtime_state_scope,
                )
            )
            if isinstance(item.payload, dict)
        ]
        count = 0
        for report in reports:
            if report.recommended_profile_id != candidate_profile_id:
                break
            count += 1
        return count

    def _activation_gate_decision(
        self,
        *,
        recommendation: StrategyProfileRecommendation,
        optimization_report: StrategyProfileOptimizationReport | None,
    ) -> dict[str, Any]:
        return self.activation.activation_gate_decision(
            recommendation=recommendation,
            optimization_report=optimization_report,
        )

    def _maybe_auto_execute_activation_policy(
        self,
        *,
        optimization_report: StrategyProfileOptimizationReport,
        selection_decision: StrategyProfileSelectionDecision,
    ) -> dict[str, Any] | None:
        return self.activation.maybe_auto_execute_activation_policy(
            optimization_report=optimization_report,
            selection_decision=selection_decision,
        )

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
        signals = self._resolved_context_signals(context)
        execution_health = context.get("execution_health") or {}
        regime = str(signals["regime"])
        volatility_state = str(signals["volatility_state"])
        composite_alpha = float(signals["composite_alpha_score"] or 0.0)
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

    def _transition_class(
        self,
        *,
        target: StrategyProfileRevision | None,
        transition_risk_direction: str,
        fast_track: dict[str, Any] | None = None,
    ) -> str:
        if target is None:
            return "unknown"
        if fast_track is not None and fast_track.get("eligible"):
            return "emergency_safety"
        if transition_risk_direction == "more_aggressive":
            return "aggressive_optimization"
        if transition_risk_direction == "more_conservative":
            return "conservative_rebalance"
        return "same_risk_optimization"

    def _selection_operator_summary(
        self,
        *,
        candidate_profile_id: str | None,
        transition_class: str,
        blocked_reasons: list[str],
        fast_track: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> str:
        candidate = candidate_profile_id or "none"
        if fast_track is not None and fast_track.get("eligible") and not blocked_reasons:
            return f"当前候选档位 {candidate} 走紧急安全快速通道，系统允许直接切向更保守档位。"
        if blocked_reasons:
            remaining_trades = max(
                int((evidence or {}).get("min_closed_trades", 0)) - int((evidence or {}).get("closed_trades", 0)),
                0,
            )
            remaining_replays = max(
                int((evidence or {}).get("min_replay_validations", 0))
                - int((evidence or {}).get("replay_validations", 0)),
                0,
            )
            if remaining_trades or remaining_replays:
                return (
                    f"当前候选档位 {candidate} 仍被自动切档门槛阻断，"
                    f"还差 {remaining_trades} 笔已平仓交易、{remaining_replays} 次 replay 验证。"
                )
            return f"当前候选档位 {candidate} 已产生，但仍有阻断条件未解除。"
        if transition_class == "aggressive_optimization":
            return f"当前候选档位 {candidate} 属于更激进切换，系统会要求更高置信度后才允许自动生效。"
        if transition_class == "conservative_rebalance":
            return f"当前候选档位 {candidate} 属于更保守切换，系统准备在门槛满足后自动收缩。"
        return f"当前候选档位 {candidate} 与现档位同风险级，系统会继续观察是否值得切换。"

    def _activation_blockers(self) -> list[str]:
        return self.activation.activation_blockers()

    def _manual_override_freeze_until(self) -> datetime | None:
        return self.activation.manual_override_freeze_until()

    def _recommendation_validation(self, recommendation: StrategyProfileRecommendation) -> dict[str, Any]:
        return self.recommendations.recommendation_validation(recommendation)

    def _auto_apply_recommendation(self, *, recommendation: StrategyProfileRecommendation) -> dict[str, Any]:
        return self.recommendations.auto_apply_recommendation(recommendation=recommendation)

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
        freeze_until: datetime | None = None,
    ) -> StrategyProfileActivationRecord:
        return self.activation.activate_revision(
            target=target,
            state=state,
            trigger_type=trigger_type,
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=recommendation_id,
            reason_code=reason_code,
            reason_detail=reason_detail,
            freeze_until=freeze_until,
        )

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
        return self.activation.reject_recommendation_record(
            recommendation=recommendation,
            source=source,
            reason_code=reason_code,
            reason_detail=reason_detail,
            actor_identity=actor_identity,
            actor_role=actor_role,
        )
