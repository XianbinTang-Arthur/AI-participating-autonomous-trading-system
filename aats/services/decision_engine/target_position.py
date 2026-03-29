from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.ai_shadow import AIShadowDecision
from aats.schemas.common import utc_now
from aats.schemas.decision import (
    AIDecisionIntent,
    AIMarketAssessment,
    BaselineAssessment,
    CanonicalAIOperatingMode,
    DecisionContext,
    DecisionOutcome,
    HedgeOverlayDecision,
    ProfileControlDecision,
    PositionTarget,
    normalize_ai_operating_mode,
)
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_overlay_rollout import overlay_rollout_status
from aats.services.trade_costs import TradeCostService
from aats.schemas.strategy_runtime import StrategyLegIntent


class TargetPositionEngine:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        fee_resolver: EffectiveFeeResolver | None = None,
    ) -> None:
        self.settings = settings
        self.fee_resolver = fee_resolver or EffectiveFeeResolver(settings=settings)
        self.trade_cost_service = TradeCostService(settings=settings, fee_resolver=self.fee_resolver)

    def build(
        self,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None = None,
        profile_control_decision: ProfileControlDecision | None = None,
        *,
        operating_mode: str | None = None,
    ) -> PositionTarget:
        effective_mode = operating_mode or self.settings.ai_operating_mode
        ai_decision_intent = ai_decision_intent or self.build_ai_decision_intent(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            operating_mode=effective_mode,
        )
        return self._build(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            profile_control_decision=profile_control_decision,
            operating_mode=effective_mode,
        )

    def build_shadow(
        self,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment,
        actual_target: PositionTarget,
        *,
        operating_mode: str | None = None,
    ) -> AIShadowDecision:
        shadow_mode = normalize_ai_operating_mode(operating_mode or self.settings.ai_operating_mode)
        if shadow_mode == "baseline_only":
            shadow_mode = "ai_decision_maker"
        shadow_intent = self.build_ai_decision_intent(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            operating_mode=shadow_mode,
        )
        shadow_target = self._build(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=shadow_intent,
            profile_control_decision=None,
            operating_mode=shadow_mode,
        )
        return AIShadowDecision(
            decision_id=context.decision_id,
            symbol=context.symbol,
            timeframe=context.timeframe,
            baseline_target_qty=actual_target.target_position_qty,
            baseline_action=actual_target.position_intent,
            ai_shadow_target_qty=shadow_target.target_position_qty,
            ai_shadow_action=shadow_target.position_intent,
            would_override_baseline=(
                abs(actual_target.target_position_qty - shadow_target.target_position_qty) > EPSILON_DECIMAL_12
                or actual_target.position_intent != shadow_target.position_intent
            ),
            shadow_action_type=self._shadow_action_type(
                baseline_action=actual_target.position_intent,
                shadow_action=shadow_target.position_intent,
            ),
            reason_codes=list(ai_assessment.override_reason_codes),
        )

    def build_ai_decision_intent(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        operating_mode: str | None,
    ) -> AIDecisionIntent | None:
        canonical_mode = normalize_ai_operating_mode(operating_mode)
        if canonical_mode == "baseline_only" or ai_assessment is None:
            return None
        direction = self._direction_from_assessment(ai_assessment)
        baseline_qty = self._baseline_target_qty(baseline=baseline, product_type=context.product_type)
        default_qty = to_decimal(self.settings.default_order_qty) * Decimal("0.35")
        desired_abs_qty = max(abs(baseline_qty), default_qty)
        current_side = self._exposure_side(context.current_position_qty)
        if direction == "flat" or not ai_assessment.economically_actionable:
            action = "hold"
            target_qty = context.current_position_qty
        elif current_side == "flat":
            action = "enter"
            target_qty = desired_abs_qty if direction == "long" else -desired_abs_qty
        elif current_side != direction:
            action = "reverse"
            target_qty = desired_abs_qty if direction == "long" else -desired_abs_qty
        else:
            current_abs = abs(context.current_position_qty)
            desired_abs_qty = max(current_abs, desired_abs_qty)
            action = "hold" if desired_abs_qty <= current_abs + EPSILON_DECIMAL_12 else "scale_in"
            target_qty = desired_abs_qty if direction == "long" else -desired_abs_qty
        return AIDecisionIntent(
            decision_id=context.decision_id,
            symbol=context.symbol,
            timeframe=context.timeframe,
            direction=direction,
            action=action,
            target_qty=target_qty,
            confidence=max(ai_assessment.calibrated_confidence, ai_assessment.confidence),
            economically_actionable=ai_assessment.economically_actionable,
            reason_codes=list(ai_assessment.override_reason_codes or ai_assessment.validation_flags),
            fallback_used=ai_assessment.fallback_used,
            degraded=ai_assessment.degraded,
            provider_name=ai_assessment.provider_name,
            provider_request_id=ai_assessment.provider_request_id,
            requested_profile_id=None,
            requested_profile_reason_codes=[],
            raw_assessment_ref=ai_assessment.model_dump(mode="json"),
        )

    def _build(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        profile_control_decision: ProfileControlDecision | None,
        operating_mode: str,
    ) -> PositionTarget:
        canonical_mode = normalize_ai_operating_mode(operating_mode)
        signal_edge_bps = self._signal_edge_bps(baseline=baseline, ai_assessment=ai_assessment)
        expected_cost_bps = self._estimated_trade_cost_bps(
            symbol=context.symbol,
            product_type=context.product_type,
            ai_assessment=ai_assessment,
        )
        expected_net_edge_bps = signal_edge_bps - expected_cost_bps - max(self.settings.strategy_edge_noise_buffer_bps, 0.0)
        guardrail_flags = list(context.strategy_guardrail_flags)
        ai_decision_authorized, ai_decision_blockers = self._ai_decision_gate(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            operating_mode=canonical_mode,
        )
        target_qty = self._target_quantity(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            product_type=context.product_type,
            operating_mode=operating_mode,
            ai_decision_authorized=ai_decision_authorized,
            signal_edge_bps=signal_edge_bps,
            guardrail_flags=guardrail_flags,
        )
        if (
            not self._short_bias_allowed(context.product_type)
            and (
                baseline.direction_bias == "short"
                or target_qty < Decimal("0")
                or (ai_decision_intent is not None and ai_decision_intent.direction == "short")
            )
        ):
            guardrail_flags.append("short_bias_disabled")
        if not self._short_bias_allowed(context.product_type):
            target_qty = self._normalize_long_only_target(
                current_position_qty=context.current_position_qty,
                target_qty=target_qty,
                baseline=baseline,
                ai_assessment=ai_assessment,
            )
        target_leverage = self._target_leverage(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            target_qty=target_qty,
        )
        strategy_execution_legs: list[StrategyLegIntent] = []
        hedge_overlay_decision: HedgeOverlayDecision | None = None
        if self._hedge_overlay_runtime_supported(context=context):
            target_qty, strategy_execution_legs, hedge_overlay_decision = self._hedge_mode_strategy_legs(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                directional_target_qty=target_qty,
                target_leverage=target_leverage,
                guardrail_flags=guardrail_flags,
            )
        target_exposure_side = self._exposure_side(target_qty)
        position_intent = self._position_intent(
            current_position_qty=context.current_position_qty,
            target_position_qty=target_qty,
        )
        source_mix = self._source_mix(
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            operating_mode=operating_mode,
            ai_decision_authorized=ai_decision_authorized,
        )
        rebalance_reason = f"{canonical_mode}_decision"
        ai_decision_applied = canonical_mode in {
            "ai_decision_maker",
            "ai_decision_maker_with_profile_control",
        } and ai_decision_authorized and ai_decision_blockers == []
        decision_outcome = self._decision_outcome(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            ai_decision_intent=ai_decision_intent,
            profile_control_decision=profile_control_decision,
            canonical_mode=canonical_mode,
            target_qty=target_qty,
            target_exposure_side=target_exposure_side,
            position_intent=position_intent,
            ai_decision_authorized=ai_decision_authorized,
            ai_decision_applied=ai_decision_applied,
            ai_decision_blockers=ai_decision_blockers,
            guardrail_flags=guardrail_flags,
        )

        return PositionTarget(
            decision_id=context.decision_id,
            symbol=context.symbol,
            current_position_qty=context.current_position_qty,
            target_position_qty=target_qty,
            delta_position_qty=target_qty - context.current_position_qty,
            current_notional=Decimal("0"),
            target_notional=Decimal("0"),
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
            expected_signal_edge_bps=signal_edge_bps,
            expected_cost_bps=expected_cost_bps,
            expected_net_edge_bps=expected_net_edge_bps,
            strategy_execution_legs=strategy_execution_legs,
            hedge_overlay_decision=hedge_overlay_decision,
            guardrail_flags=list(dict.fromkeys(guardrail_flags)),
            ai_execution_parameter_suggestion=(
                None
                if ai_assessment is None
                else ai_assessment.ai_execution_parameter_suggestion
            ),
            ai_decision_intent=ai_decision_intent,
            profile_control_decision=profile_control_decision,
            decision_outcome=decision_outcome,
        )

    def _target_quantity(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        product_type: str,
        operating_mode: str,
        ai_decision_authorized: bool,
        signal_edge_bps: float,
        guardrail_flags: list[str],
    ) -> Decimal:
        legacy_mode = (operating_mode or "").strip()
        mode = normalize_ai_operating_mode(operating_mode)
        baseline_qty_raw = self._baseline_target_qty(baseline=baseline, product_type=product_type)
        baseline_fallback_qty = self._apply_entry_edge_gate(
            context=context,
            desired_target_qty=baseline_qty_raw,
            baseline=baseline,
            ai_assessment=ai_assessment,
            product_type=product_type,
            signal_edge_bps=signal_edge_bps,
            guardrail_flags=guardrail_flags,
        )
        baseline_fallback_qty = self._apply_strategy_execution_guards(
            context=context,
            desired_target_qty=baseline_fallback_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
            signal_edge_bps=signal_edge_bps,
            product_type=product_type,
            guardrail_flags=guardrail_flags,
        )
        if mode == "baseline_only":
            return self._target_quantity_baseline_only(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                product_type=product_type,
                baseline_qty=baseline_fallback_qty,
                guardrail_flags=guardrail_flags,
            )
        if mode == "ai_assisted":
            return self._target_quantity_ai_assisted(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                product_type=product_type,
                baseline_qty=baseline_fallback_qty,
                guardrail_flags=guardrail_flags,
                legacy_mode=legacy_mode,
            )
        if mode == "ai_decision_maker":
            return self._target_quantity_ai_decision_maker(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                ai_decision_intent=ai_decision_intent,
                product_type=product_type,
                baseline_fallback_qty=baseline_fallback_qty,
                ai_decision_authorized=ai_decision_authorized,
                guardrail_flags=guardrail_flags,
            )
        if mode == "ai_decision_maker_with_profile_control":
            return self._target_quantity_ai_decision_maker(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                ai_decision_intent=ai_decision_intent,
                product_type=product_type,
                baseline_fallback_qty=baseline_fallback_qty,
                ai_decision_authorized=ai_decision_authorized,
                guardrail_flags=guardrail_flags,
            )
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=baseline_fallback_qty,
            product_type=product_type,
        )

    def _target_quantity_baseline_only(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
        baseline_qty: Decimal,
        guardrail_flags: list[str],
    ) -> Decimal:
        if self._should_hold_on_flat_signal(
            current_position_qty=context.current_position_qty,
            desired_target_qty=baseline_qty,
            baseline=baseline,
            ai_assessment=None,
            product_type=product_type,
        ):
            guardrail_flags.append("flat_signal_hold")
            return context.current_position_qty
        managed_target_qty = self._manage_existing_position(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            desired_target_qty=baseline_qty,
            product_type=product_type,
            guardrail_flags=guardrail_flags,
        )
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=managed_target_qty,
            product_type=product_type,
        )

    def _target_quantity_ai_assisted(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
        baseline_qty: Decimal,
        guardrail_flags: list[str],
        legacy_mode: str,
    ) -> Decimal:
        if legacy_mode == "ai_blended" and self._legacy_ai_blended_blocks_baseline(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            desired_target_qty=baseline_qty,
        ):
            guardrail_flags.append("ai_consistency_filter_blocked")
            return context.current_position_qty
        if self._should_hold_on_flat_signal(
            current_position_qty=context.current_position_qty,
            desired_target_qty=baseline_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
            product_type=product_type,
        ):
            guardrail_flags.append("flat_signal_hold")
            return context.current_position_qty
        managed_target_qty = self._manage_existing_position(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            desired_target_qty=baseline_qty,
            product_type=product_type,
            guardrail_flags=guardrail_flags,
        )
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=managed_target_qty,
            product_type=product_type,
        )

    def _legacy_ai_blended_blocks_baseline(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        desired_target_qty: Decimal,
    ) -> bool:
        if ai_assessment is None:
            return False
        if not ai_assessment.output_valid or ai_assessment.fallback_used or ai_assessment.degraded:
            return False
        if abs(desired_target_qty - context.current_position_qty) < EPSILON_DECIMAL_12:
            return False
        if not ai_assessment.economically_actionable:
            return True
        ai_direction = self._direction_from_assessment(ai_assessment)
        if ai_direction == "flat":
            return True
        if ai_direction != baseline.direction_bias:
            return True
        return False

    def _target_quantity_ai_decision_maker(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        product_type: str,
        baseline_fallback_qty: Decimal,
        ai_decision_authorized: bool,
        guardrail_flags: list[str],
    ) -> Decimal:
        if ai_decision_intent is not None and ai_decision_authorized:
            desired_target_qty = self._desired_target_qty_from_ai_decision_intent(
                context=context,
                ai_decision_intent=ai_decision_intent,
            )
            managed_target_qty = self._manage_existing_position(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                desired_target_qty=desired_target_qty,
                product_type=product_type,
                guardrail_flags=guardrail_flags,
            )
            return self._apply_position_management(
                current_position_qty=context.current_position_qty,
                desired_target_qty=managed_target_qty,
                product_type=product_type,
            )
        managed_target_qty = self._manage_existing_position(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            desired_target_qty=baseline_fallback_qty,
            product_type=product_type,
            guardrail_flags=guardrail_flags,
        )
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=managed_target_qty,
            product_type=product_type,
        )

    def _desired_target_qty_from_ai_decision_intent(
        self,
        *,
        context: DecisionContext,
        ai_decision_intent: AIDecisionIntent,
    ) -> Decimal:
        if ai_decision_intent.action == "hold":
            return context.current_position_qty
        if ai_decision_intent.action == "exit":
            return Decimal("0")
        return ai_decision_intent.target_qty

    def _baseline_target_qty(self, *, baseline: BaselineAssessment, product_type: str) -> Decimal:
        scale = to_decimal(self._clamp(baseline.suggested_position_scale, 0.0, 1.0))
        # FeatureCalculator already applies volatility_target_scale when computing
        # suggested_position_scale. Reapplying it here would shrink exposure twice.
        return self._qty_from_bias(baseline.direction_bias, product_type=product_type) * scale

    def _volatility_target_multiplier(self, baseline: BaselineAssessment) -> Decimal:
        floor = to_decimal(self.settings.strategy_volatility_target_scale_floor)
        ceiling = to_decimal(self.settings.strategy_volatility_target_scale_ceiling)
        raw_value = to_decimal(baseline.volatility_target_scale)
        return min(max(raw_value, floor), ceiling)

    def _manage_existing_position(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        desired_target_qty: Decimal,
        product_type: str,
        guardrail_flags: list[str],
    ) -> Decimal:
        current_position_qty = context.current_position_qty
        if abs(current_position_qty) < EPSILON_DECIMAL_12:
            return desired_target_qty

        explicit_flat_exit_required = self._explicit_flat_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        if self._emergency_protective_exit_required(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        ):
            guardrail_flags.append("emergency_protective_exit")
            return Decimal("0")

        if self._alpha_decay_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        ):
            reduced_target = self._apply_position_management_hold_gate(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                desired_target_qty=Decimal("0"),
                bypass_min_hold=explicit_flat_exit_required,
                guardrail_flags=guardrail_flags,
            )
            if abs(reduced_target - current_position_qty) > EPSILON_DECIMAL_12:
                guardrail_flags.append("alpha_decay_exit")
            return reduced_target

        alpha_decay_target = self._alpha_decay_reduce_target_qty(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
        )
        if alpha_decay_target is not None:
            managed_target = self._apply_position_management_hold_gate(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                desired_target_qty=alpha_decay_target,
                bypass_min_hold=False,
                guardrail_flags=guardrail_flags,
            )
            if abs(managed_target) + EPSILON_DECIMAL_12 < abs(current_position_qty):
                guardrail_flags.append("alpha_decay_reduce")
                return managed_target

        risk_contracted_target = self._risk_contracted_target_qty(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
        )
        if risk_contracted_target is not None:
            managed_target = self._apply_position_management_hold_gate(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                desired_target_qty=risk_contracted_target,
                bypass_min_hold=False,
                guardrail_flags=guardrail_flags,
            )
            if abs(managed_target) + EPSILON_DECIMAL_12 < abs(current_position_qty):
                guardrail_flags.append("risk_contraction_exit")
                return managed_target

        return desired_target_qty

    def _apply_position_management_hold_gate(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        desired_target_qty: Decimal,
        bypass_min_hold: bool,
        guardrail_flags: list[str],
    ) -> Decimal:
        if bypass_min_hold:
            return desired_target_qty
        if not self._min_hold_blocks_adjustment(
            context=context,
            current_position_qty=context.current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        ):
            return desired_target_qty
        guardrail_flags.append("min_hold_blocks_exit")
        return context.current_position_qty

    def _position_adverse_factors(
        self,
        *,
        current_position_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> dict[str, object]:
        side_sign = self._sign(current_position_qty)
        microstructure = to_decimal(baseline.factor_scores.get("microstructure_alpha", 0.0))
        momentum_alpha = to_decimal(baseline.factor_scores.get("momentum_alpha", 0.0))
        trend_alpha = to_decimal(baseline.factor_scores.get("trend_alpha", 0.0))
        ai_edge = Decimal("0") if ai_assessment is None else to_decimal(ai_assessment.directional_edge)
        adverse_microstructure = (
            side_sign * microstructure
        ) <= -abs(to_decimal(self.settings.strategy_flat_exit_microstructure_threshold))
        adverse_momentum = (
            side_sign * momentum_alpha
        ) <= -abs(to_decimal(self.settings.strategy_flat_exit_factor_threshold))
        adverse_trend = (
            side_sign * trend_alpha
        ) <= -abs(to_decimal(self.settings.strategy_flat_exit_factor_threshold))
        adverse_ai = (
            side_sign * ai_edge
        ) <= -abs(to_decimal(self.settings.strategy_flat_exit_ai_edge_threshold))
        return {
            "adverse_microstructure": adverse_microstructure,
            "adverse_momentum": adverse_momentum,
            "adverse_trend": adverse_trend,
            "adverse_ai": adverse_ai,
            "adverse_count": sum((adverse_microstructure, adverse_momentum, adverse_trend, adverse_ai)),
        }

    def _emergency_protective_exit_required(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> bool:
        factors = self._position_adverse_factors(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        adverse_count = int(factors["adverse_count"])
        if adverse_count >= 3:
            return True
        if baseline.volatility_state == "high" and baseline.regime in {"breakout", "trend"} and adverse_count >= 2:
            return True
        return False

    def _alpha_decay_exit_required(
        self,
        *,
        current_position_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> bool:
        if self._exposure_side(current_position_qty) == "flat":
            return False
        alpha = abs(to_decimal(baseline.composite_alpha_score))
        if baseline.direction_bias != "flat":
            return False
        if alpha <= to_decimal(self.settings.strategy_position_alpha_decay_exit_alpha):
            return True
        return self._explicit_flat_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )

    def _alpha_decay_reduce_target_qty(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
    ) -> Decimal | None:
        current_side = self._exposure_side(current_position_qty)
        desired_side = self._exposure_side(desired_target_qty)
        if current_side == "flat" or desired_side not in {current_side, "flat"}:
            return None
        alpha = abs(to_decimal(baseline.composite_alpha_score))
        confidence = to_decimal(baseline.confidence)
        alpha_threshold = to_decimal(self.settings.strategy_position_alpha_decay_reduce_alpha)
        confidence_threshold = to_decimal(self.settings.strategy_position_alpha_decay_reduce_confidence)
        if (
            alpha + EPSILON_DECIMAL_12 >= alpha_threshold
            and confidence + EPSILON_DECIMAL_12 >= confidence_threshold
        ):
            return None
        reduce_fraction = Decimal("0.55") if baseline.direction_bias == "flat" else Decimal("0.72")
        current_abs = abs(current_position_qty)
        desired_abs = abs(desired_target_qty) if desired_side == current_side else current_abs
        reduced_abs = min(current_abs * reduce_fraction, desired_abs)
        if reduced_abs + EPSILON_DECIMAL_12 >= current_abs:
            return None
        return self._sign(current_position_qty) * reduced_abs

    def _risk_contracted_target_qty(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
    ) -> Decimal | None:
        current_side = self._exposure_side(current_position_qty)
        desired_side = self._exposure_side(desired_target_qty)
        if current_side == "flat" or desired_side != current_side:
            return None
        contraction_fraction = Decimal("1")
        if baseline.volatility_state == "high":
            contraction_fraction = min(
                contraction_fraction,
                to_decimal(self.settings.strategy_position_high_volatility_reduce_fraction),
            )
        if baseline.regime == "range":
            contraction_fraction = min(
                contraction_fraction,
                to_decimal(self.settings.strategy_position_range_reduce_fraction),
            )
        if baseline.regime == "uncertain":
            contraction_fraction = min(
                contraction_fraction,
                to_decimal(self.settings.strategy_position_uncertain_reduce_fraction),
            )
        contraction_fraction = min(contraction_fraction, self._volatility_target_multiplier(baseline))
        if contraction_fraction + EPSILON_DECIMAL_12 >= Decimal("1"):
            return None
        contracted_abs = abs(current_position_qty) * contraction_fraction
        desired_abs = abs(desired_target_qty)
        if contracted_abs + EPSILON_DECIMAL_12 >= desired_abs:
            return None
        return self._sign(current_position_qty) * contracted_abs

    def _apply_entry_edge_gate(
        self,
        *,
        context: DecisionContext,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
        signal_edge_bps: float,
        guardrail_flags: list[str],
    ) -> Decimal:
        desired_target_qty = self._apply_trade_qualification_gate(
            current_position_qty=context.current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
            product_type=product_type,
            signal_edge_bps=signal_edge_bps,
            guardrail_flags=guardrail_flags,
        )
        if not self.settings.strategy_cost_guard_enabled:
            return desired_target_qty
        if abs(desired_target_qty) < EPSILON_DECIMAL_12:
            return desired_target_qty
        if self._same_direction(context.current_position_qty, desired_target_qty) and abs(desired_target_qty) <= abs(context.current_position_qty):
            return desired_target_qty
        estimated_cost_bps = self._estimated_trade_cost_bps(
            symbol=context.symbol,
            product_type=product_type,
            ai_assessment=ai_assessment,
        )
        required_edge_bps = (
            estimated_cost_bps
            + max(self.settings.strategy_edge_noise_buffer_bps, 0.0)
            + max(self.settings.strategy_min_net_edge_bps, 0.0)
        )
        if signal_edge_bps + float(EPSILON_DECIMAL_12) >= required_edge_bps:
            return desired_target_qty
        guardrail_flags.append("expected_edge_below_cost_buffer")
        return context.current_position_qty

    def _apply_trade_qualification_gate(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
        signal_edge_bps: float,
        guardrail_flags: list[str],
    ) -> Decimal:
        if product_type != "derivatives":
            return desired_target_qty
        trade_kind = self._trade_kind(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
        )
        if trade_kind is None:
            return desired_target_qty
        target_side = self._exposure_side(desired_target_qty)
        if trade_kind == "entry" and not self._regime_allowed_for_entry(
            baseline.regime,
            desired_target_qty=desired_target_qty,
        ):
            guardrail_flags.append("short_entry_regime_not_allowed" if target_side == "short" else "entry_regime_not_allowed")
            return current_position_qty
        alpha = abs(baseline.composite_alpha_score)
        confidence = baseline.confidence
        edge_threshold, alpha_threshold, confidence_threshold, flag_prefix = self._trade_thresholds(
            trade_kind=trade_kind,
            desired_target_qty=desired_target_qty,
        )
        if trade_kind == "entry":
            if alpha + float(EPSILON_DECIMAL_12) < alpha_threshold:
                guardrail_flags.append(f"{flag_prefix}_alpha_below_threshold")
                return current_position_qty
            if confidence + float(EPSILON_DECIMAL_12) < confidence_threshold:
                guardrail_flags.append(f"{flag_prefix}_confidence_below_threshold")
                return current_position_qty
            if signal_edge_bps + float(EPSILON_DECIMAL_12) < edge_threshold:
                guardrail_flags.append(f"{flag_prefix}_signal_edge_below_threshold")
                return current_position_qty
            return desired_target_qty
        if trade_kind == "scale_in":
            if alpha + float(EPSILON_DECIMAL_12) < alpha_threshold:
                guardrail_flags.append(f"{flag_prefix}_alpha_below_threshold")
                return current_position_qty
            if confidence + float(EPSILON_DECIMAL_12) < confidence_threshold:
                guardrail_flags.append(f"{flag_prefix}_confidence_below_threshold")
                return current_position_qty
            if signal_edge_bps + float(EPSILON_DECIMAL_12) < edge_threshold:
                guardrail_flags.append(f"{flag_prefix}_signal_edge_below_threshold")
                return current_position_qty
            return desired_target_qty
        if trade_kind == "reversal":
            if alpha + float(EPSILON_DECIMAL_12) < alpha_threshold:
                guardrail_flags.append(f"{flag_prefix}_alpha_below_threshold")
                return current_position_qty
            if confidence + float(EPSILON_DECIMAL_12) < confidence_threshold:
                guardrail_flags.append(f"{flag_prefix}_confidence_below_threshold")
                return current_position_qty
            if signal_edge_bps + float(EPSILON_DECIMAL_12) < edge_threshold:
                guardrail_flags.append(f"{flag_prefix}_signal_edge_below_threshold")
                return current_position_qty
        return desired_target_qty

    def _apply_strategy_execution_guards(
        self,
        *,
        context: DecisionContext,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        signal_edge_bps: float,
        product_type: str,
        guardrail_flags: list[str],
    ) -> Decimal:
        current_position_qty = context.current_position_qty
        if abs(desired_target_qty - current_position_qty) < EPSILON_DECIMAL_12:
            return desired_target_qty

        trade_kind = self._trade_kind(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
        )
        if self._min_hold_blocks_adjustment(
            context=context,
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        ):
            guardrail_flags.append("min_hold_blocks_exit")
            return current_position_qty

        if trade_kind in {"entry", "scale_in", "reversal"}:
            if self._post_close_cooldown_active(context):
                guardrail_flags.append("post_close_cooldown_blocks_entry")
                return current_position_qty
            if self._low_edge_cooldown_active(context):
                guardrail_flags.append("low_edge_cooldown_blocks_entry")
                return current_position_qty
            if self._performance_degraded(context):
                guardrail_flags.append("execution_churn_guard_active")
                return current_position_qty
            if trade_kind == "reversal" and self._reversal_requires_additional_edge(
                signal_edge_bps=signal_edge_bps,
                desired_target_qty=desired_target_qty,
            ):
                guardrail_flags.append(
                    "short_reversal_edge_not_strong_enough"
                    if self._exposure_side(desired_target_qty) == "short"
                    else "reversal_edge_not_strong_enough"
                )
                return current_position_qty
        return desired_target_qty

    def _trade_kind(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
    ) -> str | None:
        if abs(desired_target_qty) < EPSILON_DECIMAL_12:
            return None
        if abs(current_position_qty) < EPSILON_DECIMAL_12:
            return "entry"
        if self._same_direction(current_position_qty, desired_target_qty):
            if abs(desired_target_qty) > abs(current_position_qty) + EPSILON_DECIMAL_12:
                return "scale_in"
            return None
        if abs(desired_target_qty) + EPSILON_DECIMAL_12 >= self._reverse_threshold(current_position_qty=current_position_qty):
            return "reversal"
        return None

    def _regime_allowed_for_entry(self, regime: str, *, desired_target_qty: Decimal) -> bool:
        allowed_regimes_source = (
            self.settings.strategy_short_entry_allowed_regimes
            if self._exposure_side(desired_target_qty) == "short"
            else self.settings.strategy_entry_allowed_regimes
        )
        allowed_regimes = {value.lower() for value in allowed_regimes_source if value}
        if not allowed_regimes:
            return True
        return regime.lower() in allowed_regimes

    def _trade_thresholds(
        self,
        *,
        trade_kind: str,
        desired_target_qty: Decimal,
    ) -> tuple[float, float, float, str]:
        target_side = self._exposure_side(desired_target_qty)
        if trade_kind == "entry":
            if target_side == "short":
                return (
                    self.settings.strategy_short_entry_min_signal_edge_bps,
                    self.settings.strategy_short_entry_alpha_min,
                    self.settings.strategy_short_entry_confidence_min,
                    "short_entry",
                )
            return (
                self.settings.strategy_entry_min_signal_edge_bps,
                self.settings.strategy_entry_alpha_min,
                self.settings.strategy_entry_confidence_min,
                "entry",
            )
        if trade_kind == "scale_in":
            if target_side == "short":
                return (
                    self.settings.strategy_short_scale_in_min_signal_edge_bps,
                    self.settings.strategy_short_scale_in_alpha_min,
                    self.settings.strategy_short_scale_in_confidence_min,
                    "short_scale_in",
                )
            return (
                self.settings.strategy_scale_in_min_signal_edge_bps,
                self.settings.strategy_scale_in_alpha_min,
                self.settings.strategy_scale_in_confidence_min,
                "scale_in",
            )
        if target_side == "short":
            return (
                self.settings.strategy_short_reversal_min_signal_edge_bps,
                self.settings.strategy_short_reversal_alpha_min,
                self.settings.strategy_short_reversal_confidence_min,
                "short_reversal",
            )
        return (
            self.settings.strategy_reversal_min_signal_edge_bps,
            self.settings.strategy_reversal_alpha_min,
            self.settings.strategy_reversal_confidence_min,
            "reversal",
        )

    def _should_hold_on_flat_signal(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        product_type: str,
    ) -> bool:
        if not self.settings.strategy_flat_signal_hold_enabled:
            return False
        if product_type != "derivatives":
            return False
        if abs(current_position_qty) < EPSILON_DECIMAL_12 or abs(desired_target_qty) > EPSILON_DECIMAL_12:
            return False
        if baseline.direction_bias != "flat":
            return False
        return not self._explicit_flat_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )

    def _min_hold_blocks_adjustment(
        self,
        *,
        context: DecisionContext,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> bool:
        if (
            self.settings.strategy_min_hold_seconds <= 0
            or context.current_position_opened_at is None
            or abs(current_position_qty) < EPSILON_DECIMAL_12
        ):
            return False
        held_for = max((utc_now() - context.current_position_opened_at).total_seconds(), 0.0)
        if held_for + float(EPSILON_DECIMAL_12) >= self.settings.strategy_min_hold_seconds:
            return False
        if not self._is_reducing_or_closing(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
        ):
            return False
        return not self._explicit_flat_exit_required(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )

    def _post_close_cooldown_active(self, context: DecisionContext) -> bool:
        if (
            context.last_position_closed_at is None
            or self.settings.strategy_post_close_cooldown_seconds <= 0
        ):
            return False
        return max((utc_now() - context.last_position_closed_at).total_seconds(), 0.0) < self.settings.strategy_post_close_cooldown_seconds

    def _low_edge_cooldown_active(self, context: DecisionContext) -> bool:
        if (
            context.recent_low_edge_trade_streak < self.settings.strategy_low_edge_streak_limit
            or context.recent_low_edge_trade_at is None
            or self.settings.strategy_low_edge_cooldown_seconds <= 0
        ):
            return False
        return max((utc_now() - context.recent_low_edge_trade_at).total_seconds(), 0.0) < self.settings.strategy_low_edge_cooldown_seconds

    def _performance_degraded(self, context: DecisionContext) -> bool:
        if context.recent_closed_trade_count < self.settings.strategy_performance_guard_min_closed_trades:
            return False
        return (
            context.recent_fee_drag_ratio > self.settings.strategy_max_fee_drag_ratio
            or context.recent_churn_ratio > self.settings.strategy_max_churn_ratio
        )

    def _reversal_requires_additional_edge(
        self,
        signal_edge_bps: float,
        *,
        desired_target_qty: Decimal,
    ) -> bool:
        reversal_threshold = (
            self.settings.strategy_short_reversal_min_signal_edge_bps
            if self._exposure_side(desired_target_qty) == "short"
            else self.settings.strategy_reversal_min_signal_edge_bps
        )
        required = reversal_threshold + max(self.settings.strategy_edge_noise_buffer_bps, 0.0)
        return signal_edge_bps + float(EPSILON_DECIMAL_12) < required

    @staticmethod
    def _is_reducing_or_closing(
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
    ) -> bool:
        if abs(current_position_qty) < EPSILON_DECIMAL_12:
            return False
        if abs(desired_target_qty) < EPSILON_DECIMAL_12:
            return True
        if current_position_qty * desired_target_qty < 0:
            return True
        if (current_position_qty > 0 and desired_target_qty > 0) or (current_position_qty < 0 and desired_target_qty < 0):
            return abs(desired_target_qty) + EPSILON_DECIMAL_12 < abs(current_position_qty)
        return False

    def _explicit_flat_exit_required(
        self,
        *,
        current_position_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> bool:
        factors = self._position_adverse_factors(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        adverse_count = int(factors["adverse_count"])
        if adverse_count >= 2:
            return True
        if bool(factors["adverse_microstructure"]) and bool(factors["adverse_ai"]):
            return True
        return False

    def _qty_from_bias(self, direction_bias: str, *, product_type: str) -> Decimal:
        if direction_bias == "long":
            return to_decimal(self.settings.default_order_qty)
        if direction_bias == "short" and self._short_bias_allowed(product_type):
            return -to_decimal(self.settings.default_order_qty)
        return Decimal("0")

    def _target_leverage(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        target_qty: Decimal,
    ) -> float:
        if abs(target_qty) < EPSILON_DECIMAL_12:
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
        ai_assessment: AIMarketAssessment | None,
    ) -> float:
        conviction = max(
            0.0,
            (baseline.confidence * 0.45)
            + (abs(self._ai_directional_edge(ai_assessment)) * 0.35)
            + (self._ai_confidence_component(ai_assessment) * 0.2),
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
        if ai_assessment is not None and (ai_assessment.degraded or ai_assessment.fallback_used):
            conviction *= 0.85
        return self._clamp(0.85 + conviction, 0.85, 2.5)

    def _short_bias_allowed(self, product_type: str) -> bool:
        return product_type == "derivatives" and bool(self.settings.strategy_short_bias_enabled)

    def _normalize_min_actionable_target_qty(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        product_type: str,
    ) -> Decimal:
        if product_type != "derivatives":
            return desired_target_qty
        if abs(current_position_qty) > EPSILON_DECIMAL_12 or abs(desired_target_qty) <= EPSILON_DECIMAL_12:
            return desired_target_qty
        minimum_qty = max(to_decimal(self.settings.default_order_qty), EPSILON_DECIMAL_12)
        if abs(desired_target_qty) + EPSILON_DECIMAL_12 >= minimum_qty:
            return desired_target_qty
        return self._sign(desired_target_qty) * minimum_qty

    def _normalize_long_only_target(
        self,
        *,
        current_position_qty: Decimal,
        target_qty: Decimal,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> Decimal:
        if target_qty >= 0:
            bearish_signal = baseline.direction_bias == "short" or self._ai_directional_edge(ai_assessment) < 0.0
            if current_position_qty > EPSILON_DECIMAL_12 and bearish_signal and target_qty < current_position_qty:
                return current_position_qty
            if current_position_qty > EPSILON_DECIMAL_12 and baseline.direction_bias == "flat" and target_qty <= EPSILON_DECIMAL_12:
                if current_position_qty <= self._flat_cleanup_threshold():
                    return Decimal("0")
                return max(current_position_qty * Decimal("0.5"), Decimal("0"))
            return target_qty
        if current_position_qty > EPSILON_DECIMAL_12 and (baseline.direction_bias == "short" or self._ai_directional_edge(ai_assessment) < 0.0):
            # Long-only spot should treat bearish reversal signals as "stop adding"
            # rather than forcing churn into immediate flat on every negative flip.
            return current_position_qty
        return Decimal("0")

    def _apply_position_management(
        self,
        *,
        current_position_qty: Decimal,
        desired_target_qty: Decimal,
        product_type: str,
    ) -> Decimal:
        desired_target_qty = self._normalize_min_actionable_target_qty(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
            product_type=product_type,
        )
        rebalance_band = self._rebalance_band(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
        )
        delta_qty = desired_target_qty - current_position_qty
        if abs(current_position_qty) < EPSILON_DECIMAL_12:
            return desired_target_qty
        if abs(desired_target_qty) < EPSILON_DECIMAL_12 and abs(current_position_qty) <= rebalance_band:
            return Decimal("0")
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

        if abs(current_position_qty) > EPSILON_DECIMAL_12 and abs(desired_target_qty) > EPSILON_DECIMAL_12:
            if abs(desired_target_qty) < self._reverse_threshold(current_position_qty=current_position_qty):
                if product_type == "derivatives":
                    return self._derivatives_reversal_step(current_position_qty=current_position_qty)
                return Decimal("0")
        return desired_target_qty

    def _hedge_overlay_runtime_supported(self, *, context: DecisionContext) -> bool:
        return (
            context.product_type == "derivatives"
            and self.settings.margin_mode != "cash"
            and self.settings.derivatives_position_mode == "hedge"
        )

    def _hedge_mode_strategy_legs(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        directional_target_qty: Decimal,
        target_leverage: float,
        guardrail_flags: list[str],
    ) -> tuple[Decimal, list[StrategyLegIntent], HedgeOverlayDecision]:
        if self.settings.strategy_hedge_overlay_mode == "independent":
            return self._independent_books_strategy_legs(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                directional_target_qty=directional_target_qty,
                target_leverage=target_leverage,
                guardrail_flags=guardrail_flags,
            )
        long_target_qty = max(to_decimal(directional_target_qty), Decimal("0"))
        short_target_qty = max(-to_decimal(directional_target_qty), Decimal("0"))
        overlay_decision = self._overlay_decision(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            long_target_qty=long_target_qty,
            short_target_qty=short_target_qty,
        )
        if overlay_decision.active and overlay_decision.hedge_leg_target_qty > EPSILON_DECIMAL_12:
            if overlay_decision.hedge_leg_signal == "short":
                short_target_qty = max(short_target_qty, overlay_decision.hedge_leg_target_qty)
            elif overlay_decision.hedge_leg_signal == "long":
                long_target_qty = max(long_target_qty, overlay_decision.hedge_leg_target_qty)
            overlay_mode = overlay_decision.effective_mode or overlay_decision.configured_mode
            if overlay_mode == "opportunistic":
                guardrail_flags.append("opportunistic_hedge_overlay_active")
            else:
                guardrail_flags.append("protective_hedge_overlay_active")
        elif overlay_decision.blocked_reasons:
            overlay_mode = overlay_decision.effective_mode or overlay_decision.configured_mode
            if overlay_mode == "opportunistic":
                guardrail_flags.append("opportunistic_hedge_overlay_blocked")
            else:
                guardrail_flags.append("protective_hedge_overlay_blocked")
        final_target_qty = long_target_qty - short_target_qty
        legs = [
            leg
            for leg in (
                self._build_hedge_execution_leg(
                    symbol=context.symbol,
                    pos_side="long",
                    current_leg_qty=context.current_long_position_qty,
                    target_leg_qty=long_target_qty,
                    target_leverage=target_leverage,
                    overlay_decision=overlay_decision,
                ),
                self._build_hedge_execution_leg(
                    symbol=context.symbol,
                    pos_side="short",
                    current_leg_qty=context.current_short_position_qty,
                    target_leg_qty=short_target_qty,
                    target_leverage=target_leverage,
                    overlay_decision=overlay_decision,
                ),
            )
            if leg is not None
        ]
        return final_target_qty, legs, overlay_decision

    def _independent_books_strategy_legs(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        directional_target_qty: Decimal,
        target_leverage: float,
        guardrail_flags: list[str],
    ) -> tuple[Decimal, list[StrategyLegIntent], HedgeOverlayDecision]:
        configured_mode = self.settings.strategy_hedge_overlay_mode
        if not self.settings.strategy_hedge_independent_enabled:
            guardrail_flags.append("independent_books_blocked")
            return (
                context.current_net_position_qty,
                [],
                HedgeOverlayDecision(
                    enabled=True,
                    runtime_supported=True,
                    configured_mode=configured_mode,
                    state="blocked",
                    overlay_source="independent_books",
                    blocked_reasons=["independent_books_not_enabled"],
                ),
            )
        rollout = overlay_rollout_status(self.settings, mode="independent")
        if not rollout["runtime_allowed"]:
            guardrail_flags.append("independent_books_blocked")
            return (
                context.current_net_position_qty,
                [],
                HedgeOverlayDecision(
                    enabled=True,
                    runtime_supported=True,
                    configured_mode=configured_mode,
                    effective_mode="independent",
                    overlay_source="independent_books",
                    state="blocked",
                    blocked_reasons=list(rollout["blocking_reasons"]),
                    reason_codes=["independent_books_rollout_gate_active"],
                    rollout_stage=rollout["configured_rollout_stage"],
                    runtime_rollout_stage=rollout["runtime_stage"],
                ),
            )

        directional_long_target_qty = max(to_decimal(directional_target_qty), Decimal("0"))
        directional_short_target_qty = max(-to_decimal(directional_target_qty), Decimal("0"))
        long_book = self._independent_book_decision(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            leg="long",
            directional_leg_target_qty=directional_long_target_qty,
        )
        short_book = self._independent_book_decision(
            context=context,
            baseline=baseline,
            ai_assessment=ai_assessment,
            leg="short",
            directional_leg_target_qty=directional_short_target_qty,
        )
        long_target_qty = to_decimal(long_book["target_qty"])
        short_target_qty = to_decimal(short_book["target_qty"])
        final_target_qty = long_target_qty - short_target_qty
        active = any(
            to_decimal(book["target_qty"]) > EPSILON_DECIMAL_12 or to_decimal(book["current_qty"]) > EPSILON_DECIMAL_12
            for book in (long_book, short_book)
        )
        blocked_reasons = list(
            dict.fromkeys(
                [
                    *(str(item) for item in long_book["blocked_reasons"]),
                    *(str(item) for item in short_book["blocked_reasons"]),
                ]
            )
        )
        if active:
            guardrail_flags.append("independent_books_active")
        elif blocked_reasons:
            guardrail_flags.append("independent_books_blocked")

        main_leg = long_book if float(long_book["score"]) >= float(short_book["score"]) else short_book
        secondary_leg = short_book if main_leg is long_book else long_book
        if to_decimal(main_leg["target_qty"]) <= EPSILON_DECIMAL_12 and to_decimal(main_leg["current_qty"]) <= EPSILON_DECIMAL_12:
            if to_decimal(secondary_leg["target_qty"]) > EPSILON_DECIMAL_12 or to_decimal(secondary_leg["current_qty"]) > EPSILON_DECIMAL_12:
                main_leg, secondary_leg = secondary_leg, main_leg
        actionable_states = {"opening", "closing"}
        state = "inactive"
        if any(str(book["state"]) in actionable_states for book in (long_book, short_book)):
            state = "opening" if any(str(book["state"]) == "opening" for book in (long_book, short_book)) else "closing"
        elif blocked_reasons:
            state = "blocked"
        elif active:
            state = "holding"
        reason_codes = list(
            dict.fromkeys(
                [
                    *(str(item) for item in long_book["reason_codes"]),
                    *(str(item) for item in short_book["reason_codes"]),
                ]
            )
        )
        legs = [
            leg
            for leg in (
                self._build_independent_execution_leg(
                    symbol=context.symbol,
                    pos_side="long",
                    current_leg_qty=context.current_long_position_qty,
                    target_leg_qty=long_target_qty,
                    target_leverage=target_leverage,
                    reason_codes=list(long_book["reason_codes"]),
                ),
                self._build_independent_execution_leg(
                    symbol=context.symbol,
                    pos_side="short",
                    current_leg_qty=context.current_short_position_qty,
                    target_leg_qty=short_target_qty,
                    target_leverage=target_leverage,
                    reason_codes=list(short_book["reason_codes"]),
                ),
            )
            if leg is not None
        ]
        main_leg_signal = str(main_leg["leg"]) if to_decimal(main_leg["target_qty"]) > EPSILON_DECIMAL_12 else "flat"
        hedge_leg_signal = (
            str(secondary_leg["leg"])
            if to_decimal(secondary_leg["target_qty"]) > EPSILON_DECIMAL_12 or to_decimal(secondary_leg["current_qty"]) > EPSILON_DECIMAL_12
            else "flat"
        )
        main_leg_target_qty = to_decimal(main_leg["target_qty"])
        secondary_leg_target_qty = to_decimal(secondary_leg["target_qty"])
        return (
            final_target_qty,
            legs,
            HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode="independent",
                effective_mode="independent",
                overlay_source="independent_books",
                active=active,
                state=state,  # type: ignore[arg-type]
                main_leg_signal=main_leg_signal,  # type: ignore[arg-type]
                hedge_leg_signal=hedge_leg_signal,  # type: ignore[arg-type]
                main_leg_current_qty=to_decimal(main_leg["current_qty"]),
                hedge_leg_current_qty=to_decimal(secondary_leg["current_qty"]),
                main_leg_target_qty=main_leg_target_qty,
                hedge_leg_target_qty=secondary_leg_target_qty,
                hedge_ratio=(
                    Decimal("0")
                    if main_leg_target_qty <= EPSILON_DECIMAL_12
                    else min(secondary_leg_target_qty / main_leg_target_qty, Decimal("1"))
                ),
                max_ratio=Decimal("1"),
                pressure_score=max(float(long_book["score"]), float(short_book["score"])),
                open_threshold=max(
                    float(self.settings.strategy_hedge_independent_long_entry_threshold),
                    float(self.settings.strategy_hedge_independent_short_entry_threshold),
                ),
                close_threshold=min(
                    float(self.settings.strategy_hedge_independent_long_entry_threshold),
                    float(self.settings.strategy_hedge_independent_short_entry_threshold),
                ),
                open_condition="long / short book 各自按 entry 阈值决定是否开仓。",
                close_condition="每条腿都按自己的 entry 阈值、最小持有与冷却独立决定是否退出。",
                fee_drag_ratio=max(
                    float(self._leg_health_value(context, "long", "recent_fee_drag_ratio") or 0.0),
                    float(self._leg_health_value(context, "short", "recent_fee_drag_ratio") or 0.0),
                ),
                churn_ratio=max(
                    float(self._leg_health_value(context, "long", "recent_churn_ratio") or 0.0),
                    float(self._leg_health_value(context, "short", "recent_churn_ratio") or 0.0),
                ),
                long_leg_score=float(long_book["score"]),
                short_leg_score=float(short_book["score"]),
                long_leg_reason_codes=list(long_book["reason_codes"]),
                short_leg_reason_codes=list(short_book["reason_codes"]),
                long_leg_blocked_reasons=list(long_book["blocked_reasons"]),
                short_leg_blocked_reasons=list(short_book["blocked_reasons"]),
                reason_codes=reason_codes,
                blocked_reasons=blocked_reasons,
                min_hold_remaining_seconds=max(
                    float(long_book["min_hold_remaining_seconds"]),
                    float(short_book["min_hold_remaining_seconds"]),
                ),
                rebalance_cooldown_remaining_seconds=max(
                    float(long_book["rebalance_cooldown_remaining_seconds"]),
                    float(short_book["rebalance_cooldown_remaining_seconds"]),
                ),
                rollout_stage=rollout["configured_rollout_stage"],
                runtime_rollout_stage=rollout["runtime_stage"],
            ),
        )

    def _overlay_decision(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        long_target_qty: Decimal,
        short_target_qty: Decimal,
    ) -> HedgeOverlayDecision:
        enabled = bool(self.settings.strategy_hedge_overlay_enabled)
        runtime_supported = self._hedge_overlay_runtime_supported(context=context)
        configured_mode = self.settings.strategy_hedge_overlay_mode
        if not enabled:
            return HedgeOverlayDecision(
                enabled=False,
                runtime_supported=runtime_supported,
                configured_mode=configured_mode,
                state="disabled",
            )
        if not runtime_supported:
            return HedgeOverlayDecision(
                enabled=True,
                runtime_supported=False,
                configured_mode=configured_mode,
                state="blocked",
                blocked_reasons=["hedge_overlay_runtime_not_supported"],
            )
        if configured_mode == "protective" and not self.settings.strategy_hedge_protective_enabled:
            return HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode=configured_mode,
                state="blocked",
                blocked_reasons=["protective_overlay_not_enabled"],
            )
        if configured_mode == "protective":
            return self._protective_overlay_decision(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                long_target_qty=long_target_qty,
                short_target_qty=short_target_qty,
            )
        if configured_mode == "opportunistic":
            return self._opportunistic_overlay_decision(
                context=context,
                baseline=baseline,
                ai_assessment=ai_assessment,
                long_target_qty=long_target_qty,
                short_target_qty=short_target_qty,
            )
        if configured_mode == "independent":
            return HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode=configured_mode,
                effective_mode="independent",
                overlay_source="independent_books",
                state="blocked",
                blocked_reasons=["independent_books_require_phase_b_strategy_path"],
            )
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode=configured_mode,
            state="blocked",
            blocked_reasons=["hedge_overlay_mode_not_enabled_in_current_phase"],
        )

    def _protective_overlay_decision(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        long_target_qty: Decimal,
        short_target_qty: Decimal,
    ) -> HedgeOverlayDecision:
        configured_mode = self.settings.strategy_hedge_overlay_mode
        if configured_mode != "protective":
            return HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode=configured_mode,
                state="blocked",
                blocked_reasons=["hedge_overlay_mode_not_enabled_in_current_phase"],
            )

        main_leg_signal = self._exposure_side(long_target_qty - short_target_qty)
        if main_leg_signal == "flat":
            return HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode=configured_mode,
                effective_mode="protective",
                overlay_source="protective",
                state="inactive",
                main_leg_signal="flat",
                hedge_leg_signal="flat",
                max_ratio=to_decimal(self.settings.strategy_hedge_max_ratio),
                open_threshold=self.settings.strategy_hedge_open_threshold,
                close_threshold=self.settings.strategy_hedge_close_threshold,
                fee_drag_ratio=context.recent_fee_drag_ratio,
                churn_ratio=context.recent_churn_ratio,
                reason_codes=["protective_overlay_main_signal_flat"],
            )

        if main_leg_signal == "long":
            main_leg_current_qty = to_decimal(context.current_long_position_qty)
            hedge_leg_current_qty = to_decimal(context.current_short_position_qty)
            main_leg_target_qty = to_decimal(long_target_qty)
            hedge_leg_signal = "short"
            current_leg_opened_at = context.current_short_leg_opened_at
            last_leg_closed_at = context.last_short_leg_closed_at
            latest_leg_fill_timestamp = context.latest_short_leg_fill_timestamp
        else:
            main_leg_current_qty = to_decimal(context.current_short_position_qty)
            hedge_leg_current_qty = to_decimal(context.current_long_position_qty)
            main_leg_target_qty = to_decimal(short_target_qty)
            hedge_leg_signal = "long"
            current_leg_opened_at = context.current_long_leg_opened_at
            last_leg_closed_at = context.last_long_leg_closed_at
            latest_leg_fill_timestamp = context.latest_long_leg_fill_timestamp

        if main_leg_current_qty <= EPSILON_DECIMAL_12 and hedge_leg_current_qty <= EPSILON_DECIMAL_12:
            return HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode="protective",
                effective_mode="protective",
                overlay_source="protective",
                state="inactive",
                main_leg_signal=main_leg_signal,  # type: ignore[arg-type]
                hedge_leg_signal=hedge_leg_signal,  # type: ignore[arg-type]
                main_leg_current_qty=main_leg_current_qty,
                hedge_leg_current_qty=hedge_leg_current_qty,
                main_leg_target_qty=main_leg_target_qty,
                hedge_leg_target_qty=Decimal("0"),
                max_ratio=to_decimal(self.settings.strategy_hedge_max_ratio),
                open_threshold=self.settings.strategy_hedge_open_threshold,
                close_threshold=self.settings.strategy_hedge_close_threshold,
                fee_drag_ratio=context.recent_fee_drag_ratio,
                churn_ratio=context.recent_churn_ratio,
                reason_codes=["protective_overlay_no_existing_inventory"],
            )

        pressure_score = self._protective_pressure_score(
            main_leg_signal=main_leg_signal,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        max_ratio = to_decimal(self.settings.strategy_hedge_max_ratio)
        open_threshold = float(self.settings.strategy_hedge_open_threshold)
        close_threshold = float(self.settings.strategy_hedge_close_threshold)
        target_ratio = Decimal("0")
        reason_codes: list[str] = []
        blocked_reasons: list[str] = []
        min_hold_remaining_seconds = 0.0
        rebalance_cooldown_remaining_seconds = 0.0

        if main_leg_target_qty <= EPSILON_DECIMAL_12:
            reason_codes.append("protective_overlay_main_leg_target_flat")
        elif pressure_score >= open_threshold:
            target_ratio = min(max_ratio, max_ratio * to_decimal(pressure_score))
            reason_codes.append("protective_overlay_pressure_above_open_threshold")
        elif hedge_leg_current_qty > EPSILON_DECIMAL_12 and pressure_score > close_threshold:
            target_ratio = min(max_ratio, max_ratio * to_decimal(pressure_score))
            reason_codes.append("protective_overlay_hold_above_close_threshold")
        else:
            reason_codes.append("protective_overlay_pressure_below_open_threshold")

        hedge_leg_target_qty = main_leg_target_qty * target_ratio
        now = utc_now()
        if hedge_leg_current_qty > EPSILON_DECIMAL_12:
            held_for = (
                0.0
                if current_leg_opened_at is None
                else max((now - current_leg_opened_at).total_seconds(), 0.0)
            )
            remaining_hold = max(self.settings.strategy_hedge_min_hold_seconds - held_for, 0.0)
            if hedge_leg_target_qty <= EPSILON_DECIMAL_12 and remaining_hold > 0:
                hedge_leg_target_qty = hedge_leg_current_qty
                min_hold_remaining_seconds = remaining_hold
                blocked_reasons.append("protective_overlay_min_hold_active")

        if latest_leg_fill_timestamp is not None:
            since_rebalance = max((now - latest_leg_fill_timestamp).total_seconds(), 0.0)
            remaining_rebalance = max(
                self.settings.strategy_hedge_rebalance_cooldown_seconds - since_rebalance,
                0.0,
            )
            if (
                remaining_rebalance > 0
                and abs(hedge_leg_target_qty - hedge_leg_current_qty) > EPSILON_DECIMAL_12
            ):
                hedge_leg_target_qty = hedge_leg_current_qty
                rebalance_cooldown_remaining_seconds = remaining_rebalance
                blocked_reasons.append("protective_overlay_rebalance_cooldown_active")
        elif hedge_leg_current_qty <= EPSILON_DECIMAL_12 and last_leg_closed_at is not None:
            since_close = max((now - last_leg_closed_at).total_seconds(), 0.0)
            remaining_rebalance = max(
                self.settings.strategy_hedge_rebalance_cooldown_seconds - since_close,
                0.0,
            )
            if remaining_rebalance > 0 and hedge_leg_target_qty > EPSILON_DECIMAL_12:
                hedge_leg_target_qty = Decimal("0")
                rebalance_cooldown_remaining_seconds = remaining_rebalance
                blocked_reasons.append("protective_overlay_rebalance_cooldown_active")

        state = "inactive"
        active = hedge_leg_target_qty > EPSILON_DECIMAL_12 or hedge_leg_current_qty > EPSILON_DECIMAL_12
        if hedge_leg_target_qty > EPSILON_DECIMAL_12 and hedge_leg_current_qty <= EPSILON_DECIMAL_12:
            state = "blocked" if blocked_reasons else "opening"
        elif hedge_leg_target_qty > EPSILON_DECIMAL_12 and hedge_leg_current_qty > EPSILON_DECIMAL_12:
            state = "blocked" if blocked_reasons and abs(hedge_leg_target_qty - hedge_leg_current_qty) > EPSILON_DECIMAL_12 else "holding"
        elif hedge_leg_target_qty <= EPSILON_DECIMAL_12 and hedge_leg_current_qty > EPSILON_DECIMAL_12:
            state = "blocked" if blocked_reasons else "closing"

        open_condition = f"压力分 {pressure_score:.2f} >= {open_threshold:.2f}"
        close_condition = f"压力分 {pressure_score:.2f} <= {close_threshold:.2f}"
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode="protective",
            effective_mode="protective",
            overlay_source="protective",
            active=active,
            state=state,  # type: ignore[arg-type]
            main_leg_signal=main_leg_signal,  # type: ignore[arg-type]
            hedge_leg_signal=hedge_leg_signal,  # type: ignore[arg-type]
            main_leg_current_qty=main_leg_current_qty,
            hedge_leg_current_qty=hedge_leg_current_qty,
            main_leg_target_qty=main_leg_target_qty,
            hedge_leg_target_qty=hedge_leg_target_qty,
            hedge_ratio=(
                Decimal("0")
                if main_leg_target_qty <= EPSILON_DECIMAL_12
                else min(hedge_leg_target_qty / main_leg_target_qty, Decimal("1"))
            ),
            max_ratio=max_ratio,
            pressure_score=pressure_score,
            open_threshold=open_threshold,
            close_threshold=close_threshold,
            open_condition=open_condition,
            close_condition=close_condition,
            fee_drag_ratio=context.recent_fee_drag_ratio,
            churn_ratio=context.recent_churn_ratio,
            reason_codes=reason_codes,
            blocked_reasons=blocked_reasons,
            min_hold_remaining_seconds=min_hold_remaining_seconds,
            rebalance_cooldown_remaining_seconds=rebalance_cooldown_remaining_seconds,
        )

    def _opportunistic_overlay_decision(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        long_target_qty: Decimal,
        short_target_qty: Decimal,
    ) -> HedgeOverlayDecision:
        configured_mode = self.settings.strategy_hedge_overlay_mode
        if not self.settings.strategy_hedge_opportunistic_enabled:
            return HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode=configured_mode,
                state="blocked",
                blocked_reasons=["opportunistic_overlay_not_enabled"],
            )
        rollout = overlay_rollout_status(self.settings, mode="opportunistic")
        if not rollout["runtime_allowed"]:
            return HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode=configured_mode,
                effective_mode="opportunistic",
                overlay_source="opportunistic",
                state="blocked",
                blocked_reasons=list(rollout["blocking_reasons"]),
                reason_codes=["opportunistic_overlay_rollout_gate_active"],
                rollout_stage=rollout["configured_rollout_stage"],
                runtime_rollout_stage=rollout["runtime_stage"],
            )

        main_leg_signal = self._exposure_side(long_target_qty - short_target_qty)
        if main_leg_signal == "flat":
            return HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode=configured_mode,
                effective_mode="opportunistic",
                overlay_source="opportunistic",
                state="inactive",
                main_leg_signal="flat",
                hedge_leg_signal="flat",
                max_ratio=to_decimal(self.settings.strategy_hedge_opportunistic_max_ratio),
                open_threshold=self.settings.strategy_hedge_opportunistic_open_threshold,
                close_threshold=self.settings.strategy_hedge_opportunistic_close_threshold,
                fee_drag_ratio=context.recent_fee_drag_ratio,
                churn_ratio=context.recent_churn_ratio,
                reason_codes=["opportunistic_overlay_main_signal_flat"],
                rollout_stage=rollout["configured_rollout_stage"],
                runtime_rollout_stage=rollout["runtime_stage"],
            )

        if main_leg_signal == "long":
            main_leg_current_qty = to_decimal(context.current_long_position_qty)
            hedge_leg_current_qty = to_decimal(context.current_short_position_qty)
            main_leg_target_qty = to_decimal(long_target_qty)
            hedge_leg_signal = "short"
            current_leg_opened_at = context.current_short_leg_opened_at
            last_leg_closed_at = context.last_short_leg_closed_at
            latest_leg_fill_timestamp = context.latest_short_leg_fill_timestamp
        else:
            main_leg_current_qty = to_decimal(context.current_short_position_qty)
            hedge_leg_current_qty = to_decimal(context.current_long_position_qty)
            main_leg_target_qty = to_decimal(short_target_qty)
            hedge_leg_signal = "long"
            current_leg_opened_at = context.current_long_leg_opened_at
            last_leg_closed_at = context.last_long_leg_closed_at
            latest_leg_fill_timestamp = context.latest_long_leg_fill_timestamp

        if main_leg_current_qty <= EPSILON_DECIMAL_12:
            return HedgeOverlayDecision(
                enabled=True,
                runtime_supported=True,
                configured_mode="opportunistic",
                effective_mode="opportunistic",
                overlay_source="opportunistic",
                state="inactive",
                main_leg_signal=main_leg_signal,  # type: ignore[arg-type]
                hedge_leg_signal=hedge_leg_signal,  # type: ignore[arg-type]
                main_leg_current_qty=main_leg_current_qty,
                hedge_leg_current_qty=Decimal("0"),
                main_leg_target_qty=main_leg_target_qty,
                hedge_leg_target_qty=Decimal("0"),
                max_ratio=to_decimal(self.settings.strategy_hedge_opportunistic_max_ratio),
                open_threshold=self.settings.strategy_hedge_opportunistic_open_threshold,
                close_threshold=self.settings.strategy_hedge_opportunistic_close_threshold,
                fee_drag_ratio=context.recent_fee_drag_ratio,
                churn_ratio=context.recent_churn_ratio,
                reason_codes=["opportunistic_overlay_no_existing_inventory"],
                rollout_stage=rollout["configured_rollout_stage"],
                runtime_rollout_stage=rollout["runtime_stage"],
            )

        opportunity_score = self._opportunistic_overlay_score(
            main_leg_signal=main_leg_signal,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        max_ratio = to_decimal(self.settings.strategy_hedge_opportunistic_max_ratio)
        open_threshold = float(self.settings.strategy_hedge_opportunistic_open_threshold)
        close_threshold = float(self.settings.strategy_hedge_opportunistic_close_threshold)
        target_ratio = Decimal("0")
        reason_codes: list[str] = []
        blocked_reasons: list[str] = []
        min_hold_remaining_seconds = 0.0
        rebalance_cooldown_remaining_seconds = 0.0

        if main_leg_target_qty <= EPSILON_DECIMAL_12:
            reason_codes.append("opportunistic_overlay_main_leg_target_flat")
        elif opportunity_score >= open_threshold:
            target_ratio = min(max_ratio, max_ratio * to_decimal(opportunity_score))
            reason_codes.append("opportunistic_overlay_signal_above_open_threshold")
        elif hedge_leg_current_qty > EPSILON_DECIMAL_12 and opportunity_score > close_threshold:
            target_ratio = min(max_ratio, max_ratio * to_decimal(opportunity_score))
            reason_codes.append("opportunistic_overlay_hold_above_close_threshold")
        else:
            reason_codes.append("opportunistic_overlay_signal_below_open_threshold")

        hedge_leg_target_qty = main_leg_target_qty * target_ratio
        opening_or_expanding = hedge_leg_target_qty > hedge_leg_current_qty + EPSILON_DECIMAL_12
        enough_history = context.recent_closed_trade_count >= self.settings.strategy_performance_guard_min_closed_trades
        if opening_or_expanding and enough_history:
            if context.recent_fee_drag_ratio > self.settings.strategy_hedge_opportunistic_max_fee_drag_ratio:
                hedge_leg_target_qty = hedge_leg_current_qty
                blocked_reasons.append("opportunistic_overlay_fee_drag_guard_active")
            elif context.recent_churn_ratio > self.settings.strategy_hedge_opportunistic_max_churn_ratio:
                hedge_leg_target_qty = hedge_leg_current_qty
                blocked_reasons.append("opportunistic_overlay_churn_guard_active")

        now = utc_now()
        if hedge_leg_current_qty > EPSILON_DECIMAL_12:
            held_for = (
                0.0
                if current_leg_opened_at is None
                else max((now - current_leg_opened_at).total_seconds(), 0.0)
            )
            remaining_hold = max(self.settings.strategy_hedge_opportunistic_min_hold_seconds - held_for, 0.0)
            if hedge_leg_target_qty <= EPSILON_DECIMAL_12 and remaining_hold > 0:
                hedge_leg_target_qty = hedge_leg_current_qty
                min_hold_remaining_seconds = remaining_hold
                blocked_reasons.append("opportunistic_overlay_min_hold_active")

        if latest_leg_fill_timestamp is not None:
            since_rebalance = max((now - latest_leg_fill_timestamp).total_seconds(), 0.0)
            remaining_rebalance = max(
                self.settings.strategy_hedge_opportunistic_rebalance_cooldown_seconds - since_rebalance,
                0.0,
            )
            if (
                remaining_rebalance > 0
                and abs(hedge_leg_target_qty - hedge_leg_current_qty) > EPSILON_DECIMAL_12
            ):
                hedge_leg_target_qty = hedge_leg_current_qty
                rebalance_cooldown_remaining_seconds = remaining_rebalance
                blocked_reasons.append("opportunistic_overlay_rebalance_cooldown_active")
        elif hedge_leg_current_qty <= EPSILON_DECIMAL_12 and last_leg_closed_at is not None:
            since_close = max((now - last_leg_closed_at).total_seconds(), 0.0)
            remaining_rebalance = max(
                self.settings.strategy_hedge_opportunistic_rebalance_cooldown_seconds - since_close,
                0.0,
            )
            if remaining_rebalance > 0 and hedge_leg_target_qty > EPSILON_DECIMAL_12:
                hedge_leg_target_qty = Decimal("0")
                rebalance_cooldown_remaining_seconds = remaining_rebalance
                blocked_reasons.append("opportunistic_overlay_rebalance_cooldown_active")

        state = "inactive"
        active = hedge_leg_target_qty > EPSILON_DECIMAL_12 or hedge_leg_current_qty > EPSILON_DECIMAL_12
        if hedge_leg_target_qty > EPSILON_DECIMAL_12 and hedge_leg_current_qty <= EPSILON_DECIMAL_12:
            state = "blocked" if blocked_reasons else "opening"
        elif hedge_leg_target_qty > EPSILON_DECIMAL_12 and hedge_leg_current_qty > EPSILON_DECIMAL_12:
            state = "blocked" if blocked_reasons and abs(hedge_leg_target_qty - hedge_leg_current_qty) > EPSILON_DECIMAL_12 else "holding"
        elif hedge_leg_target_qty <= EPSILON_DECIMAL_12 and hedge_leg_current_qty > EPSILON_DECIMAL_12:
            state = "blocked" if blocked_reasons else "closing"

        open_condition = f"机会分 {opportunity_score:.2f} >= {open_threshold:.2f}"
        close_condition = f"机会分 {opportunity_score:.2f} <= {close_threshold:.2f}"
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode="opportunistic",
            effective_mode="opportunistic",
            overlay_source="opportunistic",
            active=active,
            state=state,  # type: ignore[arg-type]
            main_leg_signal=main_leg_signal,  # type: ignore[arg-type]
            hedge_leg_signal=hedge_leg_signal,  # type: ignore[arg-type]
            main_leg_current_qty=main_leg_current_qty,
            hedge_leg_current_qty=hedge_leg_current_qty,
            main_leg_target_qty=main_leg_target_qty,
            hedge_leg_target_qty=hedge_leg_target_qty,
            hedge_ratio=(
                Decimal("0")
                if main_leg_target_qty <= EPSILON_DECIMAL_12
                else min(hedge_leg_target_qty / main_leg_target_qty, Decimal("1"))
            ),
            max_ratio=max_ratio,
            pressure_score=opportunity_score,
            open_threshold=open_threshold,
            close_threshold=close_threshold,
            open_condition=open_condition,
            close_condition=close_condition,
            fee_drag_ratio=context.recent_fee_drag_ratio,
            churn_ratio=context.recent_churn_ratio,
            reason_codes=reason_codes,
            blocked_reasons=blocked_reasons,
            min_hold_remaining_seconds=min_hold_remaining_seconds,
            rebalance_cooldown_remaining_seconds=rebalance_cooldown_remaining_seconds,
            rollout_stage=rollout["configured_rollout_stage"],
            runtime_rollout_stage=rollout["runtime_stage"],
        )

    def _independent_book_decision(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        leg: str,
        directional_leg_target_qty: Decimal,
    ) -> dict[str, object]:
        current_qty = (
            to_decimal(context.current_long_position_qty)
            if leg == "long"
            else to_decimal(context.current_short_position_qty)
        )
        score = self._independent_book_score(
            leg=leg,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        entry_threshold = (
            float(self.settings.strategy_hedge_independent_long_entry_threshold)
            if leg == "long"
            else float(self.settings.strategy_hedge_independent_short_entry_threshold)
        )
        scale_threshold = (
            float(self.settings.strategy_hedge_independent_long_scale_in_threshold)
            if leg == "long"
            else float(self.settings.strategy_hedge_independent_short_scale_in_threshold)
        )
        target_qty = current_qty
        base_target_qty = max(to_decimal(self.settings.default_order_qty), directional_leg_target_qty)
        reason_codes: list[str] = []
        blocked_reasons: list[str] = []
        state = "inactive"
        min_hold_remaining_seconds = 0.0
        rebalance_cooldown_remaining_seconds = 0.0

        if current_qty <= EPSILON_DECIMAL_12:
            if score >= entry_threshold:
                reason_codes.append(f"independent_{leg}_book_signal_above_entry_threshold")
                blocked_reasons.extend(self._independent_open_blockers(context=context, leg=leg))
                rebalance_cooldown_remaining_seconds = self._independent_rebalance_remaining_seconds(
                    context=context,
                    leg=leg,
                    opening_or_expanding=True,
                    desired_target_qty=base_target_qty,
                    current_qty=current_qty,
                )
                if rebalance_cooldown_remaining_seconds > 0:
                    blocked_reasons.append(f"independent_{leg}_book_rebalance_cooldown_active")
                if blocked_reasons:
                    state = "blocked"
                    target_qty = Decimal("0")
                else:
                    target_qty = base_target_qty
                    state = "opening"
            else:
                reason_codes.append(f"independent_{leg}_book_signal_below_entry_threshold")
        else:
            state = "holding"
            if score >= scale_threshold and base_target_qty > current_qty + EPSILON_DECIMAL_12:
                reason_codes.append(f"independent_{leg}_book_signal_above_scale_in_threshold")
                blocked_reasons.extend(self._independent_open_blockers(context=context, leg=leg))
                rebalance_cooldown_remaining_seconds = self._independent_rebalance_remaining_seconds(
                    context=context,
                    leg=leg,
                    opening_or_expanding=True,
                    desired_target_qty=base_target_qty,
                    current_qty=current_qty,
                )
                if rebalance_cooldown_remaining_seconds > 0:
                    blocked_reasons.append(f"independent_{leg}_book_rebalance_cooldown_active")
                if blocked_reasons:
                    target_qty = current_qty
                    state = "blocked"
                else:
                    target_qty = base_target_qty
                    state = "opening"
            elif score >= entry_threshold:
                reason_codes.append(f"independent_{leg}_book_hold_above_entry_threshold")
            else:
                reason_codes.append(f"independent_{leg}_book_signal_below_entry_threshold")
                min_hold_remaining_seconds = self._independent_min_hold_remaining_seconds(
                    context=context,
                    leg=leg,
                )
                if min_hold_remaining_seconds > 0:
                    blocked_reasons.append(f"independent_{leg}_book_min_hold_active")
                    state = "blocked"
                else:
                    rebalance_cooldown_remaining_seconds = self._independent_rebalance_remaining_seconds(
                        context=context,
                        leg=leg,
                        opening_or_expanding=False,
                        desired_target_qty=Decimal("0"),
                        current_qty=current_qty,
                    )
                    if rebalance_cooldown_remaining_seconds > 0:
                        blocked_reasons.append(f"independent_{leg}_book_rebalance_cooldown_active")
                        state = "blocked"
                    else:
                        target_qty = Decimal("0")
                        state = "closing"
        return {
            "leg": leg,
            "score": score,
            "current_qty": current_qty,
            "target_qty": target_qty,
            "reason_codes": reason_codes,
            "blocked_reasons": blocked_reasons,
            "state": state,
            "min_hold_remaining_seconds": min_hold_remaining_seconds,
            "rebalance_cooldown_remaining_seconds": rebalance_cooldown_remaining_seconds,
        }

    def _independent_open_blockers(self, *, context: DecisionContext, leg: str) -> list[str]:
        blocked_reasons: list[str] = []
        if self._independent_post_close_cooldown_active(context=context, leg=leg):
            blocked_reasons.append(f"independent_{leg}_book_post_close_cooldown_active")
        if self._independent_low_edge_cooldown_active(context=context, leg=leg):
            blocked_reasons.append(f"independent_{leg}_book_low_edge_cooldown_active")
        if self._independent_performance_degraded(context=context, leg=leg):
            fee_drag_ratio = float(self._leg_health_value(context, leg, "recent_fee_drag_ratio") or 0.0)
            churn_ratio = float(self._leg_health_value(context, leg, "recent_churn_ratio") or 0.0)
            if fee_drag_ratio > self.settings.strategy_max_fee_drag_ratio:
                blocked_reasons.append(f"independent_{leg}_book_fee_drag_guard_active")
            if churn_ratio > self.settings.strategy_max_churn_ratio:
                blocked_reasons.append(f"independent_{leg}_book_churn_guard_active")
        if self._independent_trial_guard_active(context=context, leg=leg):
            blocked_reasons.append(f"independent_{leg}_book_trial_guard_active")
        return blocked_reasons

    def _independent_book_score(
        self,
        *,
        leg: str,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> float:
        if leg == "short" and not self._short_bias_allowed("derivatives"):
            return 0.0
        side_sign = 1.0 if leg == "long" else -1.0
        momentum_alpha = float(baseline.factor_scores.get("momentum_alpha", 0.0))
        trend_alpha = float(baseline.factor_scores.get("trend_alpha", 0.0))
        microstructure_alpha = float(baseline.factor_scores.get("microstructure_alpha", 0.0))
        alpha_component = self._clamp(max(0.0, side_sign * float(baseline.composite_alpha_score)), 0.0, 1.0)
        ai_component = self._clamp(max(0.0, side_sign * self._ai_directional_edge(ai_assessment)), 0.0, 1.0)
        momentum_component = self._clamp(max(0.0, side_sign * momentum_alpha), 0.0, 1.0)
        trend_component = self._clamp(max(0.0, side_sign * trend_alpha), 0.0, 1.0)
        microstructure_component = self._clamp(max(0.0, side_sign * microstructure_alpha), 0.0, 1.0)
        confidence = self._clamp(float(baseline.confidence), 0.0, 1.0)
        score = (
            (alpha_component * 0.28)
            + (ai_component * 0.26)
            + (momentum_component * 0.16)
            + (trend_component * 0.12)
            + (microstructure_component * 0.08)
            + (confidence * 0.10)
        )
        if baseline.regime in {"range", "uncertain"}:
            score += 0.04
        if baseline.direction_bias == leg:
            score += 0.06
        if baseline.volatility_state == "high":
            score += 0.03
        return self._clamp(score, 0.0, 1.0)

    def _independent_min_hold_remaining_seconds(
        self,
        *,
        context: DecisionContext,
        leg: str,
    ) -> float:
        opened_at = context.current_long_leg_opened_at if leg == "long" else context.current_short_leg_opened_at
        min_hold_seconds = (
            self.settings.strategy_hedge_independent_long_min_hold_seconds
            if leg == "long"
            else self.settings.strategy_hedge_independent_short_min_hold_seconds
        )
        current_qty = (
            to_decimal(context.current_long_position_qty)
            if leg == "long"
            else to_decimal(context.current_short_position_qty)
        )
        if opened_at is None or min_hold_seconds <= 0 or current_qty <= EPSILON_DECIMAL_12:
            return 0.0
        held_for = max((utc_now() - opened_at).total_seconds(), 0.0)
        return max(float(min_hold_seconds) - held_for, 0.0)

    def _independent_rebalance_remaining_seconds(
        self,
        *,
        context: DecisionContext,
        leg: str,
        opening_or_expanding: bool,
        desired_target_qty: Decimal,
        current_qty: Decimal,
    ) -> float:
        if (
            self.settings.strategy_hedge_independent_rebalance_cooldown_seconds <= 0
            or abs(desired_target_qty - current_qty) <= EPSILON_DECIMAL_12
        ):
            return 0.0
        anchor = (
            context.latest_long_leg_fill_timestamp if leg == "long" else context.latest_short_leg_fill_timestamp
        )
        if opening_or_expanding and current_qty <= EPSILON_DECIMAL_12 and anchor is None:
            anchor = context.last_long_leg_closed_at if leg == "long" else context.last_short_leg_closed_at
        if anchor is None:
            return 0.0
        since_anchor = max((utc_now() - anchor).total_seconds(), 0.0)
        return max(self.settings.strategy_hedge_independent_rebalance_cooldown_seconds - since_anchor, 0.0)

    def _independent_post_close_cooldown_active(self, *, context: DecisionContext, leg: str) -> bool:
        if self.settings.strategy_post_close_cooldown_seconds <= 0:
            return False
        closed_at = context.last_long_leg_closed_at if leg == "long" else context.last_short_leg_closed_at
        if closed_at is None:
            return False
        return (
            max((utc_now() - closed_at).total_seconds(), 0.0)
            < self.settings.strategy_post_close_cooldown_seconds
        )

    def _independent_low_edge_cooldown_active(self, *, context: DecisionContext, leg: str) -> bool:
        if self.settings.strategy_low_edge_cooldown_seconds <= 0:
            return False
        streak = int(self._leg_health_value(context, leg, "recent_low_edge_trade_streak") or 0)
        if streak < self.settings.strategy_low_edge_streak_limit:
            return False
        recent_at = self._leg_health_datetime(context, leg, "recent_low_edge_trade_at")
        if recent_at is None:
            return False
        return (
            max((utc_now() - recent_at).total_seconds(), 0.0)
            < self.settings.strategy_low_edge_cooldown_seconds
        )

    def _independent_performance_degraded(self, *, context: DecisionContext, leg: str) -> bool:
        closed_trade_count = int(self._leg_health_value(context, leg, "recent_closed_trade_count") or 0)
        if closed_trade_count < self.settings.strategy_performance_guard_min_closed_trades:
            return False
        return (
            float(self._leg_health_value(context, leg, "recent_fee_drag_ratio") or 0.0)
            > self.settings.strategy_max_fee_drag_ratio
            or float(self._leg_health_value(context, leg, "recent_churn_ratio") or 0.0)
            > self.settings.strategy_max_churn_ratio
        )

    def _independent_trial_guard_active(self, *, context: DecisionContext, leg: str) -> bool:
        if not self.settings.strategy_hedge_independent_trial_guard_enabled:
            return False
        closed_trade_count = int(self._leg_health_value(context, leg, "recent_closed_trade_count") or 0)
        if closed_trade_count < self.settings.strategy_performance_guard_min_closed_trades:
            return False
        recent_net_realized_pnl = to_decimal(self._leg_health_value(context, leg, "recent_net_realized_pnl") or Decimal("0"))
        recent_win_rate = float(self._leg_health_value(context, leg, "recent_win_rate") or 0.0)
        return recent_net_realized_pnl < -EPSILON_DECIMAL_12 and recent_win_rate < 0.5

    def _leg_health_value(self, context: DecisionContext, leg: str, key: str) -> object | None:
        payload = context.leg_strategy_health.get(leg)
        if not isinstance(payload, dict):
            return None
        return payload.get(key)

    def _leg_health_datetime(self, context: DecisionContext, leg: str, key: str):
        value = self._leg_health_value(context, leg, key)
        return value if hasattr(value, "isoformat") else None

    def _build_independent_execution_leg(
        self,
        *,
        symbol: str,
        pos_side: str,
        current_leg_qty: Decimal,
        target_leg_qty: Decimal,
        target_leverage: float,
        reason_codes: list[str],
    ) -> StrategyLegIntent | None:
        current_leg_qty = max(to_decimal(current_leg_qty), Decimal("0"))
        target_leg_qty = max(to_decimal(target_leg_qty), Decimal("0"))
        delta_qty = target_leg_qty - current_leg_qty
        if abs(delta_qty) <= EPSILON_DECIMAL_12:
            return None
        opening = delta_qty > 0
        action = "open" if opening else ("close" if target_leg_qty <= EPSILON_DECIMAL_12 else "reduce")
        if pos_side == "long":
            side = "buy" if opening else "sell"
            signed_current_qty = current_leg_qty
            signed_target_qty = target_leg_qty
            execution_mode = "independent_long_book"
            note = "Independent long book 决策腿。"
        else:
            side = "sell" if opening else "buy"
            signed_current_qty = -current_leg_qty
            signed_target_qty = -target_leg_qty
            execution_mode = "independent_short_book"
            note = "Independent short book 决策腿。"
        return StrategyLegIntent(
            symbol=symbol,
            product_type="derivatives",
            side=side,  # type: ignore[arg-type]
            position_mode="long_short_mode",
            pos_side=pos_side,  # type: ignore[arg-type]
            action=action,  # type: ignore[arg-type]
            family="directional",
            role="primary",
            margin_mode=self.settings.margin_mode,
            target_leverage=target_leverage,
            current_position_qty=signed_current_qty,
            target_position_qty=signed_target_qty,
            delta_position_qty=signed_target_qty - signed_current_qty,
            execution_compatible=True,
            execution_mode=execution_mode,
            state_phase="active",
            overlay_mode="independent",
            trigger_reason_codes=reason_codes,
            note=note,
        )

    def _protective_pressure_score(
        self,
        *,
        main_leg_signal: str,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> float:
        current_position_qty = Decimal("1") if main_leg_signal == "long" else Decimal("-1")
        factors = self._position_adverse_factors(
            current_position_qty=current_position_qty,
            baseline=baseline,
            ai_assessment=ai_assessment,
        )
        adverse_score = self._clamp(float(int(factors["adverse_count"])) / 4.0, 0.0, 1.0)
        side_sign = 1.0 if main_leg_signal == "long" else -1.0
        opposite_alpha = self._clamp(max(0.0, -(side_sign * float(baseline.composite_alpha_score))), 0.0, 1.0)
        opposite_ai = self._clamp(max(0.0, -(side_sign * self._ai_directional_edge(ai_assessment))), 0.0, 1.0)
        confidence = self._clamp(float(baseline.confidence), 0.0, 1.0)
        pressure = (adverse_score * 0.45) + (opposite_alpha * 0.25) + (opposite_ai * 0.20) + (confidence * 0.10)
        if baseline.direction_bias not in {main_leg_signal, "flat"}:
            pressure += 0.08
        if baseline.volatility_state == "high":
            pressure += 0.08
        if baseline.regime in {"breakout", "trend"} and adverse_score >= 0.5:
            pressure += 0.05
        return self._clamp(pressure, 0.0, 1.0)

    def _opportunistic_overlay_score(
        self,
        *,
        main_leg_signal: str,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> float:
        side_sign = 1.0 if main_leg_signal == "long" else -1.0
        microstructure_alpha = float(baseline.factor_scores.get("microstructure_alpha", 0.0))
        momentum_alpha = float(baseline.factor_scores.get("momentum_alpha", 0.0))
        trend_alpha = float(baseline.factor_scores.get("trend_alpha", 0.0))
        opposite_microstructure = self._clamp(max(0.0, -(side_sign * microstructure_alpha)), 0.0, 1.0)
        opposite_momentum = self._clamp(max(0.0, -(side_sign * momentum_alpha)), 0.0, 1.0)
        opposite_trend = self._clamp(max(0.0, -(side_sign * trend_alpha)), 0.0, 1.0)
        opposite_ai = self._clamp(max(0.0, -(side_sign * self._ai_directional_edge(ai_assessment))), 0.0, 1.0)
        confidence = self._clamp(float(baseline.confidence), 0.0, 1.0)
        opportunity = (
            (opposite_microstructure * 0.28)
            + (opposite_ai * 0.24)
            + (opposite_momentum * 0.18)
            + (opposite_trend * 0.12)
            + (confidence * 0.10)
        )
        if baseline.regime in {"range", "uncertain"}:
            opportunity += 0.08
        if baseline.volatility_state == "high":
            opportunity += 0.06
        if baseline.direction_bias not in {main_leg_signal, "flat"}:
            opportunity += 0.10
        return self._clamp(opportunity, 0.0, 1.0)

    def _build_hedge_execution_leg(
        self,
        *,
        symbol: str,
        pos_side: str,
        current_leg_qty: Decimal,
        target_leg_qty: Decimal,
        target_leverage: float,
        overlay_decision: HedgeOverlayDecision,
    ) -> StrategyLegIntent | None:
        current_leg_qty = max(to_decimal(current_leg_qty), Decimal("0"))
        target_leg_qty = max(to_decimal(target_leg_qty), Decimal("0"))
        delta_qty = target_leg_qty - current_leg_qty
        if abs(delta_qty) <= EPSILON_DECIMAL_12:
            return None
        opening = delta_qty > 0
        action = "open" if opening else ("close" if target_leg_qty <= EPSILON_DECIMAL_12 else "reduce")
        if pos_side == "long":
            side = "buy" if opening else "sell"
            signed_current_qty = current_leg_qty
            signed_target_qty = target_leg_qty
        else:
            side = "sell" if opening else "buy"
            signed_current_qty = -current_leg_qty
            signed_target_qty = -target_leg_qty
        role = "primary"
        overlay_mode = None
        hedge_ratio = None
        trigger_reason_codes: list[str] = []
        note = "Directional 主腿按 hedge mode 双腿执行。"
        if overlay_decision.active and overlay_decision.hedge_leg_signal == pos_side:
            role = "hedge"
            effective_mode = overlay_decision.effective_mode or overlay_decision.configured_mode
            overlay_mode = effective_mode
            hedge_ratio = overlay_decision.hedge_ratio
            trigger_reason_codes = list(overlay_decision.reason_codes)
            if effective_mode == "opportunistic":
                note = "Directional opportunistic overlay 生成的机会腿。"
                execution_mode = "opportunistic_overlay"
            else:
                note = "Directional protective overlay 生成的保护腿。"
                execution_mode = "protective_overlay"
        elif overlay_decision.active and overlay_decision.main_leg_signal == pos_side:
            effective_mode = overlay_decision.effective_mode or overlay_decision.configured_mode
            if effective_mode == "opportunistic":
                note = "Directional 主腿在 opportunistic overlay 下继续保留。"
            else:
                note = "Directional 主腿在 protective overlay 下继续保留。"
            execution_mode = "directional_main_leg"
        else:
            execution_mode = "directional_main_leg"
        return StrategyLegIntent(
            symbol=symbol,
            product_type="derivatives",
            side=side,  # type: ignore[arg-type]
            position_mode="long_short_mode",
            pos_side=pos_side,  # type: ignore[arg-type]
            action=action,  # type: ignore[arg-type]
            family="directional",
            role=role,  # type: ignore[arg-type]
            margin_mode=self.settings.margin_mode,
            target_leverage=target_leverage,
            current_position_qty=signed_current_qty,
            target_position_qty=signed_target_qty,
            delta_position_qty=signed_target_qty - signed_current_qty,
            execution_compatible=True,
            execution_mode=execution_mode,
            state_phase=overlay_decision.state if role == "hedge" else "active",
            overlay_mode=overlay_mode,  # type: ignore[arg-type]
            hedge_ratio=hedge_ratio,
            trigger_reason_codes=trigger_reason_codes,
            note=note,
        )

    def _rebalance_band(self, *, current_position_qty: Decimal, desired_target_qty: Decimal) -> Decimal:
        return max(
            to_decimal(self.settings.default_order_qty) * Decimal("0.12"),
            abs(desired_target_qty) * Decimal("0.08"),
            abs(current_position_qty) * Decimal("0.08"),
            EPSILON_DECIMAL_12,
        )

    def _reduce_threshold(self, *, current_position_qty: Decimal, desired_target_qty: Decimal) -> Decimal:
        return max(
            to_decimal(self.settings.default_order_qty) * Decimal("0.1"),
            abs(current_position_qty) * Decimal("0.12"),
            abs(desired_target_qty) * Decimal("0.12"),
        )

    def _reverse_threshold(self, *, current_position_qty: Decimal) -> Decimal:
        return max(
            to_decimal(self.settings.default_order_qty) * Decimal("0.45"),
            abs(current_position_qty) * Decimal("0.35"),
        )

    def _max_scale_step(self, desired_target_qty: Decimal) -> Decimal:
        return max(to_decimal(self.settings.default_order_qty) * Decimal("0.4"), abs(desired_target_qty) * Decimal("0.45"))

    @staticmethod
    def _derivatives_reversal_step(*, current_position_qty: Decimal) -> Decimal:
        return current_position_qty * Decimal("0.35")

    def _urgency(self, *, current_position_qty: Decimal, target_position_qty: Decimal) -> str:
        delta_qty = abs(target_position_qty - current_position_qty)
        if delta_qty < EPSILON_DECIMAL_12:
            return "low"
        if current_position_qty * target_position_qty < 0:
            return "high"
        if delta_qty >= to_decimal(self.settings.default_order_qty) * Decimal("0.75"):
            return "high"
        return "medium"

    def _position_intent(
        self,
        *,
        current_position_qty: Decimal,
        target_position_qty: Decimal,
    ) -> str:
        if abs(target_position_qty - current_position_qty) < EPSILON_DECIMAL_12:
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
    def _exposure_side(quantity: Decimal) -> str:
        if quantity > EPSILON_DECIMAL_12:
            return "long"
        if quantity < -EPSILON_DECIMAL_12:
            return "short"
        return "flat"

    def _source_mix(
        self,
        *,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        operating_mode: str,
        ai_decision_authorized: bool,
    ) -> dict[str, float]:
        mode = normalize_ai_operating_mode(operating_mode)
        if mode == "baseline_only":
            return {"baseline": 1.0, "ai": 0.0}
        if mode == "ai_assisted":
            return {"baseline": 0.6, "ai": 0.4}
        if (
            mode in {"ai_decision_maker", "ai_decision_maker_with_profile_control"}
            and ai_decision_authorized
            and ai_decision_intent is not None
            and not ai_decision_intent.fallback_used
        ):
            return {"baseline": 0.2, "ai": 0.8}
        return {"baseline": 1.0, "ai": 0.0}

    def _ai_decision_gate(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        operating_mode: CanonicalAIOperatingMode,
    ) -> tuple[bool, list[str]]:
        if operating_mode not in {"ai_decision_maker", "ai_decision_maker_with_profile_control"}:
            return False, []
        if ai_assessment is None or ai_decision_intent is None:
            return False, ["ai_decision_intent_missing"]
        blockers: list[str] = []
        if ai_decision_intent.fallback_used:
            blockers.append("ai_fallback_used")
        if not ai_assessment.output_valid:
            blockers.append("ai_output_invalid")
        blockers.extend(ai_assessment.rejection_flags)
        if ai_decision_intent.degraded:
            blockers.append("ai_degraded")
        if ai_decision_intent.confidence + float(EPSILON_DECIMAL_12) < self.settings.ai_decision_min_confidence:
            blockers.append("ai_confidence_below_threshold")
        if ai_assessment.uncertainty - float(EPSILON_DECIMAL_12) > self.settings.ai_decision_max_uncertainty:
            blockers.append("ai_uncertainty_above_threshold")
        if abs(ai_assessment.directional_edge) + float(EPSILON_DECIMAL_12) < self.settings.ai_decision_min_directional_edge:
            blockers.append("ai_directional_edge_too_small")
        if not ai_assessment.baseline_override_recommended:
            blockers.append("ai_override_not_recommended")
        if not ai_assessment.economically_actionable:
            blockers.append("ai_not_economically_actionable")
        allowed_regimes = {item.lower() for item in self.settings.strategy_entry_allowed_regimes if item}
        if allowed_regimes and ai_assessment.regime.lower() not in allowed_regimes:
            blockers.append("ai_regime_not_allowed")
        if context.current_open_orders:
            blockers.append("ai_open_orders_present")
        if self._post_close_cooldown_active(context):
            blockers.append("ai_post_close_cooldown_active")
        if self._low_edge_cooldown_active(context):
            blockers.append("ai_low_edge_cooldown_active")
        if self._performance_degraded(context):
            blockers.append("ai_execution_performance_guard_active")
        if baseline.direction_bias == "flat" and abs(ai_assessment.directional_edge) < self.settings.ai_decision_min_directional_edge + 0.05:
            blockers.append("ai_flat_context_requires_stronger_edge")
        return not blockers, blockers

    def _decision_outcome(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
        ai_decision_intent: AIDecisionIntent | None,
        profile_control_decision: ProfileControlDecision | None,
        canonical_mode: CanonicalAIOperatingMode,
        target_qty: Decimal,
        target_exposure_side: str,
        position_intent: str,
        ai_decision_authorized: bool,
        ai_decision_applied: bool,
        ai_decision_blockers: list[str],
        guardrail_flags: list[str],
    ) -> DecisionOutcome:
        authority_map = {
            "baseline_only": "reference_only",
            "ai_assisted": "advisory",
            "ai_decision_maker": "final_decision",
            "ai_decision_maker_with_profile_control": "final_decision_with_profile_control",
        }
        if canonical_mode in {"ai_decision_maker", "ai_decision_maker_with_profile_control"}:
            decision_source = "ai" if ai_decision_applied else "baseline_fallback"
        else:
            decision_source = "baseline"
        profile_control_source = "env_default"
        active_profile_id = None
        if profile_control_decision is not None:
            active_profile_id = (
                profile_control_decision.requested_profile_id
                if profile_control_decision.applied
                else profile_control_decision.current_profile_id
            )
            profile_control_source = (
                "ai"
                if profile_control_decision.applied
                else "admin" if profile_control_decision.frozen_by_admin_override else "system"
            )
        action_map = {
            "hold": "hold",
            "open_long": "enter",
            "open_short": "enter",
            "reduce_long": "reduce",
            "reduce_short": "reduce",
            "close_long": "exit",
            "close_short": "exit",
            "reverse_to_long": "reverse",
            "reverse_to_short": "reverse",
        }
        blocked_reasons = list(dict.fromkeys([*guardrail_flags, *ai_decision_blockers]))
        position_management_reason_codes = [
            code
            for code in ("alpha_decay_exit", "alpha_decay_reduce", "risk_contraction_exit", "emergency_protective_exit")
            if code in guardrail_flags
        ]
        exit_attribution = None
        if "emergency_protective_exit" in guardrail_flags:
            exit_attribution = "emergency_protective_exit"
        elif "alpha_decay_exit" in guardrail_flags:
            exit_attribution = "alpha_decay_exit"
        elif "alpha_decay_reduce" in guardrail_flags:
            exit_attribution = "alpha_decay_reduce"
        elif "risk_contraction_exit" in guardrail_flags:
            exit_attribution = "risk_contraction_exit"
        return DecisionOutcome(
            decision_id=context.decision_id,
            symbol=context.symbol,
            ai_operating_mode=canonical_mode,
            finalized=False,
            decision_source=decision_source,
            decision_authority=authority_map[canonical_mode],
            final_direction=target_exposure_side,
            final_action=action_map.get(position_intent, "hold"),
            final_target_qty=target_qty,
            baseline_reference={
                "direction_bias": baseline.direction_bias,
                "confidence": baseline.confidence,
                "regime": baseline.regime,
                "volatility_state": baseline.volatility_state,
                "composite_alpha_score": baseline.composite_alpha_score,
                "suggested_position_scale": baseline.suggested_position_scale,
                "reason_codes": list(baseline.reason_codes),
            },
            baseline_disagreement=None if ai_assessment is None else {
                "disagreed": (ai_decision_intent.direction if ai_decision_intent is not None else self._direction_from_assessment(ai_assessment)) != baseline.direction_bias,
                "baseline_direction": baseline.direction_bias,
                "ai_direction": ai_decision_intent.direction if ai_decision_intent is not None else self._direction_from_assessment(ai_assessment),
            },
            decision_blocked_reasons=blocked_reasons,
            guardrail_flags=list(dict.fromkeys(guardrail_flags)),
            policy_blocked=False,
            policy_blocked_reasons=[],
            risk_capped=False,
            risk_capped_reasons=[],
            risk_capped_target_qty=None,
            active_profile_id=active_profile_id,
            profile_control_source=profile_control_source,
            ai_fallback_used=False if ai_decision_intent is None else ai_decision_intent.fallback_used,
            ai_degraded=False if ai_decision_intent is None else ai_decision_intent.degraded,
            position_management_reason_codes=position_management_reason_codes,
            exit_attribution=exit_attribution,
        )

    @staticmethod
    def _direction_from_assessment(ai_assessment: AIMarketAssessment) -> str:
        if ai_assessment.directional_edge > 0.0:
            return "long"
        if ai_assessment.directional_edge < 0.0:
            return "short"
        return "flat"

    @staticmethod
    def _shadow_action_type(*, baseline_action: str, shadow_action: str) -> str:
        if baseline_action == shadow_action:
            return "same_as_baseline"
        if baseline_action == "hold" and shadow_action != "hold":
            return "entry_override"
        if baseline_action != "hold" and shadow_action == "hold":
            return "hold_instead"
        if shadow_action.startswith("reverse"):
            return "reverse_override"
        return "exit_override"

    @staticmethod
    def _same_direction(left: Decimal, right: Decimal) -> bool:
        if abs(left) < EPSILON_DECIMAL_12 or abs(right) < EPSILON_DECIMAL_12:
            return True
        return (left > 0 and right > 0) or (left < 0 and right < 0)

    @staticmethod
    def _sign(value: Decimal) -> Decimal:
        if value > 0:
            return Decimal("1")
        if value < 0:
            return Decimal("-1")
        return Decimal("0")

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))

    def _flat_cleanup_threshold(self) -> Decimal:
        return max(to_decimal(self.settings.default_order_qty) * Decimal("0.15"), EPSILON_DECIMAL_12)

    def _estimated_trade_cost_bps(
        self,
        *,
        symbol: str | None = None,
        product_type: str = "spot",
        ai_assessment: AIMarketAssessment | None = None,
    ) -> float:
        expected_slippage_bps = max(self.settings.max_slippage_tolerance_bps, 0) * max(
            self.settings.strategy_expected_slippage_bps_fraction,
            0.0,
        )
        envelope = None if ai_assessment is None else ai_assessment.ai_execution_parameter_suggestion
        suggestion = None if envelope is None else envelope.suggestion
        estimate = self.trade_cost_service.estimate_single_leg_entry(
            model_name="directional_target_position",
            symbol=symbol,
            product_type=product_type,
            margin_mode=self.settings.margin_mode,
            execution_style="bounded_limit_ioc" if suggestion is not None else "taker",
            order_type="limit" if suggestion is not None else "market",
            passive_bias=None if suggestion is None else suggestion.passive_bias,
            maker_taker_bias=None if suggestion is None else suggestion.maker_taker_bias,
            expected_slippage_bps=expected_slippage_bps,
            include_spread=False,
            include_funding=product_type == "derivatives",
        )
        return float(estimate.executable_total_drag_bps)

    def _signal_edge_bps(
        self,
        *,
        baseline: BaselineAssessment,
        ai_assessment: AIMarketAssessment | None,
    ) -> float:
        alpha_edge = abs(baseline.composite_alpha_score) * max(self.settings.strategy_alpha_edge_bps_scale, 0.0)
        microstructure_bonus = max(abs(baseline.factor_scores.get("microstructure_alpha", 0.0)) - 0.08, 0.0) * 25.0
        ai_bonus = max(abs(self._ai_directional_edge(ai_assessment)) - 0.1, 0.0) * 20.0
        return alpha_edge + microstructure_bonus + ai_bonus

    @staticmethod
    def _ai_directional_edge(ai_assessment: AIMarketAssessment | None) -> float:
        return 0.0 if ai_assessment is None else ai_assessment.directional_edge

    @staticmethod
    def _ai_confidence_component(ai_assessment: AIMarketAssessment | None) -> float:
        if ai_assessment is None:
            return 0.0
        return max(ai_assessment.calibrated_confidence, ai_assessment.confidence)
