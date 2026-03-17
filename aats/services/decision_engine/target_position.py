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
            product_type=context.product_type,
        )
        if not self._short_bias_allowed(context.product_type):
            target_qty = self._normalize_long_only_target(
                current_position_qty=context.current_position_qty,
                target_qty=target_qty,
                baseline=baseline,
                ai_assessment=ai_assessment,
            )
        target_exposure_side = self._exposure_side(target_qty)
        position_intent = self._position_intent(
            current_position_qty=context.current_position_qty,
            target_position_qty=target_qty,
        )
        target_leverage = self._target_leverage(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            target_qty=target_qty,
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
            product_type=context.product_type,
            current_exposure_side=context.current_exposure_side,
            target_exposure_side=target_exposure_side,
            position_intent=position_intent,
            target_leverage=target_leverage,
            margin_mode=self.settings.margin_mode,
            leverage_bias=self._leverage_bias(
                baseline=baseline,
                ai_assessment=ai_assessment,
            ),
        )

    def _target_quantity(
        self,
        *,
        current_position_qty: float,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
        product_type: str,
    ) -> float:
        mode = self.settings.ai_operating_mode
        baseline_qty = self._baseline_target_qty(baseline=baseline, product_type=product_type)
        baseline_qty = self._apply_entry_edge_gate(
            current_position_qty=current_position_qty,
            desired_target_qty=baseline_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        if mode in {"baseline_only", "ai_advisory"}:
            if self._should_hold_on_flat_signal(
                current_position_qty=current_position_qty,
                desired_target_qty=baseline_qty,
                baseline=baseline,
                ai_assessment=ai_assessment,
                product_type=product_type,
            ):
                return current_position_qty
            return self._apply_position_management(
                current_position_qty=current_position_qty,
                desired_target_qty=baseline_qty,
                product_type=product_type,
            )
        if mode == "ai_blended":
            if baseline.direction_bias == "long" and ai_assessment.directional_edge >= 0.0:
                return self._apply_position_management(
                    current_position_qty=current_position_qty,
                    desired_target_qty=baseline_qty,
                    product_type=product_type,
                )
            if baseline.direction_bias == "short" and ai_assessment.directional_edge <= 0.0:
                return self._apply_position_management(
                    current_position_qty=current_position_qty,
                    desired_target_qty=baseline_qty,
                    product_type=product_type,
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
                        product_type=product_type,
                    )
                if ai_assessment.directional_edge < 0.0:
                    return self._apply_position_management(
                        current_position_qty=current_position_qty,
                        desired_target_qty=-(abs(baseline_qty) or self.settings.default_order_qty * 0.35),
                        product_type=product_type,
                    )
            return self._apply_position_management(
                current_position_qty=current_position_qty,
                desired_target_qty=baseline_qty,
                product_type=product_type,
            )
        return self._apply_position_management(
            current_position_qty=current_position_qty,
            desired_target_qty=baseline_qty,
            product_type=product_type,
        )

    def _baseline_target_qty(self, *, baseline: BaselineAssessment, product_type: str) -> float:
        scale = self._clamp(baseline.suggested_position_scale, 0.0, 1.0)
        target_qty = self._qty_from_bias(baseline.direction_bias, product_type=product_type) * scale
        if baseline.volatility_target_scale < 0.55:
            target_qty *= baseline.volatility_target_scale
        return target_qty

    def _apply_entry_edge_gate(
        self,
        *,
        current_position_qty: float,
        desired_target_qty: float,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
    ) -> float:
        if not self.settings.strategy_cost_guard_enabled:
            return desired_target_qty
        if abs(desired_target_qty) < 1e-12:
            return desired_target_qty
        if self._same_direction(current_position_qty, desired_target_qty) and abs(desired_target_qty) <= abs(current_position_qty):
            return desired_target_qty
        estimated_cost_bps = self._estimated_trade_cost_bps()
        required_edge_bps = estimated_cost_bps + max(self.settings.strategy_min_net_edge_bps, 0.0)
        signal_edge_bps = self._signal_edge_bps(baseline=baseline, ai_assessment=ai_assessment)
        if signal_edge_bps + 1e-12 >= required_edge_bps:
            return desired_target_qty
        return current_position_qty

    def _should_hold_on_flat_signal(
        self,
        *,
        current_position_qty: float,
        desired_target_qty: float,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
        product_type: str,
    ) -> bool:
        if not self.settings.strategy_flat_signal_hold_enabled:
            return False
        if product_type != "derivatives":
            return False
        if abs(current_position_qty) < 1e-12 or abs(desired_target_qty) > 1e-12:
            return False
        if baseline.direction_bias != "flat":
            return False
        return not self._explicit_flat_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )

    def _explicit_flat_exit_required(
        self,
        *,
        current_position_qty: float,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
    ) -> bool:
        side_sign = self._sign(current_position_qty)
        microstructure = baseline.factor_scores.get("microstructure_alpha", 0.0)
        momentum_alpha = baseline.factor_scores.get("momentum_alpha", 0.0)
        trend_alpha = baseline.factor_scores.get("trend_alpha", 0.0)
        ai_edge = ai_assessment.directional_edge
        adverse_microstructure = (side_sign * microstructure) <= -abs(self.settings.strategy_flat_exit_microstructure_threshold)
        adverse_momentum = (side_sign * momentum_alpha) <= -abs(self.settings.strategy_flat_exit_factor_threshold)
        adverse_trend = (side_sign * trend_alpha) <= -abs(self.settings.strategy_flat_exit_factor_threshold)
        adverse_ai = (side_sign * ai_edge) <= -abs(self.settings.strategy_flat_exit_ai_edge_threshold)
        adverse_count = sum((adverse_microstructure, adverse_momentum, adverse_trend, adverse_ai))
        if adverse_count >= 2:
            return True
        if adverse_microstructure and adverse_ai:
            return True
        return False

    def _qty_from_bias(self, direction_bias: str, *, product_type: str) -> float:
        if direction_bias == "long":
            return self.settings.default_order_qty
        if direction_bias == "short" and self._short_bias_allowed(product_type):
            return -self.settings.default_order_qty
        return 0.0

    def _target_leverage(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
        target_qty: float,
    ) -> float:
        if abs(target_qty) < 1e-12:
            return 1.0
        if context.product_type != "derivatives":
            return 1.0
        if not self.settings.strategy_dynamic_leverage_enabled:
            return min(max(self.settings.default_target_leverage, 1.0), self.settings.max_target_leverage)
        leverage_bias = self._leverage_bias(baseline=baseline, ai_assessment=ai_assessment)
        return self._clamp(
            max(1.0, self.settings.default_target_leverage * leverage_bias),
            1.0,
            self.settings.max_target_leverage,
        )

    def _leverage_bias(
        self,
        *,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
    ) -> float:
        conviction = max(
            0.0,
            (baseline.confidence * 0.45)
            + (abs(ai_assessment.directional_edge) * 0.35)
            + (max(ai_assessment.calibrated_confidence, ai_assessment.confidence) * 0.2),
        )
        if baseline.volatility_state == "high":
            conviction *= 0.62
        if baseline.regime == "breakout":
            conviction *= 1.08
        if baseline.regime in {"range", "uncertain"}:
            conviction *= 0.85
        microstructure = baseline.factor_scores.get("microstructure_alpha", 0.0)
        liquidity_scale = baseline.factor_scores.get("liquidity_scale", 1.0)
        conviction *= max(0.75, min(1.15, liquidity_scale + (abs(microstructure) * 0.2)))
        if microstructure and (
            (baseline.direction_bias == "long" and microstructure < 0.0)
            or (baseline.direction_bias == "short" and microstructure > 0.0)
        ):
            conviction *= 0.75
        if ai_assessment.degraded or ai_assessment.fallback_used:
            conviction *= 0.85
        return self._clamp(0.85 + conviction, 0.85, 2.5)

    def _short_bias_allowed(self, product_type: str) -> bool:
        return product_type == "derivatives"

    def _normalize_long_only_target(
        self,
        *,
        current_position_qty: float,
        target_qty: float,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
    ) -> float:
        if target_qty >= 0.0:
            bearish_signal = baseline.direction_bias == "short" or ai_assessment.directional_edge < 0.0
            if current_position_qty > 1e-12 and bearish_signal and target_qty < current_position_qty:
                return current_position_qty
            if current_position_qty > 1e-12 and baseline.direction_bias == "flat" and target_qty <= 1e-12:
                if current_position_qty <= self._flat_cleanup_threshold():
                    return 0.0
                return max(current_position_qty * 0.5, 0.0)
            return target_qty
        if current_position_qty > 1e-12 and (baseline.direction_bias == "short" or ai_assessment.directional_edge < 0.0):
            # Long-only spot should treat bearish reversal signals as "stop adding"
            # rather than forcing churn into immediate flat on every negative flip.
            return current_position_qty
        return 0.0

    def _apply_position_management(
        self,
        *,
        current_position_qty: float,
        desired_target_qty: float,
        product_type: str,
    ) -> float:
        rebalance_band = self._rebalance_band(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
        )
        delta_qty = desired_target_qty - current_position_qty
        if abs(desired_target_qty) < 1e-12 and abs(current_position_qty) <= rebalance_band:
            return 0.0
        if abs(delta_qty) <= rebalance_band:
            return current_position_qty

        if self._same_direction(current_position_qty, desired_target_qty):
            if abs(desired_target_qty) > abs(current_position_qty):
                max_step = self._max_scale_step(desired_target_qty)
                step = min(abs(delta_qty), max_step)
                return current_position_qty + (self._sign(delta_qty) * step)
            if abs(delta_qty) <= self._reduce_threshold(
                current_position_qty=current_position_qty,
                desired_target_qty=desired_target_qty,
            ):
                return current_position_qty
            return desired_target_qty

        if abs(current_position_qty) > 1e-12 and abs(desired_target_qty) > 1e-12:
            if abs(desired_target_qty) < self._reverse_threshold(current_position_qty=current_position_qty):
                if product_type == "derivatives":
                    return self._derivatives_reversal_step(current_position_qty=current_position_qty)
                return 0.0
        return desired_target_qty

    def _rebalance_band(self, *, current_position_qty: float, desired_target_qty: float) -> float:
        return max(
            self.settings.default_order_qty * 0.12,
            abs(desired_target_qty) * 0.08,
            abs(current_position_qty) * 0.08,
            1e-12,
        )

    def _reduce_threshold(self, *, current_position_qty: float, desired_target_qty: float) -> float:
        return max(
            self.settings.default_order_qty * 0.1,
            abs(current_position_qty) * 0.12,
            abs(desired_target_qty) * 0.12,
        )

    def _reverse_threshold(self, *, current_position_qty: float) -> float:
        return max(
            self.settings.default_order_qty * 0.45,
            abs(current_position_qty) * 0.35,
        )

    def _max_scale_step(self, desired_target_qty: float) -> float:
        return max(self.settings.default_order_qty * 0.4, abs(desired_target_qty) * 0.45)

    @staticmethod
    def _derivatives_reversal_step(*, current_position_qty: float) -> float:
        return current_position_qty * 0.35

    def _urgency(self, *, current_position_qty: float, target_position_qty: float) -> str:
        delta_qty = abs(target_position_qty - current_position_qty)
        if delta_qty < 1e-12:
            return "low"
        if current_position_qty * target_position_qty < 0.0:
            return "high"
        if delta_qty >= self.settings.default_order_qty * 0.75:
            return "high"
        return "medium"

    def _position_intent(
        self,
        *,
        current_position_qty: float,
        target_position_qty: float,
    ) -> str:
        if abs(target_position_qty - current_position_qty) < 1e-12:
            return "hold"
        current_side = self._exposure_side(current_position_qty)
        target_side = self._exposure_side(target_position_qty)
        if current_side == "flat":
            return "open_long" if target_side == "long" else "open_short"
        if target_side == "flat":
            return "close_long" if current_side == "long" else "close_short"
        if current_side != target_side:
            return "reverse_to_long" if target_side == "long" else "reverse_to_short"
        if current_side == "long":
            if abs(target_position_qty) >= abs(current_position_qty):
                return "open_long"
            return "reduce_long"
        if abs(target_position_qty) >= abs(current_position_qty):
            return "open_short"
        return "reduce_short"

    @staticmethod
    def _exposure_side(quantity: float) -> str:
        if quantity > 1e-12:
            return "long"
        if quantity < -1e-12:
            return "short"
        return "flat"

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

    def _flat_cleanup_threshold(self) -> float:
        return max(self.settings.default_order_qty * 0.15, 1e-12)

    def _estimated_trade_cost_bps(self) -> float:
        expected_slippage_bps = max(self.settings.max_slippage_tolerance_bps, 0) * max(
            self.settings.strategy_expected_slippage_bps_fraction,
            0.0,
        )
        return max(self.settings.paper_taker_fee_bps, 0.0) + expected_slippage_bps

    def _signal_edge_bps(
        self,
        *,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
    ) -> float:
        alpha_edge = abs(baseline.composite_alpha_score) * max(self.settings.strategy_alpha_edge_bps_scale, 0.0)
        microstructure_bonus = max(abs(baseline.factor_scores.get("microstructure_alpha", 0.0)) - 0.08, 0.0) * 25.0
        ai_bonus = max(abs(ai_assessment.directional_edge) - 0.1, 0.0) * 20.0
        return alpha_edge + microstructure_bonus + ai_bonus
