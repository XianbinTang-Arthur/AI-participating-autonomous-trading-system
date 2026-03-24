from __future__ import annotations

from decimal import Decimal

from aats.schemas.common import new_id
from aats.schemas.decision import PositionTarget
from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    StrategyCandidate,
    StrategyFamily,
    StrategyLegIntent,
    StrategyRouteAction,
    StrategySleeveIntent,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, quantize_decimal, to_decimal


class PortfolioAllocatorV1:
    _FAMILY_PRIORITY: tuple[StrategyFamily, ...] = ("smart_arbitrage", "spot_grid", "dca", "directional")

    def allocate(
        self,
        *,
        base_target: PositionTarget,
        selected_family: StrategyFamily,
        selection_reason_codes: list[str],
        sleeve_intents: list[StrategySleeveIntent],
    ) -> PortfolioAllocationDecision:
        intents_by_family = {intent.family: intent for intent in sleeve_intents}
        active_families = [intent.family for intent in sleeve_intents if intent.state not in {"disabled", "incompatible"}]
        approved: list[StrategySleeveIntent] = []
        blocked_reason_codes: list[str] = []

        if base_target.product_type == "derivatives":
            smart_arbitrage_intent = intents_by_family.get("smart_arbitrage")
            directional_intent = intents_by_family.get("directional")
            smart_arbitrage_active = smart_arbitrage_intent is not None and self._intent_is_actionable(
                smart_arbitrage_intent,
                include_active_inventory=True,
            )
            if smart_arbitrage_active:
                approved.append(smart_arbitrage_intent)
                if directional_intent is not None and self._intent_has_delta(directional_intent):
                    blocked_reason_codes.append("allocator_directional_blocked_by_active_smart_arbitrage")
            elif directional_intent is not None:
                approved.append(directional_intent)
        else:
            spot_inventory_intents = [
                intent
                for family in ("spot_grid", "dca")
                for intent in [intents_by_family.get(family)]
                if intent is not None and self._intent_is_actionable(intent, include_active_inventory=True)
            ]
            if spot_inventory_intents:
                approved.extend(spot_inventory_intents)
                directional_intent = intents_by_family.get("directional")
                if directional_intent is not None and self._intent_has_delta(directional_intent):
                    blocked_reason_codes.append("allocator_directional_blocked_by_active_inventory_sleeves")
            elif intents_by_family.get("directional") is not None:
                approved.append(intents_by_family["directional"])

        approved_families = [intent.family for intent in approved]
        approved_weights = self._normalized_weights(approved)
        approved_budget_multipliers = {
            intent.strategy_sleeve_id: to_decimal(intent.budget_multiplier)
            for intent in approved
        }
        primary_intent = self._primary_intent(
            approved=approved,
            selected_family=selected_family,
        )
        allocation_id = None
        for intent in sleeve_intents:
            if intent.allocation_id:
                allocation_id = intent.allocation_id
                break
        execution_legs = self._execution_legs(
            approved=approved,
            base_target=base_target,
        )
        aggregate_delta = sum(
            (
                to_decimal(leg.delta_position_qty or Decimal("0"))
                for leg in execution_legs
                if leg.symbol == base_target.symbol
                and leg.product_type == base_target.product_type
                and leg.margin_mode == base_target.margin_mode
            ),
            start=Decimal("0"),
        )
        current_position_qty = to_decimal(base_target.current_position_qty)
        target_position_qty = current_position_qty + aggregate_delta
        route_action: StrategyRouteAction = (
            "override_target"
            if execution_legs
            else (
                "hold_current"
                if approved_families
                else "advisory_only"
            )
        )
        reason_codes = list(
            dict.fromkeys(
                [
                    "allocator_v1_applied",
                    f"allocator_primary_family_{primary_intent.family if primary_intent is not None else 'directional'}",
                    *selection_reason_codes,
                    *blocked_reason_codes,
                ]
            )
        )
        if execution_legs:
            operator_summary = "当前 allocator 已按 sleeve 库存真相生成账户级净执行目标。"
        elif approved_families:
            operator_summary = "当前 allocator 识别到活跃 sleeve，但本轮没有新增可执行 delta。"
        else:
            operator_summary = "当前 allocator 没有批准新的 sleeve 执行动作，系统保持当前仓位。"
        return PortfolioAllocationDecision(
            allocation_id=allocation_id or new_id("alloc"),
            decision_id=base_target.decision_id,
            symbol=base_target.symbol,
            product_type=base_target.product_type,
            margin_mode=base_target.margin_mode,
            route_action=route_action,
            primary_family="directional" if primary_intent is None else primary_intent.family,
            primary_strategy_sleeve_id=None if primary_intent is None else primary_intent.strategy_sleeve_id,
            active_families=active_families,
            approved_families=approved_families,
            blocked_reason_codes=blocked_reason_codes,
            reason_codes=reason_codes,
            operator_summary=operator_summary,
            current_position_qty=current_position_qty,
            target_position_qty=target_position_qty,
            delta_position_qty=aggregate_delta,
            target_notional=self._target_notional(base_target=base_target, target_qty=target_position_qty),
            approved_sleeve_weights=approved_weights,
            approved_sleeve_budget_multipliers=approved_budget_multipliers,
            sleeve_intents=[intent.model_copy(deep=True) for intent in sleeve_intents],
            execution_legs=execution_legs,
        )

    def _primary_intent(
        self,
        *,
        approved: list[StrategySleeveIntent],
        selected_family: StrategyFamily,
    ) -> StrategySleeveIntent | None:
        if not approved:
            return None
        for intent in approved:
            if intent.family == selected_family:
                return intent
        weighted = sorted(
            approved,
            key=lambda item: (
                to_decimal(item.allocator_weight),
                to_decimal(item.budget_multiplier),
                item.priority_score,
            ),
            reverse=True,
        )
        if weighted:
            return weighted[0]
        approved_by_family = {intent.family: intent for intent in approved}
        for family in self._FAMILY_PRIORITY:
            if family in approved_by_family:
                return approved_by_family[family]
        return approved[0]

    @staticmethod
    def _intent_has_delta(intent: StrategySleeveIntent) -> bool:
        return abs(to_decimal(intent.delta_position_qty)) > EPSILON_DECIMAL_12

    def _intent_is_actionable(
        self,
        intent: StrategySleeveIntent,
        *,
        include_active_inventory: bool,
    ) -> bool:
        if not intent.execution_compatible:
            return False
        if intent.route_action == "override_target" and intent.selectable:
            return True
        if include_active_inventory and abs(to_decimal(intent.current_position_qty)) > EPSILON_DECIMAL_12:
            return True
        return intent.route_action == "hold_current" and abs(to_decimal(intent.target_position_qty)) > EPSILON_DECIMAL_12

    def _execution_legs(
        self,
        *,
        approved: list[StrategySleeveIntent],
        base_target: PositionTarget,
    ) -> list[StrategyLegIntent]:
        legs: list[StrategyLegIntent] = []
        for intent in approved:
            if intent.family == "smart_arbitrage":
                for leg in intent.legs:
                    delta_qty = to_decimal(leg.delta_position_qty or Decimal("0"))
                    if abs(delta_qty) <= EPSILON_DECIMAL_12:
                        continue
                    legs.append(
                        leg.model_copy(
                            deep=True,
                            update={
                                "family": intent.family,
                                "strategy_sleeve_id": leg.strategy_sleeve_id or intent.strategy_sleeve_id,
                                "allocation_id": intent.allocation_id,
                            },
                        )
                    )
                continue

            delta_qty = to_decimal(intent.delta_position_qty)
            if abs(delta_qty) <= EPSILON_DECIMAL_12:
                continue
            account_current_qty = to_decimal(intent.account_current_position_qty or base_target.current_position_qty)
            account_target_qty = account_current_qty + delta_qty
            legs.append(
                StrategyLegIntent(
                    symbol=intent.symbol,
                    product_type=intent.product_type,
                    side="buy" if delta_qty >= 0 else "sell",
                    family=intent.family,
                    role=self._role_for_family(intent.family),
                    strategy_sleeve_id=intent.strategy_sleeve_id,
                    allocation_id=intent.allocation_id,
                    margin_mode=intent.margin_mode,
                    target_leverage=base_target.target_leverage,
                    current_position_qty=account_current_qty,
                    target_position_qty=account_target_qty,
                    delta_position_qty=delta_qty,
                    reference_price=self._reference_price(intent),
                    execution_compatible=intent.execution_compatible,
                    note=f"{intent.family} sleeve delta converted by allocator v1.",
                )
            )
        return legs

    @staticmethod
    def _normalized_weights(approved: list[StrategySleeveIntent]) -> dict[str, Decimal]:
        if not approved:
            return {}
        raw_weights = {
            intent.strategy_sleeve_id: max(to_decimal(intent.allocator_weight), Decimal("0"))
            for intent in approved
        }
        total = sum(raw_weights.values(), start=Decimal("0"))
        if total <= EPSILON_DECIMAL_12:
            equal_weight = quantize_decimal(Decimal("1") / Decimal(len(approved)))
            return {intent.strategy_sleeve_id: equal_weight for intent in approved}
        normalized: dict[str, Decimal] = {}
        remainder = Decimal("1")
        items = list(raw_weights.items())
        for index, (sleeve_id, weight) in enumerate(items):
            value = remainder if index == len(items) - 1 else quantize_decimal(weight / total)
            remainder -= value
            normalized[sleeve_id] = value
        return normalized

    @staticmethod
    def _role_for_family(family: StrategyFamily) -> str:
        if family == "spot_grid":
            return "inventory"
        if family == "dca":
            return "accumulation"
        if family == "smart_arbitrage":
            return "hedge"
        return "primary"

    @staticmethod
    def _reference_price(intent: StrategySleeveIntent) -> Decimal | None:
        for key in ("current_price", "spot_price", "derivatives_price", "anchor_price"):
            value = intent.metrics.get(key)
            if value is None:
                continue
            price = to_decimal(value)
            if price > EPSILON_DECIMAL_12:
                return price
        return None

    @staticmethod
    def _target_notional(*, base_target: PositionTarget, target_qty: Decimal) -> Decimal:
        current_qty = to_decimal(base_target.current_position_qty)
        current_notional = to_decimal(base_target.current_notional)
        target_notional = to_decimal(base_target.target_notional)
        if abs(to_decimal(base_target.target_position_qty)) > EPSILON_DECIMAL_12 and abs(target_notional) > EPSILON_DECIMAL_12:
            reference_price = abs(target_notional / to_decimal(base_target.target_position_qty))
        elif abs(current_qty) > EPSILON_DECIMAL_12 and abs(current_notional) > EPSILON_DECIMAL_12:
            reference_price = abs(current_notional / current_qty)
        else:
            reference_price = Decimal("0")
        if reference_price <= EPSILON_DECIMAL_12:
            return Decimal("0")
        return abs(target_qty) * reference_price
