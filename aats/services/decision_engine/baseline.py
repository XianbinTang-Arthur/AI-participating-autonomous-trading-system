from __future__ import annotations

from typing import TYPE_CHECKING

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import BaselineAssessment, DecisionContext
from aats.schemas.features import AnalysisContext, DirectionalBias, FeatureSnapshot, RegimeIndicator
from aats.storage.base import EventStore

if TYPE_CHECKING:
    from aats.services.decision_engine.feature_resolver import FeatureSnapshotResolver


class BaselineStrategy:
    def __init__(
        self,
        *,
        event_store: EventStore,
        feature_resolver: "FeatureSnapshotResolver | None" = None,
        settings: AATSSettings | None = None,
    ) -> None:
        self.event_store = event_store
        self._feature_resolver = feature_resolver
        self.settings = settings or AATSSettings()

    def evaluate(self, context: DecisionContext) -> BaselineAssessment:
        # 按 feature_snapshot_ref(event_id) 精确读取，保证同一 context 行情基准一致。
        # resolver 路径: stream_cache.get(ref) -> event_store.get(ref)
        if self._feature_resolver is not None:
            feature_event = self._feature_resolver.resolve(context.feature_snapshot_ref)
        else:
            feature_event = self.event_store.get(context.feature_snapshot_ref)
        if feature_event is None:
            raise RuntimeError("Feature snapshot reference is missing from the event store")

        features = FeatureSnapshot.model_validate(feature_event.payload)
        analysis = features.analysis_context
        alpha_score = features.composite_alpha_score
        microstructure_alpha = analysis.alpha_factors.microstructure_alpha if analysis is not None else 0.0
        direction_bias, direction_rule, direction_threshold = self._direction_bias(
            alpha_score=alpha_score,
            regime_indicator=features.regime_indicator,
            microstructure_alpha=microstructure_alpha,
            directional_alignment=analysis.multi_timeframe.directional_alignment if analysis is not None else "flat",
            analysis=analysis,
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
        if direction_rule:
            reason_codes.append(direction_rule)
        if direction_threshold is not None:
            reason_codes.append(
                f"baseline_direction_threshold_{features.regime_indicator}_{direction_threshold:.3f}".replace(".", "_")
            )
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
            direction_threshold=direction_threshold,
            direction_rule=direction_rule,
            factor_scores=factor_scores,
            holding_horizon=context.timeframe,
            invalidation_conditions=["feature_regime_flip"],
            reason_codes=reason_codes,
            engine_version="0.2.0",
        )

    def _direction_bias(
        self,
        *,
        alpha_score: float,
        regime_indicator: RegimeIndicator,
        microstructure_alpha: float,
        directional_alignment: DirectionalBias,
        analysis: AnalysisContext | None,
    ) -> tuple[DirectionalBias, str | None, float | None]:
        impulse_bias = self._impulse_override_bias(
            alpha_score=alpha_score,
            regime_indicator=regime_indicator,
            microstructure_alpha=microstructure_alpha,
            directional_alignment=directional_alignment,
            analysis=analysis,
        )
        if impulse_bias != "flat":
            return impulse_bias, f"baseline_impulse_override_{impulse_bias}", None

        breakout_threshold = float(self.settings.strategy_baseline_breakout_alpha_threshold)
        trend_threshold = float(self.settings.strategy_baseline_trend_alpha_threshold)
        range_threshold = float(self.settings.strategy_baseline_range_alpha_threshold)
        uncertain_threshold = float(self.settings.strategy_baseline_uncertain_alpha_threshold)
        alignment_bonus = (
            float(self.settings.strategy_baseline_alignment_bonus)
            if directional_alignment in {"long", "short"}
            else 0.0
        )
        same_sign = (
            alpha_score == 0.0
            or (alpha_score > 0.0 and microstructure_alpha > 0.0)
            or (alpha_score < 0.0 and microstructure_alpha < 0.0)
        )
        opposite_sign = (
            (alpha_score > 0.0 and microstructure_alpha < 0.0)
            or (alpha_score < 0.0 and microstructure_alpha > 0.0)
        )
        significant_micro = abs(microstructure_alpha) >= float(
            self.settings.strategy_baseline_significant_microstructure_threshold
        )
        microstructure_support = significant_micro and same_sign
        microstructure_conflict = significant_micro and opposite_sign

        def adjusted_threshold(value: float) -> float:
            threshold = value - alignment_bonus if microstructure_support else value
            if microstructure_conflict:
                threshold += float(self.settings.strategy_baseline_microstructure_conflict_penalty)
            return max(threshold, float(self.settings.strategy_baseline_min_threshold_floor))

        if regime_indicator == "breakout":
            threshold = adjusted_threshold(breakout_threshold)
            if alpha_score >= threshold:
                return "long", "baseline_regime_breakout_threshold_crossed", threshold
            if alpha_score <= -threshold:
                return "short", "baseline_regime_breakout_threshold_crossed", threshold
            return "flat", "baseline_regime_breakout_threshold_not_met", threshold
        if regime_indicator == "trend":
            threshold = adjusted_threshold(trend_threshold)
            if alpha_score >= threshold:
                return "long", "baseline_regime_trend_threshold_crossed", threshold
            if alpha_score <= -threshold:
                return "short", "baseline_regime_trend_threshold_crossed", threshold
            return "flat", "baseline_regime_trend_threshold_not_met", threshold
        if regime_indicator == "range":
            threshold = adjusted_threshold(range_threshold)
            range_micro_floor = float(self.settings.strategy_baseline_range_microstructure_floor)
            if alpha_score >= threshold and microstructure_alpha >= range_micro_floor:
                return "long", "baseline_regime_range_threshold_crossed", threshold
            if alpha_score <= -threshold and microstructure_alpha <= -range_micro_floor:
                return "short", "baseline_regime_range_threshold_crossed", threshold
            return "flat", "baseline_regime_range_threshold_not_met", threshold
        if regime_indicator == "uncertain":
            threshold = adjusted_threshold(uncertain_threshold)
            uncertain_micro = float(self.settings.strategy_baseline_uncertain_microstructure_threshold)
            if alpha_score >= threshold and microstructure_alpha > uncertain_micro:
                return "long", "baseline_regime_uncertain_threshold_crossed", threshold
            if alpha_score <= -threshold and microstructure_alpha < -uncertain_micro:
                return "short", "baseline_regime_uncertain_threshold_crossed", threshold
            return "flat", "baseline_regime_uncertain_threshold_not_met", threshold
        return "flat", "baseline_direction_bias_flat", None

    def _impulse_override_bias(
        self,
        *,
        alpha_score: float,
        regime_indicator: RegimeIndicator,
        microstructure_alpha: float,
        directional_alignment: DirectionalBias,
        analysis: AnalysisContext | None,
    ) -> DirectionalBias:
        if (
            not self.settings.strategy_baseline_impulse_override_enabled
            or analysis is None
            or regime_indicator not in set(self.settings.strategy_baseline_impulse_allowed_regimes)
        ):
            return "flat"
        primary = analysis.timeframe_features.get("15m")
        if primary is None:
            return "flat"
        body_ratio = float(primary.candle_body_ratio)
        range_ratio = float(primary.range_ratio)
        momentum_score = float(primary.momentum_score)
        alpha_min = float(self.settings.strategy_baseline_impulse_alpha_min)
        micro_min = float(self.settings.strategy_baseline_impulse_microstructure_min)
        momentum_min = float(self.settings.strategy_baseline_impulse_momentum_min)
        range_min = float(self.settings.strategy_baseline_impulse_range_ratio_min)
        body_min = float(self.settings.strategy_baseline_impulse_body_ratio_min)

        def aligned(direction: DirectionalBias) -> bool:
            if not self.settings.strategy_baseline_impulse_require_mtf_alignment:
                return True
            return directional_alignment == direction

        if (
            alpha_score >= alpha_min
            and microstructure_alpha >= micro_min
            and momentum_score >= momentum_min
            and range_ratio >= range_min
            and body_ratio >= body_min
            and aligned("long")
        ):
            return "long"
        if (
            alpha_score <= -alpha_min
            and microstructure_alpha <= -micro_min
            and momentum_score <= -momentum_min
            and range_ratio >= range_min
            and body_ratio >= body_min
            and aligned("short")
        ):
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
