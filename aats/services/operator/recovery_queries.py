from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aats.events import topics
from aats.services.runtime_scope import latest_topic_event_for_scope

if TYPE_CHECKING:
    from aats.services.operator.query_service import OperatorQueryService


class RecoveryQueryFacade:
    def __init__(self, owner: "OperatorQueryService") -> None:
        self.owner = owner

    def recovery_view(self) -> dict[str, Any]:
        return self.owner._cached("recovery_view", self.build_recovery_view)

    def build_recovery_view(self) -> dict[str, Any]:
        latest_reconciliation = self.owner._latest_scoped_reconciliation()
        latest_state_snapshot_getter = getattr(
            self.owner.runtime.reconciliation_repo,
            "latest_state_snapshot_for_scope",
            None,
        )
        latest_state_snapshot = (
            latest_state_snapshot_getter(scope=self.owner.state_scope)
            if callable(latest_state_snapshot_getter)
            else None
        )
        latest_generation_getter = getattr(
            self.owner.runtime.reconciliation_repo,
            "latest_baseline_generation_for_scope",
            None,
        )
        latest_baseline_generation = (
            latest_generation_getter(scope=self.owner.state_scope)
            if callable(latest_generation_getter)
            else None
        )
        latest_ack_getter = getattr(
            self.owner.runtime.reconciliation_repo,
            "latest_exchange_ack_watermark_for_scope",
            None,
        )
        latest_exchange_ack_watermark = (
            latest_ack_getter(scope=self.owner.state_scope)
            if callable(latest_ack_getter)
            else None
        )
        latest_baseline = self.owner.latest_account_baseline()
        latest_rebaseline_action = self.owner.latest_operator_action("rebaseline")
        latest_resume_action = self.owner.latest_operator_action("resume")
        latest_ai_degradation = latest_topic_event_for_scope(
            self.owner.runtime.event_store,
            topics.AI_DEGRADATION_EVENTS,
            self.owner.state_scope,
        )
        latest_ai_shadow_evaluation = latest_topic_event_for_scope(
            self.owner.runtime.event_store,
            topics.AI_SHADOW_EVALUATIONS,
            self.owner.state_scope,
        )
        base = self.owner.recovery_posture.finalize_status(latest_reconciliation=latest_reconciliation)
        if not self.owner._ai_history_visible():
            latest_ai_degradation = None
            latest_ai_shadow_evaluation = None
        return {
            **base.model_dump(mode="json"),
            "last_rebaseline_action": latest_rebaseline_action,
            "last_resume_action": latest_resume_action,
            "latest_account_baseline": latest_baseline,
            "latest_baseline_generation": (
                latest_baseline_generation.model_dump(mode="json")
                if latest_baseline_generation is not None
                else None
            ),
            "latest_exchange_ack_watermark": (
                latest_exchange_ack_watermark.model_dump(mode="json")
                if latest_exchange_ack_watermark is not None
                else None
            ),
            "latest_state_snapshot": (
                latest_state_snapshot.model_dump(mode="json")
                if latest_state_snapshot is not None
                else None
            ),
            "latest_reconciliation": (
                latest_reconciliation.model_dump(mode="json")
                if latest_reconciliation is not None
                else None
            ),
            "latest_ai_degradation": self.owner.payload(latest_ai_degradation),
            "latest_ai_shadow_evaluation": self.owner.payload(latest_ai_shadow_evaluation),
            "ai_runtime": self.owner.ai_runtime(),
        }

    def system_recovery(self) -> dict[str, Any]:
        recovery = self.recovery_view()
        return {
            "recovery": recovery,
            "latest_rebaseline_action": recovery["last_rebaseline_action"],
            "latest_resume_action": recovery["last_resume_action"],
            "latest_account_baseline": recovery["latest_account_baseline"],
        }

    def system_mode(self) -> dict[str, Any]:
        return self.owner._cached("system_mode", self.build_system_mode)

    def build_system_mode(self) -> dict[str, Any]:
        snapshot = dict(self.owner.runtime.mode_controller.snapshot())
        readiness = self.owner.runtime.execution_adapter.readiness()
        recovery = self.recovery_view()
        submit_blocked_reasons = list(
            dict.fromkeys(
                list(snapshot.get("submit_blocked_reasons", []))
                + list(readiness.get("submit_blocked_reasons", []))
            )
        )
        health_blockers = list(dict.fromkeys(self.owner.runtime.health_service.execution_blockers()))
        recovery_blockers = list(dict.fromkeys(recovery["resume_blocked_reasons"]))
        exchange_submit_allowed = bool(
            readiness.get("exchange_submit_allowed", snapshot.get("exchange_submit_allowed", False))
        )
        execution_blockers = self.owner.recovery_posture.execution_blockers(
            health_blockers=health_blockers,
            recovery_blockers=recovery_blockers,
            submit_blocked_reasons=submit_blocked_reasons,
        )

        snapshot["exchange_submit_allowed"] = exchange_submit_allowed
        snapshot["submit_blocked"] = bool(submit_blocked_reasons) or not exchange_submit_allowed
        snapshot["submit_blocked_reasons"] = submit_blocked_reasons
        snapshot["execution_blocked"] = bool(execution_blockers)
        snapshot["blocked_reason"] = execution_blockers[0] if execution_blockers else None
        snapshot["recovery_state"] = recovery["recovery_state"]
        snapshot["review_required"] = recovery["review_required"]
        snapshot["rebaseline_available"] = recovery["rebaseline_available"]
        snapshot["profile_source"] = self.owner.runtime.runtime_profile_resolution.profile_source
        snapshot["active_profile_revision_id"] = None
        snapshot["pending_profile_revision_id"] = None
        snapshot["restart_required"] = False
        snapshot["trial_guard"] = self.owner.trial_guard()
        return snapshot
