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
        target_qty = self._target_quantity(
            current_position_qty=context.current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
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
            urgency=self._urgency(
                current_position_qty=context.current_position_qty,
                target_position_qty=target_qty,
            ),
            max_slippage_tolerance_bps=self.settings.max_slippage_tolerance_bps,
            source_mix=source_mix,
            decision_expiry_ts=utc_now() + timedelta(minutes=15),
        )

    def _target_quantity(
        self,
        *,
        current_position_qty: float,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
    ) -> float:
        mode = self.settings.ai_operating_mode
        baseline_qty = self._baseline_target_qty(baseline=baseline)
        if mode in {"baseline_only", "ai_advisory"}:
            return self._apply_position_management(
                current_position_qty=current_position_qty,
                desired_target_qty=baseline_qty,
            )
        if mode == "ai_blended":
            if baseline.direction_bias == "long" and ai_assessment.directional_edge >= 0.0:
                return self._apply_position_management(
                    current_position_qty=current_position_qty,
                    desired_target_qty=baseline_qty,
                )
            if baseline.direction_bias == "short" and ai_assessment.directional_edge <= 0.0:
                return self._apply_position_management(
                    current_position_qty=current_position_qty,
                    desired_target_qty=baseline_qty,
                )
            return 0.0
        if mode == "ai_primary":
            if (
                not ai_assessment.fallback_used
                and ai_assessment.output_valid
                and ai_assessment.calibrated_confidence >= self.settings.ai_primary_min_confidence
            ):
                if ai_assessment.directional_edge > 0.0:
                    return self._apply_position_management(
                        current_position_qty=current_position_qty,
                        desired_target_qty=abs(baseline_qty) or self.settings.default_order_qty * 0.35,
                    )
                if ai_assessment.directional_edge < 0.0:
                    return self._apply_position_management(
                        current_position_qty=current_position_qty,
                        desired_target_qty=-(abs(baseline_qty) or self.settings.default_order_qty * 0.35),
                    )
            return self._apply_position_management(
                current_position_qty=current_position_qty,
                desired_target_qty=baseline_qty,
            )
        return self._apply_position_management(
            current_position_qty=current_position_qty,
            desired_target_qty=baseline_qty,
        )

    def _baseline_target_qty(self, *, baseline: BaselineAssessment) -> float:
        scale = self._clamp(baseline.suggested_position_scale, 0.0, 1.0)
        target_qty = self._qty_from_bias(baseline.direction_bias) * scale
        if baseline.volatility_target_scale < 0.55:
            target_qty *= baseline.volatility_target_scale
        return target_qty

    def _qty_from_bias(self, direction_bias: str) -> float:
        if direction_bias == "long":
            return self.settings.default_order_qty
        if direction_bias == "short":
            return -self.settings.default_order_qty
        return 0.0

    def _apply_position_management(
        self,
        *,
        current_position_qty: float,
        desired_target_qty: float,
    ) -> float:
        rebalance_band = max(self.settings.default_order_qty * 0.15, abs(desired_target_qty) * 0.1, 1e-12)
        delta_qty = desired_target_qty - current_position_qty
        if abs(delta_qty) <= rebalance_band:
            return current_position_qty

        if self._same_direction(current_position_qty, desired_target_qty) and abs(desired_target_qty) > abs(current_position_qty):
            max_step = max(self.settings.default_order_qty * 0.35, abs(desired_target_qty) * 0.5)
            step = min(abs(delta_qty), max_step)
            return current_position_qty + (self._sign(delta_qty) * step)
        return desired_target_qty

    def _urgency(self, *, current_position_qty: float, target_position_qty: float) -> str:
        delta_qty = abs(target_position_qty - current_position_qty)
        if delta_qty < 1e-12:
            return "low"
        if delta_qty >= self.settings.default_order_qty * 0.75:
            return "high"
        return "medium"

    def _source_mix(self, *, ai_assessment: AIMarketAssessment) -> dict[str, float]:
        mode = self.settings.ai_operating_mode
        if mode in {"baseline_only", "ai_advisory"}:
            return {"baseline": 1.0, "ai": 0.0}
        if mode == "ai_blended":
            return {"baseline": 0.6, "ai": 0.4}
        if mode == "ai_primary" and not ai_assessment.fallback_used:
            return {"baseline": 0.2, "ai": 0.8}
        return {"baseline": 1.0, "ai": 0.0}

    @staticmethod
    def _same_direction(left: float, right: float) -> bool:
        if abs(left) < 1e-12 or abs(right) < 1e-12:
            return True
        return (left > 0 and right > 0) or (left < 0 and right < 0)

    @staticmethod
    def _sign(value: float) -> float:
        if value > 0.0:
            return 1.0
        if value < 0.0:
            return -1.0
        return 0.0

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))
