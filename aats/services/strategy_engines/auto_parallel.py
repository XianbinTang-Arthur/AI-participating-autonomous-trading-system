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
from aats.services.strategy_engines.sleeve_budget_controller import SleeveBudgetController
from aats.services.strategy_engines.sleeve_execution_permission import SleeveExecutionPermissionPolicy
from aats.services.strategy_engines.sleeve_reason_codes import (
    AUTO_EXECUTION_DISABLED_BY_PROFILE,
    CANDIDATE_DISABLED,
    CANDIDATE_EXECUTION_INCOMPATIBLE,
    RUNTIME_NOT_SUPPORTED,
)
from aats.services.strategy_engines.sleeve_routing_composer import SleeveRoutingComposer
from aats.services.strategy_engines.sleeve_routing_models import (
    BudgetControlDecision,
    ComposedSleeveRoutingDecision,
    ExecutionPermissionDecision,
    RawSleeveCandidateInputs,
)


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
        self.permission_policy = SleeveExecutionPermissionPolicy(settings)
        self.budget_controller = SleeveBudgetController(settings)
        self.routing_composer = SleeveRoutingComposer()

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
            raw = self._extract_raw_inputs(candidate=candidate, intent=intent)
            recent_net_pnl = recent_net_by_sleeve.get(intent.strategy_sleeve_id, Decimal("0"))
            permission = self.permission_policy.evaluate(raw=raw)
            budget = self.budget_controller.evaluate(
                raw=raw,
                baseline=baseline,
                recent_net_pnl=recent_net_pnl,
                latest_reconciliation=latest_reconciliation,
            )
            composed = self.routing_composer.compose(
                raw=raw,
                permission=permission,
                budget=budget,
            )
            decision = self._build_decision(
                raw=raw,
                permission=permission,
                budget=budget,
                composed=composed,
                recent_net_pnl=recent_net_pnl,
            )
            controlled_candidate, controlled_intent = self._apply_decision(
                candidate=candidate,
                intent=intent,
                raw=raw,
                decision=decision,
                permission=permission,
                budget=budget,
                composed=composed,
            )
            controlled_candidates[intent.family] = controlled_candidate
            controlled_intents.append(controlled_intent)
            decisions.append(decision)
        for family, candidate in candidates_by_family.items():
            controlled_candidates.setdefault(family, candidate)
        return controlled_candidates, controlled_intents, decisions

    def _extract_raw_inputs(
        self,
        *,
        candidate: StrategyCandidate,
        intent: StrategySleeveIntent,
    ) -> RawSleeveCandidateInputs:
        current_inventory_notional = self._inventory_notional(intent=intent)
        return RawSleeveCandidateInputs(
            family=intent.family,
            strategy_sleeve_id=intent.strategy_sleeve_id,
            symbol=intent.symbol,
            current_position_qty=to_decimal(intent.current_position_qty),
            target_position_qty=to_decimal(intent.target_position_qty),
            delta_position_qty=to_decimal(intent.delta_position_qty),
            account_current_position_qty=(
                None
                if intent.account_current_position_qty is None
                else to_decimal(intent.account_current_position_qty)
            ),
            target_notional=None if intent.target_notional is None else to_decimal(intent.target_notional),
            route_action=intent.route_action,
            requested_legs=tuple(leg.model_copy(deep=True) for leg in intent.legs),
            metrics=dict(intent.metrics or {}),
            candidate_state=str(candidate.state),
            candidate_enabled=bool(candidate.enabled),
            candidate_selectable=bool(candidate.selectable),
            candidate_execution_compatible=bool(candidate.execution_compatible),
            candidate_score=float(candidate.score or 0.0),
            candidate_confidence=float(candidate.confidence or 0.0),
            state_runtime_supported=candidate.state != "incompatible",
            active_inventory=current_inventory_notional > EPSILON_DECIMAL_12,
            current_inventory_notional=quantize_decimal(current_inventory_notional),
            protective_intent=self._is_protective_intent(intent),
        )

    def _build_decision(
        self,
        *,
        raw: RawSleeveCandidateInputs,
        permission: ExecutionPermissionDecision,
        budget: BudgetControlDecision,
        composed: ComposedSleeveRoutingDecision,
        recent_net_pnl: Decimal,
    ) -> StrategySleeveAutomationDecision:
        allocator_weight = self._allocator_weight(
            raw=raw,
            permission=permission,
            budget=budget,
            composed=composed,
        )
        permission_reason_codes = list(permission.reason_codes)
        budget_reason_codes = list(budget.contraction_reason_codes)
        composition_reason_codes = list(composed.composition_reason_codes)
        merged_reason_codes = list(
            dict.fromkeys(permission_reason_codes + budget_reason_codes + composition_reason_codes)
        )
        if not merged_reason_codes:
            merged_reason_codes = ["sleeve_auto_parallel_nominal"]
        automation_state = self._automation_state(
            raw=raw,
            permission=permission,
            budget=budget,
            composed=composed,
        )
        return StrategySleeveAutomationDecision(
            family=raw.family,
            strategy_sleeve_id=raw.strategy_sleeve_id,
            automatic_enabled=bool(
                permission.configured_auto_execution_enabled and raw.candidate_enabled
            ),
            runtime_supported=raw.state_runtime_supported,
            approved_for_execution=permission.approved_for_execution,
            permission_mode=permission.permission_mode,
            execution_control_mode=composed.execution_control_mode,
            execution_behavior=composed.execution_behavior,
            automation_state=automation_state,
            compatibility={
                "legacy_automation_state": automation_state,
                "legacy_automation_state_note": (
                    "compatibility-only coarse projection; prefer execution_control_mode and execution_behavior"
                ),
                "legacy_automation_projection": {
                    "source_execution_control_mode": composed.execution_control_mode,
                    "source_execution_behavior": composed.execution_behavior,
                },
            },
            budget_multiplier=budget.effective_scale,
            effective_scale=budget.effective_scale,
            allocator_weight=allocator_weight,
            recent_net_pnl=quantize_decimal(recent_net_pnl),
            current_inventory_notional=raw.current_inventory_notional,
            requested_delta_position_qty=quantize_decimal(raw.delta_position_qty),
            composed_delta_position_qty=quantize_decimal(composed.composed_delta_position_qty),
            composed_route_action=composed.route_action,
            protective_intent=raw.protective_intent,
            budget_zero_suppressed=composed.budget_zero_suppressed,
            reason_codes=merged_reason_codes,
            permission_reason_codes=permission_reason_codes,
            budget_reason_codes=budget_reason_codes,
            composition_reason_codes=composition_reason_codes,
            scale_trace=list(budget.scale_trace),
            operator_summary=self._operator_summary(
                raw=raw,
                permission=permission,
                budget=budget,
                composed=composed,
                recent_net_pnl=recent_net_pnl,
            ),
        )

    def _apply_decision(
        self,
        *,
        candidate: StrategyCandidate,
        intent: StrategySleeveIntent,
        raw: RawSleeveCandidateInputs,
        decision: StrategySleeveAutomationDecision,
        permission: ExecutionPermissionDecision,
        budget: BudgetControlDecision,
        composed: ComposedSleeveRoutingDecision,
    ) -> tuple[StrategyCandidate, StrategySleeveIntent]:
        route_action = composed.route_action
        composed_delta = quantize_decimal(composed.composed_delta_position_qty)
        account_current_qty = raw.account_current_position_qty
        account_target_qty = None if account_current_qty is None else quantize_decimal(account_current_qty + composed_delta)
        target_notional = self._scaled_target_notional(
            target_notional=raw.target_notional,
            metrics=raw.metrics,
            target_qty=composed.composed_target_position_qty,
        )
        merged_reason_codes = list(
            dict.fromkeys(
                decision.permission_reason_codes
                + decision.budget_reason_codes
                + decision.composition_reason_codes
            )
        )
        control_trace = self._control_trace(
            permission=permission,
            budget=budget,
            composed=composed,
        )
        intent_execution_compatible = self._execution_compatible(
            raw=raw,
            permission=permission,
            budget=budget,
        )
        intent_selectable = route_action in {"override_target", "hold_current"} and (
            route_action == "override_target" or raw.active_inventory or raw.protective_intent
        )
        controlled_intent = intent.model_copy(
            update={
                "route_action": route_action,
                "selectable": intent_selectable,
                "execution_compatible": intent_execution_compatible,
                "target_position_qty": quantize_decimal(
                    composed.composed_target_position_qty
                    if composed.composed_target_position_qty is not None
                    else raw.current_position_qty
                ),
                "delta_position_qty": composed_delta,
                "account_target_position_qty": account_target_qty,
                "target_notional": target_notional,
                "requested_target_position_qty": quantize_decimal(raw.target_position_qty),
                "requested_delta_position_qty": quantize_decimal(raw.delta_position_qty),
                "automatic_enabled": decision.automatic_enabled,
                "approved_for_execution": decision.approved_for_execution,
                "permission_mode": decision.permission_mode,
                "execution_control_mode": decision.execution_control_mode,
                "execution_behavior": decision.execution_behavior,
                "budget_zero_suppressed": decision.budget_zero_suppressed,
                "budget_multiplier": decision.budget_multiplier,
                "allocator_weight": decision.allocator_weight,
                "control_reason_codes": merged_reason_codes,
                "control_summary": decision.operator_summary,
                "control_trace": control_trace,
                "legs": list(composed.composed_legs),
                "metrics": {
                    **intent.metrics,
                    "auto_budget_multiplier": decision.budget_multiplier,
                    "auto_effective_scale": decision.effective_scale,
                    "auto_allocator_weight": decision.allocator_weight,
                    "auto_recent_net_pnl": decision.recent_net_pnl,
                    "auto_current_inventory_notional": decision.current_inventory_notional,
                    "auto_legacy_automation_state": decision.automation_state,
                    "auto_permission_mode": decision.permission_mode,
                    "auto_execution_control_mode": decision.execution_control_mode,
                    "auto_execution_behavior": decision.execution_behavior,
                    "auto_budget_zero_suppressed": decision.budget_zero_suppressed,
                    "auto_requested_delta_position_qty": decision.requested_delta_position_qty,
                    "auto_composed_delta_position_qty": decision.composed_delta_position_qty,
                    "auto_control_trace": control_trace,
                },
            }
        )

        aggregate_smart_arbitrage = (
            candidate.family == "smart_arbitrage"
            and bool((candidate.metrics or {}).get("aggregate_candidate"))
        )
        candidate_route_action = route_action if route_action != "protective_fallback" else candidate.route_action
        controlled_candidate = candidate.model_copy(
            update={
                "state": str(candidate.state),
                "selectable": intent_selectable,
                "execution_compatible": intent_execution_compatible,
                "route_action": candidate_route_action,
                "target_position_qty": (
                    None
                    if aggregate_smart_arbitrage
                    else (account_target_qty if account_target_qty is not None else controlled_intent.target_position_qty)
                ),
                "delta_position_qty": None if aggregate_smart_arbitrage else composed_delta,
                "automatic_enabled": decision.automatic_enabled,
                "budget_multiplier": decision.budget_multiplier,
                "allocator_weight": decision.allocator_weight,
                "control_reason_codes": merged_reason_codes,
                "control_summary": decision.operator_summary,
                "metrics": {
                    **candidate.metrics,
                    "auto_budget_multiplier": decision.budget_multiplier,
                    "auto_effective_scale": decision.effective_scale,
                    "auto_allocator_weight": decision.allocator_weight,
                    "auto_recent_net_pnl": decision.recent_net_pnl,
                    "auto_current_inventory_notional": decision.current_inventory_notional,
                    "auto_legacy_automation_state": decision.automation_state,
                    "auto_permission_mode": decision.permission_mode,
                    "auto_execution_control_mode": decision.execution_control_mode,
                    "auto_execution_behavior": decision.execution_behavior,
                    "auto_budget_zero_suppressed": decision.budget_zero_suppressed,
                    "auto_requested_delta_position_qty": decision.requested_delta_position_qty,
                    "auto_composed_delta_position_qty": decision.composed_delta_position_qty,
                    "auto_control_trace": control_trace,
                },
                "legs": list(composed.composed_legs),
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

    def _allocator_weight(
        self,
        *,
        raw: RawSleeveCandidateInputs,
        permission: ExecutionPermissionDecision,
        budget: BudgetControlDecision,
        composed: ComposedSleeveRoutingDecision,
    ) -> Decimal:
        if not permission.approved_for_execution or composed.budget_zero_suppressed:
            return Decimal("0")
        min_budget = self._decimal(self.settings.strategy_sleeve_auto_min_budget_multiplier)
        base_weight = max(self._decimal(raw.candidate_confidence if raw.candidate_confidence > 0 else 0.5), Decimal("0.25"))
        score_weight = max(self._decimal(raw.candidate_score if raw.candidate_score > 0 else 0.25), Decimal("0.25"))
        allocator_weight = quantize_decimal(max(budget.effective_scale, min_budget) * base_weight * score_weight)
        if allocator_weight <= EPSILON_DECIMAL_12:
            return Decimal("0")
        return allocator_weight

    @staticmethod
    def _execution_compatible(
        *,
        raw: RawSleeveCandidateInputs,
        permission: ExecutionPermissionDecision,
        budget: BudgetControlDecision,
    ) -> bool:
        return bool(
            raw.candidate_execution_compatible
            and raw.state_runtime_supported
            and (
                permission.approved_for_execution
                or raw.protective_intent
                or raw.active_inventory
                or budget.budget_zero_suppressed
            )
        )

    @staticmethod
    def _scaled_target_notional(
        *,
        target_notional: Decimal | None,
        metrics: dict[str, object],
        target_qty: Decimal | None,
    ) -> Decimal | None:
        if target_notional is None or target_qty is None:
            return target_notional
        reference_price = StrategySleeveAutoController._reference_price(metrics)
        if reference_price <= EPSILON_DECIMAL_12:
            return quantize_decimal(target_notional)
        return quantize_decimal(abs(target_qty) * reference_price)

    @staticmethod
    def _reference_price(metrics: dict[str, object]) -> Decimal:
        for key in ("current_price", "spot_price", "derivatives_price", "anchor_price"):
            price = to_decimal(metrics.get(key))
            if price > EPSILON_DECIMAL_12:
                return abs(price)
        return Decimal("0")

    @staticmethod
    def _automation_state(
        *,
        raw: RawSleeveCandidateInputs,
        permission: ExecutionPermissionDecision,
        budget: BudgetControlDecision,
        composed: ComposedSleeveRoutingDecision,
    ) -> str:
        if permission.is_protective_override:
            return "protective_only"
        if (
            RUNTIME_NOT_SUPPORTED in permission.reason_codes
            or CANDIDATE_DISABLED in permission.reason_codes
            or CANDIDATE_EXECUTION_INCOMPATIBLE in permission.reason_codes
        ):
            return "disabled"
        if AUTO_EXECUTION_DISABLED_BY_PROFILE in permission.reason_codes:
            return "protective_only" if raw.active_inventory else "paused"
        if composed.budget_zero_suppressed:
            return "contracted"
        if budget.effective_scale < Decimal("1") - EPSILON_DECIMAL_12:
            return "contracted"
        return "active"

    @staticmethod
    def _operator_summary(
        *,
        raw: RawSleeveCandidateInputs,
        permission: ExecutionPermissionDecision,
        budget: BudgetControlDecision,
        composed: ComposedSleeveRoutingDecision,
        recent_net_pnl: Decimal,
    ) -> str:
        if permission.is_protective_override:
            return (
                f"{raw.family} 当前走保护性例外通道；即使普通自动执行关闭，保护性收缩仍继续提交。"
            )
        if RUNTIME_NOT_SUPPORTED in permission.reason_codes:
            return f"{raw.family} 当前运行环境不支持自动执行，因此这条 sleeve 只保留状态，不进入执行链。"
        if CANDIDATE_EXECUTION_INCOMPATIBLE in permission.reason_codes:
            return (
                f"{raw.family} 当前候选没有通过执行兼容性检查；因此不会进入自动执行链，"
                "也不会再出现“权限已批准但 intent 仍不可执行”的语义分裂。"
            )
        if CANDIDATE_DISABLED in permission.reason_codes:
            return f"{raw.family} 当前候选未启用，因此不会自动进入执行链。"
        if AUTO_EXECUTION_DISABLED_BY_PROFILE in permission.reason_codes:
            return (
                f"{raw.family} 当前非保护性自动执行被配置关闭；新的开仓/加仓只保留参考，不会自动下单。"
            )
        if composed.budget_zero_suppressed:
            return (
                f"{raw.family} 当前允许自动执行，但预算层把可执行量压成了 0；"
                f"最近净收益 {format(recent_net_pnl, 'f')}，系统本轮保持仓位。"
            )
        if budget.effective_scale < Decimal("1") - EPSILON_DECIMAL_12:
            return (
                f"{raw.family} 当前仍可自动执行，但预算已收缩到 {format(budget.effective_scale, 'f')}；"
                f"最近净收益 {format(recent_net_pnl, 'f')}。"
            )
        if raw.active_inventory:
            return f"{raw.family} 当前自动运行正常，并继续管理已有库存。"
        return f"{raw.family} 当前自动运行正常，可按本轮目标继续进入执行链。"

    @staticmethod
    def _control_trace(
        *,
        permission: ExecutionPermissionDecision,
        budget: BudgetControlDecision,
        composed: ComposedSleeveRoutingDecision,
    ) -> dict[str, object]:
        return {
            "execution_control_mode": composed.execution_control_mode,
            "execution_behavior": composed.execution_behavior,
            "permission": {
                "configured_auto_execution_enabled": permission.configured_auto_execution_enabled,
                "runtime_supported": permission.state_runtime_supported,
                "state_runtime_supported": permission.state_runtime_supported,
                "candidate_enabled": permission.candidate_enabled,
                "candidate_execution_compatible": permission.candidate_execution_compatible,
                "protective_intent": permission.protective_intent,
                "approved_for_execution": permission.approved_for_execution,
                "permission_mode": permission.permission_mode,
                "reason_codes": list(permission.reason_codes),
                "human_summary": permission.human_summary,
            },
            "budget": {
                "base_scale": str(budget.base_scale),
                "effective_scale": str(budget.effective_scale),
                "requested_delta_position_qty": str(budget.requested_delta_position_qty),
                "scaled_delta_position_qty": str(budget.scaled_delta_position_qty),
                "budget_zero_suppressed": budget.budget_zero_suppressed,
                "reason_codes": list(budget.contraction_reason_codes),
                "scale_trace": list(budget.scale_trace),
            },
            "composition": {
                "route_action": composed.route_action,
                "approved_for_execution": composed.approved_for_execution,
                "execution_control_mode": composed.execution_control_mode,
                "execution_behavior": composed.execution_behavior,
                "requested_delta_position_qty": str(composed.requested_delta_position_qty),
                "composed_delta_position_qty": str(composed.composed_delta_position_qty),
                "budget_zero_suppressed": composed.budget_zero_suppressed,
                "reason_codes": list(composed.composition_reason_codes),
            },
        }

    @staticmethod
    def _decimal(value: Decimal | float | int | str | None) -> Decimal:
        return to_decimal(value)
