from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.common import new_id
from aats.schemas.decision import PositionTarget
from aats.schemas.strategy_runtime import (
    AllocatorBudgetSnapshot,
    AllocatorConflictResolution,
    AllocatorNettingDecision,
    PortfolioAllocationDecision,
    SleeveBudgetAssignment,
    StrategyFamily,
    StrategyLegIntent,
    StrategyRouteAction,
    StrategySleeveIntent,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, quantize_decimal, to_decimal


class PortfolioAllocatorV2Phase1:
    _FAMILY_PRIORITY: tuple[StrategyFamily, ...] = ("smart_arbitrage", "spot_grid", "dca", "directional")

    def __init__(self, *, settings: AATSSettings | None = None) -> None:
        self.settings = settings

    def allocate(
        self,
        *,
        base_target: PositionTarget,
        selected_family: StrategyFamily,
        selection_reason_codes: list[str],
        sleeve_intents: list[StrategySleeveIntent],
        budget_assignments: list[SleeveBudgetAssignment] | None = None,
    ) -> PortfolioAllocationDecision:
        assignments_by_sleeve = {
            item.strategy_sleeve_id: item
            for item in (budget_assignments or [])
        }
        intents_by_family = {intent.family: intent for intent in sleeve_intents}
        active_families = [intent.family for intent in sleeve_intents if intent.state not in {"disabled", "incompatible"}]
        approved: list[StrategySleeveIntent] = []
        blocked_reason_codes: list[str] = []
        conflict_resolutions: list[AllocatorConflictResolution] = []

        if base_target.product_type == "derivatives":
            smart_arbitrage_intent = intents_by_family.get("smart_arbitrage")
            directional_intent = intents_by_family.get("directional")
            smart_arbitrage_active = smart_arbitrage_intent is not None and self._intent_is_actionable(
                smart_arbitrage_intent,
                include_active_inventory=True,
            )
            directional_active = directional_intent is not None and self._intent_has_delta(directional_intent)
            if smart_arbitrage_active and smart_arbitrage_intent is not None:
                approved.append(smart_arbitrage_intent)
                if directional_active and directional_intent is not None:
                    blocked_reason_codes.append("allocator_directional_blocked_by_active_smart_arbitrage")
                    conflict_resolutions.append(
                        self._conflict_resolution(
                            allocation_id=self._allocation_id(sleeve_intents),
                            base_target=base_target,
                            conflict_type="hedge_priority",
                            resolution_action="directional_reduced_to_protect_hedge",
                            input_intents=[smart_arbitrage_intent, directional_intent],
                            approved_intents=[smart_arbitrage_intent],
                            protected_notional=self._requested_notional(
                                intent=smart_arbitrage_intent,
                                base_target=base_target,
                                assignment=assignments_by_sleeve.get(smart_arbitrage_intent.strategy_sleeve_id),
                            ),
                            reduced_notional=self._requested_notional(
                                intent=directional_intent,
                                base_target=base_target,
                                assignment=assignments_by_sleeve.get(directional_intent.strategy_sleeve_id),
                            ),
                            reason_codes=[
                                "allocator_v2_hedge_priority",
                                "allocator_directional_blocked_by_active_smart_arbitrage",
                            ],
                        )
                    )
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
                    conflict_resolutions.append(
                        self._conflict_resolution(
                            allocation_id=self._allocation_id(sleeve_intents),
                            base_target=base_target,
                            conflict_type="inventory_priority",
                            resolution_action="directional_reduced_to_preserve_inventory_sleeves",
                            input_intents=[*spot_inventory_intents, directional_intent],
                            approved_intents=spot_inventory_intents,
                            protected_notional=sum(
                                (
                                    self._requested_notional(
                                        intent=item,
                                        base_target=base_target,
                                        assignment=assignments_by_sleeve.get(item.strategy_sleeve_id),
                                    )
                                    for item in spot_inventory_intents
                                ),
                                start=Decimal("0"),
                            ),
                            reduced_notional=self._requested_notional(
                                intent=directional_intent,
                                base_target=base_target,
                                assignment=assignments_by_sleeve.get(directional_intent.strategy_sleeve_id),
                            ),
                            reason_codes=[
                                "allocator_v2_inventory_priority",
                                "allocator_directional_blocked_by_active_inventory_sleeves",
                            ],
                        )
                    )
            elif intents_by_family.get("directional") is not None:
                approved.append(intents_by_family["directional"])

        scaled_approved: list[StrategySleeveIntent] = []
        budget_snapshots: list[AllocatorBudgetSnapshot] = []
        approved_notional_by_sleeve: dict[str, Decimal] = {}
        for intent in approved:
            scaled_intent, snapshot = self._apply_budget_assignment(
                intent=intent,
                base_target=base_target,
                assignment=assignments_by_sleeve.get(intent.strategy_sleeve_id),
                allocation_id=self._allocation_id(sleeve_intents),
            )
            if self._intent_is_actionable(scaled_intent, include_active_inventory=True):
                scaled_approved.append(scaled_intent)
            budget_snapshots.append(snapshot)
            approved_notional_by_sleeve[snapshot.strategy_sleeve_id] = snapshot.approved_notional

        approved_families = [intent.family for intent in scaled_approved]
        approved_weights = self._normalized_weights(
            approved=scaled_approved,
            assignments_by_sleeve=assignments_by_sleeve,
        )
        approved_budget_multipliers = {
            intent.strategy_sleeve_id: self._budget_multiplier_for(
                intent=intent,
                assignment=assignments_by_sleeve.get(intent.strategy_sleeve_id),
            )
            for intent in scaled_approved
        }
        primary_intent = self._primary_intent(
            approved=scaled_approved,
            selected_family=selected_family,
            assignments_by_sleeve=assignments_by_sleeve,
        )
        allocation_id = self._allocation_id(sleeve_intents)
        execution_legs = self._execution_legs(
            approved=scaled_approved,
            base_target=base_target,
        )
        netting_decisions = self._netting_decisions(
            allocation_id=allocation_id,
            execution_legs=execution_legs,
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
            else ("hold_current" if approved_families else "advisory_only")
        )
        reason_codes = list(
            dict.fromkeys(
                [
                    "allocator_v2_phase1_applied",
                    f"allocator_primary_family_{primary_intent.family if primary_intent is not None else 'directional'}",
                    *selection_reason_codes,
                    *blocked_reason_codes,
                ]
            )
        )
        budget_state = "contracted" if any(item.clamped for item in budget_snapshots) else "normal"
        if conflict_resolutions and budget_state == "normal":
            budget_state = "hedge_protected"
        if execution_legs:
            operator_summary = "当前 allocator v2 已按 sleeve 预算、冲突净额与 hedge 优先级生成账户级执行目标。"
        elif approved_families:
            operator_summary = "当前 allocator v2 识别到活跃 sleeve，但本轮没有新增可执行 delta。"
        else:
            operator_summary = "当前 allocator v2 没有批准新的 sleeve 执行动作，系统保持当前仓位。"
        return PortfolioAllocationDecision(
            allocation_id=allocation_id,
            decision_id=base_target.decision_id,
            symbol=base_target.symbol,
            product_type=base_target.product_type,
            margin_mode=base_target.margin_mode,
            allocator_version="task76_allocator_v2_phase1",
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
            approved_notional_by_sleeve=approved_notional_by_sleeve,
            budget_assignments=[item.model_copy(deep=True) for item in assignments_by_sleeve.values()],
            budget_snapshots=budget_snapshots,
            conflict_resolutions=conflict_resolutions,
            netting_decisions=netting_decisions,
            hedge_protected_notional=sum((item.protected_notional for item in conflict_resolutions), start=Decimal("0")),
            directional_reduced_notional=sum((item.reduced_notional for item in conflict_resolutions), start=Decimal("0")),
            portfolio_risk_budget_state=budget_state,
            sleeve_intents=[intent.model_copy(deep=True) for intent in sleeve_intents],
            execution_legs=execution_legs,
        )

    def _apply_budget_assignment(
        self,
        *,
        intent: StrategySleeveIntent,
        base_target: PositionTarget,
        assignment: SleeveBudgetAssignment | None,
        allocation_id: str,
    ) -> tuple[StrategySleeveIntent, AllocatorBudgetSnapshot]:
        requested_notional = self._requested_notional(intent=intent, base_target=base_target, assignment=assignment)
        requested_delta_qty = to_decimal(intent.delta_position_qty)
        budget_multiplier = self._budget_multiplier_for(intent=intent, assignment=assignment)
        allocator_weight = self._allocator_weight_for(intent=intent, assignment=assignment)
        quote_limit = None if assignment is None else assignment.effective_quote_budget_limit
        margin_limit = None if assignment is None else assignment.effective_margin_budget_limit
        notional_cap = None if assignment is None else assignment.effective_notional_cap
        max_symbol_notional = None if assignment is None else assignment.effective_max_symbol_notional
        cap_candidates = [value for value in (notional_cap, max_symbol_notional, quote_limit, margin_limit) if value is not None]
        effective_cap = min(cap_candidates) if cap_candidates else None
        approved_notional = requested_notional
        approved_delta_qty = requested_delta_qty
        clamped = False
        reason_codes: list[str] = ["allocator_v2_budget_profile_applied"] if assignment is not None else ["allocator_v2_budget_profile_missing"]
        scaled_intent = intent.model_copy(deep=True)
        if effective_cap is not None and requested_notional > effective_cap + EPSILON_DECIMAL_12 and requested_notional > EPSILON_DECIMAL_12:
            ratio = max(Decimal("0"), min(Decimal("1"), effective_cap / requested_notional))
            approved_notional = quantize_decimal(requested_notional * ratio)
            approved_delta_qty = quantize_decimal(requested_delta_qty * ratio)
            clamped = True
            reason_codes.append("allocator_budget_notional_capped")
            scaled_intent = self._scale_intent(intent=intent, scaled_delta_qty=approved_delta_qty)
        snapshot = AllocatorBudgetSnapshot(
            allocation_id=allocation_id,
            strategy_sleeve_id=intent.strategy_sleeve_id,
            family=intent.family,
            symbol=intent.symbol,
            product_type=intent.product_type,
            margin_mode=intent.margin_mode,
            requested_notional=requested_notional,
            approved_notional=approved_notional,
            requested_delta_qty=requested_delta_qty,
            approved_delta_qty=approved_delta_qty,
            budget_multiplier=budget_multiplier,
            allocator_weight=allocator_weight,
            quote_budget_limit=quote_limit,
            margin_budget_limit=margin_limit,
            notional_cap=notional_cap,
            max_symbol_notional=max_symbol_notional,
            hedge_priority_class="standard" if assignment is None else assignment.hedge_priority_class,
            clamped=clamped,
            reason_codes=reason_codes,
        )
        return scaled_intent, snapshot

    @staticmethod
    def _scale_intent(*, intent: StrategySleeveIntent, scaled_delta_qty: Decimal) -> StrategySleeveIntent:
        current_qty = to_decimal(intent.current_position_qty)
        account_current_qty = (
            None if intent.account_current_position_qty is None else to_decimal(intent.account_current_position_qty)
        )
        scaled_legs: list[StrategyLegIntent] = []
        if intent.legs:
            original_delta = to_decimal(intent.delta_position_qty)
            multiplier = Decimal("0") if abs(original_delta) <= EPSILON_DECIMAL_12 else (scaled_delta_qty / original_delta)
            for leg in intent.legs:
                leg_current_qty = to_decimal(leg.current_position_qty or Decimal("0"))
                leg_delta = quantize_decimal(to_decimal(leg.delta_position_qty or Decimal("0")) * multiplier)
                scaled_legs.append(
                    leg.model_copy(
                        deep=True,
                        update={
                            "delta_position_qty": leg_delta,
                            "target_position_qty": leg_current_qty + leg_delta,
                            "note": (
                                f"{leg.note} | allocator_v2_budget_scaled"
                                if leg.note
                                else "allocator_v2_budget_scaled"
                            ),
                        },
                    )
                )
        return intent.model_copy(
            update={
                "delta_position_qty": scaled_delta_qty,
                "target_position_qty": current_qty + scaled_delta_qty,
                "account_target_position_qty": (
                    None if account_current_qty is None else account_current_qty + scaled_delta_qty
                ),
                "legs": scaled_legs if scaled_legs else intent.legs,
                "reason_codes": [*intent.reason_codes, "allocator_v2_budget_scaled"],
            }
        )

    def _primary_intent(
        self,
        *,
        approved: list[StrategySleeveIntent],
        selected_family: StrategyFamily,
        assignments_by_sleeve: dict[str, SleeveBudgetAssignment],
    ) -> StrategySleeveIntent | None:
        if not approved:
            return None
        for intent in approved:
            if intent.family == selected_family:
                return intent
        weighted = sorted(
            approved,
            key=lambda item: (
                self._allocator_weight_for(intent=item, assignment=assignments_by_sleeve.get(item.strategy_sleeve_id)),
                self._budget_multiplier_for(intent=item, assignment=assignments_by_sleeve.get(item.strategy_sleeve_id)),
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
                    note=f"{intent.family} sleeve delta converted by allocator v2 phase1.",
                )
            )
        return legs

    def _netting_decisions(
        self,
        *,
        allocation_id: str,
        execution_legs: list[StrategyLegIntent],
    ) -> list[AllocatorNettingDecision]:
        by_symbol: dict[tuple[str, str, str], list[StrategyLegIntent]] = {}
        for leg in execution_legs:
            key = (leg.symbol, leg.product_type, leg.margin_mode)
            by_symbol.setdefault(key, []).append(leg)
        decisions: list[AllocatorNettingDecision] = []
        for (symbol, product_type, margin_mode), legs in by_symbol.items():
            gross_buy_qty = sum(
                (
                    abs(to_decimal(leg.delta_position_qty or Decimal("0")))
                    for leg in legs
                    if str(leg.side).lower() == "buy"
                ),
                start=Decimal("0"),
            )
            gross_sell_qty = sum(
                (
                    abs(to_decimal(leg.delta_position_qty or Decimal("0")))
                    for leg in legs
                    if str(leg.side).lower() == "sell"
                ),
                start=Decimal("0"),
            )
            net_qty = quantize_decimal(gross_buy_qty - gross_sell_qty)
            decisions.append(
                AllocatorNettingDecision(
                    allocation_id=allocation_id,
                    symbol=symbol,
                    product_type=product_type,
                    margin_mode=margin_mode,
                    gross_buy_qty=gross_buy_qty,
                    gross_sell_qty=gross_sell_qty,
                    net_approved_qty=net_qty,
                    participating_sleeve_ids=list(
                        dict.fromkeys(
                            str(leg.strategy_sleeve_id or "")
                            for leg in legs
                            if str(leg.strategy_sleeve_id or "").strip()
                        )
                    ),
                    reason_codes=["allocator_v2_symbol_netting"],
                )
            )
        return decisions

    def _conflict_resolution(
        self,
        *,
        allocation_id: str,
        base_target: PositionTarget,
        conflict_type: str,
        resolution_action: str,
        input_intents: list[StrategySleeveIntent],
        approved_intents: list[StrategySleeveIntent],
        protected_notional: Decimal,
        reduced_notional: Decimal,
        reason_codes: list[str],
    ) -> AllocatorConflictResolution:
        gross_requested_qty = sum((abs(to_decimal(item.delta_position_qty)) for item in input_intents), start=Decimal("0"))
        net_approved_qty = sum((to_decimal(item.delta_position_qty) for item in approved_intents), start=Decimal("0"))
        blocked_qty = gross_requested_qty - abs(net_approved_qty)
        return AllocatorConflictResolution(
            allocation_id=allocation_id,
            symbol=base_target.symbol,
            product_type=base_target.product_type,
            margin_mode=base_target.margin_mode,
            conflict_type=conflict_type,
            resolution_action=resolution_action,
            input_sleeve_ids=[item.strategy_sleeve_id for item in input_intents],
            approved_sleeve_ids=[item.strategy_sleeve_id for item in approved_intents],
            gross_requested_qty=gross_requested_qty,
            net_approved_qty=quantize_decimal(net_approved_qty),
            blocked_qty=quantize_decimal(blocked_qty),
            protected_notional=quantize_decimal(protected_notional),
            reduced_notional=quantize_decimal(reduced_notional),
            reason_codes=reason_codes,
        )

    def _normalized_weights(
        self,
        *,
        approved: list[StrategySleeveIntent],
        assignments_by_sleeve: dict[str, SleeveBudgetAssignment],
    ) -> dict[str, Decimal]:
        if not approved:
            return {}
        raw_weights = {
            intent.strategy_sleeve_id: max(
                self._allocator_weight_for(
                    intent=intent,
                    assignment=assignments_by_sleeve.get(intent.strategy_sleeve_id),
                ),
                Decimal("0"),
            )
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

    def _requested_notional(
        self,
        *,
        intent: StrategySleeveIntent,
        base_target: PositionTarget,
        assignment: SleeveBudgetAssignment | None,
    ) -> Decimal:
        if intent.target_notional is not None and abs(to_decimal(intent.target_notional)) > EPSILON_DECIMAL_12:
            return abs(to_decimal(intent.target_notional))
        if intent.legs:
            total = Decimal("0")
            for leg in intent.legs:
                delta_qty = abs(to_decimal(leg.delta_position_qty or Decimal("0")))
                reference_price = abs(to_decimal(leg.reference_price or Decimal("0")))
                if reference_price <= EPSILON_DECIMAL_12:
                    reference_price = self._reference_price(intent) or self._base_target_reference_price(base_target)
                total += delta_qty * reference_price
            return total
        reference_price = self._reference_price(intent) or self._base_target_reference_price(base_target)
        if reference_price <= EPSILON_DECIMAL_12 and assignment is not None and assignment.effective_max_symbol_notional is not None:
            return abs(to_decimal(assignment.effective_max_symbol_notional))
        return abs(to_decimal(intent.delta_position_qty)) * reference_price

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
        reference_price = PortfolioAllocatorV2Phase1._base_target_reference_price(base_target)
        if reference_price <= EPSILON_DECIMAL_12:
            return Decimal("0")
        return abs(target_qty) * reference_price

    @staticmethod
    def _base_target_reference_price(base_target: PositionTarget) -> Decimal:
        current_qty = to_decimal(base_target.current_position_qty)
        current_notional = to_decimal(base_target.current_notional)
        target_qty = to_decimal(base_target.target_position_qty)
        target_notional = to_decimal(base_target.target_notional)
        if abs(target_qty) > EPSILON_DECIMAL_12 and abs(target_notional) > EPSILON_DECIMAL_12:
            return abs(target_notional / target_qty)
        if abs(current_qty) > EPSILON_DECIMAL_12 and abs(current_notional) > EPSILON_DECIMAL_12:
            return abs(current_notional / current_qty)
        return Decimal("0")

    @staticmethod
    def _allocation_id(sleeve_intents: list[StrategySleeveIntent]) -> str:
        for intent in sleeve_intents:
            if intent.allocation_id:
                return intent.allocation_id
        return new_id("alloc")

    @staticmethod
    def _budget_multiplier_for(
        *,
        intent: StrategySleeveIntent,
        assignment: SleeveBudgetAssignment | None,
    ) -> Decimal:
        if assignment is not None:
            return to_decimal(assignment.active_budget_multiplier)
        return to_decimal(intent.budget_multiplier)

    @staticmethod
    def _allocator_weight_for(
        *,
        intent: StrategySleeveIntent,
        assignment: SleeveBudgetAssignment | None,
    ) -> Decimal:
        intent_weight = to_decimal(intent.allocator_weight)
        if assignment is None:
            return intent_weight
        return quantize_decimal(intent_weight * to_decimal(assignment.allocator_base_weight))


PortfolioAllocatorV1 = PortfolioAllocatorV2Phase1
