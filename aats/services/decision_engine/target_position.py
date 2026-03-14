from __future__ import annotations

from datetime import timedelta

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import utc_now
from aats.schemas.decision import AIMarketAssessment, BaselineAssessment, DecisionContext, PositionTarget


class TargetPositionEngine:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    def build(
        self,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
    ) -> PositionTarget:
        target_qty = self._target_quantity(baseline=baseline, ai_assessment=ai_assessment)
        source_mix = self._source_mix(ai_assessment=ai_assessment)
        rebalance_reason = f"{self.settings.ai_operating_mode}_decision"

        return PositionTarget(
            decision_id=context.decision_id,
            symbol=context.symbol,
            current_position_qty=context.current_position_qty,
            target_position_qty=target_qty,
            delta_position_qty=target_qty - context.current_position_qty,
            current_notional=0.0,
            target_notional=0.0,
            rebalance_reason=rebalance_reason,
            urgency="medium" if target_qty != context.current_position_qty else "low",
            max_slippage_tolerance_bps=self.settings.max_slippage_tolerance_bps,
            source_mix=source_mix,
            decision_expiry_ts=utc_now() + timedelta(minutes=15),
        )

    def _target_quantity(
        self,
        *,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
    ) -> float:
        mode = self.settings.ai_operating_mode
        if mode in {"baseline_only", "ai_advisory"}:
            return self._qty_from_bias(baseline.direction_bias)
        if mode == "ai_blended":
            if baseline.direction_bias == "long" and ai_assessment.directional_edge >= 0.0:
                return self.settings.default_order_qty
            if baseline.direction_bias == "short" and ai_assessment.directional_edge <= 0.0:
                return -self.settings.default_order_qty
            return 0.0
        if mode == "ai_primary":
            if (
                not ai_assessment.fallback_used
                and ai_assessment.output_valid
                and ai_assessment.calibrated_confidence >= self.settings.ai_primary_min_confidence
            ):
                if ai_assessment.directional_edge > 0.0:
                    return self.settings.default_order_qty
                if ai_assessment.directional_edge < 0.0:
                    return -self.settings.default_order_qty
            return self._qty_from_bias(baseline.direction_bias)
        return self._qty_from_bias(baseline.direction_bias)

    def _qty_from_bias(self, direction_bias: str) -> float:
        if direction_bias == "long":
            return self.settings.default_order_qty
        if direction_bias == "short":
            return -self.settings.default_order_qty
        return 0.0

    def _source_mix(self, *, ai_assessment: AIMarketAssessment) -> dict[str, float]:
        mode = self.settings.ai_operating_mode
        if mode in {"baseline_only", "ai_advisory"}:
            return {"baseline": 1.0, "ai": 0.0}
        if mode == "ai_blended":
            return {"baseline": 0.6, "ai": 0.4}
        if mode == "ai_primary" and not ai_assessment.fallback_used:
            return {"baseline": 0.2, "ai": 0.8}
        return {"baseline": 1.0, "ai": 0.0}
