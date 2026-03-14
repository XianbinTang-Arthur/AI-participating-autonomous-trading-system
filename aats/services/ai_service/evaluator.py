from __future__ import annotations

from aats.events.envelopes import parse_envelope, parse_payload
from aats.schemas.decision import AIDecisionEvaluation, AIMarketAssessment
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport


class AIEvaluationTracker:
    def __init__(self) -> None:
        self._evaluations: dict[str, AIDecisionEvaluation] = {}

    def record_assessment(self, assessment: AIMarketAssessment) -> None:
        self._evaluations[assessment.decision_id] = AIDecisionEvaluation(
            decision_id=assessment.decision_id,
            operating_mode=assessment.operating_mode,
            provider_name=assessment.provider_name,
            output_valid=assessment.output_valid,
            calibrated_confidence=assessment.calibrated_confidence,
            fallback_used=assessment.fallback_used,
            fallback_reason=assessment.fallback_reason,
            degraded=assessment.degraded,
        )

    def latest(self, decision_id: str) -> AIDecisionEvaluation | None:
        return self._evaluations.get(decision_id)

    async def handle_portfolio_snapshot(self, message: dict) -> None:
        envelope = parse_envelope(message)
        snapshot = PortfolioSnapshot.model_validate(envelope.payload)
        if snapshot.decision_id is None:
            return
        evaluation = self._evaluations.get(snapshot.decision_id)
        if evaluation is None:
            return
        self._evaluations[snapshot.decision_id] = evaluation.model_copy(
            update={
                "observed_total_equity": snapshot.total_equity,
                "portfolio_snapshot_ref": envelope.event_id,
            }
        )

    async def handle_reconciliation_report(self, message: dict) -> None:
        envelope = parse_envelope(message)
        report = ReconciliationReport.model_validate(envelope.payload)
        if report.decision_id is None:
            return
        evaluation = self._evaluations.get(report.decision_id)
        if evaluation is None:
            return
        self._evaluations[report.decision_id] = evaluation.model_copy(
            update={
                "reconciliation_ref": envelope.event_id,
                "reconciliation_severity": report.severity,
            }
        )
