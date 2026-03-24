from __future__ import annotations

from decimal import Decimal
from typing import Literal

from aats.bootstrap.settings import AATSSettings
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import publish_model
from aats.schemas.common import new_id
from aats.schemas.execution import (
    AIExecutionParameterSuggestionEnvelope,
    ExecutionParameterTranslationPreview,
    ExecutionParameterSuggestion,
    ExecutionAction,
    ExecutionPlan,
    OrderIntent,
    close_only_from_position_intent,
    default_close_only_reason,
    default_reduce_only_reason,
    pos_side_from_position_intent,
    reduce_only_from_position_intent,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal


class ExecutionPlanner:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    def build_plan(
        self,
        *,
        decision_id: str,
        symbol: str,
        current_position_qty: Decimal | float,
        target_position_qty: Decimal | float,
        approved_target_position_qty: Decimal | float,
        delta_qty: Decimal | float,
        urgency: str,
        max_slippage_tolerance_bps: int,
        reference_price: Decimal | float | None = None,
        product_type: str = "spot",
        target_leverage: float = 1.0,
        margin_mode: str = "cash",
        position_mode: str | None = None,
        instrument_family: str | None = None,
        settle_currency: str | None = None,
        td_mode: str | None = None,
        required_initial_margin: Decimal | float | None = None,
        projected_margin_usage: Decimal | float | None = None,
        projected_notional: Decimal | float | None = None,
        risk_budget_multiplier: Decimal | float | None = None,
        risk_budget_state: dict[str, object] | None = None,
        execution_aggressiveness_multiplier: Decimal | float | None = None,
        execution_aggressiveness_state: dict[str, object] | None = None,
        only_reduce_required: bool = False,
        risk_limit_breached: bool = False,
        liquidation_buffer_remaining: Decimal | float | None = None,
        strategy_family: str | None = None,
        strategy_bundle_id: str | None = None,
        strategy_leg_role: Literal["primary", "hedge", "inventory", "accumulation"] | None = None,
        ai_execution_parameter_suggestion: AIExecutionParameterSuggestionEnvelope | None = None,
    ) -> ExecutionPlan | None:
        if abs(to_decimal(delta_qty)) < EPSILON_DECIMAL_12:
            return None

        normalized_urgency = urgency if urgency in {"low", "medium", "high"} else "medium"
        side = "buy" if delta_qty > 0 else "sell"
        execution_action = self._execution_action(
            current_position_qty=current_position_qty,
            target_position_qty=approved_target_position_qty,
        )
        position_intent = self._position_intent(
            current_position_qty=current_position_qty,
            target_position_qty=approved_target_position_qty,
        )
        reduce_only = reduce_only_from_position_intent(position_intent)
        close_only = close_only_from_position_intent(position_intent)
        resolved_position_mode = position_mode if position_mode in {"net_mode", "long_short_mode"} else None
        resolved_td_mode = td_mode or margin_mode
        exposure_side = self._exposure_side(approved_target_position_qty)
        normalized_execution_multiplier = self._normalized_multiplier(execution_aggressiveness_multiplier)
        effective_slippage_tolerance_bps = self._effective_slippage_tolerance_bps(
            max_slippage_tolerance_bps=max_slippage_tolerance_bps,
            execution_aggressiveness_multiplier=normalized_execution_multiplier,
        )
        pos_side = pos_side_from_position_intent(
            position_intent=position_intent,
            position_mode=resolved_position_mode,
        )
        translated_suggestion = self._translate_ai_execution_parameter_suggestion(
            ai_execution_parameter_suggestion,
            side=side,
            reference_price=reference_price,
            max_slippage_tolerance_bps=effective_slippage_tolerance_bps,
            execution_aggressiveness_multiplier=normalized_execution_multiplier,
        )
        execution_style = "taker"
        order_type = "market"
        limit_price = None
        time_in_force = "IOC"
        if (
            translated_suggestion is not None
            and translated_suggestion.applied_to_live_execution
            and translated_suggestion.translation_preview is not None
        ):
            execution_style = translated_suggestion.translation_preview.execution_style
            order_type = translated_suggestion.translation_preview.order_type
            time_in_force = translated_suggestion.translation_preview.time_in_force
            limit_price = self._bounded_live_limit_price(
                side=side,
                reference_price=reference_price,
                preview=translated_suggestion.translation_preview,
                max_slippage_tolerance_bps=effective_slippage_tolerance_bps,
            )
        return ExecutionPlan(
            plan_id=new_id("plan"),
            decision_id=decision_id,
            symbol=symbol,
            current_position_qty=current_position_qty,
            target_position_qty=target_position_qty,
            approved_target_position_qty=approved_target_position_qty,
            delta_qty=delta_qty,
            side=side,
            execution_style=execution_style,
            order_type=order_type,  # type: ignore[arg-type]
            limit_price=limit_price,
            time_in_force=time_in_force,
            urgency=normalized_urgency,
            max_slippage_tolerance_bps=effective_slippage_tolerance_bps,
            reference_price=reference_price,
            reduce_only=reduce_only,
            close_only=close_only,
            td_mode=resolved_td_mode,  # type: ignore[arg-type]
            position_mode=resolved_position_mode,  # type: ignore[arg-type]
            pos_side=pos_side,  # type: ignore[arg-type]
            reduce_only_reason=default_reduce_only_reason(
                position_intent=position_intent,
                reduce_only=reduce_only,
            ),
            close_only_reason=default_close_only_reason(
                position_intent=position_intent,
                close_only=close_only,
            ),
            instrument_family=instrument_family,
            settle_currency=settle_currency,
            required_initial_margin=(
                None if required_initial_margin is None else to_decimal(required_initial_margin)
            ),
            projected_margin_usage=(
                None if projected_margin_usage is None else to_decimal(projected_margin_usage)
            ),
            projected_notional=(
                None if projected_notional is None else to_decimal(projected_notional)
            ),
            risk_budget_multiplier=(
                None if risk_budget_multiplier is None else to_decimal(risk_budget_multiplier)
            ),
            risk_budget_state=dict(risk_budget_state or {}),
            execution_aggressiveness_multiplier=normalized_execution_multiplier,
            execution_aggressiveness_state=dict(execution_aggressiveness_state or {}),
            only_reduce_required=bool(only_reduce_required),
            risk_limit_breached=bool(risk_limit_breached),
            liquidation_buffer_remaining=(
                None if liquidation_buffer_remaining is None else to_decimal(liquidation_buffer_remaining)
            ),
            strategy_family=strategy_family,
            strategy_bundle_id=strategy_bundle_id,
            strategy_leg_role=strategy_leg_role,
            product_type=product_type,  # type: ignore[arg-type]
            target_leverage=target_leverage,
            margin_mode=margin_mode,  # type: ignore[arg-type]
            exposure_side=exposure_side,  # type: ignore[arg-type]
            execution_action=execution_action,
            position_intent=position_intent,  # type: ignore[arg-type]
            ai_execution_parameter_suggestion=translated_suggestion,
        )

    def build_intent(self, *, plan: ExecutionPlan) -> OrderIntent | None:
        if abs(plan.delta_qty) < EPSILON_DECIMAL_12:
            return None

        quantity = abs(plan.delta_qty)
        intent_id = new_id("intent")
        return OrderIntent(
            intent_id=intent_id,
            decision_id=plan.decision_id,
            symbol=plan.symbol,
            side=plan.side,
            quantity=quantity,
            execution_style=plan.execution_style,
            order_type=plan.order_type,
            limit_price=plan.limit_price,
            reference_price=plan.reference_price,
            urgency=plan.urgency,
            time_in_force=plan.time_in_force,
            max_slippage_tolerance_bps=plan.max_slippage_tolerance_bps,
            reduce_only=plan.reduce_only,
            close_only=plan.close_only,
            td_mode=plan.td_mode,
            position_mode=plan.position_mode,
            pos_side=plan.pos_side,
            reduce_only_reason=plan.reduce_only_reason,
            close_only_reason=plan.close_only_reason,
            instrument_family=plan.instrument_family,
            settle_currency=plan.settle_currency,
            required_initial_margin=plan.required_initial_margin,
            projected_margin_usage=plan.projected_margin_usage,
            projected_notional=plan.projected_notional,
            risk_budget_multiplier=plan.risk_budget_multiplier,
            risk_budget_state=plan.risk_budget_state,
            execution_aggressiveness_multiplier=plan.execution_aggressiveness_multiplier,
            execution_aggressiveness_state=plan.execution_aggressiveness_state,
            only_reduce_required=plan.only_reduce_required,
            risk_limit_breached=plan.risk_limit_breached,
            liquidation_buffer_remaining=plan.liquidation_buffer_remaining,
            strategy_family=plan.strategy_family,
            strategy_bundle_id=plan.strategy_bundle_id,
            strategy_leg_role=plan.strategy_leg_role,
            idempotency_key=intent_id,
            product_type=plan.product_type,
            target_leverage=plan.target_leverage,
            margin_mode=plan.margin_mode,
            exposure_side=plan.exposure_side,
            execution_action=plan.execution_action,
            position_intent=plan.position_intent,
            ai_execution_parameter_suggestion=plan.ai_execution_parameter_suggestion,
        )

    async def publish_plan(self, *, bus: EventBus, plan: ExecutionPlan) -> None:
        await publish_model(
            bus=bus,
            topic=topics.EXECUTION_PLANS,
            key=plan.symbol,
            payload_model=plan,
            source_component="execution_engine",
        )

    async def publish_intent(self, *, bus: EventBus, intent: OrderIntent) -> None:
        await publish_model(
            bus=bus,
            topic=topics.ORDER_INTENTS,
            key=intent.symbol,
            payload_model=intent,
            source_component="execution_engine",
        )

    @staticmethod
    def _execution_action(
        *,
        current_position_qty: Decimal | float,
        target_position_qty: Decimal | float,
    ) -> ExecutionAction:
        current_qty = to_decimal(current_position_qty)
        target_qty = to_decimal(target_position_qty)
        if abs(target_qty - current_qty) < EPSILON_DECIMAL_12:
            return "hold"
        current_side = "long" if current_qty > EPSILON_DECIMAL_12 else "short" if current_qty < -EPSILON_DECIMAL_12 else "flat"
        target_side = "long" if target_qty > EPSILON_DECIMAL_12 else "short" if target_qty < -EPSILON_DECIMAL_12 else "flat"
        if current_side == "flat":
            return "enter"
        if target_side == "flat":
            return "exit"
        if current_side != target_side:
            return "reverse"
        if abs(target_qty) > abs(current_qty):
            return "scale_in"
        return "reduce"

    @staticmethod
    def _position_intent(*, current_position_qty: Decimal | float, target_position_qty: Decimal | float) -> str:
        current_qty = to_decimal(current_position_qty)
        target_qty = to_decimal(target_position_qty)
        if current_qty > EPSILON_DECIMAL_12:
            if target_qty > current_qty:
                return "open_long"
            if target_qty > EPSILON_DECIMAL_12:
                return "reduce_long"
            if target_qty < -EPSILON_DECIMAL_12:
                return "reverse_to_short"
            return "close_long"
        if current_qty < -EPSILON_DECIMAL_12:
            if target_qty < current_qty:
                return "open_short"
            if target_qty < -EPSILON_DECIMAL_12:
                return "reduce_short"
            if target_qty > EPSILON_DECIMAL_12:
                return "reverse_to_long"
            return "close_short"
        return "open_long" if target_qty > 0 else "open_short"

    @staticmethod
    def _exposure_side(quantity: Decimal | float) -> str:
        decimal_quantity = to_decimal(quantity)
        if decimal_quantity > EPSILON_DECIMAL_12:
            return "long"
        if decimal_quantity < -EPSILON_DECIMAL_12:
            return "short"
        return "flat"

    def _translate_ai_execution_parameter_suggestion(
        self,
        suggestion: AIExecutionParameterSuggestionEnvelope | None,
        *,
        side: str,
        reference_price: Decimal | float | None,
        max_slippage_tolerance_bps: int,
        execution_aggressiveness_multiplier: Decimal,
    ) -> AIExecutionParameterSuggestionEnvelope | None:
        if suggestion is None:
            return None
        requested_mode = self.settings.ai_execution_suggestion_mode
        clipped_fields: list[str] = list(suggestion.clipped_fields)
        notes: list[str] = list(suggestion.notes)
        sanitized = ExecutionParameterSuggestion.model_validate(suggestion.suggestion.model_dump(mode="python"))
        scaled_aggressiveness = self._normalized_multiplier(execution_aggressiveness_multiplier)

        def clip_decimal(value: Decimal | None, lower: Decimal, upper: Decimal, field_name: str) -> Decimal | None:
            if value is None:
                return None
            clipped = min(max(to_decimal(value), lower), upper)
            if clipped != value:
                clipped_fields.append(field_name)
                notes.append(f"{field_name}_planner_clamped")
            return clipped

        def clip_int(value: int | None, lower: int, upper: int, field_name: str) -> int | None:
            if value is None:
                return None
            clipped = min(max(int(value), lower), upper)
            if clipped != value:
                clipped_fields.append(field_name)
                notes.append(f"{field_name}_planner_clamped")
            return clipped

        sanitized.passive_bias = clip_decimal(
            sanitized.passive_bias,
            Decimal("0"),
            to_decimal(self.settings.ai_execution_max_passive_bias),
            "passive_bias",
        )
        sanitized.maker_taker_bias = clip_decimal(
            sanitized.maker_taker_bias,
            -(to_decimal(self.settings.ai_execution_max_maker_taker_bias) * scaled_aggressiveness),
            to_decimal(self.settings.ai_execution_max_maker_taker_bias) * scaled_aggressiveness,
            "maker_taker_bias",
        )
        sanitized.max_cross_spread_bps = clip_decimal(
            sanitized.max_cross_spread_bps,
            Decimal("0"),
            to_decimal(self.settings.ai_execution_max_cross_spread_bps) * scaled_aggressiveness,
            "max_cross_spread_bps",
        )
        sanitized.slice_count = clip_int(
            sanitized.slice_count,
            1,
            max(int(round(self.settings.ai_execution_max_slice_count * float(scaled_aggressiveness))), 1),
            "slice_count",
        )
        sanitized.max_participation_rate = clip_decimal(
            sanitized.max_participation_rate,
            Decimal("0"),
            to_decimal(self.settings.ai_execution_max_participation_rate) * scaled_aggressiveness,
            "max_participation_rate",
        )
        sanitized.cancel_replace_patience_ms = clip_int(
            sanitized.cancel_replace_patience_ms,
            0,
            max(int(round(self.settings.ai_execution_max_cancel_replace_patience_ms * float(scaled_aggressiveness))), 0),
            "cancel_replace_patience_ms",
        )
        if scaled_aggressiveness <= Decimal("0.60") and sanitized.passive_bias is not None:
            preferred_passive_floor = Decimal("0.60")
            if sanitized.passive_bias < preferred_passive_floor:
                sanitized.passive_bias = preferred_passive_floor
                clipped_fields.append("passive_bias")
                notes.append("passive_bias_planner_raised_for_safe_execution")

        if requested_mode == "disabled":
            return AIExecutionParameterSuggestionEnvelope(
                status="reserved_not_enabled",
                diagnostic_only=True,
                requested_mode=requested_mode,
                suggestion=sanitized,
                accepted_by_execution_planner=False,
                applied_to_live_execution=False,
                clipped_fields=list(dict.fromkeys(clipped_fields)),
                rejection_reasons=["execution_parameter_suggestions_disabled"],
                notes=list(dict.fromkeys(notes + ["planner_boundary_disabled"])),
            )

        if requested_mode == "diagnostic_only":
            return AIExecutionParameterSuggestionEnvelope(
                status="diagnostic_only",
                diagnostic_only=True,
                requested_mode=requested_mode,
                suggestion=sanitized,
                accepted_by_execution_planner=False,
                applied_to_live_execution=False,
                clipped_fields=list(dict.fromkeys(clipped_fields)),
                rejection_reasons=["diagnostic_only_no_live_execution"],
                notes=list(dict.fromkeys(notes + ["planner_recorded_suggestion_only"])),
            )

        translation_preview = self._build_translation_preview(sanitized)
        if requested_mode == "enabled_live":
            live_translation_reason = self._bounded_live_translation_reason(
                side=side,
                reference_price=reference_price,
                preview=translation_preview,
                max_slippage_tolerance_bps=max_slippage_tolerance_bps,
            )
            if live_translation_reason is None:
                return AIExecutionParameterSuggestionEnvelope(
                    status="enabled",
                    diagnostic_only=False,
                    requested_mode=requested_mode,
                    suggestion=sanitized,
                    translation_preview=translation_preview,
                    accepted_by_execution_planner=True,
                    applied_to_live_execution=True,
                    applied_live_fields=["execution_style", "order_type", "limit_price", "time_in_force"],
                    clipped_fields=list(dict.fromkeys(clipped_fields)),
                    rejection_reasons=[],
                    notes=list(dict.fromkeys(notes + ["planner_translated_execution_preview", "bounded_live_translation_applied"])),
                    live_translation_reason="bounded_limit_ioc_cap",
                )
            return AIExecutionParameterSuggestionEnvelope(
                status="shadow_translation",
                diagnostic_only=True,
                requested_mode=requested_mode,
                suggestion=sanitized,
                translation_preview=translation_preview,
                accepted_by_execution_planner=True,
                applied_to_live_execution=False,
                applied_live_fields=[],
                clipped_fields=list(dict.fromkeys(clipped_fields)),
                rejection_reasons=["shadow_translation_preview_only"],
                notes=list(dict.fromkeys(notes + ["planner_translated_execution_preview", "live_translation_not_enabled"])),
                live_translation_fallback_reason=live_translation_reason,
            )
        return AIExecutionParameterSuggestionEnvelope(
            status="shadow_translation",
            diagnostic_only=True,
            requested_mode=requested_mode,
            suggestion=sanitized,
            translation_preview=translation_preview,
            accepted_by_execution_planner=True,
            applied_to_live_execution=False,
            applied_live_fields=[],
            clipped_fields=list(dict.fromkeys(clipped_fields)),
            rejection_reasons=["shadow_translation_preview_only"],
            notes=list(dict.fromkeys(notes + ["planner_translated_execution_preview", "live_translation_not_enabled"])),
        )

    @staticmethod
    def _normalized_multiplier(value: Decimal | float | None) -> Decimal:
        if value is None:
            return Decimal("1")
        return max(min(to_decimal(value), Decimal("1")), Decimal("0.1"))

    @staticmethod
    def _effective_slippage_tolerance_bps(
        *,
        max_slippage_tolerance_bps: int,
        execution_aggressiveness_multiplier: Decimal,
    ) -> int:
        if max_slippage_tolerance_bps <= 0:
            return max_slippage_tolerance_bps
        scaled = int(
            max(
                Decimal("1"),
                (Decimal(str(max_slippage_tolerance_bps)) * execution_aggressiveness_multiplier).quantize(Decimal("1")),
            )
        )
        return min(max_slippage_tolerance_bps, scaled)

    @staticmethod
    def _build_translation_preview(
        suggestion: ExecutionParameterSuggestion,
    ) -> ExecutionParameterTranslationPreview:
        passive_bias = to_decimal(suggestion.passive_bias or Decimal("0"))
        maker_taker_bias = to_decimal(suggestion.maker_taker_bias or Decimal("0"))
        prefer_passive = passive_bias >= Decimal("0.6") or maker_taker_bias <= Decimal("-0.2")
        use_limit = prefer_passive or suggestion.slice_count not in {None, 1}
        return ExecutionParameterTranslationPreview(
            execution_style="bounded_limit_ioc" if use_limit else "bounded_taker_cap",
            order_type="limit" if use_limit else "market",
            time_in_force="IOC",
            limit_offset_bps=suggestion.max_cross_spread_bps if use_limit else None,
            slice_count=suggestion.slice_count,
            max_participation_rate=suggestion.max_participation_rate,
            cancel_replace_patience_ms=suggestion.cancel_replace_patience_ms,
            passive_bias=suggestion.passive_bias,
            maker_taker_bias=suggestion.maker_taker_bias,
        )

    def _bounded_live_translation_reason(
        self,
        *,
        side: str,
        reference_price: Decimal | float | None,
        preview: ExecutionParameterTranslationPreview,
        max_slippage_tolerance_bps: int,
    ) -> str | None:
        if preview.order_type != "limit":
            return "live_translation_requires_limit_cap"
        if reference_price is None or to_decimal(reference_price) <= Decimal("0"):
            return "live_translation_requires_reference_price"
        if preview.limit_offset_bps is None or preview.limit_offset_bps <= Decimal("0"):
            return "live_translation_requires_limit_offset"
        if max_slippage_tolerance_bps <= 0:
            return "live_translation_requires_slippage_guard"
        _ = side
        return None

    def _bounded_live_limit_price(
        self,
        *,
        side: str,
        reference_price: Decimal | float | None,
        preview: ExecutionParameterTranslationPreview,
        max_slippage_tolerance_bps: int,
    ) -> Decimal | None:
        if reference_price is None or preview.limit_offset_bps is None:
            return None
        reference = to_decimal(reference_price)
        slippage_guard = (
            to_decimal(max(max_slippage_tolerance_bps, 0))
            * to_decimal(self.settings.ai_execution_live_limit_offset_fraction_of_slippage)
        )
        effective_offset = min(
            max(preview.limit_offset_bps, Decimal("0")),
            max(slippage_guard, Decimal("0")),
        )
        if effective_offset <= Decimal("0"):
            return None
        offset_fraction = effective_offset / Decimal("10000")
        if side == "buy":
            return reference * (Decimal("1") + offset_fraction)
        return reference * (Decimal("1") - offset_fraction)
