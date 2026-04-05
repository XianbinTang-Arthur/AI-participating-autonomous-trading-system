from __future__ import annotations

from aats.schemas.decision import BaselineAssessment, DecisionContext
from aats.schemas.features import AnalysisContext, DirectionalBias, FeatureSnapshot, RegimeIndicator
from aats.storage.base import EventStore


class BaselineStrategy:
    def __init__(self, *, event_store: EventStore) -> None:
        self.event_store = event_store

    def evaluate(self, context: DecisionContext) -> BaselineAssessment:
        feature_event = self.event_store.get(context.feature_snapshot_ref)
        if feature_event is None:
            raise RuntimeError("Feature snapshot reference is missing from the event store")

        features = FeatureSnapshot.model_validate(feature_event.payload)
        analysis = features.analysis_context
        alpha_score = features.composite_alpha_score
        microstructure_alpha = analysis.alpha_factors.microstructure_alpha if analysis is not None else 0.0
        direction_bias = self._direction_bias(
            alpha_score=alpha_score,
            regime_indicator=features.regime_indicator,
            microstructure_alpha=microstructure_alpha,
            directional_alignment=analysis.multi_timeframe.directional_alignment if analysis is not None else "flat",
        )
        position_scale = features.suggested_position_scale
        volatility_scale = features.volatility_target_scale
        confidence = min(
            max(
                0.35
                + (abs(alpha_score) * 0.35)
                + (features.regime_confidence * 0.2)
                + (position_scale * 0.1),
                0.4,
            ),
            0.96,
        )
        reason_codes = ["baseline_multi_factor_alpha", f"regime_{features.regime_indicator}"]
        factor_scores: dict[str, float] = {}
        if analysis is not None:
            reason_codes.append(f"mtf_alignment_{analysis.multi_timeframe.directional_alignment}")
            factor_scores = {
                "momentum_alpha": analysis.alpha_factors.momentum_alpha,
                "trend_alpha": analysis.alpha_factors.trend_alpha,
                "regime_alpha": analysis.alpha_factors.regime_alpha,
                "multi_timeframe_alpha": analysis.alpha_factors.multi_timeframe_alpha,
                "microstructure_alpha": analysis.alpha_factors.microstructure_alpha,
                "liquidity_scale": analysis.alpha_factors.liquidity_scale,
            }
            reason_codes.extend(self._factor_reason_codes(analysis))
            reason_codes.extend(
                self._microstructure_reason_codes(
                    direction_bias=direction_bias,
                    microstructure_alpha=analysis.alpha_factors.microstructure_alpha,
                )
            )
            if features.liquidity_score < 0.3:
                reason_codes.append("liquidity_thin")
        return BaselineAssessment(
            decision_id=context.decision_id,
            symbol=context.symbol,
            regime=features.regime_indicator,
            direction_bias=direction_bias,
            trend_strength=features.trend_strength,
            volatility_state=features.volatility_state,
            confidence=confidence,
            composite_alpha_score=alpha_score,
            suggested_position_scale=position_scale,
            volatility_target_scale=volatility_scale,
            factor_scores=factor_scores,
            holding_horizon=context.timeframe,
            invalidation_conditions=["feature_regime_flip"],
            reason_codes=reason_codes,
            engine_version="0.2.0",
        )

    @staticmethod
    def _direction_bias(
        *,
        alpha_score: float,
        regime_indicator: RegimeIndicator,
        microstructure_alpha: float,
        directional_alignment: DirectionalBias,
    ) -> DirectionalBias:
        breakout_threshold = 0.1
        trend_threshold = 0.16
        range_threshold = 0.24
        uncertain_threshold = 0.3
        alignment_bonus = 0.02 if directional_alignment in {"long", "short"} else 0.0
        same_sign = (
            alpha_score == 0.0
            or (alpha_score > 0.0 and microstructure_alpha > 0.0)
            or (alpha_score < 0.0 and microstructure_alpha < 0.0)
        )
        opposite_sign = (
            (alpha_score > 0.0 and microstructure_alpha < 0.0)
            or (alpha_score < 0.0 and microstructure_alpha > 0.0)
        )
        significant_micro = abs(microstructure_alpha) >= 0.08
        microstructure_support = significant_micro and same_sign
        microstructure_conflict = significant_micro and opposite_sign

        def adjusted_threshold(value: float) -> float:
            threshold = value - alignment_bonus if microstructure_support else value
            if microstructure_conflict:
                threshold += 0.04
            return max(threshold, 0.06)

        if regime_indicator == "breakout":
            threshold = adjusted_threshold(breakout_threshold)
            if alpha_score >= threshold:
                return "long"
            if alpha_score <= -threshold:
                return "short"
            return "flat"
        if regime_indicator == "trend":
            threshold = adjusted_threshold(trend_threshold)
            if alpha_score >= threshold:
                return "long"
            if alpha_score <= -threshold:
                return "short"
            return "flat"
        if regime_indicator == "range":
            threshold = adjusted_threshold(range_threshold)
            if alpha_score >= threshold and microstructure_alpha >= -0.02:
                return "long"
            if alpha_score <= -threshold and microstructure_alpha <= 0.02:
                return "short"
            return "flat"
        if regime_indicator == "uncertain":
            if alpha_score >= adjusted_threshold(uncertain_threshold) and microstructure_alpha > 0.1:
                return "long"
            if alpha_score <= -adjusted_threshold(uncertain_threshold) and microstructure_alpha < -0.1:
                return "short"
        return "flat"

    @staticmethod
    def _factor_reason_codes(analysis: AnalysisContext) -> list[str]:
        reason_codes: list[str] = []
        factors = analysis.alpha_factors
        if abs(factors.momentum_alpha) >= 0.2:
            reason_codes.append("alpha_momentum_support")
        if abs(factors.trend_alpha) >= 0.15:
            reason_codes.append("alpha_trend_support")
        if abs(factors.regime_alpha) >= 0.15:
            reason_codes.append("alpha_regime_support")
        if abs(factors.multi_timeframe_alpha) >= 0.15:
            reason_codes.append("alpha_multi_timeframe_support")
        if analysis.position_sizing.volatility_target_scale < 0.8:
            reason_codes.append("volatility_targeting_reduced_size")
        elif analysis.position_sizing.volatility_target_scale > 1.05:
            reason_codes.append("volatility_targeting_expanded_size")
        return reason_codes

    @staticmethod
    def _microstructure_reason_codes(
        *,
        direction_bias: DirectionalBias,
        microstructure_alpha: float,
    ) -> list[str]:
        if abs(microstructure_alpha) < 0.08:
            return ["microstructure_neutral"]
        if direction_bias == "long" and microstructure_alpha > 0.0:
            return ["microstructure_confirms_long"]
        if direction_bias == "short" and microstructure_alpha < 0.0:
            return ["microstructure_confirms_short"]
        if direction_bias == "flat":
            return ["microstructure_not_strong_enough"]
        return ["microstructure_conflicts_with_direction"]
