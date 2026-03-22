from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.events.envelopes import build_envelope
from aats.schemas.common import utc_now
from aats.schemas.decision import ProfileControlDecision
from aats.schemas.strategy_profile_reports import (
    StrategyProfileOptimizationReport,
    StrategyProfileSelectionDecision,
)
from aats.schemas.strategy_profiles import (
    StrategyProfileActivationRecord,
    StrategyProfileActivationState,
    StrategyProfileRecommendation,
    StrategyProfileRejectionRecord,
    StrategyProfileRevision,
    apply_strategy_profile_payload,
    diff_strategy_profile_payload,
    strategy_profile_payload_from_settings,
)

if TYPE_CHECKING:
    from aats.schemas.operator import AuthSource, OperatorRole
    from aats.services.operator.strategy_profiles import StrategyProfileControlService


class StrategyProfileActivationFacade:
    def __init__(self, owner: "StrategyProfileControlService") -> None:
        self.owner = owner

    def accept_recommendation(
        self,
        *,
        recommendation_id: str,
        actor_role: "OperatorRole",
        actor_identity: str | None,
        auth_source: "AuthSource",
        activation_mode: str,
        reason: str,
    ) -> dict[str, Any]:
        self.owner.ensure_seed_profiles()
        recommendation = self.owner.repo.get_recommendation(recommendation_id)
        if recommendation is None:
            raise KeyError("strategy_profile_recommendation_not_found")
        if recommendation.expires_at <= utc_now():
            raise ValueError("strategy_profile_recommendation_expired")
        revision = self.owner._revision_for_profile(recommendation.recommended_profile_id)
        if revision is None:
            raise ValueError("strategy_profile_revision_missing")
        state = self.owner._activation_state()

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
            self.owner.repo.save_activation_state(state)
            updated = recommendation.model_copy(
                update={
                    "decision_status": "accepted",
                    "decision_reason_code": "staged_for_manual_activation",
                    "decision_reason_detail": reason,
                }
            )
            self.owner.repo.save_recommendation(updated)
            self.owner._append_selection_decision_transition(
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

        blockers = self.activation_blockers()
        if blockers:
            self.reject_recommendation_record(
                recommendation=recommendation,
                source="local_guard",
                reason_code=blockers[0],
                reason_detail=";".join(blockers),
                actor_identity=actor_identity,
                actor_role=actor_role,
            )
            raise ValueError(blockers[0])

        record = self.activate_revision(
            target=revision,
            state=state,
            trigger_type="manual",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=recommendation.recommendation_id,
            reason_code="operator_accept_recommendation",
            reason_detail=reason,
            freeze_until=self.manual_override_freeze_until(),
            pause_auto_switch=True,
        )
        updated = recommendation.model_copy(
            update={
                "decision_status": "accepted",
                "decision_reason_code": "operator_accepted",
                "decision_reason_detail": reason,
            }
        )
        self.owner.repo.save_recommendation(updated)
        self.owner._append_selection_decision_transition(
            status="manual_activation_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="executed",
            recommended_action="observe_outcome",
            rationale=["operator_accepted_recommendation"],
            execution_outcome=self.owner._activation_outcome_payload(record=record),
            notes=[reason],
        )
        return {
            "status": "accepted_and_activated",
            "recommendation": updated.model_dump(mode="json"),
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self.owner._revision_view(revision),
        }

    def reject_recommendation(
        self,
        *,
        recommendation_id: str,
        actor_role: "OperatorRole",
        actor_identity: str | None,
        reason_code: str,
        reason_detail: str | None,
    ) -> dict[str, Any]:
        self.owner.ensure_seed_profiles()
        recommendation = self.owner.repo.get_recommendation(recommendation_id)
        if recommendation is None:
            raise KeyError("strategy_profile_recommendation_not_found")
        updated = recommendation.model_copy(
            update={
                "decision_status": "rejected",
                "decision_reason_code": reason_code,
                "decision_reason_detail": reason_detail,
            }
        )
        self.owner.repo.save_recommendation(updated)
        rejection = self.reject_recommendation_record(
            recommendation=updated,
            source="operator",
            reason_code=reason_code,
            reason_detail=reason_detail,
            actor_identity=actor_identity,
            actor_role=actor_role,
        )
        self.owner._append_selection_decision_transition(
            status="recommendation_rejected",
            candidate_profile_id=updated.recommended_profile_id,
            rollback_profile_id=self.owner._activation_state().active_profile_id,
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
        actor_role: "OperatorRole",
        actor_identity: str | None,
        auth_source: "AuthSource",
        reason: str,
    ) -> dict[str, Any]:
        state = self.owner._activation_state()
        if state.pending_revision_id is None:
            raise ValueError("strategy_profile_pending_revision_missing")
        blockers = self.activation_blockers()
        if blockers:
            raise ValueError(blockers[0])
        revision = self.owner._revision(state.pending_revision_id)
        if revision is None:
            raise ValueError("strategy_profile_pending_revision_missing")
        record = self.activate_revision(
            target=revision,
            state=state,
            trigger_type="manual",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=revision.source_recommendation_id,
            reason_code="operator_activate_pending_profile",
            reason_detail=reason,
            freeze_until=self.manual_override_freeze_until(),
            pause_auto_switch=True,
        )
        self.owner._append_selection_decision_transition(
            status="pending_activation_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="executed",
            recommended_action="observe_outcome",
            rationale=["operator_activated_pending_profile"],
            execution_outcome=self.owner._activation_outcome_payload(record=record),
            notes=[reason],
        )
        return {
            "status": "activated",
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self.owner._revision_view(revision),
        }

    def activate_profile(
        self,
        *,
        profile_id: str,
        actor_role: "OperatorRole",
        actor_identity: str | None,
        auth_source: "AuthSource",
        reason: str,
    ) -> dict[str, Any]:
        state = self.owner._activation_state()
        if profile_id == state.active_profile_id:
            raise ValueError("strategy_profile_already_active")
        blockers = self.activation_blockers()
        if blockers:
            raise ValueError(blockers[0])
        revision = self.owner._revision_for_profile(profile_id)
        if revision is None:
            raise ValueError("strategy_profile_profile_not_found")
        record = self.activate_revision(
            target=revision,
            state=state,
            trigger_type="manual",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=revision.source_recommendation_id,
            reason_code="operator_manual_profile_activation",
            reason_detail=reason,
            freeze_until=self.manual_override_freeze_until(),
            pause_auto_switch=True,
        )
        self.owner._append_selection_decision_transition(
            status="manual_profile_activation_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="executed",
            recommended_action="observe_outcome",
            rationale=["operator_manual_profile_activation"],
            execution_outcome=self.owner._activation_outcome_payload(record=record),
            notes=[reason],
        )
        return {
            "status": "manually_activated",
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self.owner._revision_view(revision),
        }

    def restore_auto(
        self,
        *,
        actor_role: "OperatorRole",
        actor_identity: str | None,
        auth_source: "AuthSource",
        reason: str,
    ) -> dict[str, Any]:
        state = self.owner._activation_state()
        if state.auto_switch_enabled and (state.frozen_until is None or state.frozen_until <= utc_now()):
            cleared_state = state.model_copy(update={"frozen_until": None})
            self.owner.repo.save_activation_state(cleared_state)
            return {
                "status": "already_auto",
                "activation": cleared_state.model_dump(mode="json"),
                "active_revision": self.owner._revision_view(self.owner._revision(cleared_state.active_revision_id)),
            }

        next_state = state.model_copy(
            update={
                "auto_switch_enabled": True,
                "frozen_until": None,
                "last_switch_reason": "operator_restore_auto_strategy_profile_control",
                "last_switch_actor": actor_identity,
            }
        )
        self.owner.repo.save_activation_state(next_state)
        self.owner._append_selection_decision_transition(
            status="manual_profile_auto_switch_restored",
            candidate_profile_id=next_state.active_profile_id,
            rollback_profile_id=state.previous_active_revision_id,
            execution_state="not_executed",
            recommended_action="keep_current_profile",
            rationale=["operator_restore_auto_strategy_profile_control"],
            notes=[reason],
        )
        return {
            "status": "auto_restored",
            "activation": next_state.model_dump(mode="json"),
            "active_revision": self.owner._revision_view(self.owner._revision(next_state.active_revision_id)),
        }

    def rollback(
        self,
        *,
        actor_role: "OperatorRole",
        actor_identity: str | None,
        auth_source: "AuthSource",
        reason: str,
    ) -> dict[str, Any]:
        state = self.owner._activation_state()
        if state.previous_active_revision_id is None:
            raise ValueError("strategy_profile_no_previous_revision")
        blockers = self.activation_blockers()
        if blockers:
            raise ValueError(blockers[0])
        revision = self.owner._revision(state.previous_active_revision_id)
        if revision is None:
            raise ValueError("strategy_profile_previous_revision_missing")
        record = self.activate_revision(
            target=revision,
            state=state,
            trigger_type="rollback",
            actor_role=actor_role,
            actor_identity=actor_identity,
            auth_source=auth_source,
            recommendation_id=revision.source_recommendation_id,
            reason_code="operator_manual_rollback",
            reason_detail=reason,
            freeze_until=self.manual_override_freeze_until(),
            pause_auto_switch=True,
        )
        self.owner._append_selection_decision_transition(
            status="rollback_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="rolled_back",
            recommended_action="observe_after_rollback",
            rationale=["operator_manual_rollback"],
            execution_outcome=self.owner._activation_outcome_payload(record=record),
            notes=[reason],
        )
        return {
            "status": "rolled_back",
            "activation_record": record.model_dump(mode="json"),
            "active_revision": self.owner._revision_view(revision),
        }

    async def evaluate_mainline_profile_control(self, *, decision_id: str) -> ProfileControlDecision | None:
        result = await self.owner.evaluate_now(allow_auto_activation=True)
        recommendation = result.get("recommendation") or {}
        activation = result.get("auto_activation") or {}
        requested_profile_id = activation.get("candidate_profile_id") or recommendation.get("recommended_profile_id")
        if not requested_profile_id:
            return None
        state = self.owner._activation_state()
        activation_record = activation.get("activation_record") or {}
        blocked_reasons = list(activation.get("blocked_reasons") or [])
        freeze_until = state.frozen_until
        return ProfileControlDecision(
            decision_id=decision_id,
            requested_by="ai",
            requested_profile_id=str(requested_profile_id),
            current_profile_id=state.active_profile_id,
            applied=bool(activation_record),
            blocked_reasons=blocked_reasons,
            frozen_by_admin_override=bool(
                freeze_until is not None
                and freeze_until > utc_now()
                and "strategy_profile_auto_switch_frozen" in blocked_reasons
            ),
            freeze_until=freeze_until,
            decision_reason_codes=list(recommendation.get("reason_codes") or []),
            activation_record_ref=activation_record.get("activation_event_id"),
        )

    def maybe_auto_execute_rollback(self, *, decision: StrategyProfileSelectionDecision) -> dict[str, Any] | None:
        recommendation = decision.auto_rollback_recommendation or {}
        policy = self.owner._resolved_auto_rollback_policy()
        if not policy.enabled or not policy.effective or policy.frozen:
            return None
        if not recommendation.get("recommended"):
            return None
        allowed_symbols = set(policy.matrix_allowed_symbols)
        if allowed_symbols and str(recommendation.get("symbol") or self.owner.settings.default_symbol) not in allowed_symbols:
            return None
        allowed_regimes = set(policy.matrix_allowed_regimes)
        if allowed_regimes and str(recommendation.get("regime") or "unknown") not in allowed_regimes:
            return None
        allowed_profiles = set(policy.matrix_allowed_profiles)
        if allowed_profiles and str(recommendation.get("active_profile_id") or "") not in allowed_profiles:
            return None
        if policy.review_required_only and "ai_shadow_guard_review_required" not in (recommendation.get("reason_codes") or []):
            return None
        target_profile_id = recommendation.get("target_profile_id")
        if not target_profile_id:
            return None
        state = self.owner._activation_state()
        if state.active_profile_id == target_profile_id:
            return None
        latest_activation = next(
            (
                item
                for item in reversed(
                    self.owner.repo.list_activation_history(
                        product_type=self.owner.settings.trading_product_type,
                        margin_mode=self.owner.settings.margin_mode,
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
        blockers = self.activation_blockers()
        if blockers:
            return None
        revision = self.owner._revision_for_profile(target_profile_id)
        if revision is None:
            return None
        record = self.activate_revision(
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
        self.owner._append_selection_decision_transition(
            status="auto_rollback_executed",
            candidate_profile_id=revision.profile_id,
            rollback_profile_id=record.from_profile_id,
            execution_state="rolled_back",
            recommended_action="observe_after_rollback",
            rationale=["system_auto_rollback_executed", *(recommendation.get("reason_codes") or [])],
            execution_outcome=self.owner._activation_outcome_payload(record=record),
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

    def activation_gate_decision(
        self,
        *,
        recommendation: StrategyProfileRecommendation,
        optimization_report: StrategyProfileOptimizationReport | None,
    ) -> dict[str, Any]:
        active = self.owner._activation_state()
        revision = self.owner._revision_for_profile(recommendation.recommended_profile_id)
        current = self.owner._revision(active.active_revision_id)
        safety_state = self.owner._safety_state()
        blocked_reasons: list[str] = []
        transition = "unknown"
        control_summary = optimization_report.control_summary if optimization_report is not None else {}
        evidence = control_summary.get("evidence") or {}
        candidate = (
            self.owner._candidate_for_profile(
                optimization_report=optimization_report,
                profile_id=recommendation.recommended_profile_id,
            )
            if optimization_report is not None
            else None
        )
        if revision is None:
            blocked_reasons.append("strategy_profile_revision_missing")
        else:
            transition = self.owner._transition_risk_direction(current=current, target=revision)
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
            confidence_floor = self.owner._auto_switch_confidence_min(current=current, target=revision)
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
        if evidence.get("closed_trades", 0) < evidence.get("min_closed_trades", 0):
            blocked_reasons.append("strategy_profile_requires_more_realized_trades")
        if evidence.get("replay_validations", 0) < evidence.get("min_replay_validations", 0):
            blocked_reasons.append("strategy_profile_requires_more_replay_validations")
        if bool(evidence.get("cold_start_active")):
            blocked_reasons.append("strategy_profile_cold_start_lock_active")
        if candidate is not None:
            blocked_reasons.extend(candidate.selection_blocked_reasons)
        blocked_reasons.extend(self.activation_blockers())

        consecutive_candidate_wins = self.owner._consecutive_candidate_win_count(
            candidate_profile_id=recommendation.recommended_profile_id
        )
        if consecutive_candidate_wins < int(self.owner.settings.strategy_profile_activation_required_consecutive_wins):
            blocked_reasons.append("strategy_profile_candidate_requires_more_confirmations")
        if (
            active.last_activation_at is not None
            and recommendation.recommended_profile_id != active.active_profile_id
            and (utc_now() - active.last_activation_at).total_seconds()
            < float(self.owner.settings.strategy_profile_activation_min_active_minutes) * 60.0
        ):
            blocked_reasons.append("strategy_profile_min_active_duration_not_reached")

        if optimization_report is not None and float(optimization_report.score_delta_vs_active or 0.0) < float(
            self.owner.settings.strategy_profile_activation_min_score_delta
        ):
            blocked_reasons.append("strategy_profile_score_delta_below_threshold")
        blocked_reasons = self.owner._dedupe_items(blocked_reasons)
        return {
            "auto_apply_allowed": not blocked_reasons,
            "blocked_reasons": blocked_reasons,
            "requires_manual_approval": any(
                item in {"strategy_profile_manual_approval_required", "strategy_profile_auto_switch_not_allowed"}
                for item in blocked_reasons
            ),
            "transition_risk_direction": transition,
            "candidate_profile_id": recommendation.recommended_profile_id,
            "candidate_source": recommendation.selection_source or "winner_engine",
            "activation_decision_source": "activation_gate",
            "consecutive_candidate_wins": consecutive_candidate_wins,
            "ai_preferred_profile_id": recommendation.ai_advice.preferred_profile_id if recommendation.ai_advice else None,
            "ai_agreement_with_candidate": (
                recommendation.ai_advice.agreement_with_candidate if recommendation.ai_advice else True
            ),
        }

    def maybe_auto_execute_activation_policy(
        self,
        *,
        optimization_report: StrategyProfileOptimizationReport,
        selection_decision: StrategyProfileSelectionDecision,
    ) -> dict[str, Any] | None:
        _ = selection_decision
        recommendation = self.owner.repo.latest_recommendation(
            product_type=self.owner.settings.trading_product_type,
            margin_mode=self.owner.settings.margin_mode,
            allowed_symbols=self.owner.settings.allowed_symbols,
        )
        if recommendation is None:
            return None
        decision = self.activation_gate_decision(
            recommendation=recommendation,
            optimization_report=optimization_report,
        )
        if decision["auto_apply_allowed"]:
            return {
                "status": "ready",
                "candidate_profile_id": recommendation.recommended_profile_id,
                "policy_id": decision.get("activation_policy_id"),
                "blocked_reasons": [],
            }
        return {
            "status": "blocked",
            "candidate_profile_id": recommendation.recommended_profile_id,
            "policy_id": decision.get("activation_policy_id"),
            "blocked_reasons": decision["blocked_reasons"],
        }

    def activation_blockers(self) -> list[str]:
        open_orders = self.owner.runtime.execution_repo.order_states_for_scope(
            scope=self.owner.runtime_state_scope,
            open_only=True,
        )
        return ["strategy_profile_open_orders_present"] if open_orders else []

    def manual_override_freeze_until(self) -> datetime | None:
        freeze_seconds = float(self.owner.settings.ai_profile_control_freeze_after_admin_override_seconds)
        if freeze_seconds <= 0:
            return None
        return utc_now() + timedelta(seconds=freeze_seconds)

    def activate_revision(
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
        pause_auto_switch: bool = False,
    ) -> StrategyProfileActivationRecord:
        previous = self.owner._revision(state.active_revision_id)
        previous_payload = previous.payload if previous is not None else strategy_profile_payload_from_settings(self.owner.settings)
        now = utc_now()
        retained_freeze_until = freeze_until
        if retained_freeze_until is None and state.frozen_until is not None and state.frozen_until > now:
            retained_freeze_until = state.frozen_until
        if previous is not None and previous.revision_id != target.revision_id:
            self.owner.repo.save_revision(previous.model_copy(update={"status": "superseded", "updated_at": utc_now()}))
        target = target.model_copy(update={"status": "active", "updated_at": utc_now()})
        self.owner.repo.save_revision(target)
        apply_strategy_profile_payload(self.owner.settings, target.payload)
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
                    else "auto" if trigger_type in {"ai_auto", "system_guard"} else "rollback"
                ),
                "last_activation_result": (
                    "rollback_succeeded" if trigger_type == "rollback" else "activation_succeeded"
                ),
                "last_activation_at": utc_now(),
                "last_activation_error": None,
                "last_switch_reason": reason_code,
                "last_switch_actor": actor_identity,
                "cooldown_until": utc_now() + timedelta(minutes=120),
                "frozen_until": retained_freeze_until,
                "auto_switch_enabled": False if pause_auto_switch else state.auto_switch_enabled,
            }
        )
        self.owner.repo.save_activation_state(next_state)
        record = StrategyProfileActivationRecord(
            product_type=self.owner.settings.trading_product_type,
            margin_mode=self.owner.settings.margin_mode,
            allowed_symbols=self.owner.settings.allowed_symbols,
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
        self.owner.repo.save_activation_record(record)
        self.owner.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_ACTIVATIONS,
                key=target.profile_id,
                payload_model=record,
                source_component="strategy_profile_service",
            )
        )
        return record

    def reject_recommendation_record(
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
            product_type=self.owner.settings.trading_product_type,
            margin_mode=self.owner.settings.margin_mode,
            allowed_symbols=self.owner.settings.allowed_symbols,
            recommendation_id=recommendation.recommendation_id,
            recommended_profile_id=recommendation.recommended_profile_id,
            rejection_source=source,
            rejection_reason_code=reason_code,
            rejection_reason_detail=reason_detail,
            actor_identity=actor_identity,
            actor_role=actor_role,
        )
        self.owner.repo.save_rejection(record)
        self.owner.event_store.append(
            build_envelope(
                topic=topics.STRATEGY_PROFILE_REJECTIONS,
                key=recommendation.recommended_profile_id,
                payload_model=record,
                source_component="strategy_profile_service",
            )
        )
        return record
