from __future__ import annotations

from aats.schemas.decision import AIMarketAssessment


class AssessmentValidator:
    def validate(self, assessment: AIMarketAssessment) -> AIMarketAssessment:
        return assessment.model_copy(
            update={
                "confidence": min(max(assessment.confidence, 0.0), 1.0),
                "uncertainty": min(max(assessment.uncertainty, 0.0), 1.0),
            }
        )

