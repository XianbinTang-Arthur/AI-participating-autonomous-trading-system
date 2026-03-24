from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aats.schemas.reconciliation import ReconciliationReport
from aats.schemas.system import RecoveryStatus
from aats.services.execution_engine.bundle_recovery import scoped_bundle_recovery_assessment
from aats.services.runtime_scope import latest_matching_reconciliation, runtime_state_scope

if TYPE_CHECKING:
    from aats.bootstrap.config import ApplicationRuntime


@dataclass(frozen=True, slots=True)
class ResumeCheck:
    blockers: tuple[str, ...]
    runnable: bool


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    recovery_state: str
    review_required: bool
    rebaseline_available: bool
    resume_eligible: bool
    safe_to_trade: bool
    resume_blocked_reasons: tuple[str, ...]


class RecoveryPostureEvaluator:
    _SUBMIT_ONLY_BLOCKERS = {
        "guarded_execution_dry_run",
        "live_submit_disabled",
        "okx_simulated_trading_required",
        "local_demo_no_exchange_submission",
        "real_market_paper_uses_local_paper_execution",
    }
    _PERSISTENT_STATUS_BLOCKERS = {
        "pending_execution_commands",
        "strategy_bundle_recovery_in_progress",
        "strategy_bundle_recovery_requires_review",
    }

    def __init__(self, runtime: ApplicationRuntime) -> None:
        self.runtime = runtime
        self.state_scope = runtime_state_scope(runtime.settings)

    def _ai_requires_manual_review(self) -> bool:
        if self.runtime.settings.ai_operating_mode == "baseline_only":
            return False
        ai_runtime = self.runtime.ai_service.status()
        if ai_runtime.get("manual_override_mode") == "baseline_only":
            return False
        return bool(ai_runtime.get("degraded")) and not bool(ai_runtime.get("auto_downgrade_active"))

    def _current_only_reduce_state(
        self,
        *,
        status: RecoveryStatus,
        report: ReconciliationReport | None,
        bundle_recovery,
    ) -> tuple[bool, list[str]]:
        if report is None:
            only_reduce_required = bool(status.only_reduce_required)
            reasons = list(status.only_reduce_reasons)
        else:
            only_reduce_required = bool(report.only_reduce_required)
            reasons = list(report.only_reduce_reasons)
        if bundle_recovery.bundle_recovery_required and "strategy_bundle_recovery_in_progress" not in reasons:
            reasons.append("strategy_bundle_recovery_in_progress")
        return only_reduce_required or bundle_recovery.bundle_recovery_required, reasons

    def _should_clear_lingering_review_state(
        self,
        *,
        status: RecoveryStatus,
        report: ReconciliationReport | None,
        bundle_recovery,
        ai_requires_manual_review: bool,
    ) -> bool:
        if status.recovery_state not in {"review_required", "resume_blocked", "bundle_recovery"}:
            return False
        if status.baseline_requires_operator_review or ai_requires_manual_review:
            return False
        if bundle_recovery.bundle_recovery_required or bundle_recovery.recovery_blocking:
            return False
        if report is None:
            return not status.resume_blocked_reasons and not status.only_reduce_required
        return not (
            report.halt_required
            or report.review_required
            or bool(getattr(report, "resume_blocking", False))
            or report.only_reduce_required
        )

    def resume_check(
        self,
        *,
        include_kill_switch: bool,
        base_status: RecoveryStatus | None = None,
        latest_reconciliation: ReconciliationReport | None = None,
    ) -> ResumeCheck:
        status = base_status or self.runtime.recovery_status
        report = latest_reconciliation or latest_matching_reconciliation(
            self.runtime.reconciliation_repo.history(),
            self.state_scope,
        )
        blockers = [
            blocker
            for blocker in status.resume_blocked_reasons
            if blocker in self._PERSISTENT_STATUS_BLOCKERS
        ] + list(self.runtime.health_service.snapshot().blockers)
        if not include_kill_switch:
            blockers = [blocker for blocker in blockers if blocker != "kill_switch_active"]
        if report is not None:
            if report.halt_required and "reconciliation_halt_required" not in blockers:
                blockers.append("reconciliation_halt_required")
            if getattr(report, "resume_blocking", False) and "operator_rebaseline_required" not in blockers:
                blockers.append("operator_rebaseline_required")
            if (
                report.review_required
                and self.runtime.recovery_policy.review_required_blocks_resume
                and "operator_rebaseline_required" not in blockers
            ):
                blockers.append("operator_rebaseline_required")
        if status.recovery_state == "rebaseline_pending" and "rebaseline_in_progress" not in blockers:
            blockers.append("rebaseline_in_progress")
        if self._ai_requires_manual_review() and "ai_degraded_requires_manual_review" not in blockers:
            blockers.append("ai_degraded_requires_manual_review")
        if self.runtime.environment_capabilities.exchange_coupled:
            submit_blockers = list(self.runtime.execution_adapter.readiness().get("submit_blocked_reasons", []))
            if not include_kill_switch:
                submit_blockers = [blocker for blocker in submit_blockers if blocker != "kill_switch_active"]
            submit_blockers = [
                blocker for blocker in submit_blockers if blocker not in self._SUBMIT_ONLY_BLOCKERS
            ]
            blockers.extend(blocker for blocker in submit_blockers if blocker not in blockers)
        deduped = tuple(dict.fromkeys(blockers))
        return ResumeCheck(blockers=deduped, runnable=not deduped)

    def assess(
        self,
        *,
        base_status: RecoveryStatus | None = None,
        latest_reconciliation: ReconciliationReport | None = None,
    ) -> RecoveryAssessment:
        status = base_status or self.runtime.recovery_status
        report = latest_reconciliation or latest_matching_reconciliation(
            self.runtime.reconciliation_repo.history(),
            self.state_scope,
        )
        bundle_recovery = self._bundle_recovery_assessment()
        recovery_state = status.recovery_state
        ai_requires_manual_review = self._ai_requires_manual_review()
        if report is not None:
            if report.halt_required:
                recovery_state = "resume_blocked"
            elif report.only_reduce_required and recovery_state not in {"rebaseline_pending", "rebaseline_completed"}:
                recovery_state = "only_reduce"
            elif (
                report.review_required
                and self.runtime.recovery_policy.review_required_blocks_resume
                and recovery_state not in {"rebaseline_pending", "rebaseline_completed"}
            ):
                recovery_state = "review_required"
        if bundle_recovery.recovery_blocking and recovery_state not in {"rebaseline_pending", "resume_blocked"}:
            recovery_state = "review_required"
        elif (
            bundle_recovery.bundle_recovery_required
            and recovery_state in {"normal_operation", "only_reduce"}
        ):
            recovery_state = "bundle_recovery"
        if status.baseline_requires_operator_review:
            recovery_state = "review_required"
        if ai_requires_manual_review:
            recovery_state = "review_required"
        if self._should_clear_lingering_review_state(
            status=status,
            report=report,
            bundle_recovery=bundle_recovery,
            ai_requires_manual_review=ai_requires_manual_review,
        ):
            recovery_state = "normal_operation"
        if recovery_state == "rebaseline_completed" and not self.runtime.kill_switch.halted:
            recovery_state = "normal_operation"
        elif self.runtime.kill_switch.halted and recovery_state in {"normal_operation", "only_reduce"}:
            recovery_state = "resume_blocked"

        only_reduce_required, bundle_only_reduce_reasons = self._current_only_reduce_state(
            status=status,
            report=report,
            bundle_recovery=bundle_recovery,
        )
        bundle_resume_blocked_reasons = list(status.resume_blocked_reasons)
        if bundle_recovery.bundle_recovery_required:
            blocker = (
                "strategy_bundle_recovery_requires_review"
                if bundle_recovery.recovery_blocking
                else "strategy_bundle_recovery_in_progress"
            )
            if blocker not in bundle_resume_blocked_reasons:
                bundle_resume_blocked_reasons.append(blocker)

        normalized = status.model_copy(
            update={
                "recovery_state": recovery_state,
                "bundle_recovery_required": bundle_recovery.bundle_recovery_required,
                "bundle_recovery_count": bundle_recovery.open_bundle_count,
                "recoverable_bundle_count": bundle_recovery.recoverable_bundle_count,
                "unbundled_open_order_count": bundle_recovery.unbundled_open_order_count,
                "bundle_summaries": list(bundle_recovery.bundle_summaries),
                "open_order_count": bundle_recovery.open_order_count,
                "only_reduce_required": only_reduce_required,
                "only_reduce_reasons": bundle_only_reduce_reasons,
                "resume_blocked_reasons": bundle_resume_blocked_reasons,
            }
        )
        resume_check = self.resume_check(
            include_kill_switch=False,
            base_status=normalized,
            latest_reconciliation=report,
        )
        if self.runtime.kill_switch.halted and resume_check.runnable and recovery_state == "resume_blocked":
            recovery_state = "manually_halted"

        review_required = recovery_state == "review_required"
        rebaseline_available = (
            self.runtime.recovery_policy.operator_rebaseline_supported
            and recovery_state in {"review_required", "resume_blocked"}
        )
        resume_eligible = (
            recovery_state in {"normal_operation", "only_reduce", "rebaseline_completed", "manually_halted"}
            and resume_check.runnable
        )
        safe_to_trade = (
            recovery_state in {"normal_operation", "only_reduce"}
            and resume_check.runnable
            and not self.runtime.kill_switch.halted
        )
        return RecoveryAssessment(
            recovery_state=recovery_state,
            review_required=review_required,
            rebaseline_available=rebaseline_available,
            resume_eligible=resume_eligible,
            safe_to_trade=safe_to_trade,
            resume_blocked_reasons=resume_check.blockers,
        )

    def finalize_status(
        self,
        *,
        base_status: RecoveryStatus | None = None,
        latest_reconciliation: ReconciliationReport | None = None,
    ) -> RecoveryStatus:
        status = base_status or self.runtime.recovery_status
        assessment = self.assess(base_status=status, latest_reconciliation=latest_reconciliation)
        bundle_recovery = self._bundle_recovery_assessment()
        only_reduce_required, only_reduce_reasons = self._current_only_reduce_state(
            status=status,
            report=latest_reconciliation,
            bundle_recovery=bundle_recovery,
        )
        updates = {
            "recovery_state": assessment.recovery_state,
            "review_required": assessment.review_required,
            "rebaseline_available": assessment.rebaseline_available,
            "resume_eligible": assessment.resume_eligible,
            "safe_to_trade": assessment.safe_to_trade,
            "halted": self.runtime.kill_switch.halted,
            "resume_blocked_reasons": list(assessment.resume_blocked_reasons),
            "bundle_recovery_required": bundle_recovery.bundle_recovery_required,
            "bundle_recovery_count": bundle_recovery.open_bundle_count,
            "recoverable_bundle_count": bundle_recovery.recoverable_bundle_count,
            "unbundled_open_order_count": bundle_recovery.unbundled_open_order_count,
            "bundle_summaries": list(bundle_recovery.bundle_summaries),
            "open_order_count": bundle_recovery.open_order_count,
            "only_reduce_required": only_reduce_required,
            "only_reduce_reasons": only_reduce_reasons,
        }
        if latest_reconciliation is not None:
            updates["latest_reconciliation_id"] = latest_reconciliation.reconciliation_id
            updates["latest_reconciliation_severity"] = latest_reconciliation.severity
            updates["reconciliation_classification"] = latest_reconciliation.recovery_classification
            updates["recovered_reconciliation_available"] = True
            updates["only_reduce_required"] = only_reduce_required
            updates["only_reduce_reasons"] = only_reduce_reasons
            updates["unknown_state_details"] = list(latest_reconciliation.unknown_state_details)
        return status.model_copy(update=updates)

    def _bundle_recovery_assessment(self):
        order_states_for_scope = getattr(self.runtime.execution_repo, "order_states_for_scope", None)
        if callable(order_states_for_scope):
            order_states = order_states_for_scope(scope=self.state_scope)
        else:
            order_states = self.runtime.execution_repo.order_states()
        active_obligations = getattr(self.runtime.obligation_repo, "active_obligations", None)
        obligations = active_obligations() if callable(active_obligations) else []
        return scoped_bundle_recovery_assessment(
            scope=self.state_scope,
            order_states=order_states,
            obligations=obligations,
        )

    def execution_blockers(
        self,
        *,
        health_blockers: list[str],
        recovery_blockers: list[str],
        submit_blocked_reasons: list[str],
    ) -> list[str]:
        blockers = list(dict.fromkeys(health_blockers + recovery_blockers))
        if self.runtime.kill_switch.halted and "kill_switch_active" not in blockers:
            blockers.insert(0, "kill_switch_active")
        if self.runtime.environment_capabilities.exchange_coupled:
            blockers = list(dict.fromkeys(blockers + submit_blocked_reasons))
        return blockers
