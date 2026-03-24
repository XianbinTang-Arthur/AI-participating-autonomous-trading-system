from __future__ import annotations

from dataclasses import dataclass

from aats.schemas.reconciliation import ReconciliationReport


@dataclass(frozen=True, slots=True)
class ReconciliationClassification:
    classification: str
    auto_repairable: bool
    resume_blocking: bool
    review_required: bool
    halt_required: bool
    recommended_operator_action: str | None
    remediation_action: str | None


class RecoveryReconciliationClassifier:
    def classify(self, report: ReconciliationReport) -> ReconciliationClassification:
        local_projection_only = self._local_projection_only(report)
        if report.halt_required or report.severity == "HARD_MISMATCH":
            return ReconciliationClassification(
                classification="halt_required",
                auto_repairable=False,
                resume_blocking=True,
                review_required=True,
                halt_required=True,
                recommended_operator_action=report.recommended_operator_action or "halt_execution_and_investigate_state_divergence",
                remediation_action=report.remediation_action or "halt_execution_and_investigate_state_divergence",
            )
        if report.only_reduce_required:
            return ReconciliationClassification(
                classification="derivatives_only_reduce",
                auto_repairable=False,
                resume_blocking=False,
                review_required=False,
                halt_required=False,
                recommended_operator_action=report.recommended_operator_action or "go_close_position_on_exchange",
                remediation_action=report.remediation_action or "go_close_position_on_exchange",
            )
        if local_projection_only:
            return ReconciliationClassification(
                classification="projection_rebuild_required",
                auto_repairable=True,
                resume_blocking=False,
                review_required=False,
                halt_required=False,
                recommended_operator_action=report.recommended_operator_action or "local_projection_rebuild",
                remediation_action=report.remediation_action or "local_projection_rebuild",
            )
        if report.review_required or report.severity == "REVIEW_REQUIRED":
            return ReconciliationClassification(
                classification="manual_review_required",
                auto_repairable=False,
                resume_blocking=True,
                review_required=True,
                halt_required=False,
                recommended_operator_action=report.recommended_operator_action or "review_and_rebaseline_if_expected",
                remediation_action=report.remediation_action or "review_and_rebaseline_if_expected",
            )
        if report.severity == "SOFT_MISMATCH":
            if report.observational_only:
                return ReconciliationClassification(
                    classification="observational_drift",
                    auto_repairable=False,
                    resume_blocking=False,
                    review_required=False,
                    halt_required=False,
                    recommended_operator_action=report.recommended_operator_action or "observe_only",
                    remediation_action=report.remediation_action or "observe_only",
                )
            return ReconciliationClassification(
                classification="soft_divergence_continue",
                auto_repairable=False,
                resume_blocking=False,
                review_required=False,
                halt_required=False,
                recommended_operator_action=report.recommended_operator_action or "investigate_state_divergence",
                remediation_action=report.remediation_action or "investigate_state_divergence",
            )
        return ReconciliationClassification(
            classification="clean",
            auto_repairable=False,
            resume_blocking=False,
            review_required=False,
            halt_required=False,
            recommended_operator_action=report.recommended_operator_action,
            remediation_action=report.remediation_action,
        )

    def annotate(self, report: ReconciliationReport) -> ReconciliationReport:
        classified = self.classify(report)
        return report.model_copy(
            update={
                "recovery_classification": classified.classification,
                "auto_repairable": classified.auto_repairable,
                "resume_blocking": classified.resume_blocking,
                "review_required": classified.review_required,
                "halt_required": classified.halt_required,
                "recommended_operator_action": classified.recommended_operator_action,
                "remediation_action": classified.remediation_action,
            }
        )

    @staticmethod
    def _local_projection_only(report: ReconciliationReport) -> bool:
        has_local_projection_diff = bool(report.balance_diff.get("reconstructed")) or bool(
            report.position_diff.get("reconstructed_mismatches")
        )
        has_exchange_or_execution_diff = any(
            (
                bool(report.order_diff.get("exchange")),
                bool(report.fill_diff.get("exchange")),
                bool(report.fill_diff.get("replayed")),
                bool(report.order_diff.get("reconstructed")),
                bool(report.balance_diff.get("exchange")),
                bool(report.position_diff.get("exchange_mismatches")),
            )
        )
        return has_local_projection_diff and not has_exchange_or_execution_diff
