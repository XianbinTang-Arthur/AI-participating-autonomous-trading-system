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
        target_qty = 0.0
        if baseline.direction_bias == "long" and ai_assessment.directional_edge >= 0.0:
            target_qty = self.settings.default_order_qty
        elif baseline.direction_bias == "short" and ai_assessment.directional_edge <= 0.0:
            target_qty = -self.settings.default_order_qty

        return PositionTarget(
            decision_id=context.decision_id,
            symbol=context.symbol,
            current_position_qty=context.current_position_qty,
            target_position_qty=target_qty,
            delta_position_qty=target_qty - context.current_position_qty,
            current_notional=0.0,
            target_notional=0.0,
            rebalance_reason="baseline_ai_fusion",
            urgency="medium" if target_qty != context.current_position_qty else "low",
            max_slippage_tolerance_bps=self.settings.max_slippage_tolerance_bps,
            source_mix={"baseline": 0.6, "ai": 0.4},
            decision_expiry_ts=utc_now() + timedelta(minutes=15),
        )

