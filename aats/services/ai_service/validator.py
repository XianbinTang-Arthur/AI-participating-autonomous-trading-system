from __future__ import annotations

from decimal import Decimal

from pydantic import ValidationError

from aats.schemas.ai_brief import AIDecisionBrief
from aats.schemas.decision import (
    AIExecutionParameterSuggestionOutput,
    AIProviderAssessmentOutput,
    AIProviderAssessmentWithExecutionSuggestionOutput,
    AIMarketAssessment,
    AIOperatingMode,
    BaselineAssessment,
    DecisionContext,
)
from aats.schemas.execution import AIExecutionParameterSuggestionEnvelope, ExecutionParameterSuggestion
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal


class AIOutputValidationError(ValueError):
    pass


class AssessmentValidator:
    _ALLOWED_REGIMES = {"trend", "range", "breakout", "uncertain"}

    def output_schema(self, *, include_execution_suggestion: bool = False) -> dict[str, object]:
        schema_model = (
            AIProviderAssessmentWithExecutionSuggestionOutput
            if include_execution_suggestion
            else AIProviderAssessmentOutput
        )
        return schema_model.model_json_schema()

    def validate_provider_output(
        self,
        *,
        raw_output: dict[str, object],
        brief: AIDecisionBrief,
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
        edge_bps_scale: float,
    ) -> AIMarketAssessment:
        try:
            schema_model = (
                AIProviderAssessmentWithExecutionSuggestionOutput
                if "execution_parameter_suggestion" in raw_output
                else AIProviderAssessmentOutput
            )
            payload = schema_model.model_validate(raw_output)
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
        validation_flags: list[str] = []
        rejection_flags: list[str] = []
        execution_parameter_suggestion, execution_suggestion_flags = self._validate_execution_parameter_suggestion(
            getattr(payload, "execution_parameter_suggestion", None)
        )
        validation_flags.extend(execution_suggestion_flags)

        regime = payload.regime.lower()
        if regime not in self._ALLOWED_REGIMES:
            rejection_flags.append("unsupported_regime")
        directional_edge = float(payload.directional_edge)
        if abs(directional_edge) > 1.0:
            rejection_flags.append("directional_edge_out_of_range")
        if confidence >= 0.75 and uncertainty >= 0.45:
            rejection_flags.append("confidence_uncertainty_incoherent")
        if abs(directional_edge) >= 0.3 and confidence < 0.55:
            rejection_flags.append("strong_edge_low_confidence")
        if payload.baseline_override_recommended and not payload.override_reason_codes:
            rejection_flags.append("override_requires_reason_codes")
        if abs(directional_edge) >= 0.25 and len(payload.invalidation_conditions) < 2:
            rejection_flags.append("strong_direction_requires_invalidation_conditions")
        if brief.execution_condition != "normal" and payload.baseline_override_recommended:
            rejection_flags.append("override_not_allowed_during_execution_degradation")
        if not brief.safe_to_trade and payload.baseline_override_recommended:
            rejection_flags.append("override_not_allowed_when_not_safe_to_trade")

        expected_volatility = max(float(payload.expected_volatility), 0.0)
        estimated_edge_bps = abs(to_decimal(directional_edge)) * max(to_decimal(edge_bps_scale), Decimal("0"))
        estimated_cost_bps = (
            max(brief.fee_bps, Decimal("0"))
            + max(brief.funding_fee_bps, Decimal("0"))
            + max(brief.expected_slippage_proxy_bps, Decimal("0"))
        )
        estimated_net_edge_bps = estimated_edge_bps - estimated_cost_bps
        economically_actionable = estimated_net_edge_bps + EPSILON_DECIMAL_12 >= max(brief.min_net_edge_bps, Decimal("0"))
        if not economically_actionable:
            validation_flags.append("low_edge")
        if degraded:
            validation_flags.append("provider_degraded")
        if brief.execution_condition != "normal":
            validation_flags.append("execution_condition_not_normal")

        return AIMarketAssessment(
            decision_id=context.decision_id,
            symbol=context.symbol,
            regime=payload.regime,
            directional_edge=directional_edge,
            expected_volatility=expected_volatility,
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
            output_valid=not rejection_flags,
            fallback_used=False,
            fallback_reason=None,
            degraded=degraded,
            calibrated_confidence=calibrated_confidence,
            baseline_override_recommended=payload.baseline_override_recommended,
            override_reason_codes=list(payload.override_reason_codes),
            economically_actionable=economically_actionable and not rejection_flags,
            estimated_edge_bps=float(estimated_edge_bps),
            estimated_cost_bps=float(estimated_cost_bps),
            estimated_net_edge_bps=float(estimated_net_edge_bps),
            validation_flags=validation_flags,
            rejection_flags=rejection_flags,
            source_mode="provider",
            execution_condition=brief.execution_condition,
            evaluation_tags=["output_valid" if not rejection_flags else "output_rejected", "confidence_calibrated"],
            ai_execution_parameter_suggestion=execution_parameter_suggestion,
            model_name=model_name,
            model_version=model_version,
            prompt_version=prompt_version,
        )

    def fallback_assessment(
        self,
        *,
        brief: AIDecisionBrief | None,
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
        estimated_edge_bps = abs(to_decimal(directional_edge)) * Decimal("100")
        estimated_cost_bps = (
            Decimal("0")
            if brief is None
            else (
                max(brief.fee_bps, Decimal("0"))
                + max(brief.funding_fee_bps, Decimal("0"))
                + max(brief.expected_slippage_proxy_bps, Decimal("0"))
            )
        )
        estimated_net_edge_bps = estimated_edge_bps - estimated_cost_bps
        economically_actionable = estimated_net_edge_bps + EPSILON_DECIMAL_12 >= (
            Decimal("0")
            if brief is None
            else max(brief.min_net_edge_bps, Decimal("0"))
        )
        tags = ["fallback", fallback_reason]
        if degraded:
            tags.append("degraded")
        validation_flags: list[str] = []
        if not economically_actionable:
            validation_flags.append("low_edge")
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
            baseline_override_recommended=False,
            override_reason_codes=[],
            economically_actionable=economically_actionable,
            estimated_edge_bps=float(estimated_edge_bps),
            estimated_cost_bps=float(estimated_cost_bps),
            estimated_net_edge_bps=float(estimated_net_edge_bps),
            validation_flags=validation_flags,
            rejection_flags=[],
            source_mode="fallback",
            execution_condition=None if brief is None else brief.execution_condition,
            evaluation_tags=tags,
            ai_execution_parameter_suggestion=None,
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

    def _validate_execution_parameter_suggestion(
        self,
        suggestion_payload: AIExecutionParameterSuggestionOutput | None,
    ) -> tuple[AIExecutionParameterSuggestionEnvelope | None, list[str]]:
        if not suggestion_payload:
            return None, []
        suggestion_raw = suggestion_payload.model_dump(mode="python")
        suggestion = ExecutionParameterSuggestion()
        clipped_fields: list[str] = []
        notes: list[str] = []

        def clip_decimal(
            raw_value: float | int | None,
            *,
            lower: Decimal,
            upper: Decimal,
            field_name: str,
        ) -> Decimal | None:
            if raw_value is None:
                return None
            value = to_decimal(raw_value)
            clipped = min(max(value, lower), upper)
            if clipped != value:
                clipped_fields.append(field_name)
                notes.append(f"{field_name}_clamped")
            return clipped

        def clip_int(
            raw_value: float | int | None,
            *,
            lower: int,
            upper: int,
            field_name: str,
        ) -> int | None:
            if raw_value is None:
                return None
            value = int(raw_value)
            clipped = min(max(value, lower), upper)
            if clipped != value:
                clipped_fields.append(field_name)
                notes.append(f"{field_name}_clamped")
            return clipped

        suggestion.passive_bias = clip_decimal(
            suggestion_raw.get("passive_bias"),
            lower=Decimal("0"),
            upper=Decimal("1"),
            field_name="passive_bias",
        )
        suggestion.maker_taker_bias = clip_decimal(
            suggestion_raw.get("maker_taker_bias"),
            lower=Decimal("-1"),
            upper=Decimal("1"),
            field_name="maker_taker_bias",
        )
        suggestion.max_cross_spread_bps = clip_decimal(
            suggestion_raw.get("max_cross_spread_bps"),
            lower=Decimal("0"),
            upper=Decimal("50"),
            field_name="max_cross_spread_bps",
        )
        suggestion.slice_count = clip_int(
            suggestion_raw.get("slice_count"),
            lower=1,
            upper=20,
            field_name="slice_count",
        )
        suggestion.max_participation_rate = clip_decimal(
            suggestion_raw.get("max_participation_rate"),
            lower=Decimal("0"),
            upper=Decimal("1"),
            field_name="max_participation_rate",
        )
        suggestion.cancel_replace_patience_ms = clip_int(
            suggestion_raw.get("cancel_replace_patience_ms"),
            lower=0,
            upper=60_000,
            field_name="cancel_replace_patience_ms",
        )

        if not any(value is not None for value in suggestion.model_dump(mode="python").values()):
            return None, []

        flags = ["execution_suggestion_present"]
        if clipped_fields:
            flags.append("execution_suggestion_clipped")
        return (
            AIExecutionParameterSuggestionEnvelope(
                status="diagnostic_only",
                diagnostic_only=True,
                requested_mode="diagnostic_only",
                suggestion=suggestion,
                accepted_by_execution_planner=False,
                applied_to_live_execution=False,
                clipped_fields=clipped_fields,
                rejection_reasons=[],
                notes=notes or ["execution_suggestion_validated"],
            ),
            flags,
        )
