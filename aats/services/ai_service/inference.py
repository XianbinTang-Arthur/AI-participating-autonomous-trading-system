from __future__ import annotations

from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext
from aats.services.ai_service.prompt_builder import PromptBuilder
from aats.services.ai_service.validator import AssessmentValidator


class AIInferenceService:
    def __init__(
        self,
        *,
        prompt_builder: PromptBuilder,
        validator: AssessmentValidator,
    ) -> None:
        self.prompt_builder = prompt_builder
        self.validator = validator

    def assess(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
    ) -> AIMarketAssessment:
        prompt = self.prompt_builder.build(context=context, baseline=baseline)
        directional_edge = {
            "long": 0.2,
            "short": -0.2,
            "flat": 0.0,
        }[baseline.direction_bias]
        assessment = AIMarketAssessment(
            decision_id=context.decision_id,
            symbol=context.symbol,
            regime=baseline.regime,
            directional_edge=directional_edge,
            expected_volatility=0.15 if baseline.volatility_state == "high" else 0.05,
            confidence=max(0.5, baseline.confidence),
            uncertainty=0.4,
            expected_holding_horizon=context.timeframe,
            invalidation_conditions=["market_structure_change"],
            risk_tags=["stub_ai_assessment"],
            rationale_summary=f"stub_assessment:{prompt}",
            model_name="stub-ai",
            model_version="0.1.0",
            prompt_version="0.1.0",
        )
        return self.validator.validate(assessment)
