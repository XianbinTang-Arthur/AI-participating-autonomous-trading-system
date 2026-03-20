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
    ProfileControlDecision,
    PositionTarget,
    normalize_ai_operating_mode,
)
from aats.services.fee_resolver import EffectiveFeeResolver
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal


class TargetPositionEngine:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        fee_resolver: EffectiveFeeResolver | None = None,
    ) -> None:
        self.settings = settings
        self.fee_resolver = fee_resolver or EffectiveFeeResolver(settings=settings)

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
                ai_decision_intent=ai_decision_intent,
                product_type=product_type,
                baseline_fallback_qty=baseline_fallback_qty,
                ai_decision_authorized=ai_decision_authorized,
            )
        if mode == "ai_decision_maker_with_profile_control":
            return self._target_quantity_ai_decision_maker(
                context=context,
                ai_decision_intent=ai_decision_intent,
                product_type=product_type,
                baseline_fallback_qty=baseline_fallback_qty,
                ai_decision_authorized=ai_decision_authorized,
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
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=baseline_qty,
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
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=baseline_qty,
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
        ai_decision_intent: AIDecisionIntent | None,
        product_type: str,
        baseline_fallback_qty: Decimal,
        ai_decision_authorized: bool,
    ) -> Decimal:
        if ai_decision_intent is not None and ai_decision_authorized:
            desired_target_qty = self._desired_target_qty_from_ai_decision_intent(
                context=context,
                ai_decision_intent=ai_decision_intent,
            )
            return self._apply_position_management(
                current_position_qty=context.current_position_qty,
                desired_target_qty=desired_target_qty,
                product_type=product_type,
            )
        return self._apply_position_management(
            current_position_qty=context.current_position_qty,
            desired_target_qty=baseline_fallback_qty,
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
        target_qty = self._qty_from_bias(baseline.direction_bias, product_type=product_type) * scale
        if baseline.volatility_target_scale < 0.55:
            target_qty *= to_decimal(baseline.volatility_target_scale)
        return target_qty

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
        if not self._regime_allowed_for_entry(baseline.regime):
            guardrail_flags.append("entry_regime_not_allowed")
            return current_position_qty
        alpha = abs(baseline.composite_alpha_score)
        confidence = baseline.confidence
        if trade_kind == "entry":
            if alpha + float(EPSILON_DECIMAL_12) < self.settings.strategy_entry_alpha_min:
                guardrail_flags.append("entry_alpha_below_threshold")
                return current_position_qty
            if confidence + float(EPSILON_DECIMAL_12) < self.settings.strategy_entry_confidence_min:
                guardrail_flags.append("entry_confidence_below_threshold")
                return current_position_qty
            if signal_edge_bps + float(EPSILON_DECIMAL_12) < self.settings.strategy_entry_min_signal_edge_bps:
                guardrail_flags.append("entry_signal_edge_below_threshold")
                return current_position_qty
            return desired_target_qty
        if trade_kind == "scale_in":
            if alpha + float(EPSILON_DECIMAL_12) < self.settings.strategy_scale_in_alpha_min:
                guardrail_flags.append("scale_in_alpha_below_threshold")
                return current_position_qty
            if confidence + float(EPSILON_DECIMAL_12) < self.settings.strategy_scale_in_confidence_min:
                guardrail_flags.append("scale_in_confidence_below_threshold")
                return current_position_qty
            if signal_edge_bps + float(EPSILON_DECIMAL_12) < self.settings.strategy_scale_in_min_signal_edge_bps:
                guardrail_flags.append("scale_in_signal_edge_below_threshold")
                return current_position_qty
            return desired_target_qty
        if trade_kind == "reversal":
            if alpha + float(EPSILON_DECIMAL_12) < self.settings.strategy_reversal_alpha_min:
                guardrail_flags.append("reversal_alpha_below_threshold")
                return current_position_qty
            if confidence + float(EPSILON_DECIMAL_12) < self.settings.strategy_reversal_confidence_min:
                guardrail_flags.append("reversal_confidence_below_threshold")
                return current_position_qty
            if signal_edge_bps + float(EPSILON_DECIMAL_12) < self.settings.strategy_reversal_min_signal_edge_bps:
                guardrail_flags.append("reversal_signal_edge_below_threshold")
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
            if trade_kind == "reversal" and self._reversal_requires_additional_edge(signal_edge_bps):
                guardrail_flags.append("reversal_edge_not_strong_enough")
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

    def _regime_allowed_for_entry(self, regime: str) -> bool:
        allowed_regimes = {value.lower() for value in self.settings.strategy_entry_allowed_regimes if value}
        if not allowed_regimes:
            return True
        return regime.lower() in allowed_regimes

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

    def _reversal_requires_additional_edge(self, signal_edge_bps: float) -> bool:
        required = self.settings.strategy_reversal_min_signal_edge_bps + max(self.settings.strategy_edge_noise_buffer_bps, 0.0)
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
        side_sign = self._sign(current_position_qty)
        microstructure = to_decimal(baseline.factor_scores.get("microstructure_alpha", 0.0))
        momentum_alpha = to_decimal(baseline.factor_scores.get("momentum_alpha", 0.0))
        trend_alpha = to_decimal(baseline.factor_scores.get("trend_alpha", 0.0))
        ai_edge = Decimal("0") if ai_assessment is None else to_decimal(ai_assessment.directional_edge)
        adverse_microstructure = (side_sign * microstructure) <= -abs(to_decimal(self.settings.strategy_flat_exit_microstructure_threshold))
        adverse_momentum = (side_sign * momentum_alpha) <= -abs(to_decimal(self.settings.strategy_flat_exit_factor_threshold))
        adverse_trend = (side_sign * trend_alpha) <= -abs(to_decimal(self.settings.strategy_flat_exit_factor_threshold))
        adverse_ai = (side_sign * ai_edge) <= -abs(to_decimal(self.settings.strategy_flat_exit_ai_edge_threshold))
        adverse_count = sum((adverse_microstructure, adverse_momentum, adverse_trend, adverse_ai))
        if adverse_count >= 2:
            return True
        if adverse_microstructure and adverse_ai:
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
        return product_type == "derivatives"

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
        rebalance_band = self._rebalance_band(
            current_position_qty=current_position_qty,
            desired_target_qty=desired_target_qty,
        )
        delta_qty = desired_target_qty - current_position_qty
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
        estimated_fee_bps = self.fee_resolver.estimated_execution_fee_bps(
            symbol=symbol,
            execution_style="bounded_limit_ioc" if suggestion is not None else "taker",
            order_type="limit" if suggestion is not None else "market",
            passive_bias=None if suggestion is None else suggestion.passive_bias,
            maker_taker_bias=None if suggestion is None else suggestion.maker_taker_bias,
        )
        funding_fee_bps = self.fee_resolver.funding_fee_bps(symbol=symbol) if product_type == "derivatives" else 0.0
        return estimated_fee_bps + expected_slippage_bps + funding_fee_bps

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
