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


class PortfolioAllocatorV2Phase2:
    _FAMILY_PRIORITY: tuple[StrategyFamily, ...] = (
        "smart_arbitrage",
        "independent",
        "protective",
        "opportunistic",
        "spot_grid",
        "dca",
        "directional",
    )
    _HEDGE_PRIORITY_RANK: dict[str, int] = {
        "critical_hedge": 0,
        "hedge": 1,
        "inventory": 2,
        "standard": 3,
    }

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
        allocation_id = self._allocation_id(sleeve_intents)
        assignments_by_sleeve = {
            item.strategy_sleeve_id: item
            for item in (budget_assignments or [])
        }
        intents_by_family = {intent.family: intent for intent in sleeve_intents}
        active_families = [intent.family for intent in sleeve_intents if intent.state not in {"disabled", "incompatible"}]
        approved: list[StrategySleeveIntent] = []
        blocked_reason_codes: list[str] = []
        conflict_resolutions: list[AllocatorConflictResolution] = []
        preserve_selected_family = self._preserve_selected_family_without_directional_fallback(
            base_target=base_target,
            selected_family=selected_family,
            selected_intent=intents_by_family.get(selected_family),
        )

        if base_target.product_type == "derivatives":
            smart_arbitrage_intent = intents_by_family.get("smart_arbitrage")
            independent_intent = intents_by_family.get("independent")
            protective_intent = intents_by_family.get("protective")
            opportunistic_intent = intents_by_family.get("opportunistic")
            directional_intent = intents_by_family.get("directional")
            smart_arbitrage_active = smart_arbitrage_intent is not None and self._intent_is_actionable(
                smart_arbitrage_intent,
                include_active_inventory=True,
            )
            independent_active = independent_intent is not None and self._intent_is_actionable(
                independent_intent,
                include_active_inventory=True,
            )
            protective_active = protective_intent is not None and self._intent_is_actionable(
                protective_intent,
                include_active_inventory=True,
            )
            opportunistic_active = opportunistic_intent is not None and self._intent_is_actionable(
                opportunistic_intent,
                include_active_inventory=True,
            )
            directional_active = directional_intent is not None and self._intent_has_delta(directional_intent)
            overlay_cutover_intent = self._derivatives_overlay_cutover_intent(
                independent_intent=independent_intent if independent_active else None,
                protective_intent=protective_intent if protective_active else None,
                opportunistic_intent=opportunistic_intent if opportunistic_active else None,
            )
            if smart_arbitrage_active and smart_arbitrage_intent is not None:
                approved.append(smart_arbitrage_intent)
                blocked_inputs: list[StrategySleeveIntent] = []
                blocked_reason_codes_for_conflict: list[str] = []
                if directional_active and directional_intent is not None:
                    blocked_reason_codes.append("allocator_directional_blocked_by_active_smart_arbitrage")
                    blocked_inputs.append(directional_intent)
                    blocked_reason_codes_for_conflict.append("allocator_directional_blocked_by_active_smart_arbitrage")
                if independent_active and independent_intent is not None:
                    blocked_reason_codes.append("allocator_independent_blocked_by_active_smart_arbitrage")
                    blocked_inputs.append(independent_intent)
                    blocked_reason_codes_for_conflict.append("allocator_independent_blocked_by_active_smart_arbitrage")
                if protective_active and protective_intent is not None:
                    blocked_reason_codes.append("allocator_protective_blocked_by_active_smart_arbitrage")
                    blocked_inputs.append(protective_intent)
                    blocked_reason_codes_for_conflict.append("allocator_protective_blocked_by_active_smart_arbitrage")
                if opportunistic_active and opportunistic_intent is not None:
                    blocked_reason_codes.append("allocator_opportunistic_blocked_by_active_smart_arbitrage")
                    blocked_inputs.append(opportunistic_intent)
                    blocked_reason_codes_for_conflict.append("allocator_opportunistic_blocked_by_active_smart_arbitrage")
                if blocked_inputs:
                    conflict_resolutions.append(
                        self._conflict_resolution(
                            allocation_id=allocation_id,
                            base_target=base_target,
                            conflict_type="hedge_priority",
                            resolution_action="non_hedge_families_reduced_to_protect_smart_arbitrage",
                            input_intents=[smart_arbitrage_intent, *blocked_inputs],
                            approved_intents=[smart_arbitrage_intent],
                            protected_notional=self._requested_notional(
                                intent=smart_arbitrage_intent,
                                base_target=base_target,
                                assignment=assignments_by_sleeve.get(smart_arbitrage_intent.strategy_sleeve_id),
                            ),
                            reduced_notional=sum(
                                (
                                    self._requested_notional(
                                        intent=item,
                                        base_target=base_target,
                                        assignment=assignments_by_sleeve.get(item.strategy_sleeve_id),
                                    )
                                    for item in blocked_inputs
                                ),
                                start=Decimal("0"),
                            ),
                            reason_codes=[
                                "allocator_v2_hedge_priority",
                                *blocked_reason_codes_for_conflict,
                            ],
                        )
                    )
            elif overlay_cutover_intent is not None:
                approved.append(overlay_cutover_intent)
                if directional_active and directional_intent is not None:
                    shadow_reason = f"allocator_directional_shadowed_by_{overlay_cutover_intent.family}_family_cutover"
                    blocked_reason_codes.append(shadow_reason)
                    conflict_resolutions.append(
                        self._conflict_resolution(
                            allocation_id=allocation_id,
                            base_target=base_target,
                            conflict_type="family_cutover",
                            resolution_action=f"directional_shadowed_by_{overlay_cutover_intent.family}_family",
                            input_intents=[overlay_cutover_intent, directional_intent],
                            approved_intents=[overlay_cutover_intent],
                            protected_notional=self._requested_notional(
                                intent=overlay_cutover_intent,
                                base_target=base_target,
                                assignment=assignments_by_sleeve.get(overlay_cutover_intent.strategy_sleeve_id),
                            ),
                            reduced_notional=self._requested_notional(
                                intent=directional_intent,
                                base_target=base_target,
                                assignment=assignments_by_sleeve.get(directional_intent.strategy_sleeve_id),
                            ),
                            reason_codes=[
                                f"allocator_{overlay_cutover_intent.family}_family_cutover",
                                shadow_reason,
                            ],
                        )
                    )
            elif directional_intent is not None and not preserve_selected_family:
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
                            allocation_id=allocation_id,
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
        for intent in approved:
            scaled_intent, snapshot = self._apply_budget_assignment(
                intent=intent,
                base_target=base_target,
                assignment=assignments_by_sleeve.get(intent.strategy_sleeve_id),
                allocation_id=allocation_id,
            )
            if self._intent_is_actionable(scaled_intent, include_active_inventory=True):
                scaled_approved.append(scaled_intent)
            budget_snapshots.append(snapshot)

        scaled_approved, budget_snapshots, budget_cut_reason_codes = self._apply_portfolio_budget_redistribution(
            approved=scaled_approved,
            budget_snapshots=budget_snapshots,
            base_target=base_target,
        )
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
        approved_notional_by_sleeve = {
            snapshot.strategy_sleeve_id: snapshot.approved_notional
            for snapshot in budget_snapshots
            if snapshot.approved_notional > EPSILON_DECIMAL_12
        }
        primary_intent = self._primary_intent(
            approved=scaled_approved,
            selected_family=selected_family,
            assignments_by_sleeve=assignments_by_sleeve,
        )
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
        portfolio_requested_notional = sum(
            (snapshot.portfolio_requested_notional for snapshot in budget_snapshots),
            start=Decimal("0"),
        )
        portfolio_approved_notional = sum(
            (snapshot.portfolio_approved_notional for snapshot in budget_snapshots),
            start=Decimal("0"),
        )
        portfolio_budget_cut_notional = sum(
            (snapshot.portfolio_budget_cut_notional for snapshot in budget_snapshots),
            start=Decimal("0"),
        )
        resolved_primary_family = (
            selected_family
            if primary_intent is None and preserve_selected_family
            else ("directional" if primary_intent is None else primary_intent.family)
        )
        suppressed_after_approval_intents = [
            intent
            for intent in sleeve_intents
            if str(getattr(intent, "execution_behavior", "") or "").strip() == "suppressed_after_approval"
        ]
        if suppressed_after_approval_intents and "allocator_sleeve_suppressed_after_approval" not in blocked_reason_codes:
            blocked_reason_codes.append("allocator_sleeve_suppressed_after_approval")
        fallback_primary_intent = None if not preserve_selected_family else intents_by_family.get(selected_family)
        reason_codes = list(
            dict.fromkeys(
                [
                    "allocator_v2_phase2_applied",
                    f"allocator_primary_family_{resolved_primary_family}",
                    *selection_reason_codes,
                    *blocked_reason_codes,
                    *budget_cut_reason_codes,
                ]
            )
        )
        budget_state = "redistributed" if budget_cut_reason_codes else (
            "contracted" if any(item.clamped for item in budget_snapshots) else "normal"
        )
        if conflict_resolutions and budget_state == "normal":
            budget_state = "hedge_protected"
        if execution_legs:
            operator_summary = self._operator_summary_for_primary_intent(primary_intent=primary_intent)
        elif suppressed_after_approval_intents:
            operator_summary = "当前 allocator v2 识别到已批准但被预算压零的 sleeve；本轮没有新的可执行 delta。"
        elif approved_families:
            operator_summary = "当前 allocator v2 识别到活跃 sleeve，但本轮没有新的可执行 delta。"
        else:
            operator_summary = "当前 allocator v2 没有批准新的 sleeve 执行动作，系统保持当前仓位。"
        if budget_cut_reason_codes:
            operator_summary = f"{operator_summary} 组合层预算已自动再分配。"
        return PortfolioAllocationDecision(
            allocation_id=allocation_id,
            decision_id=base_target.decision_id,
            symbol=base_target.symbol,
            product_type=base_target.product_type,
            margin_mode=base_target.margin_mode,
            allocator_version="task74_allocator_v2_phase2",
            route_action=route_action,
            primary_family=resolved_primary_family,
            primary_strategy_sleeve_id=(
                None
                if primary_intent is None and fallback_primary_intent is None
                else (
                    primary_intent.strategy_sleeve_id
                    if primary_intent is not None
                    else fallback_primary_intent.strategy_sleeve_id
                )
            ),
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
            portfolio_requested_notional=portfolio_requested_notional,
            portfolio_approved_notional=portfolio_approved_notional,
            portfolio_budget_cut_notional=portfolio_budget_cut_notional,
            budget_cut_reason_codes=budget_cut_reason_codes,
            budget_snapshot_ids=[item.budget_snapshot_id for item in budget_snapshots],
            expected_edge_bps=self._portfolio_expected_bps(
                approved=scaled_approved,
                snapshots=budget_snapshots,
                kind="edge",
            ),
            expected_cost_bps=self._portfolio_expected_bps(
                approved=scaled_approved,
                snapshots=budget_snapshots,
                kind="cost",
            ),
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

    def _preserve_selected_family_without_directional_fallback(
        self,
        *,
        base_target: PositionTarget,
        selected_family: StrategyFamily,
        selected_intent: StrategySleeveIntent | None,
    ) -> bool:
        if self.settings is None:
            return False
        if base_target.product_type != "derivatives":
            return False
        if self.settings.strategy_family_auto_selection_enabled:
            return False
        if selected_family != "independent":
            return False
        if selected_intent is None:
            return False
        return selected_intent.state not in {"disabled", "incompatible"}

    @staticmethod
    def _operator_summary_for_primary_intent(
        *,
        primary_intent: StrategySleeveIntent | None,
    ) -> str:
        family_action = "" if primary_intent is None else str(primary_intent.family_action or "").strip().lower()
        if family_action == "close_protection_leg":
            return "当前 allocator v2 已批准收回保护腿的账户级执行目标。"
        if family_action == "close_opportunity_leg":
            return "当前 allocator v2 已批准收回机会腿的账户级执行目标。"
        if family_action == "de_risk_independent_book":
            return "当前 allocator v2 已批准降低独立双书风险暴露的账户级执行目标。"
        if family_action == "close_failed_thesis_independent_book":
            return "当前 allocator v2 已批准按 thesis 失效关闭独立双书的账户级执行目标。"
        if family_action == "close_stale_thesis_independent_book":
            return "当前 allocator v2 已批准按 thesis 过期关闭独立双书的账户级执行目标。"
        return "当前 allocator v2 已按 sleeve 预算、组合预算和净额规则生成账户级执行目标。"

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
            scaled_intent = self._scale_intent(intent=intent, scaled_delta_qty=approved_delta_qty, budget_ratio=ratio)
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
            priority_rank=self._hedge_priority_rank("standard" if assignment is None else assignment.hedge_priority_class),
            portfolio_requested_notional=requested_notional,
            portfolio_approved_notional=approved_notional,
            portfolio_budget_cut_notional=Decimal("0"),
            clamped=clamped,
            reason_codes=reason_codes,
        )
        return scaled_intent, snapshot

    def _apply_portfolio_budget_redistribution(
        self,
        *,
        approved: list[StrategySleeveIntent],
        budget_snapshots: list[AllocatorBudgetSnapshot],
        base_target: PositionTarget,
    ) -> tuple[list[StrategySleeveIntent], list[AllocatorBudgetSnapshot], list[str]]:
        snapshots_by_sleeve = {item.strategy_sleeve_id: item for item in budget_snapshots}
        budget_cut_reason_codes: list[str] = []
        portfolio_cap = self._portfolio_notional_cap()
        portfolio_requested_notional = sum((item.approved_notional for item in budget_snapshots), start=Decimal("0"))
        approved_notional_by_sleeve = {item.strategy_sleeve_id: item.approved_notional for item in budget_snapshots}

        if (
            portfolio_cap > EPSILON_DECIMAL_12
            and portfolio_requested_notional > portfolio_cap + EPSILON_DECIMAL_12
            and approved
        ):
            approved_notional_by_sleeve = self._redistribute_notional_by_priority(
                approved=approved,
                snapshots_by_sleeve=snapshots_by_sleeve,
                portfolio_cap=portfolio_cap,
            )
            budget_cut_reason_codes.append("allocator_portfolio_max_total_open_notional_capped")

        portfolio_approved_notional = sum(approved_notional_by_sleeve.values(), start=Decimal("0"))
        portfolio_budget_cut_notional = quantize_decimal(
            max(portfolio_requested_notional - portfolio_approved_notional, Decimal("0"))
        )
        redistributed_snapshots: list[AllocatorBudgetSnapshot] = []
        redistributed_intents: list[StrategySleeveIntent] = []
        for intent in approved:
            snapshot = snapshots_by_sleeve[intent.strategy_sleeve_id]
            original_approved_notional = snapshot.approved_notional
            approved_notional = quantize_decimal(approved_notional_by_sleeve.get(intent.strategy_sleeve_id, Decimal("0")))
            approved_delta_qty = snapshot.approved_delta_qty
            redistributed_intent = intent.model_copy(deep=True)
            if (
                original_approved_notional > EPSILON_DECIMAL_12
                and approved_notional + EPSILON_DECIMAL_12 < original_approved_notional
            ):
                ratio = max(Decimal("0"), min(Decimal("1"), approved_notional / original_approved_notional))
                approved_delta_qty = quantize_decimal(snapshot.approved_delta_qty * ratio)
                redistributed_intent = self._scale_intent(intent=intent, scaled_delta_qty=approved_delta_qty, budget_ratio=ratio)
            snapshot_reason_codes = list(snapshot.reason_codes)
            if approved_notional + EPSILON_DECIMAL_12 < original_approved_notional:
                snapshot_reason_codes.append("allocator_portfolio_budget_redistributed")
                if approved_notional <= EPSILON_DECIMAL_12:
                    snapshot_reason_codes.append("allocator_portfolio_budget_zeroed")
            redistributed_snapshot = snapshot.model_copy(
                update={
                    "approved_notional": approved_notional,
                    "approved_delta_qty": approved_delta_qty,
                    "priority_rank": self._hedge_priority_rank(snapshot.hedge_priority_class),
                    "portfolio_requested_notional": portfolio_requested_notional,
                    "portfolio_approved_notional": portfolio_approved_notional,
                    "portfolio_budget_cut_notional": portfolio_budget_cut_notional,
                    "reason_codes": list(dict.fromkeys(snapshot_reason_codes)),
                }
            )
            redistributed_snapshots.append(redistributed_snapshot)
            if self._intent_is_actionable(redistributed_intent, include_active_inventory=True):
                redistributed_intents.append(redistributed_intent)

        updated_snapshot_map = {snap.strategy_sleeve_id: snap for snap in redistributed_snapshots}
        redistributed_intents.sort(
            key=lambda item: self._intent_sort_key(
                item=item,
                snapshots_by_sleeve=updated_snapshot_map,
            )
        )
        redistributed_snapshots.sort(key=lambda item: (item.priority_rank, item.family, item.strategy_sleeve_id))
        return redistributed_intents, redistributed_snapshots, budget_cut_reason_codes

    @staticmethod
    def _scale_intent(
        *,
        intent: StrategySleeveIntent,
        scaled_delta_qty: Decimal,
        budget_ratio: Decimal | None = None,
    ) -> StrategySleeveIntent:
        current_qty = to_decimal(intent.current_position_qty)
        account_current_qty = (
            None if intent.account_current_position_qty is None else to_decimal(intent.account_current_position_qty)
        )
        scaled_legs: list[StrategyLegIntent] = []
        if intent.legs:
            # Use budget_ratio directly when available; otherwise fall back
            # to gross leg delta as divisor (not net delta) to handle
            # reversals where net delta ≈ 0 but individual legs have
            # significant deltas.
            if budget_ratio is not None:
                multiplier = budget_ratio
            else:
                original_gross_delta = sum(
                    abs(to_decimal(leg.delta_position_qty or Decimal("0")))
                    for leg in intent.legs
                )
                multiplier = (
                    Decimal("0")
                    if abs(original_gross_delta) <= EPSILON_DECIMAL_12
                    else abs(scaled_delta_qty) / original_gross_delta
                )
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

    def _derivatives_overlay_cutover_intent(
        self,
        *,
        independent_intent: StrategySleeveIntent | None,
        protective_intent: StrategySleeveIntent | None,
        opportunistic_intent: StrategySleeveIntent | None,
    ) -> StrategySleeveIntent | None:
        configured_mode = str(getattr(self.settings, "strategy_hedge_overlay_mode", "") or "").strip().lower()
        by_family = {
            "independent": independent_intent,
            "protective": protective_intent,
            "opportunistic": opportunistic_intent,
        }
        preferred = by_family.get(configured_mode)
        if preferred is not None:
            return preferred
        for family in ("independent", "protective", "opportunistic"):
            if by_family[family] is not None:
                return by_family[family]
        return None

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
        if abs(to_decimal(intent.delta_position_qty)) > EPSILON_DECIMAL_12:
            return True
        return any(
            abs(to_decimal(leg.delta_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12
            for leg in intent.legs
        )

    def _intent_is_actionable(
        self,
        intent: StrategySleeveIntent,
        *,
        include_active_inventory: bool,
    ) -> bool:
        if not intent.execution_compatible:
            return False
        if intent.route_action == "advisory_only":
            return False
        if intent.route_action == "override_target" and intent.selectable:
            return True
        if self._intent_has_explicit_leg_inventory(intent, include_active_inventory=include_active_inventory):
            return True
        if intent.family == "smart_arbitrage":
            if any(abs(to_decimal(leg.target_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12 for leg in intent.legs):
                return True
            if any(abs(to_decimal(leg.current_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12 for leg in intent.legs):
                return True
        if include_active_inventory and abs(to_decimal(intent.current_position_qty)) > EPSILON_DECIMAL_12:
            return True
        return intent.route_action == "hold_current" and abs(to_decimal(intent.target_position_qty)) > EPSILON_DECIMAL_12

    @staticmethod
    def _explicit_legs(intent: StrategySleeveIntent) -> list[StrategyLegIntent]:
        return [
            leg
            for leg in intent.legs
            if str(getattr(leg, "pos_side", "") or "").lower() in {"long", "short"}
            and str(getattr(leg, "action", "") or "").lower() in {"open", "reduce", "close"}
        ]

    @staticmethod
    def _intent_has_explicit_leg_inventory(
        intent: StrategySleeveIntent,
        *,
        include_active_inventory: bool,
    ) -> bool:
        explicit_legs = PortfolioAllocatorV2Phase2._explicit_legs(intent)
        if not explicit_legs:
            return False
        if any(abs(to_decimal(leg.delta_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12 for leg in explicit_legs):
            return True
        if any(abs(to_decimal(leg.target_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12 for leg in explicit_legs):
            return True
        if include_active_inventory and any(
            abs(to_decimal(leg.current_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12
            for leg in explicit_legs
        ):
            return True
        return False

    def _execution_legs(
        self,
        *,
        approved: list[StrategySleeveIntent],
        base_target: PositionTarget,
    ) -> list[StrategyLegIntent]:
        legs: list[StrategyLegIntent] = []
        for intent in approved:
            if self._explicit_legs(intent) or intent.family == "smart_arbitrage":
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
                    note=f"{intent.family} sleeve delta converted by allocator v2.",
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
        # For independent family with dual-leg intents, compute gross from
        # legs FIRST.  target_notional is NET (long − short) which under-
        # estimates the budget needed for reversals.  Other families (e.g.,
        # smart_arbitrage) set target_notional to a family-specific value
        # that should be respected, so the bypass only applies to
        # independent.
        _independent_legs_first = (
            bool(intent.legs)
            and str(getattr(intent, "family", "") or "") == "independent"
        )
        if not _independent_legs_first:
            if intent.target_notional is not None and abs(to_decimal(intent.target_notional)) > EPSILON_DECIMAL_12:
                return abs(to_decimal(intent.target_notional))
        if intent.legs:
            if intent.family == "smart_arbitrage":
                grouped_total = self._smart_arbitrage_requested_notional(intent=intent, base_target=base_target)
                if grouped_total > EPSILON_DECIMAL_12:
                    return grouped_total
            total = Decimal("0")
            for leg in intent.legs:
                delta_qty = abs(to_decimal(leg.delta_position_qty or Decimal("0")))
                reference_price = abs(to_decimal(leg.reference_price or Decimal("0")))
                if reference_price <= EPSILON_DECIMAL_12:
                    reference_price = self._reference_price(intent) or self._base_target_reference_price(base_target)
                total += delta_qty * reference_price
            if total > EPSILON_DECIMAL_12:
                return total
        # Fallback to target_notional (independent reaches here only if
        # leg computation yielded zero).
        if intent.target_notional is not None and abs(to_decimal(intent.target_notional)) > EPSILON_DECIMAL_12:
            return abs(to_decimal(intent.target_notional))
        reference_price = self._reference_price(intent) or self._base_target_reference_price(base_target)
        if reference_price <= EPSILON_DECIMAL_12 and assignment is not None and assignment.effective_max_symbol_notional is not None:
            return abs(to_decimal(assignment.effective_max_symbol_notional))
        return abs(to_decimal(intent.delta_position_qty)) * reference_price

    def _smart_arbitrage_requested_notional(
        self,
        *,
        intent: StrategySleeveIntent,
        base_target: PositionTarget,
    ) -> Decimal:
        fallback_reference_price = self._reference_price(intent) or self._base_target_reference_price(base_target)
        pair_notionals: dict[str, Decimal] = {}
        unscoped_total = Decimal("0")
        for leg in intent.legs:
            delta_qty = abs(to_decimal(leg.delta_position_qty or Decimal("0")))
            if delta_qty <= EPSILON_DECIMAL_12:
                continue
            reference_price = abs(to_decimal(leg.reference_price or Decimal("0")))
            if reference_price <= EPSILON_DECIMAL_12:
                reference_price = fallback_reference_price
            leg_notional = delta_qty * reference_price
            pair_id = str(leg.pair_id or "").strip()
            if pair_id:
                pair_notionals[pair_id] = max(pair_notionals.get(pair_id, Decimal("0")), leg_notional)
            else:
                unscoped_total += leg_notional
        return sum(pair_notionals.values(), start=Decimal("0")) + unscoped_total

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
        reference_price = PortfolioAllocatorV2Phase2._base_target_reference_price(base_target)
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

    def _portfolio_notional_cap(self) -> Decimal:
        if self.settings is None:
            return Decimal("0")
        return max(to_decimal(self.settings.max_total_open_notional), Decimal("0"))

    @classmethod
    def _hedge_priority_rank(cls, hedge_priority_class: str | None) -> int:
        return cls._HEDGE_PRIORITY_RANK.get(str(hedge_priority_class or "standard"), cls._HEDGE_PRIORITY_RANK["standard"])

    def _redistribute_notional_by_priority(
        self,
        *,
        approved: list[StrategySleeveIntent],
        snapshots_by_sleeve: dict[str, AllocatorBudgetSnapshot],
        portfolio_cap: Decimal,
    ) -> dict[str, Decimal]:
        remaining_cap = portfolio_cap
        approved_by_sleeve: dict[str, Decimal] = {
            intent.strategy_sleeve_id: Decimal("0")
            for intent in approved
        }
        grouped: dict[int, list[StrategySleeveIntent]] = {}
        for intent in approved:
            snapshot = snapshots_by_sleeve[intent.strategy_sleeve_id]
            grouped.setdefault(self._hedge_priority_rank(snapshot.hedge_priority_class), []).append(intent)
        for priority_rank in sorted(grouped):
            intents = grouped[priority_rank]
            if remaining_cap <= EPSILON_DECIMAL_12:
                for intent in intents:
                    approved_by_sleeve[intent.strategy_sleeve_id] = Decimal("0")
                continue
            group_total = sum(
                (snapshots_by_sleeve[intent.strategy_sleeve_id].approved_notional for intent in intents),
                start=Decimal("0"),
            )
            if group_total <= remaining_cap + EPSILON_DECIMAL_12:
                for intent in intents:
                    approved_by_sleeve[intent.strategy_sleeve_id] = snapshots_by_sleeve[intent.strategy_sleeve_id].approved_notional
                remaining_cap = quantize_decimal(remaining_cap - group_total)
                continue
            approved_by_sleeve.update(
                self._weighted_cap_distribution(
                    intents=intents,
                    snapshots_by_sleeve=snapshots_by_sleeve,
                    cap=remaining_cap,
                )
            )
            remaining_cap = Decimal("0")
        return approved_by_sleeve

    def _weighted_cap_distribution(
        self,
        *,
        intents: list[StrategySleeveIntent],
        snapshots_by_sleeve: dict[str, AllocatorBudgetSnapshot],
        cap: Decimal,
    ) -> dict[str, Decimal]:
        remaining = cap
        pending = {intent.strategy_sleeve_id: intent for intent in intents}
        approved: dict[str, Decimal] = {intent.strategy_sleeve_id: Decimal("0") for intent in intents}
        while pending and remaining > EPSILON_DECIMAL_12:
            weighted = {
                sleeve_id: max(
                    quantize_decimal(
                        max(snapshots_by_sleeve[sleeve_id].approved_notional, EPSILON_DECIMAL_12)
                        * max(self._allocator_weight_for(intent=intent, assignment=None), Decimal("0.05"))
                    ),
                    EPSILON_DECIMAL_12,
                )
                for sleeve_id, intent in pending.items()
            }
            total_weight = sum(weighted.values(), start=Decimal("0"))
            if total_weight <= EPSILON_DECIMAL_12:
                equal_share = quantize_decimal(remaining / Decimal(len(pending)))
                for sleeve_id in list(pending):
                    limit = snapshots_by_sleeve[sleeve_id].approved_notional
                    approved_value = min(limit, equal_share)
                    approved[sleeve_id] += approved_value
                    remaining -= approved_value
                break
            saturated: set[str] = set()
            for sleeve_id in list(pending):
                limit = snapshots_by_sleeve[sleeve_id].approved_notional - approved[sleeve_id]
                proposed = quantize_decimal(remaining * weighted[sleeve_id] / total_weight)
                if proposed >= limit - EPSILON_DECIMAL_12:
                    approved[sleeve_id] += max(limit, Decimal("0"))
                    remaining -= max(limit, Decimal("0"))
                    saturated.add(sleeve_id)
            if not saturated:
                for sleeve_id in list(pending):
                    proposed = quantize_decimal(remaining * weighted[sleeve_id] / total_weight)
                    approved[sleeve_id] += proposed
                break
            for sleeve_id in saturated:
                pending.pop(sleeve_id, None)
        return {key: quantize_decimal(max(value, Decimal("0"))) for key, value in approved.items()}

    def _portfolio_expected_bps(
        self,
        *,
        approved: list[StrategySleeveIntent],
        snapshots: list[AllocatorBudgetSnapshot],
        kind: str,
    ) -> Decimal | None:
        weights_by_sleeve = {item.strategy_sleeve_id: item.approved_notional for item in snapshots}
        numerator = Decimal("0")
        denominator = Decimal("0")
        for intent in approved:
            weight = weights_by_sleeve.get(intent.strategy_sleeve_id, Decimal("0"))
            if weight <= EPSILON_DECIMAL_12:
                continue
            metric = self._bps_metric(intent=intent, kind=kind)
            if metric is None:
                continue
            numerator += weight * metric
            denominator += weight
        if denominator <= EPSILON_DECIMAL_12:
            return None
        return quantize_decimal(numerator / denominator)

    @staticmethod
    def _bps_metric(*, intent: StrategySleeveIntent, kind: str) -> Decimal | None:
        keys = (
            ("expected_net_edge_bps", "executable_edge_bps", "ideal_edge_bps", "net_basis_bps", "basis_bps", "expected_signal_edge_bps")
            if kind == "edge"
            else ("expected_cost_bps", "executable_cost_bps", "ideal_cost_bps", "estimated_cost_bps")
        )
        for key in keys:
            value = intent.metrics.get(key)
            if value is None:
                continue
            return to_decimal(value)
        return None

    @staticmethod
    def _intent_sort_key(
        *,
        item: StrategySleeveIntent,
        snapshots_by_sleeve: dict[str, AllocatorBudgetSnapshot],
    ) -> tuple[int, Decimal, float, str]:
        snapshot = snapshots_by_sleeve[item.strategy_sleeve_id]
        return (
            snapshot.priority_rank,
            -to_decimal(snapshot.approved_notional),
            -float(item.priority_score),
            item.strategy_sleeve_id,
        )


PortfolioAllocatorV2Phase1 = PortfolioAllocatorV2Phase2
PortfolioAllocatorV1 = PortfolioAllocatorV2Phase2
