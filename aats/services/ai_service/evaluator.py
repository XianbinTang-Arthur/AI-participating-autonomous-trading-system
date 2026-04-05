from __future__ import annotations

from aats.events.envelopes import parse_envelope
from aats.schemas.ai_brief import AIDecisionBrief
from aats.schemas.ai_shadow import AIShadowDecision, AIShadowEvaluation
from aats.schemas.decision import AIDecisionEvaluation, AIMarketAssessment
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.reconciliation import ReconciliationReport


class AIEvaluationTracker:
    _MAX_TRACKED_DECISIONS: int = 500
    _MAX_TRACKED_SHADOW_ITEMS: int = 200

    def __init__(self) -> None:
        self._evaluations: dict[str, AIDecisionEvaluation] = {}
        self._assessments: dict[str, AIMarketAssessment] = {}
        self._decision_briefs: dict[str, AIDecisionBrief] = {}
        self._shadow_assessments: dict[str, AIMarketAssessment] = {}
        self._shadow_decisions: list[AIShadowDecision] = []
        self._shadow_evaluations: list[AIShadowEvaluation] = []

    def record_brief(self, brief: AIDecisionBrief) -> None:
        self._decision_briefs[brief.decision_id] = brief
        self._evict_oldest(self._decision_briefs, self._MAX_TRACKED_DECISIONS)

    def latest_brief(self, decision_id: str) -> AIDecisionBrief | None:
        return self._decision_briefs.get(decision_id)

    def record_assessment(self, assessment: AIMarketAssessment) -> None:
        self._assessments[assessment.decision_id] = assessment
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
        self._evict_oldest(self._assessments, self._MAX_TRACKED_DECISIONS)
        self._evict_oldest(self._evaluations, self._MAX_TRACKED_DECISIONS)

    def latest_assessment(self, decision_id: str) -> AIMarketAssessment | None:
        return self._assessments.get(decision_id)

    def assessments_recent(self, *, limit: int) -> list[AIMarketAssessment]:
        if limit <= 0:
            return []
        items = sorted(self._assessments.values(), key=lambda item: item.created_at, reverse=True)
        return items[:limit]

    def record_shadow_assessment(self, assessment: AIMarketAssessment) -> None:
        self._shadow_assessments[assessment.decision_id] = assessment
        self._evict_oldest(self._shadow_assessments, self._MAX_TRACKED_DECISIONS)

    def latest_shadow_assessment(self, decision_id: str) -> AIMarketAssessment | None:
        return self._shadow_assessments.get(decision_id)

    def record_shadow_decision(self, shadow: AIShadowDecision) -> None:
        self._shadow_decisions.append(shadow)
        if len(self._shadow_decisions) > self._MAX_TRACKED_SHADOW_ITEMS:
            self._shadow_decisions = self._shadow_decisions[-self._MAX_TRACKED_SHADOW_ITEMS:]

    def shadow_decisions_recent(self, *, limit: int) -> list[AIShadowDecision]:
        if limit <= 0:
            return []
        return list(reversed(self._shadow_decisions[-limit:]))

    def latest_shadow_decision(self) -> AIShadowDecision | None:
        if not self._shadow_decisions:
            return None
        return self._shadow_decisions[-1]

    def record_shadow_evaluation(self, evaluation: AIShadowEvaluation) -> None:
        self._shadow_evaluations.append(evaluation)
        if len(self._shadow_evaluations) > self._MAX_TRACKED_SHADOW_ITEMS:
            del self._shadow_evaluations[:len(self._shadow_evaluations) - self._MAX_TRACKED_SHADOW_ITEMS]

    def latest_shadow_evaluation(self) -> AIShadowEvaluation | None:
        if not self._shadow_evaluations:
            return None
        return self._shadow_evaluations[-1]

    def find_shadow_evaluation(self, *, decision_ids: list[str]) -> AIShadowEvaluation | None:
        target = tuple(decision_ids)
        if not target:
            return None
        for evaluation in reversed(self._shadow_evaluations):
            if tuple(evaluation.decision_ids) == target:
                return evaluation
        return None

    def shadow_evaluations_recent(self, *, limit: int) -> list[AIShadowEvaluation]:
        if limit <= 0:
            return []
        return list(reversed(self._shadow_evaluations[-limit:]))

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

    @staticmethod
    def _evict_oldest(d: dict, max_size: int) -> None:
        while len(d) > max_size:
            d.pop(next(iter(d)))
