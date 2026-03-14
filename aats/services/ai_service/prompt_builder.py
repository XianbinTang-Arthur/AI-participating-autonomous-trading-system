from __future__ import annotations

from aats.schemas.decision import BaselineAssessment, DecisionContext
from aats.schemas.features import FeatureSnapshot


class PromptBuilder:
    def build(
        self,
        context: DecisionContext,
        baseline: BaselineAssessment,
        *,
        feature_snapshot: FeatureSnapshot | None = None,
        operating_mode: str,
    ) -> str:
        feature_context = ""
        if feature_snapshot is not None and feature_snapshot.analysis_context is not None:
            analysis = feature_snapshot.analysis_context
            feature_context = (
                f" feature_regime={feature_snapshot.regime_indicator}"
                f" feature_momentum={feature_snapshot.momentum_score:.6f}"
                f" feature_liquidity={feature_snapshot.liquidity_score:.6f}"
                f" mtf_alignment={analysis.multi_timeframe.directional_alignment}"
                f" regime_confidence={feature_snapshot.regime_confidence:.6f}"
            )
        return (
            f"decision_id={context.decision_id} "
            f"symbol={context.symbol} "
            f"timeframe={context.timeframe} "
            f"ai_mode={operating_mode} "
            f"baseline_regime={baseline.regime} "
            f"baseline_bias={baseline.direction_bias}"
            f"{feature_context} "
            "Return a strict JSON object with regime, directional_edge, expected_volatility, "
            "confidence, uncertainty, expected_holding_horizon, invalidation_conditions, risk_tags, rationale_summary."
        )
