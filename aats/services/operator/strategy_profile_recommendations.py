from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from aats.bootstrap.logging import log_event
from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.strategy_profile_reports import StrategyProfileOptimizationReport, StrategyProfileSelectionDecision
from aats.schemas.strategy_profiles import StrategyProfileMarketRegimeAssessment, StrategyProfileRecommendation, StrategyProfileRecommendationOutput
from aats.services.ai_service.provider import AIProviderError, AIProviderTimeoutError

if TYPE_CHECKING:
    from aats.services.operator.strategy_profiles import StrategyProfileControlService


class StrategyProfileRecommendationFacade:
    def __init__(self, owner: "StrategyProfileControlService") -> None:
        self.owner = owner

    def build_normalized_recommendation(
        self,
        *,
        context_snapshot,
        optimization_report: StrategyProfileOptimizationReport,
        ai_recommendation: StrategyProfileRecommendation,
    ) -> StrategyProfileRecommendation:
        candidate_profile_id = optimization_report.recommended_profile_id or ai_recommendation.recommended_profile_id
        ai_advice = self.owner._ai_advice_from_recommendation(
            recommendation=ai_recommendation,
            candidate_profile_id=candidate_profile_id,
        )
        signals = self.owner._resolved_context_signals(self.owner._context_payload(context_snapshot))
        market_regime_assessment = StrategyProfileMarketRegimeAssessment(
            regime=str(
                ai_recommendation.market_regime_assessment.regime
                if ai_recommendation.market_regime_assessment
                else signals["regime"]
            ),
            volatility_state=str(
                ai_recommendation.market_regime_assessment.volatility_state
                if ai_recommendation.market_regime_assessment
                else signals["volatility_state"]
            ),
            execution_condition=str(
                ai_recommendation.market_regime_assessment.execution_condition
                if ai_recommendation.market_regime_assessment
                else (
                    "degraded"
                    if int((context_snapshot.execution_health or {}).get("recent_execution_error_count") or 0) >= 3
                    else "normal"
                )
            ),
        )
        candidate = self.owner._candidate_for_profile(
            optimization_report=optimization_report,
            profile_id=candidate_profile_id,
        )
        optimization_reasons = list(candidate.reasons if candidate is not None else [])
        if candidate_profile_id and ai_advice.preferred_profile_id and candidate_profile_id != ai_advice.preferred_profile_id:
            optimization_reasons.append("ai_assist_disagrees_with_winner")
        else:
            optimization_reasons.append("ai_assist_confirms_winner")
        summary = (
            f"winner_engine_selected={candidate_profile_id or 'none'}; "
            f"ai_preferred={ai_advice.preferred_profile_id or 'none'}; "
            f"score_delta_vs_active={format(optimization_report.score_delta_vs_active, '.3f')}"
        )
        return StrategyProfileRecommendation(
            product_type=self.owner.settings.trading_product_type,
            margin_mode=self.owner.settings.margin_mode,
            allowed_symbols=self.owner.settings.allowed_symbols,
            active_profile_id=optimization_report.active_profile_id,
            recommended_profile_id=candidate_profile_id or ai_recommendation.recommended_profile_id,
            fallback_profile_id=ai_recommendation.fallback_profile_id,
            confidence=self.owner._normalized_candidate_confidence(
                candidate=candidate,
                ai_advice=ai_advice,
                candidate_profile_id=candidate_profile_id,
            ),
            market_regime_assessment=market_regime_assessment,
            reason_codes=self.owner._dedupe_items(
                ["winner_engine_selected_candidate", *optimization_reasons[:4], *ai_advice.reason_codes[:3]]
            ),
            human_summary=summary,
            risk_notes=self.owner._dedupe_items(
                [
                    *ai_advice.risk_notes,
                    "ai_assist_only",
                    "registered_profile_only",
                    "fallback_rule_based_recommendation" if ai_advice.used_fallback else "",
                ]
            ),
            valid_for_minutes=ai_recommendation.valid_for_minutes,
            generated_by="winner_engine",
            model_name=ai_recommendation.model_name,
            prompt_version=self.owner.prompt_version,
            selection_source="winner_engine",
            context_snapshot_id=context_snapshot.snapshot_id,
            ai_advice=ai_advice,
            fallback_reason_code=ai_recommendation.fallback_reason_code,
            fallback_reason_detail=ai_recommendation.fallback_reason_detail,
            input_digest=self.owner._input_digest(self.owner._context_payload(context_snapshot)),
            input_snapshot=self.owner._context_payload(context_snapshot),
            expires_at=utc_now() + timedelta(minutes=ai_recommendation.valid_for_minutes),
        )

    async def generate_recommendation(self, *, context: dict[str, Any]) -> StrategyProfileRecommendation:
        active_state = self.owner._activation_state()
        registered_profile_ids = self.owner._registered_profile_ids()
        if self.owner.provider is not None:
            try:
                response = await self.owner.provider.generate_assessment(
                    prompt=self.owner._prompt(context),
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
                    product_type=self.owner.settings.trading_product_type,
                    margin_mode=self.owner.settings.margin_mode,
                    allowed_symbols=self.owner.settings.allowed_symbols,
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
                    model_name=self.owner.settings.ai_model_name,
                    prompt_version=self.owner.prompt_version,
                    input_digest=self.owner._input_digest(context),
                    input_snapshot=context,
                    expires_at=utc_now() + timedelta(minutes=output.valid_for_minutes),
                )
            except (AIProviderError, AIProviderTimeoutError, ValueError) as exc:
                fallback_reason_code = self.owner._fallback_reason_code(exc)
                fallback_reason_detail = str(exc)
                log_event(
                    self.owner.logger,
                    "strategy_profile_recommendation_fallback",
                    level="warning",
                    active_profile_id=active_state.active_profile_id,
                    provider=self.owner.settings.ai_provider,
                    fallback_reason_code=fallback_reason_code,
                    fallback_reason_detail=fallback_reason_detail,
                )
                return self.owner._fallback_recommendation(
                    context=context,
                    active_profile_id=active_state.active_profile_id,
                    fallback_reason_code=fallback_reason_code,
                    fallback_reason_detail=fallback_reason_detail,
                )
        return self.owner._fallback_recommendation(
            context=context,
            active_profile_id=active_state.active_profile_id,
            fallback_reason_code="strategy_profile_provider_not_configured",
            fallback_reason_detail="strategy profile recommendation provider is not configured",
        )

    def latest_optimization_report_payload(self) -> dict[str, Any] | None:
        latest = self.owner._latest_optimization_report_event()
        if latest is None or not isinstance(latest.payload, dict):
            return None
        return latest.payload

    def latest_selection_decision_payload(self) -> dict[str, Any] | None:
        latest = self.owner._latest_selection_decision_event()
        if latest is None or not isinstance(latest.payload, dict):
            return None
        return latest.payload

    def latest_optimization_report(self) -> StrategyProfileOptimizationReport | None:
        latest = self.owner._latest_optimization_report_event()
        if latest is None:
            return None
        return StrategyProfileOptimizationReport.model_validate(latest.payload)

    def latest_selection_decision(self) -> StrategyProfileSelectionDecision | None:
        latest = self.owner._latest_selection_decision_event()
        if latest is None:
            return None
        return StrategyProfileSelectionDecision.model_validate(latest.payload)

    def build_selection_decision(
        self,
        *,
        state,
        optimization_report: StrategyProfileOptimizationReport,
        activation_decision: dict[str, Any],
    ) -> StrategyProfileSelectionDecision:
        previous = self.latest_selection_decision()
        candidate = optimization_report.recommended_profile_id
        if candidate is None:
            status = "insufficient_data"
            execution_state = "not_executed"
            recommended_action = "collect_more_data"
        elif candidate == state.active_profile_id:
            status = "stable_keep_active"
            execution_state = "already_active"
            recommended_action = "keep_current_profile"
        elif activation_decision.get("auto_apply_allowed"):
            status = "recommended_not_executed"
            execution_state = "not_executed"
            recommended_action = "review_activate"
        else:
            status = "winner_policy_recommended_not_executed"
            execution_state = "not_executed"
            recommended_action = "review_activate"
        return StrategyProfileSelectionDecision(
            version=1 if previous is None else previous.version + 1,
            report_id=optimization_report.report_id,
            parent_decision_id=None if previous is None else previous.selection_decision_id,
            product_type=self.owner.settings.trading_product_type,
            margin_mode=self.owner.settings.margin_mode,
            allowed_symbols=tuple(self.owner.settings.allowed_symbols),
            context_snapshot_id=optimization_report.context_snapshot_id,
            active_profile_id=state.active_profile_id,
            candidate_profile_id=candidate,
            rollback_profile_id=state.active_profile_id,
            candidate_source=str(activation_decision.get("candidate_source") or "winner_engine"),
            activation_decision_source=str(
                activation_decision.get("activation_decision_source") or "activation_gate"
            ),
            transition_class=activation_decision.get("transition_class"),
            transition_risk_direction=activation_decision.get("transition_risk_direction"),
            decision_status=status,
            execution_state=execution_state,
            recommended_action=recommended_action,
            fast_track_eligible=bool(activation_decision.get("fast_track_eligible")),
            fast_track_applied=bool(activation_decision.get("fast_track_applied")),
            operator_summary=activation_decision.get("operator_summary"),
            gating_state=dict(activation_decision.get("gating_state") or {}),
            blocked_reasons=list(activation_decision.get("blocked_reasons") or []),
            rationale=[
                "selection_based_on_winner_engine",
                f"ai_alignment={activation_decision.get('ai_agreement_with_candidate')}",
                "rollback_target_preserved_as_previous_active_profile",
                *optimization_report.notes,
            ],
            replay_guard=optimization_report.replay_summary,
            shadow_guard=optimization_report.ai_performance_summary,
            notes=[
                f"optimization_version={optimization_report.version}",
                f"report_parent={optimization_report.parent_report_id or 'none'}",
                f"consecutive_candidate_wins={activation_decision.get('consecutive_candidate_wins')}",
            ],
        )

    def append_selection_decision_transition(
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
        previous = self.latest_selection_decision()
        latest_report = self.latest_optimization_report()
        decision = StrategyProfileSelectionDecision(
            version=1 if previous is None else previous.version + 1,
            report_id=(
                latest_report.report_id
                if latest_report is not None
                else (previous.report_id if previous is not None else "unknown")
            ),
            parent_decision_id=None if previous is None else previous.selection_decision_id,
            product_type=self.owner.settings.trading_product_type,
            margin_mode=self.owner.settings.margin_mode,
            allowed_symbols=tuple(self.owner.settings.allowed_symbols),
            context_snapshot_id=(
                latest_report.context_snapshot_id
                if latest_report is not None
                else (previous.context_snapshot_id if previous is not None else None)
            ),
            active_profile_id=self.owner._activation_state().active_profile_id,
            candidate_profile_id=candidate_profile_id,
            rollback_profile_id=rollback_profile_id,
            candidate_source=(previous.candidate_source if previous is not None else "winner_engine"),
            activation_decision_source=(
                previous.activation_decision_source if previous is not None else "activation_gate"
            ),
            transition_class=transition_class if transition_class is not None else (
                previous.transition_class if previous is not None else None
            ),
            transition_risk_direction=transition_risk_direction if transition_risk_direction is not None else (
                previous.transition_risk_direction if previous is not None else None
            ),
            decision_status=status,
            execution_state=execution_state or (previous.execution_state if previous is not None else "not_executed"),
            recommended_action=(
                recommended_action
                if recommended_action is not None
                else (previous.recommended_action if previous is not None else None)
            ),
            fast_track_eligible=(
                bool(fast_track_eligible)
                if fast_track_eligible is not None
                else (previous.fast_track_eligible if previous is not None else False)
            ),
            fast_track_applied=(
                bool(fast_track_applied)
                if fast_track_applied is not None
                else (previous.fast_track_applied if previous is not None else False)
            ),
            operator_summary=(
                operator_summary
                if operator_summary is not None
                else (previous.operator_summary if previous is not None else None)
            ),
            gating_state=(
                dict(gating_state)
                if gating_state is not None
                else (previous.gating_state if previous is not None else {})
            ),
            blocked_reasons=(
                list(blocked_reasons)
                if blocked_reasons is not None
                else (previous.blocked_reasons if previous is not None else [])
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
        self.owner.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
                key=decision.candidate_profile_id or decision.active_profile_id or "strategy_profiles",
                payload_model=decision,
                source_component="strategy_profile_service",
            )
        )
        return decision

    def write_back_selection_outcome(
        self,
        *,
        state,
        evaluations,
        optimization_report: StrategyProfileOptimizationReport,
    ) -> StrategyProfileSelectionDecision | None:
        active_profile_id = state.active_profile_id
        if not active_profile_id:
            return None
        decision_history = [
            StrategyProfileSelectionDecision.model_validate(item.payload)
            for item in reversed(
                self.owner.event_store.by_topic_scoped(
                    topics.STRATEGY_PROFILE_SELECTION_DECISIONS,
                    scope=self.owner.runtime_state_scope,
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
        activation_history = self.owner.repo.list_activation_history(
            product_type=self.owner.settings.trading_product_type,
            margin_mode=self.owner.settings.margin_mode,
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
            "symbol": self.owner.settings.default_symbol,
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
        return self.append_selection_decision_transition(
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

    def recommendation_validation(self, recommendation: StrategyProfileRecommendation) -> dict[str, Any]:
        return self.owner._activation_gate_decision(
            recommendation=recommendation,
            optimization_report=self.latest_optimization_report(),
        )

    def auto_apply_recommendation(self, *, recommendation: StrategyProfileRecommendation) -> dict[str, Any]:
        state = self.owner._activation_state()
        revision = self.owner._revision_for_profile(recommendation.recommended_profile_id)
        if revision is None:
            raise ValueError("strategy_profile_revision_missing")
        record = self.owner._activate_revision(
            target=revision,
            state=state,
            trigger_type="system_guard",
            actor_role="system",
            actor_identity="system_profile_activation_policy",
            auth_source="local_config",
            recommendation_id=recommendation.recommendation_id,
            reason_code="winner_selection_policy_auto_activation",
            reason_detail=recommendation.human_summary,
        )
        updated = recommendation.model_copy(
            update={
                "decision_status": "accepted",
                "decision_reason_code": "auto_applied",
                "decision_reason_detail": "winner_engine_auto_activation_executed",
            }
        )
        self.owner.repo.save_recommendation(updated)
        self.append_selection_decision_transition(
            status="winner_policy_auto_activation_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="executed",
            recommended_action="observe_outcome",
            rationale=["winner_engine_auto_activation_executed", record.reason_code],
            blocked_reasons=[],
            execution_outcome=self.owner._activation_outcome_payload(record=record),
            notes=[recommendation.human_summary],
        )
        return {
            "status": "winner_policy_auto_activation_executed",
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self.owner._revision_view(revision),
            "recommendation": updated.model_dump(mode="json"),
            "candidate_profile_id": revision.profile_id,
        }
