from __future__ import annotations

from pydantic import ValidationError

from aats.schemas.decision import (
    AIProviderAssessmentOutput,
    AIMarketAssessment,
    AIOperatingMode,
    BaselineAssessment,
    DecisionContext,
)


class AIOutputValidationError(ValueError):
    pass


class AssessmentValidator:
    def output_schema(self) -> dict[str, object]:
        return AIProviderAssessmentOutput.model_json_schema()

    def validate_provider_output(
        self,
        *,
        raw_output: dict[str, object],
        context: DecisionContext,
        baseline: BaselineAssessment,
        operating_mode: AIOperatingMode,
        provider_name: str,
        provider_request_id: str | None,
        provider_latency_ms: float | None,
        model_name: str,
        model_version: str,
        prompt_version: str,
        degraded: bool,
    ) -> AIMarketAssessment:
        try:
            payload = AIProviderAssessmentOutput.model_validate(raw_output)
        except ValidationError as exc:
            raise AIOutputValidationError("ai_output_schema_validation_failed") from exc

        if payload.expected_holding_horizon != context.timeframe:
            raise AIOutputValidationError("ai_output_timeframe_mismatch")

        confidence = self._clamp(payload.confidence)
        uncertainty = self._clamp(payload.uncertainty)
        calibrated_confidence = self._calibrate_confidence(
            confidence=confidence,
            uncertainty=uncertainty,
            baseline_confidence=baseline.confidence,
        )
        return AIMarketAssessment(
            decision_id=context.decision_id,
            symbol=context.symbol,
            regime=payload.regime,
            directional_edge=payload.directional_edge,
            expected_volatility=max(payload.expected_volatility, 0.0),
            confidence=confidence,
            uncertainty=uncertainty,
            expected_holding_horizon=payload.expected_holding_horizon,
            invalidation_conditions=list(payload.invalidation_conditions),
            risk_tags=list(payload.risk_tags),
            rationale_summary=payload.rationale_summary,
            operating_mode=operating_mode,
            provider_name=provider_name,
            provider_request_id=provider_request_id,
            provider_latency_ms=provider_latency_ms,
            output_valid=True,
            fallback_used=False,
            fallback_reason=None,
            degraded=degraded,
            calibrated_confidence=calibrated_confidence,
            evaluation_tags=["output_valid", "confidence_calibrated"],
            model_name=model_name,
            model_version=model_version,
            prompt_version=prompt_version,
        )

    def fallback_assessment(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        operating_mode: AIOperatingMode,
        fallback_reason: str,
        degraded: bool,
        output_valid: bool,
        model_name: str,
        model_version: str,
        prompt_version: str,
    ) -> AIMarketAssessment:
        directional_edge = {
            "long": 0.2,
            "short": -0.2,
            "flat": 0.0,
        }[baseline.direction_bias]
        calibrated_confidence = self._calibrate_confidence(
            confidence=baseline.confidence,
            uncertainty=max(0.0, 1.0 - baseline.confidence),
            baseline_confidence=baseline.confidence,
        )
        tags = ["fallback", fallback_reason]
        if degraded:
            tags.append("degraded")
        return AIMarketAssessment(
            decision_id=context.decision_id,
            symbol=context.symbol,
            regime=baseline.regime,
            directional_edge=directional_edge,
            expected_volatility=0.15 if baseline.volatility_state == "high" else 0.05,
            confidence=self._clamp(baseline.confidence),
            uncertainty=self._clamp(1.0 - baseline.confidence),
            expected_holding_horizon=context.timeframe,
            invalidation_conditions=["market_structure_change"],
            risk_tags=["fallback_ai_assessment"],
            rationale_summary=f"fallback_assessment:{fallback_reason}",
            operating_mode=operating_mode,
            provider_name="baseline_fallback",
            provider_request_id=None,
            provider_latency_ms=None,
            output_valid=output_valid,
            fallback_used=True,
            fallback_reason=fallback_reason,
            degraded=degraded,
            calibrated_confidence=calibrated_confidence,
            evaluation_tags=tags,
            model_name=model_name,
            model_version=model_version,
            prompt_version=prompt_version,
        )

    @staticmethod
    def _calibrate_confidence(*, confidence: float, uncertainty: float, baseline_confidence: float) -> float:
        return min(max((confidence * (1.0 - uncertainty) * 0.7) + (baseline_confidence * 0.3), 0.0), 1.0)

    @staticmethod
    def _clamp(value: float) -> float:
        return min(max(value, 0.0), 1.0)
