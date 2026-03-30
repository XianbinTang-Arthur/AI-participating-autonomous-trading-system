from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.decision import BaselineAssessment
from aats.schemas.strategy_runtime import (
    StrategyCandidate,
    StrategySleeveAutomationDecision,
    StrategySleeveIntent,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, quantize_decimal, to_decimal
from aats.services.runtime_scope import latest_reconciliation_for_scope, runtime_state_scope, sleeve_pnl_records_for_scope


class StrategySleeveAutoController:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        reconciliation_repo=None,
        sleeve_pnl_repo=None,
    ) -> None:
        self.settings = settings
        self.reconciliation_repo = reconciliation_repo
        self.sleeve_pnl_repo = sleeve_pnl_repo
        self.state_scope = runtime_state_scope(settings)

    def apply(
        self,
        *,
        baseline: BaselineAssessment,
        candidates_by_family: dict[str, StrategyCandidate],
        sleeve_intents: list[StrategySleeveIntent],
    ) -> tuple[dict[str, StrategyCandidate], list[StrategySleeveIntent], list[StrategySleeveAutomationDecision]]:
        latest_reconciliation = self._latest_reconciliation()
        recent_net_by_sleeve = self._recent_net_pnl_by_sleeve()
        controlled_candidates: dict[str, StrategyCandidate] = {}
        controlled_intents: list[StrategySleeveIntent] = []
        decisions: list[StrategySleeveAutomationDecision] = []

        for intent in sleeve_intents:
            candidate = candidates_by_family[intent.family]
            decision = self._build_decision(
                baseline=baseline,
                candidate=candidate,
                intent=intent,
                recent_net_pnl=recent_net_by_sleeve.get(intent.strategy_sleeve_id, Decimal("0")),
                latest_reconciliation=latest_reconciliation,
            )
            controlled_candidate, controlled_intent = self._apply_decision(
                candidate=candidate,
                intent=intent,
                decision=decision,
            )
            controlled_candidates[intent.family] = controlled_candidate
            controlled_intents.append(controlled_intent)
            decisions.append(decision)
        for family, candidate in candidates_by_family.items():
            controlled_candidates.setdefault(family, candidate)
        return controlled_candidates, controlled_intents, decisions

    def _build_decision(
        self,
        *,
        baseline: BaselineAssessment,
        candidate: StrategyCandidate,
        intent: StrategySleeveIntent,
        recent_net_pnl: Decimal,
        latest_reconciliation,
    ) -> StrategySleeveAutomationDecision:
        min_budget = self._decimal(self.settings.strategy_sleeve_auto_min_budget_multiplier)
        reconciliation_contraction = self._decimal(
            self.settings.strategy_sleeve_auto_reconciliation_contraction_multiplier
        )
        budget_multiplier = Decimal("1")
        automation_state = "active"
        automatic_enabled = bool(self.settings.strategy_sleeve_auto_parallel_enabled and candidate.enabled)
        runtime_supported = candidate.state != "incompatible"
        approved_for_execution = automatic_enabled and runtime_supported
        reason_codes: list[str] = []

        current_inventory_notional = self._inventory_notional(intent=intent)
        protective_intent = self._is_protective_intent(intent)
        active_inventory = current_inventory_notional > EPSILON_DECIMAL_12

        if not candidate.enabled:
            automatic_enabled = False
            approved_for_execution = False
            automation_state = "disabled"
            reason_codes.append(f"{intent.family}_candidate_disabled")
        elif not runtime_supported:
            automatic_enabled = False
            approved_for_execution = False
            automation_state = "disabled"
            reason_codes.append(f"{intent.family}_runtime_not_supported")

        if (
            self.settings.strategy_sleeve_auto_volatility_cap_enabled
            and automatic_enabled
            and not protective_intent
        ):
            volatility_cap = self._clamp(
                to_decimal(baseline.volatility_target_scale),
                lower=min_budget,
                upper=Decimal("1"),
            )
            if volatility_cap < Decimal("1") - EPSILON_DECIMAL_12:
                budget_multiplier = min(budget_multiplier, volatility_cap)
                automation_state = "contracted"
                reason_codes.append("sleeve_budget_scaled_by_baseline_volatility_target")

        if latest_reconciliation is not None and automatic_enabled and not protective_intent:
            if latest_reconciliation.halt_required or latest_reconciliation.resume_blocking:
                if active_inventory:
                    budget_multiplier = min(budget_multiplier, reconciliation_contraction)
                    automation_state = "protective_only"
                    reason_codes.append("sleeve_reconciliation_resume_blocking")
                else:
                    automatic_enabled = False
                    approved_for_execution = False
                    budget_multiplier = Decimal("0")
                    automation_state = "paused"
                    reason_codes.append("sleeve_reconciliation_hard_block")
            elif (
                latest_reconciliation.only_reduce_required
                or latest_reconciliation.review_required
                or str(latest_reconciliation.severity or "").upper() not in {"", "CLEAN"}
            ):
                budget_multiplier = min(budget_multiplier, reconciliation_contraction)
                automation_state = "protective_only" if active_inventory else "contracted"
                reason_codes.append("sleeve_reconciliation_contracted")

        if automatic_enabled and not protective_intent and recent_net_pnl < -EPSILON_DECIMAL_12:
            soft_loss = self._decimal(self.settings.strategy_sleeve_auto_soft_loss_usdt)
            hard_loss = self._decimal(self.settings.strategy_sleeve_auto_hard_loss_usdt)
            if intent.family != "directional" and not active_inventory and hard_loss > EPSILON_DECIMAL_12:
                if abs(recent_net_pnl) >= hard_loss:
                    automatic_enabled = False
                    approved_for_execution = False
                    budget_multiplier = Decimal("0")
                    automation_state = "paused"
                    reason_codes.append("sleeve_hard_loss_pause")
            if automatic_enabled and soft_loss > EPSILON_DECIMAL_12:
                loss_ratio = min(abs(recent_net_pnl) / soft_loss, Decimal("1"))
                pnl_multiplier = max(min_budget, Decimal("1") - (loss_ratio * Decimal("0.5")))
                if pnl_multiplier < Decimal("1") - EPSILON_DECIMAL_12:
                    budget_multiplier = min(budget_multiplier, pnl_multiplier)
                    if automation_state == "active":
                        automation_state = "contracted"
                    reason_codes.append("sleeve_recent_loss_contracted")

        if automatic_enabled and budget_multiplier <= EPSILON_DECIMAL_12 and not protective_intent:
            if active_inventory:
                automation_state = "protective_only"
                budget_multiplier = min_budget
                reason_codes.append("sleeve_inventory_hold_without_new_budget")
            else:
                automatic_enabled = False
                approved_for_execution = False
                automation_state = "paused"
                reason_codes.append("sleeve_zero_budget_pause")

        if automatic_enabled and runtime_supported:
            approved_for_execution = True
        allocator_weight = Decimal("0")
        if approved_for_execution:
            base_weight = max(
                self._decimal(candidate.confidence if candidate.confidence > 0 else 0.5),
                Decimal("0.25"),
            )
            score_weight = max(self._decimal(candidate.score if candidate.score > 0 else 0.25), Decimal("0.25"))
            allocator_weight = quantize_decimal(max(budget_multiplier, min_budget) * base_weight * score_weight)
            if allocator_weight <= EPSILON_DECIMAL_12:
                allocator_weight = min_budget

        if not reason_codes:
            reason_codes.append("sleeve_auto_parallel_nominal")
        operator_summary = self._operator_summary(
            family=intent.family,
            automation_state=automation_state,
            budget_multiplier=budget_multiplier,
            recent_net_pnl=recent_net_pnl,
            active_inventory=active_inventory,
        )
        return StrategySleeveAutomationDecision(
            family=intent.family,
            strategy_sleeve_id=intent.strategy_sleeve_id,
            automatic_enabled=automatic_enabled,
            runtime_supported=runtime_supported,
            approved_for_execution=approved_for_execution,
            automation_state=automation_state,
            budget_multiplier=quantize_decimal(budget_multiplier),
            allocator_weight=quantize_decimal(allocator_weight),
            recent_net_pnl=quantize_decimal(recent_net_pnl),
            current_inventory_notional=quantize_decimal(current_inventory_notional),
            reason_codes=reason_codes,
            operator_summary=operator_summary,
        )

    def _apply_decision(
        self,
        *,
        candidate: StrategyCandidate,
        intent: StrategySleeveIntent,
        decision: StrategySleeveAutomationDecision,
    ) -> tuple[StrategyCandidate, StrategySleeveIntent]:
        route_action = intent.route_action
        scaled_delta = to_decimal(intent.delta_position_qty)
        scaled_legs = [leg.model_copy(deep=True) for leg in intent.legs]
        active_inventory = decision.current_inventory_notional > EPSILON_DECIMAL_12
        protective_intent = self._is_protective_intent(intent)

        if not decision.approved_for_execution and not protective_intent:
            scaled_delta = Decimal("0")
            scaled_legs = self._hold_legs(scaled_legs)
            route_action = "hold_current" if active_inventory else "advisory_only"
        elif (
            decision.budget_multiplier < Decimal("1") - EPSILON_DECIMAL_12
            and not protective_intent
            and abs(scaled_delta) > EPSILON_DECIMAL_12
        ):
            scaled_delta = quantize_decimal(scaled_delta * decision.budget_multiplier)
            if abs(scaled_delta) <= EPSILON_DECIMAL_12:
                route_action = "hold_current" if active_inventory else "advisory_only"
            scaled_legs = self._scale_legs(scaled_legs, decision.budget_multiplier)

        current_qty = to_decimal(intent.current_position_qty)
        target_qty = current_qty + scaled_delta
        account_current_qty = (
            None if intent.account_current_position_qty is None else to_decimal(intent.account_current_position_qty)
        )
        account_target_qty = (
            None if account_current_qty is None else account_current_qty + scaled_delta
        )
        target_notional = self._scaled_target_notional(intent=intent, target_qty=target_qty)

        controlled_intent = intent.model_copy(
            update={
                "route_action": route_action,
                "target_position_qty": target_qty,
                "delta_position_qty": scaled_delta,
                "account_target_position_qty": account_target_qty,
                "target_notional": target_notional,
                "automatic_enabled": decision.automatic_enabled,
                "budget_multiplier": decision.budget_multiplier,
                "allocator_weight": decision.allocator_weight,
                "control_reason_codes": list(decision.reason_codes),
                "control_summary": decision.operator_summary,
                "legs": scaled_legs,
                "metrics": {
                    **intent.metrics,
                    "auto_budget_multiplier": decision.budget_multiplier,
                    "auto_allocator_weight": decision.allocator_weight,
                    "auto_recent_net_pnl": decision.recent_net_pnl,
                    "auto_current_inventory_notional": decision.current_inventory_notional,
                    "auto_automation_state": decision.automation_state,
                },
            }
        )

        candidate_state = candidate.state
        aggregate_smart_arbitrage = (
            candidate.family == "smart_arbitrage"
            and bool((candidate.metrics or {}).get("aggregate_candidate"))
        )
        candidate_route_action = route_action if route_action != "protective_fallback" else candidate.route_action
        candidate_selectable = route_action in {"override_target", "hold_current"} and (
            route_action == "override_target" or active_inventory or protective_intent
        )
        candidate_execution_compatible = candidate.execution_compatible and (
            decision.runtime_supported and (decision.automatic_enabled or protective_intent or active_inventory)
        )
        if not decision.runtime_supported or not decision.automatic_enabled:
            if candidate.state in {"disabled", "incompatible"}:
                candidate_state = candidate.state
            elif route_action == "advisory_only":
                candidate_state = "advisory_only"
            elif route_action == "hold_current":
                candidate_state = "inactive"
        elif decision.automation_state in {"contracted", "protective_only"} and candidate.state == "ready":
            candidate_state = "ready"
        controlled_candidate = candidate.model_copy(
            update={
                "state": candidate_state,
                "selectable": candidate_selectable,
                "execution_compatible": candidate_execution_compatible,
                "route_action": candidate_route_action,
                "target_position_qty": (
                    None
                    if aggregate_smart_arbitrage
                    else (account_target_qty if account_target_qty is not None else target_qty)
                ),
                "delta_position_qty": None if aggregate_smart_arbitrage else scaled_delta,
                "automatic_enabled": decision.automatic_enabled,
                "budget_multiplier": decision.budget_multiplier,
                "allocator_weight": decision.allocator_weight,
                "control_reason_codes": list(decision.reason_codes),
                "control_summary": decision.operator_summary,
                "metrics": {
                    **candidate.metrics,
                    "auto_budget_multiplier": decision.budget_multiplier,
                    "auto_allocator_weight": decision.allocator_weight,
                    "auto_recent_net_pnl": decision.recent_net_pnl,
                    "auto_current_inventory_notional": decision.current_inventory_notional,
                    "auto_automation_state": decision.automation_state,
                },
                "legs": scaled_legs,
            }
        )
        return controlled_candidate, controlled_intent

    def _latest_reconciliation(self):
        if self.reconciliation_repo is None:
            return None
        return latest_reconciliation_for_scope(self.reconciliation_repo, self.state_scope)

    def _recent_net_pnl_by_sleeve(self) -> dict[str, Decimal]:
        if self.sleeve_pnl_repo is None:
            return {}
        rows = sleeve_pnl_records_for_scope(self.sleeve_pnl_repo, self.state_scope, limit=500)
        totals: dict[str, Decimal] = {}
        for row in rows:
            sleeve_id = str(row.strategy_sleeve_id or "").strip()
            if not sleeve_id:
                continue
            totals.setdefault(sleeve_id, Decimal("0"))
            totals[sleeve_id] += (
                to_decimal(row.realized_pnl)
                + to_decimal(row.funding_fee_amount)
                + to_decimal(row.fee_amount)
            )
        return totals

    def _inventory_notional(self, *, intent: StrategySleeveIntent) -> Decimal:
        if intent.legs:
            total = Decimal("0")
            for leg in intent.legs:
                current_qty = abs(to_decimal(leg.current_position_qty))
                reference_price = to_decimal(leg.reference_price)
                if reference_price <= EPSILON_DECIMAL_12:
                    reference_price = self._reference_price(intent.metrics)
                total += current_qty * abs(reference_price)
            return total
        current_qty = abs(to_decimal(intent.current_position_qty))
        return current_qty * self._reference_price(intent.metrics)

    def _is_protective_intent(self, intent: StrategySleeveIntent) -> bool:
        if intent.legs:
            current_abs = sum((abs(to_decimal(leg.current_position_qty)) for leg in intent.legs), start=Decimal("0"))
            target_abs = sum((abs(to_decimal(leg.target_position_qty)) for leg in intent.legs), start=Decimal("0"))
            if target_abs + EPSILON_DECIMAL_12 < current_abs:
                return True
            if abs(target_abs - current_abs) <= EPSILON_DECIMAL_12 and str(intent.route_action) == "hold_current":
                return True
            return False
        current_abs = abs(to_decimal(intent.current_position_qty))
        target_abs = abs(to_decimal(intent.target_position_qty))
        if target_abs + EPSILON_DECIMAL_12 < current_abs:
            return True
        return abs(target_abs - current_abs) <= EPSILON_DECIMAL_12 and str(intent.route_action) == "hold_current"

    @staticmethod
    def _scale_legs(legs, multiplier: Decimal):
        scaled = []
        for leg in legs:
            current_qty = to_decimal(leg.current_position_qty)
            delta_qty = to_decimal(leg.delta_position_qty)
            scaled_delta = quantize_decimal(delta_qty * multiplier)
            scaled.append(
                leg.model_copy(
                    update={
                        "delta_position_qty": scaled_delta,
                        "target_position_qty": current_qty + scaled_delta,
                        "note": (
                            f"{leg.note} | auto_parallel_budget_multiplier={format(multiplier, 'f')}"
                            if leg.note
                            else f"auto_parallel_budget_multiplier={format(multiplier, 'f')}"
                        ),
                    }
                )
            )
        return scaled

    @staticmethod
    def _hold_legs(legs):
        held = []
        for leg in legs:
            current_qty = to_decimal(leg.current_position_qty)
            held.append(
                leg.model_copy(
                    update={
                        "delta_position_qty": Decimal("0"),
                        "target_position_qty": current_qty,
                        "note": (
                            f"{leg.note} | auto_parallel_hold_current"
                            if leg.note
                            else "auto_parallel_hold_current"
                        ),
                    }
                )
            )
        return held

    def _scaled_target_notional(self, *, intent: StrategySleeveIntent, target_qty: Decimal) -> Decimal | None:
        if intent.target_notional is None:
            return None
        reference_price = self._reference_price(intent.metrics)
        if reference_price <= EPSILON_DECIMAL_12:
            return quantize_decimal(intent.target_notional)
        return quantize_decimal(abs(target_qty) * reference_price)

    @staticmethod
    def _reference_price(metrics: dict[str, object]) -> Decimal:
        for key in ("current_price", "spot_price", "derivatives_price", "anchor_price"):
            price = to_decimal(metrics.get(key))
            if price > EPSILON_DECIMAL_12:
                return abs(price)
        return Decimal("0")

    @staticmethod
    def _operator_summary(
        *,
        family: str,
        automation_state: str,
        budget_multiplier: Decimal,
        recent_net_pnl: Decimal,
        active_inventory: bool,
    ) -> str:
        if automation_state == "disabled":
            return f"{family} 当前在本运行域内不可自动执行。"
        if automation_state == "paused":
            return f"{family} 已被系统自动暂停；当前没有新的预算分配。"
        if automation_state == "protective_only":
            return (
                f"{family} 当前只保留保护性仓位管理；预算已收缩到 {format(budget_multiplier, 'f')}。"
            )
        if automation_state == "contracted":
            return (
                f"{family} 当前仍可自动运行，但预算已收缩到 {format(budget_multiplier, 'f')}；"
                f"最近净收益 {format(recent_net_pnl, 'f')}。"
            )
        if active_inventory:
            return f"{family} 当前自动运行正常，并继续管理已有库存。"
        return f"{family} 当前自动运行正常，预算倍率为 {format(budget_multiplier, 'f')}。"

    @staticmethod
    def _clamp(value: Decimal, *, lower: Decimal, upper: Decimal) -> Decimal:
        if value < lower:
            return lower
        if value > upper:
            return upper
        return value

    @staticmethod
    def _decimal(value: Decimal | float | int | str | None) -> Decimal:
        return to_decimal(value)
