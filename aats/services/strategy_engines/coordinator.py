from __future__ import annotations

from decimal import Decimal
import hashlib

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.common import new_id
from aats.schemas.decision import (
    AIMarketAssessment,
    BaselineAssessment,
    DecisionContext,
    HedgeOverlayDecision,
    PositionTarget,
    StrategyExecutionSummary,
)
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.market import MarketSnapshot
from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    SleeveBudgetAssignment,
    SleeveBudgetProfile,
    StrategyBookRuntimeState,
    StrategyCandidate,
    StrategyCoordinatorSnapshot,
    StrategyFamily,
    StrategyFamilyAction,
    StrategyLegIntent,
    StrategyRouteAction,
    StrategySleeveIntent,
    StrategySleeveRecord,
)
from aats.schemas.execution import position_intent_from_leg_intent
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, quantize_decimal, to_decimal
from aats.services.runtime_scope import latest_snapshot_for_scope, runtime_state_scope
from aats.services.strategy_engines.allocator import PortfolioAllocatorV2Phase1
from aats.services.strategy_engines.auto_parallel import StrategySleeveAutoController
from aats.services.strategy_engines.base import (
    StrategyEngineInput,
    StrategyEvaluationContext,
    StrategyFamilyRuntimeControl,
    StrategyMarketHistoryRequest,
    StrategyTargetHistory,
)
from aats.services.strategy_engines.dca import DcaStrategyEngine
from aats.services.strategy_engines.families import (
    DirectionalFamilyAdapter,
    ExistingCandidateFamilyAdapter,
    IndependentFamilyEngine,
    OpportunisticFamilyEngine,
    ProtectiveFamilyEngine,
    StrategyFamilyRegistry,
)
from aats.services.strategy_engines.smart_arbitrage import SmartArbitrageStrategyEngine
from aats.services.strategy_engines.smart_arbitrage.pair_registry import load_pair_definitions
from aats.services.strategy_engines.spot_grid import SpotGridStrategyEngine
from aats.services.strategy_engines.sleeve_identity import (
    build_strategy_sleeve_id,
    build_strategy_sleeve_record,
    inventory_policy_for_family,
    normalized_symbol_scope,
)
from aats.services.strategy_engines.sleeve_inventory import StrategySleeveInventoryService


class StrategyCoordinatorService:
    _RECENT_TARGET_LOOKBACK = 200
    _ALLOCATABLE_FAMILIES: tuple[StrategyFamily, ...] = (
        "directional",
        "smart_arbitrage",
        "spot_grid",
        "dca",
        "protective",
        "opportunistic",
        "independent",
    )
    _SELECTION_PRIORITY_ORDER: tuple[StrategyFamily, ...] = (
        "smart_arbitrage",
        "spot_grid",
        "dca",
        "directional",
        "protective",
        "opportunistic",
        "independent",
    )

    def __init__(
        self,
        *,
        settings: AATSSettings,
        event_store,
        market_gateway,
        portfolio_repo,
        execution_repo=None,
        position_lot_repo=None,
        account_service=None,
        strategy_sleeve_repo=None,
        strategy_runtime_repo=None,
        reconciliation_repo=None,
        sleeve_pnl_repo=None,
    ) -> None:
        self.settings = settings
        self.event_store = event_store
        self.market_gateway = market_gateway
        self.portfolio_repo = portfolio_repo
        self.execution_repo = execution_repo
        self.position_lot_repo = position_lot_repo
        self.account_service = account_service
        self.strategy_sleeve_repo = strategy_sleeve_repo
        self.strategy_runtime_repo = strategy_runtime_repo
        self.reconciliation_repo = reconciliation_repo
        self.sleeve_pnl_repo = sleeve_pnl_repo
        self.state_scope = runtime_state_scope(settings)
        self.sleeve_inventory_service = StrategySleeveInventoryService(
            execution_repo=execution_repo,
            position_lot_repo=position_lot_repo,
        )
        self.smart_arbitrage_engine = SmartArbitrageStrategyEngine(
            settings=settings,
            market_snapshot_loader=self._latest_market_snapshot,
            account_snapshot_loader=self._latest_account_snapshot,
            sleeve_inventory_loader=self.sleeve_inventory_service,
            account_service=account_service,
        )
        self.spot_grid_engine = SpotGridStrategyEngine(
            settings=settings,
            sleeve_inventory_loader=self.sleeve_inventory_service,
            account_service=account_service,
        )
        self.dca_engine = DcaStrategyEngine(
            settings=settings,
            sleeve_inventory_loader=self.sleeve_inventory_service,
            account_service=account_service,
        )
        self.allocator = PortfolioAllocatorV2Phase1(settings=settings)
        self.auto_controller = StrategySleeveAutoController(
            settings=settings,
            reconciliation_repo=reconciliation_repo,
            sleeve_pnl_repo=sleeve_pnl_repo,
        )
        self.family_registry = StrategyFamilyRegistry()
        self._register_family_engines()

    def _register_family_engines(self) -> None:
        self.family_registry.register(
            DirectionalFamilyAdapter(candidate_loader=self._directional_candidate)
        )
        self.family_registry.register(
            ExistingCandidateFamilyAdapter(
                family_name="smart_arbitrage",
                evaluator=self.smart_arbitrage_engine.evaluate,
            )
        )
        self.family_registry.register(
            ExistingCandidateFamilyAdapter(
                family_name="spot_grid",
                evaluator=self.spot_grid_engine.evaluate,
            )
        )
        self.family_registry.register(
            ExistingCandidateFamilyAdapter(
                family_name="dca",
                evaluator=self.dca_engine.evaluate,
            )
        )
        self.family_registry.register(ProtectiveFamilyEngine(settings=self.settings))
        self.family_registry.register(OpportunisticFamilyEngine(settings=self.settings))
        self.family_registry.register(IndependentFamilyEngine(settings=self.settings))

    def _family_runtime_controls(self) -> dict[StrategyFamily, StrategyFamilyRuntimeControl]:
        return {
            "directional": StrategyFamilyRuntimeControl(
                enabled=True,
                shadow_mode_enabled=False,
                live_execution_enabled=True,
            ),
            "smart_arbitrage": StrategyFamilyRuntimeControl(
                enabled=bool(self.settings.smart_arbitrage_enabled),
                shadow_mode_enabled=False,
                live_execution_enabled=True,
            ),
            "spot_grid": StrategyFamilyRuntimeControl(
                enabled=bool(self.settings.spot_grid_enabled),
                shadow_mode_enabled=False,
                live_execution_enabled=True,
            ),
            "dca": StrategyFamilyRuntimeControl(
                enabled=bool(self.settings.dca_enabled),
                shadow_mode_enabled=False,
                live_execution_enabled=True,
            ),
            "protective": StrategyFamilyRuntimeControl(
                enabled=bool(self.settings.strategy_family_protective_enabled),
                shadow_mode_enabled=bool(self.settings.strategy_family_protective_shadow_mode_enabled),
                live_execution_enabled=bool(self.settings.strategy_family_protective_live_execution_enabled),
            ),
            "opportunistic": StrategyFamilyRuntimeControl(
                enabled=bool(self.settings.strategy_family_opportunistic_enabled),
                shadow_mode_enabled=bool(self.settings.strategy_family_opportunistic_shadow_mode_enabled),
                live_execution_enabled=bool(self.settings.strategy_family_opportunistic_live_execution_enabled),
            ),
            "independent": StrategyFamilyRuntimeControl(
                enabled=bool(self.settings.strategy_family_independent_enabled),
                shadow_mode_enabled=bool(self.settings.strategy_family_independent_shadow_mode_enabled),
                live_execution_enabled=bool(self.settings.strategy_family_independent_live_execution_enabled),
            ),
        }

    def evaluate(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        directional_target: PositionTarget,
        ai_assessment: AIMarketAssessment | None = None,
    ) -> StrategyCoordinatorSnapshot:
        self.sleeve_inventory_service.reset()
        resolved_pair_definitions_by_family = self._resolved_pair_definitions_by_family(
            primary_symbol=context.symbol,
        )
        market_history_requests_by_family = self._market_history_requests(
            primary_symbol=context.symbol,
            resolved_pair_definitions_by_family=resolved_pair_definitions_by_family,
        )
        latest_snapshot = self._latest_portfolio_snapshot()
        latest_account_snapshot = self._latest_account_snapshot()
        latest_snapshots_by_family = self._latest_snapshots_by_family(
            requests=market_history_requests_by_family,
            latest_snapshot=latest_snapshot,
        )
        latest_account_snapshots_by_family = self._latest_account_snapshots_by_family(
            requests=market_history_requests_by_family,
            latest_account_snapshot=latest_account_snapshot,
        )
        latest_market_snapshots_by_symbol_by_family = self._latest_market_snapshots_by_symbol_by_family(
            requests=market_history_requests_by_family,
        )
        latest_market_snapshots_by_family = self._latest_market_snapshots_by_family(
            requests=market_history_requests_by_family,
            latest_market_snapshots_by_symbol_by_family=latest_market_snapshots_by_symbol_by_family,
        )
        latest_market_snapshot = self._latest_market_snapshot(context.symbol)
        recent_market_snapshot_windows_by_family = {
            family: max(int(request.lookback_snapshots), 1)
            for family, request in market_history_requests_by_family.items()
        }
        engine_input = StrategyEngineInput(
            context=context,
            baseline=baseline,
            directional_target=directional_target,
            latest_snapshot=latest_snapshot,
            latest_account_snapshot=latest_account_snapshot,
            latest_market_snapshot=latest_market_snapshot,
            latest_snapshots_by_family=latest_snapshots_by_family,
            latest_account_snapshots_by_family=latest_account_snapshots_by_family,
            resolved_pair_definitions_by_family=resolved_pair_definitions_by_family,
            latest_market_snapshots_by_symbol=(
                {}
                if latest_market_snapshot is None
                else {latest_market_snapshot.symbol: latest_market_snapshot}
            ),
            latest_market_snapshots_by_symbol_by_family=latest_market_snapshots_by_symbol_by_family,
            latest_market_snapshots_by_family=latest_market_snapshots_by_family,
            recent_market_snapshots=self._recent_market_snapshots(
                requests=market_history_requests_by_family,
            ),
            recent_targets_by_family=self._recent_targets_by_family(symbol=context.symbol),
            ai_assessment=ai_assessment,
            recent_market_snapshot_windows_by_family=recent_market_snapshot_windows_by_family,
            market_history_requests_by_family=market_history_requests_by_family,
        )
        evaluation_context = StrategyEvaluationContext.from_engine_input(
            engine_input,
            family_runtime_controls=self._family_runtime_controls(),
        )
        candidate_lists_by_family = self.family_registry.evaluate_all(evaluation_context)
        candidates_by_family = StrategyFamilyRegistry.primary_candidate_map(candidate_lists_by_family)
        sleeve_intents = self._build_sleeve_intents(
            base_target=directional_target,
            candidates_by_family=candidates_by_family,
        )
        controlled_candidates, controlled_intents, automation_decisions = self.auto_controller.apply(
            baseline=baseline,
            candidates_by_family=candidates_by_family,
            sleeve_intents=sleeve_intents,
        )
        budget_profiles, budget_assignments = self._budget_assignments_for_intents(
            base_target=directional_target,
            sleeve_intents=controlled_intents,
        )
        if self.strategy_runtime_repo is not None:
            for profile in budget_profiles:
                self.strategy_runtime_repo.save_budget_profile(profile)
            for assignment in budget_assignments:
                self.strategy_runtime_repo.save_budget_assignment(assignment)
        if self.strategy_runtime_repo is not None:
            for intent in controlled_intents:
                self.strategy_runtime_repo.save_sleeve_intent(intent)
        selected_family, selected_candidate, selection_reasons = self._select_candidate(
            candidates_by_family=controlled_candidates,
        )
        allocation_decision = self.allocator.allocate(
            base_target=directional_target,
            selected_family=selected_family,
            selection_reason_codes=selection_reasons,
            sleeve_intents=controlled_intents,
            budget_assignments=budget_assignments,
        )
        if self.strategy_runtime_repo is not None:
            self.strategy_runtime_repo.save_allocation_decision(allocation_decision)
        primary_family = allocation_decision.primary_family
        selected_candidate = controlled_candidates.get(primary_family, selected_candidate)
        selected_intent = next(
            (intent for intent in controlled_intents if intent.family == primary_family),
            None,
        )
        selected_family_action = (
            selected_candidate.family_action
            if selected_intent is None
            else selected_intent.family_action
        )
        for intent in controlled_intents:
            self._register_sleeve(
                build_strategy_sleeve_record(
                    sleeve_id=intent.strategy_sleeve_id,
                    family=intent.family,
                    primary_symbol=directional_target.symbol,
                    product_scope=directional_target.product_type,
                    margin_scope=directional_target.margin_mode,
                    symbol_scope=self._intent_symbol_scope(intent=intent, base_target=directional_target),
                )
            )

        candidate_order = [primary_family] + [
            family
            for family in self.family_registry.families()
            if family != primary_family and family in controlled_candidates
        ]
        return StrategyCoordinatorSnapshot(
            decision_id=context.decision_id,
            symbol=context.symbol,
            timeframe=context.timeframe,
            product_type=context.product_type,
            margin_mode=directional_target.margin_mode,
            allowed_symbols=self.settings.expanded_allowed_symbols(),
            active_family=self.settings.strategy_family_active,
            selected_family=primary_family,
            selected_state=selected_candidate.state,
            selected_route_action=allocation_decision.route_action,
            selected_family_action=selected_family_action,
            selected_headline=allocation_decision.operator_summary or selected_candidate.headline,
            selection_reason_codes=allocation_decision.reason_codes,
            active_families=allocation_decision.active_families,
            approved_families=allocation_decision.approved_families,
            automation_decisions=automation_decisions,
            candidates=[controlled_candidates[family] for family in candidate_order if family in controlled_candidates],
            sleeve_intents=controlled_intents,
            allocation_decision=allocation_decision,
        )

    def apply_selected_target(
        self,
        *,
        base_target: PositionTarget,
        snapshot: StrategyCoordinatorSnapshot,
        snapshot_ref: str | None = None,
    ) -> PositionTarget:
        selected = next(
            (candidate for candidate in snapshot.candidates if candidate.family == snapshot.selected_family),
            self._directional_candidate(base_target),
        )
        selected_intent = next(
            (intent for intent in snapshot.sleeve_intents if intent.family == snapshot.selected_family),
            None,
        )
        allocation = snapshot.allocation_decision
        applied_route_action: StrategyRouteAction = snapshot.selected_route_action
        reason_codes = list(dict.fromkeys(snapshot.selection_reason_codes + list(selected.reason_codes)))
        target_qty = to_decimal(base_target.target_position_qty)
        urgency = base_target.urgency
        source_mix = dict(base_target.source_mix)
        selected_family_action = (
            selected.family_action
            if selected_intent is None
            else selected_intent.family_action
        )
        strategy_sleeve_id = None if allocation is None else allocation.primary_strategy_sleeve_id
        if strategy_sleeve_id is None:
            symbol_scope = self._symbol_scope(base_target=base_target, selected=selected)
            strategy_sleeve_id = build_strategy_sleeve_id(
                family=snapshot.selected_family,
                primary_symbol=base_target.symbol,
                product_scope=base_target.product_type,
                margin_scope=base_target.margin_mode,
                symbol_scope=symbol_scope,
            )
        allocation_id = new_id("alloc") if allocation is None else allocation.allocation_id
        strategy_bundle_id = None if not base_target.strategy_execution_legs else new_id("bundle")
        strategy_execution_legs = [
            leg.model_copy(
                deep=True,
                update={
                    "allocation_id": leg.allocation_id or allocation_id,
                },
            )
            for leg in (base_target.strategy_execution_legs or [])
        ]

        if allocation is not None:
            target_qty = to_decimal(allocation.target_position_qty)
            if allocation.execution_legs:
                urgency = "high" if any(leg.role == "hedge" for leg in allocation.execution_legs) else selected.urgency
                strategy_bundle_id = new_id("bundle")
                strategy_execution_legs = [
                    leg.model_copy(
                        deep=True,
                        update={
                            "allocation_id": leg.allocation_id or allocation_id,
                        },
                    )
                    for leg in allocation.execution_legs
                ]
            else:
                urgency = selected.urgency if allocation.approved_families else "low"
            source_mix = self._source_mix_for_allocation(allocation)
        elif self._is_protective_target(
            current_qty=base_target.current_position_qty,
            target_qty=base_target.target_position_qty,
        ):
            target_qty = base_target.target_position_qty
            urgency = base_target.urgency
            applied_route_action = "protective_fallback"
            reason_codes.append("strategy_family_protective_fallback_retained")
        elif snapshot.selected_family == "directional" and strategy_execution_legs:
            target_qty = base_target.target_position_qty
            urgency = "high" if any(leg.role == "hedge" for leg in strategy_execution_legs) else base_target.urgency
            reason_codes.append("directional_strategy_execution_legs_retained")
        else:
            target_qty = base_target.current_position_qty
            urgency = "low"
            applied_route_action = "advisory_only" if selected.route_action == "advisory_only" else "hold_current"
            if selected.family != "directional":
                source_mix = {selected.family: 1.0}

        target_exposure_side = self._exposure_side(target_qty)
        position_intent = self._position_intent_for_applied_target(
            selected_family=snapshot.selected_family,
            current_position_qty=base_target.current_position_qty,
            target_position_qty=target_qty,
            strategy_execution_legs=strategy_execution_legs,
        )
        family_execution_summary = self._family_execution_summary(
            selected_family=snapshot.selected_family,
            family_action=selected_family_action,
            route_action=applied_route_action,
            strategy_execution_legs=strategy_execution_legs,
            selected_candidate=selected,
        )
        book_expectancy_summary = (
            None
            if family_execution_summary is None or family_execution_summary.book_expectancy_summary is None
            else family_execution_summary.book_expectancy_summary.model_copy(deep=True)
        )
        book_runtime_states = (
            self._book_runtime_states_for_summary(selected_candidate=selected)
            if family_execution_summary is None or not family_execution_summary.book_runtime_states
            else [
                state.model_copy(deep=True)
                for state in family_execution_summary.book_runtime_states
            ]
        )
        diagnostic_metric_flags = (
            {}
            if family_execution_summary is None
            else dict(family_execution_summary.diagnostic_metric_flags or {})
        )
        if not book_runtime_states and base_target.book_runtime_states:
            book_runtime_states = [
                state.model_copy(deep=True)
                for state in base_target.book_runtime_states
            ]
        if not diagnostic_metric_flags and base_target.diagnostic_metric_flags:
            diagnostic_metric_flags = dict(base_target.diagnostic_metric_flags)
        decision_outcome = base_target.decision_outcome
        overlay_candidate = self._configured_overlay_candidate(snapshot=snapshot)
        hedge_overlay_decision = self._selected_overlay_decision(
            base_target=base_target,
            selected=selected,
            selected_family=snapshot.selected_family,
            applied_route_action=applied_route_action,
            strategy_execution_legs=strategy_execution_legs,
            overlay_candidate=overlay_candidate,
        )
        parent_signal_fields = self._overlay_parent_signal_fields(
            family_execution_summary=family_execution_summary,
            hedge_overlay_decision=hedge_overlay_decision,
        )
        if decision_outcome is not None:
            final_action = decision_outcome.final_action
            final_direction = decision_outcome.final_direction
            if snapshot.selected_family != "directional":
                final_action = self._final_action_for_selected_family(
                    family_action=selected_family_action,
                    route_action=applied_route_action,
                    strategy_execution_legs=strategy_execution_legs,
                )
                final_direction = self._final_direction_for_selected_family(
                    selected=selected,
                    strategy_execution_legs=strategy_execution_legs,
                    target_exposure_side=target_exposure_side,
                    fallback_direction=decision_outcome.final_direction,
                )
            decision_outcome = decision_outcome.model_copy(
                update={
                    "selected_strategy_family": snapshot.selected_family,
                    "selected_strategy_sleeve_id": strategy_sleeve_id,
                    "selected_strategy_route_action": applied_route_action,
                    "selected_strategy_family_action": selected_family_action,
                    "allocation_id": allocation_id,
                    "strategy_selection_reason_codes": list(dict.fromkeys(reason_codes)),
                    "strategy_selection_headline": snapshot.selected_headline,
                    "family_execution_summary": family_execution_summary,
                    "book_expectancy_summary": book_expectancy_summary,
                    "book_runtime_states": book_runtime_states,
                    "diagnostic_metric_flags": diagnostic_metric_flags,
                    **parent_signal_fields,
                    "final_action": final_action,
                    "final_direction": final_direction,
                    "final_target_qty": target_qty,
                }
            )
        rebalance_reason = base_target.rebalance_reason
        if snapshot.selected_family != "directional":
            if applied_route_action == "override_target":
                rebalance_reason = f"{snapshot.selected_family}_strategy"
            elif applied_route_action == "protective_fallback":
                rebalance_reason = f"{snapshot.selected_family}_protective_fallback"
            else:
                rebalance_reason = f"{snapshot.selected_family}_strategy_hold"
        updates = {
            "target_position_qty": target_qty,
            "delta_position_qty": target_qty - base_target.current_position_qty,
            "target_notional": (
                to_decimal(allocation.target_notional)
                if allocation is not None
                and allocation.target_notional is not None
                and abs(to_decimal(allocation.target_notional)) > EPSILON_DECIMAL_12
                else self._target_notional(
                    base_target=base_target,
                    target_qty=target_qty,
                    selected=selected,
                )
            ),
            "target_exposure_side": target_exposure_side,
            "position_intent": position_intent,
            "family_execution_summary": family_execution_summary,
            "book_expectancy_summary": book_expectancy_summary,
            "book_runtime_states": book_runtime_states,
            "diagnostic_metric_flags": diagnostic_metric_flags,
            **parent_signal_fields,
            "urgency": urgency,
            "rebalance_reason": rebalance_reason,
            "source_mix": source_mix,
            "strategy_family": snapshot.selected_family,
            "strategy_family_action": selected_family_action,
            "strategy_sleeve_id": strategy_sleeve_id,
            "strategy_route_action": applied_route_action,
            "strategy_pair_id": selected.pair_id,
            "strategy_opportunity_kind": selected.opportunity_kind,
            "strategy_execution_mode": selected.execution_mode,
            "strategy_state_phase": selected.state_phase,
            "strategy_reason_codes": list(dict.fromkeys(reason_codes)),
            "strategy_blocking_reasons": list(selected.blocking_reasons or []),
            "strategy_headline": snapshot.selected_headline,
            "allocation_id": allocation_id,
            "strategy_bundle_id": strategy_bundle_id,
            "strategy_execution_legs": strategy_execution_legs,
            "hedge_overlay_decision": hedge_overlay_decision,
            "decision_outcome": decision_outcome,
        }
        if snapshot_ref is not None:
            updates["guardrail_flags"] = list(
                dict.fromkeys([*base_target.guardrail_flags, f"strategy_snapshot_ref:{snapshot_ref}"])
            )
        return base_target.model_copy(update=updates)

    def _build_sleeve_intents(
        self,
        *,
        base_target: PositionTarget,
        candidates_by_family: dict[StrategyFamily, StrategyCandidate],
    ) -> list[StrategySleeveIntent]:
        intents: list[StrategySleeveIntent] = []
        overlay_cutover_candidate = self._overlay_cutover_candidate(candidates_by_family)
        overlay_cutover_family = None if overlay_cutover_candidate is None else overlay_cutover_candidate.family
        for family in self._ALLOCATABLE_FAMILIES:
            candidate = candidates_by_family[family]
            symbol_scope = self._symbol_scope(base_target=base_target, selected=candidate)
            strategy_sleeve_id = build_strategy_sleeve_id(
                family=family,
                primary_symbol=base_target.symbol,
                product_scope=base_target.product_type,
                margin_scope=base_target.margin_mode,
                symbol_scope=symbol_scope,
            )
            if family == "directional":
                directional_route_action = (
                    "hold_current"
                    if abs(to_decimal(base_target.delta_position_qty)) <= EPSILON_DECIMAL_12
                    else candidate.route_action
                )
                directional_target_qty = to_decimal(base_target.target_position_qty)
                directional_delta_qty = to_decimal(base_target.delta_position_qty)
                directional_target_notional = to_decimal(base_target.target_notional)
                directional_legs = [
                    leg.model_copy(
                        deep=True,
                        update={
                            "family": family,
                            "strategy_sleeve_id": leg.strategy_sleeve_id or strategy_sleeve_id,
                        },
                    )
                    for leg in (base_target.strategy_execution_legs or [])
                ]
                directional_reason_codes = list(candidate.reason_codes)
                directional_execution_compatible = candidate.execution_compatible
                directional_selectable = candidate.selectable
                if overlay_cutover_family is not None:
                    directional_route_action = "hold_current"
                    directional_target_qty = to_decimal(base_target.current_position_qty)
                    directional_delta_qty = Decimal("0")
                    directional_target_notional = self._target_notional(
                        base_target=base_target,
                        target_qty=directional_target_qty,
                        selected=candidate,
                    )
                    directional_legs = []
                    directional_execution_compatible = False
                    directional_selectable = False
                    directional_reason_codes = list(
                        dict.fromkeys(
                            [
                                *directional_reason_codes,
                                f"directional_shadowed_by_{overlay_cutover_family}_family_cutover",
                            ]
                        )
                    )
                intent = StrategySleeveIntent(
                    decision_id=base_target.decision_id,
                    family=family,
                    strategy_sleeve_id=strategy_sleeve_id,
                    state=candidate.state,
                    symbol=base_target.symbol,
                    product_type=base_target.product_type,
                    margin_mode=base_target.margin_mode,
                    inventory_policy="account_net_inventory",
                    route_action=directional_route_action,
                    family_action="hold_family",
                    headline=candidate.headline,
                    selectable=directional_selectable,
                    execution_compatible=directional_execution_compatible,
                    current_position_qty=to_decimal(base_target.current_position_qty),
                    target_position_qty=directional_target_qty,
                    delta_position_qty=directional_delta_qty,
                    account_current_position_qty=to_decimal(base_target.current_position_qty),
                    account_target_position_qty=directional_target_qty,
                    target_notional=directional_target_notional,
                    priority_score=candidate.score,
                    reason_codes=directional_reason_codes,
                    pair_id=candidate.pair_id,
                    opportunity_kind=candidate.opportunity_kind,
                    execution_mode=candidate.execution_mode,
                    state_phase=candidate.state_phase,
                    blocking_reasons=list(candidate.blocking_reasons),
                    metrics=dict(candidate.metrics or {}),
                    legs=directional_legs,
                )
            else:
                metrics = dict(candidate.metrics or {})
                aggregate_smart_arbitrage = family == "smart_arbitrage" and self._is_aggregate_smart_arbitrage_candidate(candidate)
                current_sleeve_qty = self._metric_decimal(metrics, "current_sleeve_position_qty")
                if current_sleeve_qty is None and family == "smart_arbitrage":
                    current_sleeve_qty = self._metric_decimal(metrics, "current_sleeve_derivatives_qty")
                target_sleeve_qty = self._metric_decimal(metrics, "target_sleeve_position_qty")
                if target_sleeve_qty is None and family == "smart_arbitrage":
                    target_sleeve_qty = self._metric_decimal(metrics, "target_sleeve_derivatives_qty")
                account_current_qty = self._metric_decimal(metrics, "current_account_position_qty")
                if account_current_qty is None and family == "smart_arbitrage":
                    account_current_qty = self._metric_decimal(metrics, "current_account_derivatives_qty")
                account_target_qty = self._metric_decimal(metrics, "target_account_position_qty")
                if account_target_qty is None and family == "smart_arbitrage":
                    account_target_qty = self._metric_decimal(metrics, "target_account_derivatives_qty")
                if family in {"protective", "opportunistic", "independent"}:
                    candidate_target_qty = to_decimal(candidate.target_position_qty or Decimal("0"))
                    candidate_delta_qty = to_decimal(candidate.delta_position_qty or Decimal("0"))
                    candidate_current_qty = candidate_target_qty - candidate_delta_qty
                    if current_sleeve_qty is None:
                        current_sleeve_qty = candidate_current_qty
                    if target_sleeve_qty is None:
                        target_sleeve_qty = candidate_target_qty
                    if account_current_qty is None:
                        account_current_qty = candidate_current_qty
                    if account_target_qty is None:
                        account_target_qty = candidate_target_qty
                leg_current_qty = Decimal("0")
                leg_target_qty = Decimal("0")
                leg_delta_qty = Decimal("0")
                if aggregate_smart_arbitrage:
                    leg_current_qty, leg_target_qty, leg_delta_qty = self._leg_quantities_for_symbol(
                        candidate.legs,
                        symbol=base_target.symbol,
                        product_type=base_target.product_type,
                        margin_mode=base_target.margin_mode,
                    )
                    current_sleeve_qty = leg_current_qty
                    target_sleeve_qty = leg_target_qty
                    account_current_qty = leg_current_qty
                    account_target_qty = leg_target_qty
                intent = StrategySleeveIntent(
                    decision_id=base_target.decision_id,
                    family=family,
                    strategy_sleeve_id=strategy_sleeve_id,
                    state=candidate.state,
                    symbol=str(base_target.symbol if aggregate_smart_arbitrage else (candidate.recommended_symbol or base_target.symbol)),
                    product_type=base_target.product_type,
                    margin_mode=base_target.margin_mode,
                    inventory_policy=inventory_policy_for_family(family),
                    route_action=candidate.route_action,
                    family_action=candidate.family_action,
                    headline=candidate.headline,
                    selectable=candidate.selectable,
                    execution_compatible=candidate.execution_compatible,
                    current_position_qty=current_sleeve_qty or Decimal("0"),
                    target_position_qty=(
                        target_sleeve_qty
                        if target_sleeve_qty is not None
                        else (
                            leg_target_qty
                            if aggregate_smart_arbitrage
                            else to_decimal(candidate.delta_position_qty or Decimal("0")) + (current_sleeve_qty or Decimal("0"))
                        )
                    ),
                    delta_position_qty=leg_delta_qty if aggregate_smart_arbitrage else to_decimal(candidate.delta_position_qty or Decimal("0")),
                    account_current_position_qty=account_current_qty,
                    account_target_position_qty=account_target_qty,
                    target_notional=(
                        None
                        if aggregate_smart_arbitrage or candidate.target_position_qty is None
                        else self._target_notional(
                            base_target=base_target,
                            target_qty=to_decimal(candidate.target_position_qty),
                            selected=candidate,
                        )
                    ),
                    priority_score=candidate.score,
                    reason_codes=list(candidate.reason_codes),
                    pair_id=candidate.pair_id,
                    opportunity_kind=candidate.opportunity_kind,
                    execution_mode=candidate.execution_mode,
                    state_phase=candidate.state_phase,
                    blocking_reasons=list(candidate.blocking_reasons),
                    metrics=metrics,
                    legs=[
                        leg.model_copy(
                            deep=True,
                            update={
                                "family": family,
                                "strategy_sleeve_id": leg.strategy_sleeve_id or strategy_sleeve_id,
                            },
                        )
                        for leg in candidate.legs
                    ],
                )
            intents.append(intent)
        shared_allocation_id = new_id("alloc")
        return [intent.model_copy(update={"allocation_id": shared_allocation_id}) for intent in intents]

    def _directional_candidate(self, target: PositionTarget) -> StrategyCandidate:
        reason_codes = list(
            dict.fromkeys(
                [
                    *(target.strategy_reason_codes or []),
                    "directional_strategy_target",
                ]
            )
        )
        return StrategyCandidate(
            family="directional",
            state="ready",
            enabled=True,
            selectable=True,
            execution_compatible=True,
            route_action="override_target",
            headline=target.strategy_headline or "Use the directional strategy target.",
            recommended_symbol=target.symbol,
            target_position_qty=target.target_position_qty,
            delta_position_qty=target.delta_position_qty,
            score=max(abs(float(target.expected_net_edge_bps)), 0.0),
            confidence=min(0.95, 0.45 + max(target.expected_signal_edge_bps, 0.0) / 100.0),
            urgency=target.urgency,
            reason_codes=reason_codes,
            pair_id=target.strategy_pair_id,
            opportunity_kind=target.strategy_opportunity_kind,
            execution_mode=target.strategy_execution_mode,
            state_phase=target.strategy_state_phase,
            blocking_reasons=list(target.strategy_blocking_reasons or []),
            metrics={
                "expected_signal_edge_bps": target.expected_signal_edge_bps,
                "expected_cost_bps": target.expected_cost_bps,
                "expected_net_edge_bps": target.expected_net_edge_bps,
            },
            legs=[leg.model_copy(deep=True) for leg in (target.strategy_execution_legs or [])],
        )

    def _latest_market_snapshot(self, symbol: str) -> MarketSnapshot | None:
        snapshot = self.market_gateway.latest_snapshot(symbol)
        if snapshot is not None:
            return snapshot
        latest_event = self.event_store.latest(topics.MARKET_SNAPSHOTS, key=symbol)
        if latest_event is None:
            return None
        return MarketSnapshot.model_validate(latest_event.payload)

    def _latest_portfolio_snapshot(self) -> PortfolioSnapshot | None:
        return latest_snapshot_for_scope(self.portfolio_repo, self.state_scope)

    def _latest_snapshots_by_family(
        self,
        *,
        requests: dict[StrategyFamily, StrategyMarketHistoryRequest],
        latest_snapshot: PortfolioSnapshot | None,
    ) -> dict[StrategyFamily, PortfolioSnapshot | None]:
        return {
            family: self._resolve_latest_portfolio_snapshot_request(
                request,
                latest_snapshot=latest_snapshot,
            )
            for family, request in requests.items()
        }

    @staticmethod
    def _resolve_latest_portfolio_snapshot_request(
        request: StrategyMarketHistoryRequest,
        *,
        latest_snapshot: PortfolioSnapshot | None,
    ) -> PortfolioSnapshot | None:
        if request.latest_portfolio_snapshot_source != "runtime_scope_latest":
            return None
        return latest_snapshot

    def _latest_market_snapshots_by_symbol_by_family(
        self,
        *,
        requests: dict[StrategyFamily, StrategyMarketHistoryRequest],
    ) -> dict[StrategyFamily, dict[str, MarketSnapshot]]:
        cache: dict[tuple[str, str | None, str], MarketSnapshot | None] = {}
        results: dict[StrategyFamily, dict[str, MarketSnapshot]] = {}
        for family, request in requests.items():
            resolved: dict[str, MarketSnapshot] = {}
            symbols = self._latest_market_snapshot_symbols_for_request(request)
            for symbol in symbols:
                cache_key = (request.latest_snapshot_source, request.latest_snapshot_topic, symbol)
                if cache_key not in cache:
                    cache[cache_key] = self._resolve_latest_market_snapshot_source(
                        source=request.latest_snapshot_source,
                        symbol=symbol,
                        topic=request.latest_snapshot_topic,
                    )
                snapshot = cache[cache_key]
                if snapshot is not None:
                    resolved[symbol] = snapshot
            results[family] = resolved
        return results

    def _latest_market_snapshots_by_family(
        self,
        *,
        requests: dict[StrategyFamily, StrategyMarketHistoryRequest],
        latest_market_snapshots_by_symbol_by_family: dict[StrategyFamily, dict[str, MarketSnapshot]],
    ) -> dict[StrategyFamily, MarketSnapshot | None]:
        return {
            family: self._resolve_latest_market_snapshot_request(
                request,
                latest_market_snapshots_by_symbol=latest_market_snapshots_by_symbol_by_family.get(family, {}),
            )
            for family, request in requests.items()
        }

    def _resolve_latest_market_snapshot_request(
        self,
        request: StrategyMarketHistoryRequest,
        *,
        latest_market_snapshots_by_symbol: dict[str, MarketSnapshot],
    ) -> MarketSnapshot | None:
        symbol = str(request.latest_snapshot_symbol or "").strip()
        if not symbol:
            symbols = self._latest_market_snapshot_symbols_for_request(request)
            if not symbols:
                return None
            symbol = symbols[0]
        return latest_market_snapshots_by_symbol.get(symbol)

    @staticmethod
    def _latest_market_snapshot_symbols_for_request(
        request: StrategyMarketHistoryRequest,
    ) -> tuple[str, ...]:
        configured_symbols = tuple(
            symbol
            for symbol in request.latest_snapshot_symbols
            if str(symbol).strip()
        )
        if configured_symbols:
            return configured_symbols
        symbol = str(request.latest_snapshot_symbol or "").strip()
        return () if not symbol else (symbol,)

    def _resolve_latest_market_snapshot_source(
        self,
        *,
        source: str,
        symbol: str,
        topic: str | None,
    ) -> MarketSnapshot | None:
        if source == "not_required":
            return None
        if source == "market_gateway_latest":
            return self.market_gateway.latest_snapshot(symbol)
        latest_event = None if topic is None else self.event_store.latest(topic, key=symbol)
        if source == "event_store_latest":
            return None if latest_event is None else MarketSnapshot.model_validate(latest_event.payload)
        snapshot = self.market_gateway.latest_snapshot(symbol)
        if snapshot is not None:
            return snapshot
        return None if latest_event is None else MarketSnapshot.model_validate(latest_event.payload)

    def _latest_account_snapshot(self) -> ExchangeAccountSnapshot | None:
        if self.account_service is None:
            return None
        getter = getattr(self.account_service, "latest_snapshot", None)
        if not callable(getter):
            return None
        snapshot = getter()
        return snapshot if isinstance(snapshot, ExchangeAccountSnapshot) else None

    def _latest_account_snapshots_by_family(
        self,
        *,
        requests: dict[StrategyFamily, StrategyMarketHistoryRequest],
        latest_account_snapshot: ExchangeAccountSnapshot | None,
    ) -> dict[StrategyFamily, ExchangeAccountSnapshot | None]:
        return {
            family: self._resolve_latest_account_snapshot_request(
                request,
                latest_account_snapshot=latest_account_snapshot,
            )
            for family, request in requests.items()
        }

    @staticmethod
    def _resolve_latest_account_snapshot_request(
        request: StrategyMarketHistoryRequest,
        *,
        latest_account_snapshot: ExchangeAccountSnapshot | None,
    ) -> ExchangeAccountSnapshot | None:
        if request.latest_account_snapshot_source != "account_service_latest":
            return None
        return latest_account_snapshot

    def _recent_market_snapshots(
        self,
        *,
        requests: dict[StrategyFamily, StrategyMarketHistoryRequest],
    ) -> dict[str, list[MarketSnapshot]]:
        rows: dict[str, list[MarketSnapshot]] = {}
        event_store_requests = [
            request
            for request in requests.values()
            if request.sampling_source == "event_store_recent"
            and request.topic == topics.MARKET_SNAPSHOTS
        ]
        symbols = {
            symbol
            for request in event_store_requests
            for symbol in request.symbols
            if str(symbol).strip()
        }
        if not symbols:
            return rows
        limit = max((max(int(request.lookback_snapshots), 1) for request in event_store_requests), default=1)
        events = self.event_store.recent_by_topic(
            topics.MARKET_SNAPSHOTS,
            limit=max(limit * max(len(symbols), 1), limit),
        )
        for symbol in symbols:
            symbol_rows = [
                MarketSnapshot.model_validate(item.payload)
                for item in events
                if item.key == symbol
            ]
            rows[symbol] = symbol_rows[-limit:]
        return rows

    def _recent_market_snapshot_windows_by_family(
        self,
        *,
        primary_symbol: str | None = None,
    ) -> dict[StrategyFamily, int]:
        resolved_pair_definitions_by_family = self._resolved_pair_definitions_by_family(
            primary_symbol=self.settings.default_symbol if primary_symbol is None else primary_symbol
        )
        return {
            family: max(int(request.lookback_snapshots), 1)
            for family, request in self._market_history_requests(
                primary_symbol=self.settings.default_symbol if primary_symbol is None else primary_symbol,
                resolved_pair_definitions_by_family=resolved_pair_definitions_by_family,
            ).items()
        }

    def _resolved_pair_definitions_by_family(
        self,
        *,
        primary_symbol: str,
    ) -> dict[StrategyFamily, tuple[object, ...]]:
        return {
            "smart_arbitrage": tuple(
                load_pair_definitions(
                    settings=self.settings,
                    primary_symbol=primary_symbol,
                )
            )
        }

    def _market_history_requests(
        self,
        *,
        primary_symbol: str,
        resolved_pair_definitions_by_family: dict[StrategyFamily, tuple[object, ...]],
    ) -> dict[StrategyFamily, StrategyMarketHistoryRequest]:
        smart_arbitrage_pairs = tuple(resolved_pair_definitions_by_family.get("smart_arbitrage", ()))
        smart_arbitrage_symbols = tuple(
            sorted(
                {
                    str(symbol).upper()
                    for pair in smart_arbitrage_pairs
                    for symbol in (
                        getattr(pair, "spot_symbol", None),
                        getattr(pair, "hedge_symbol", None),
                    )
                    if str(symbol or "").strip()
                }
            )
        )
        return {
            "directional": StrategyMarketHistoryRequest(
                family="directional",
                symbols=(primary_symbol,),
                sampling_source="not_required",
                lookback_snapshots=1,
                latest_snapshot_source="not_required",
                latest_portfolio_snapshot_source="not_required",
                latest_account_snapshot_source="not_required",
            ),
            "smart_arbitrage": StrategyMarketHistoryRequest(
                family="smart_arbitrage",
                symbols=smart_arbitrage_symbols,
                sampling_source="not_required",
                lookback_snapshots=1,
                latest_snapshot_symbols=smart_arbitrage_symbols,
                latest_snapshot_topic=topics.MARKET_SNAPSHOTS,
                latest_snapshot_source="gateway_or_event_store_latest",
                latest_portfolio_snapshot_source="runtime_scope_latest",
                latest_account_snapshot_source="account_service_latest",
            ),
            "spot_grid": StrategyMarketHistoryRequest(
                family="spot_grid",
                symbols=(primary_symbol,),
                topic=topics.MARKET_SNAPSHOTS,
                sampling_source="event_store_recent",
                lookback_snapshots=max(int(self.settings.spot_grid_anchor_lookback_snapshots), 1),
                latest_snapshot_symbols=(primary_symbol,),
                latest_snapshot_symbol=primary_symbol,
                latest_snapshot_topic=topics.MARKET_SNAPSHOTS,
                latest_snapshot_source="gateway_or_event_store_latest",
                latest_portfolio_snapshot_source="not_required",
                latest_account_snapshot_source="not_required",
            ),
            "dca": StrategyMarketHistoryRequest(
                family="dca",
                symbols=(primary_symbol,),
                topic=topics.MARKET_SNAPSHOTS if self.settings.dca_pullback_only_enabled else None,
                sampling_source="event_store_recent" if self.settings.dca_pullback_only_enabled else "not_required",
                lookback_snapshots=2 if self.settings.dca_pullback_only_enabled else 1,
                latest_snapshot_symbols=(primary_symbol,),
                latest_snapshot_symbol=primary_symbol,
                latest_snapshot_topic=topics.MARKET_SNAPSHOTS,
                latest_snapshot_source="gateway_or_event_store_latest",
                latest_portfolio_snapshot_source="not_required",
                latest_account_snapshot_source="not_required",
            ),
            "protective": StrategyMarketHistoryRequest(
                family="protective",
                symbols=(primary_symbol,),
                sampling_source="not_required",
                lookback_snapshots=1,
                latest_snapshot_source="not_required",
                latest_portfolio_snapshot_source="not_required",
                latest_account_snapshot_source="not_required",
            ),
            "opportunistic": StrategyMarketHistoryRequest(
                family="opportunistic",
                symbols=(primary_symbol,),
                sampling_source="not_required",
                lookback_snapshots=1,
                latest_snapshot_source="not_required",
                latest_portfolio_snapshot_source="not_required",
                latest_account_snapshot_source="not_required",
            ),
            "independent": StrategyMarketHistoryRequest(
                family="independent",
                symbols=(primary_symbol,),
                sampling_source="not_required",
                lookback_snapshots=1,
                latest_snapshot_source="not_required",
                latest_portfolio_snapshot_source="not_required",
                latest_account_snapshot_source="not_required",
            ),
        }

    def _recent_targets_by_family(self, *, symbol: str) -> dict[str, list[StrategyTargetHistory]]:
        rows: dict[str, list[StrategyTargetHistory]] = {
            "directional": [],
            "smart_arbitrage": [],
            "spot_grid": [],
            "dca": [],
            "protective": [],
            "opportunistic": [],
            "independent": [],
        }
        for event in reversed(
            self.event_store.recent_by_topic(
                topics.POSITION_TARGETS,
                limit=self._RECENT_TARGET_LOOKBACK,
            )
        ):
            if event.key != symbol:
                continue
            target = PositionTarget.model_validate(event.payload)
            families = {
                str(getattr(target, "strategy_family", "directional") or "directional"),
                *(
                    str(getattr(leg, "family", "") or "")
                    for leg in (target.strategy_execution_legs or [])
                    if str(getattr(leg, "family", "") or "")
                ),
            }
            for family in families:
                if family not in rows or len(rows[family]) >= 10:
                    continue
                rows[family].append(StrategyTargetHistory(created_at=event.event_timestamp, target=target))
        return rows

    def _select_candidate(
        self,
        *,
        candidates_by_family: dict[StrategyFamily, StrategyCandidate],
    ) -> tuple[StrategyFamily, StrategyCandidate, list[str]]:
        configured_family = self.settings.strategy_family_active
        directional = candidates_by_family["directional"]
        overlay_cutover_candidate = self._overlay_cutover_candidate(candidates_by_family)
        if not self.settings.strategy_family_auto_selection_enabled:
            if configured_family == "directional" and overlay_cutover_candidate is not None:
                return (
                    overlay_cutover_candidate.family,
                    overlay_cutover_candidate,
                    [
                        f"strategy_family_{overlay_cutover_candidate.family}_live_cutover",
                        f"legacy_configured_strategy_directional_shadowed_by_{overlay_cutover_candidate.family}_family",
                        *overlay_cutover_candidate.reason_codes,
                    ],
                )
            selected_family = (
                configured_family
                if configured_family in candidates_by_family
                else "directional"
            )
            candidate = candidates_by_family.get(selected_family, directional)
            if candidate.enabled and candidate.selectable and candidate.execution_compatible:
                return (
                    selected_family,
                    candidate,
                    [f"legacy_configured_strategy_family_{selected_family}", *candidate.reason_codes],
                )
            return (
                "directional",
                directional,
                list(
                    dict.fromkeys(
                        [
                            f"legacy_configured_strategy_family_{selected_family}_unavailable",
                            *candidate.reason_codes,
                            "legacy_configured_strategy_directional_fallback",
                        ]
                    )
                ),
            )

        selection_priority_order = list(self._SELECTION_PRIORITY_ORDER)
        if overlay_cutover_candidate is not None and overlay_cutover_candidate.family in selection_priority_order:
            selection_priority_order.remove(overlay_cutover_candidate.family)
            directional_index = selection_priority_order.index("directional")
            selection_priority_order.insert(directional_index, overlay_cutover_candidate.family)
        for family in selection_priority_order:
            candidate = candidates_by_family.get(family)
            if candidate is None:
                continue
            if not candidate.enabled or not candidate.selectable or not candidate.execution_compatible:
                continue
            if candidate.route_action == "override_target":
                return (
                    family,
                    candidate,
                    [f"automatic_strategy_family_{family}", "automatic_strategy_override_target_ready"],
                )
        for family in selection_priority_order:
            candidate = candidates_by_family.get(family)
            if candidate is None:
                continue
            if not candidate.enabled or not candidate.selectable or not candidate.execution_compatible:
                continue
            if candidate.route_action == "hold_current":
                return (
                    family,
                    candidate,
                    [f"automatic_strategy_family_{family}", "automatic_strategy_hold_current_selected"],
                )
        directional = candidates_by_family["directional"]
        return (
            "directional",
            directional,
            ["automatic_strategy_family_directional", "automatic_strategy_directional_fallback"],
        )

    def _overlay_cutover_candidate(
        self,
        candidates_by_family: dict[StrategyFamily, StrategyCandidate],
    ) -> StrategyCandidate | None:
        overlay_mode = str(self.settings.strategy_hedge_overlay_mode or "").strip()
        if overlay_mode not in {"protective", "opportunistic", "independent"}:
            return None
        candidate = candidates_by_family.get(overlay_mode)
        if candidate is None:
            return None
        if not candidate.enabled or not candidate.selectable or not candidate.execution_compatible:
            return None
        if candidate.route_action not in {"override_target", "hold_current"}:
            return None
        return candidate

    def _selected_overlay_decision(
        self,
        *,
        base_target: PositionTarget,
        selected: StrategyCandidate,
        selected_family: StrategyFamily,
        applied_route_action: StrategyRouteAction,
        strategy_execution_legs: list[StrategyLegIntent],
        overlay_candidate: StrategyCandidate | None,
    ) -> HedgeOverlayDecision | None:
        if overlay_candidate is None:
            return base_target.hedge_overlay_decision
        if base_target.hedge_overlay_decision is not None:
            return base_target.hedge_overlay_decision
        metrics = dict(overlay_candidate.metrics or {})
        effective_mode = str(overlay_candidate.family)
        overlay_legs = (
            strategy_execution_legs
            if selected_family == overlay_candidate.family
            else [
                leg.model_copy(deep=True)
                for leg in overlay_candidate.legs
            ]
        )
        overlay_source = {
            "protective": "protective",
            "opportunistic": "opportunistic",
            "independent": "independent_books",
        }[effective_mode]
        active = applied_route_action in {"override_target", "hold_current"} and (
            bool(overlay_legs) or self._overlay_candidate_has_inventory_or_target(overlay_candidate)
        )
        return HedgeOverlayDecision(
            enabled=True,
            runtime_supported=True,
            configured_mode=effective_mode,
            effective_mode=effective_mode,
            overlay_source=overlay_source,
            active=active if selected_family == overlay_candidate.family else self._overlay_candidate_has_inventory_or_target(overlay_candidate),
            state=self._overlay_state_from_selected_candidate(selected=overlay_candidate, active=active),
            main_leg_signal=self._overlay_leg_signal(metrics.get("main_leg_signal")),
            hedge_leg_signal=self._overlay_leg_signal(
                metrics.get("hedge_leg_signal"),
                strategy_execution_legs=overlay_legs,
            ),
            parent_target_signal=self._optional_overlay_signal(metrics.get("parent_target_signal")),
            parent_current_signal=self._optional_overlay_signal(metrics.get("parent_current_signal")),
            parent_effective_signal=self._optional_overlay_signal(metrics.get("parent_effective_signal")),
            signal_source=self._optional_text(metrics.get("parent_exposure_signal_source")),
            main_leg_current_qty=to_decimal(metrics.get("main_leg_current_qty") or Decimal("0")),
            hedge_leg_current_qty=to_decimal(metrics.get("hedge_leg_current_qty") or Decimal("0")),
            main_leg_target_qty=to_decimal(metrics.get("main_leg_target_qty") or Decimal("0")),
            hedge_leg_target_qty=to_decimal(metrics.get("hedge_leg_target_qty") or Decimal("0")),
            hedge_ratio=to_decimal(metrics.get("hedge_ratio") or Decimal("0")),
            max_ratio=to_decimal(metrics.get("max_ratio") or Decimal("0")),
            pressure_score=float(metrics.get("pressure_score") or overlay_candidate.score or 0.0),
            open_threshold=float(metrics.get("open_threshold") or 0.0),
            close_threshold=float(metrics.get("close_threshold") or 0.0),
            open_condition=self._optional_text(metrics.get("open_condition")),
            close_condition=self._optional_text(metrics.get("close_condition")),
            fee_drag_ratio=float(metrics.get("fee_drag_ratio") or 0.0),
            churn_ratio=float(metrics.get("churn_ratio") or 0.0),
            long_leg_score=float(metrics.get("long_leg_score") or 0.0),
            short_leg_score=float(metrics.get("short_leg_score") or 0.0),
            long_leg_reason_codes=list(metrics.get("long_leg_reason_codes") or []),
            short_leg_reason_codes=list(metrics.get("short_leg_reason_codes") or []),
            long_leg_blocked_reasons=list(metrics.get("long_leg_blocked_reasons") or []),
            short_leg_blocked_reasons=list(metrics.get("short_leg_blocked_reasons") or []),
            reason_codes=list(overlay_candidate.reason_codes or []),
            blocked_reasons=list(overlay_candidate.blocking_reasons or []),
            min_hold_remaining_seconds=float(metrics.get("min_hold_remaining_seconds") or 0.0),
            rebalance_cooldown_remaining_seconds=float(metrics.get("rebalance_cooldown_remaining_seconds") or 0.0),
            rollout_stage=metrics.get("rollout_stage"),
            runtime_rollout_stage=metrics.get("runtime_rollout_stage"),
        )

    def _configured_overlay_candidate(self, *, snapshot: StrategyCoordinatorSnapshot) -> StrategyCandidate | None:
        overlay_mode = str(self.settings.strategy_hedge_overlay_mode or "").strip()
        if overlay_mode not in {"protective", "opportunistic", "independent"}:
            return None
        return next((candidate for candidate in snapshot.candidates if candidate.family == overlay_mode), None)

    @staticmethod
    def _overlay_candidate_has_inventory_or_target(selected: StrategyCandidate) -> bool:
        metrics = dict(selected.metrics or {})
        return any(
            abs(to_decimal(value or Decimal("0"))) > EPSILON_DECIMAL_12
            for value in (
                metrics.get("main_leg_current_qty"),
                metrics.get("hedge_leg_current_qty"),
                metrics.get("main_leg_target_qty"),
                metrics.get("hedge_leg_target_qty"),
                selected.target_position_qty,
                selected.delta_position_qty,
            )
        )

    @staticmethod
    def _overlay_state_from_selected_candidate(*, selected: StrategyCandidate, active: bool) -> str:
        candidate_state = str(selected.state_phase or selected.state or "").strip().lower()
        if candidate_state in {"disabled", "inactive", "opening", "holding", "closing", "blocked"}:
            return candidate_state
        if list(selected.blocking_reasons or []):
            return "blocked"
        if active:
            return "holding" if not selected.legs else "opening"
        return "inactive"

    @staticmethod
    def _overlay_leg_signal(
        value: object | None,
        *,
        strategy_execution_legs: list[StrategyLegIntent] | None = None,
    ) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"long", "short", "flat"}:
            return normalized
        for leg in strategy_execution_legs or []:
            pos_side = str(getattr(leg, "pos_side", "") or "").strip().lower()
            if pos_side in {"long", "short"}:
                return pos_side
        return "flat"

    @staticmethod
    def _optional_overlay_signal(value: object | None) -> str | None:
        normalized = str(value or "").strip().lower()
        if normalized in {"long", "short", "flat"}:
            return normalized
        return None

    @staticmethod
    def _optional_text(value: object | None) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _overlay_parent_signal_fields(
        *,
        family_execution_summary: StrategyExecutionSummary | None,
        hedge_overlay_decision: HedgeOverlayDecision | None,
    ) -> dict[str, str | None]:
        return {
            "parent_target_signal": (
                None
                if family_execution_summary is None
                else family_execution_summary.parent_target_signal
            ) or (
                None if hedge_overlay_decision is None else hedge_overlay_decision.parent_target_signal
            ),
            "parent_current_signal": (
                None
                if family_execution_summary is None
                else family_execution_summary.parent_current_signal
            ) or (
                None if hedge_overlay_decision is None else hedge_overlay_decision.parent_current_signal
            ),
            "parent_effective_signal": (
                None
                if family_execution_summary is None
                else family_execution_summary.parent_effective_signal
            ) or (
                None if hedge_overlay_decision is None else hedge_overlay_decision.parent_effective_signal
            ),
            "signal_source": (
                None
                if family_execution_summary is None
                else family_execution_summary.signal_source
            ) or (
                None if hedge_overlay_decision is None else hedge_overlay_decision.signal_source
            ),
        }

    @staticmethod
    def _final_action_for_selected_family(
        *,
        family_action: StrategyFamilyAction,
        route_action: StrategyRouteAction,
        strategy_execution_legs: list[StrategyLegIntent],
    ) -> str:
        if route_action == "advisory_only":
            return "hold"
        leg_action = StrategyCoordinatorService._final_action_from_legs(strategy_execution_legs)
        if leg_action is not None:
            return leg_action
        action_map = {
            "hold_family": "hold",
            "blocked": "hold",
            "protect": "enter",
            "rebalance_protection": "scale_in",
            "close_protection_leg": "exit",
            "open_opportunity_leg": "enter",
            "close_opportunity_leg": "exit",
            "open_independent_book": "enter",
            "scale_independent_book": "scale_in",
            "rebalance_independent_books": "scale_in",
            "de_risk_independent_book": "reduce",
            "close_failed_thesis_independent_book": "exit",
            "close_stale_thesis_independent_book": "exit",
            "close_independent_book": "exit",
        }
        return action_map.get(family_action, "hold")

    @staticmethod
    def _final_action_from_legs(strategy_execution_legs: list[StrategyLegIntent]) -> str | None:
        actionable_legs = [
            leg
            for leg in strategy_execution_legs
            if abs(to_decimal(leg.delta_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12
        ]
        if not actionable_legs:
            return None
        opening_legs = [leg for leg in actionable_legs if str(leg.action or "").lower() == "open"]
        if opening_legs:
            if any(abs(to_decimal(leg.current_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12 for leg in opening_legs):
                return "scale_in"
            return "enter"
        if any(str(leg.action or "").lower() == "reduce" for leg in actionable_legs):
            return "reduce"
        if any(str(leg.action or "").lower() == "close" for leg in actionable_legs):
            return "exit"
        return None

    @staticmethod
    def _final_direction_for_selected_family(
        *,
        selected: StrategyCandidate,
        strategy_execution_legs: list[StrategyLegIntent],
        target_exposure_side: str,
        fallback_direction: str | None,
    ) -> str | None:
        executing_pos_sides = {
            str(getattr(leg, "pos_side", "") or "").strip().lower()
            for leg in strategy_execution_legs
            if abs(to_decimal(leg.delta_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12
            and str(getattr(leg, "pos_side", "") or "").strip().lower() in {"long", "short"}
        }
        if len(executing_pos_sides) == 1:
            return next(iter(executing_pos_sides))
        for leg in strategy_execution_legs:
            leg_pos_side = str(getattr(leg, "pos_side", "") or "").strip().lower()
            if leg_pos_side in {"long", "short"}:
                return leg_pos_side
        main_leg_signal = str((selected.metrics or {}).get("main_leg_signal") or "").strip().lower()
        if main_leg_signal in {"long", "short", "flat"}:
            return main_leg_signal
        if str(target_exposure_side or "").strip():
            return target_exposure_side
        return fallback_direction

    @staticmethod
    def _exposure_side(quantity: Decimal) -> str:
        if quantity > EPSILON_DECIMAL_12:
            return "long"
        if quantity < -EPSILON_DECIMAL_12:
            return "short"
        return "flat"

    def _position_intent(self, *, current_position_qty: Decimal, target_position_qty: Decimal) -> str:
        current_qty = to_decimal(current_position_qty)
        target_qty = to_decimal(target_position_qty)
        if current_qty > EPSILON_DECIMAL_12:
            if target_qty > current_qty:
                return "scale_in_long"
            if target_qty > EPSILON_DECIMAL_12:
                return "reduce_long"
            if target_qty < -EPSILON_DECIMAL_12:
                return "reverse_to_short"
            return "close_long"
        if current_qty < -EPSILON_DECIMAL_12:
            if target_qty < current_qty:
                return "scale_in_short"
            if target_qty < -EPSILON_DECIMAL_12:
                return "reduce_short"
            if target_qty > EPSILON_DECIMAL_12:
                return "reverse_to_long"
            return "close_short"
        if target_qty > EPSILON_DECIMAL_12:
            return "open_long"
        if target_qty < -EPSILON_DECIMAL_12:
            return "open_short"
        return "hold"

    def _position_intent_for_applied_target(
        self,
        *,
        selected_family: StrategyFamily,
        current_position_qty: Decimal,
        target_position_qty: Decimal,
        strategy_execution_legs: list[StrategyLegIntent],
    ) -> str:
        if selected_family != "directional":
            leg_intent = self._position_intent_from_legs(strategy_execution_legs)
            if leg_intent is not None:
                return leg_intent
        return self._position_intent(
            current_position_qty=current_position_qty,
            target_position_qty=target_position_qty,
        )

    @staticmethod
    def _position_intent_from_legs(
        strategy_execution_legs: list[StrategyLegIntent],
    ) -> str | None:
        derived = StrategyCoordinatorService._derived_leg_position_intents(strategy_execution_legs)
        if derived is None:
            return None
        unique = list(dict.fromkeys(derived))
        if len(unique) == 1:
            return unique[0]
        if len(derived) == 1:
            return derived[0]
        return None

    @staticmethod
    def _derived_leg_position_intents(
        strategy_execution_legs: list[StrategyLegIntent],
    ) -> list[str] | None:
        actionable_legs = [
            leg
            for leg in strategy_execution_legs
            if abs(to_decimal(leg.delta_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12
        ]
        if not actionable_legs:
            return []
        derived: list[str] = []
        for leg in actionable_legs:
            side = str(getattr(leg, "side", "") or "").strip().lower()
            pos_side = getattr(leg, "pos_side", None)
            action = getattr(leg, "action", None)
            position_mode = getattr(leg, "position_mode", None)
            if side not in {"buy", "sell"} or pos_side not in {"long", "short"} or action not in {"open", "reduce", "close"}:
                return None
            intent = position_intent_from_leg_intent(
                side=side,  # type: ignore[arg-type]
                pos_side=pos_side,
                action=action,
                position_mode=position_mode,
            )
            if action == "open" and abs(to_decimal(leg.current_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12:
                intent = intent.replace("open_", "scale_in_", 1)
            derived.append(intent)
        return derived

    @staticmethod
    def _family_execution_summary(
        *,
        selected_family: StrategyFamily,
        family_action: StrategyFamilyAction,
        route_action: StrategyRouteAction,
        strategy_execution_legs: list[StrategyLegIntent],
        selected_candidate: StrategyCandidate | None = None,
    ) -> StrategyExecutionSummary | None:
        actionable_legs = [
            leg
            for leg in strategy_execution_legs
            if abs(to_decimal(leg.delta_position_qty or Decimal("0"))) > EPSILON_DECIMAL_12
        ]
        if not actionable_legs:
            return None
        derived_intents = StrategyCoordinatorService._derived_leg_position_intents(actionable_legs) or []
        directions = list(
            dict.fromkeys(
                str(getattr(leg, "pos_side", "") or "").strip().lower()
                for leg in actionable_legs
                if str(getattr(leg, "pos_side", "") or "").strip().lower() in {"long", "short", "flat"}
            )
        )
        leg_actions = list(
            dict.fromkeys(
                str(getattr(leg, "action", "") or "").strip().lower()
                for leg in actionable_legs
                if str(getattr(leg, "action", "") or "").strip().lower()
            )
        )
        execution_modes = list(
            dict.fromkeys(
                str(getattr(leg, "execution_mode", "") or "").strip()
                for leg in actionable_legs
                if str(getattr(leg, "execution_mode", "") or "").strip()
            )
        )
        metrics = dict(selected_candidate.metrics or {}) if selected_candidate is not None else {}
        return StrategyExecutionSummary(
            summary_mode="single_leg" if len(actionable_legs) == 1 and len(derived_intents) == 1 else "multi_leg",
            family=selected_family,
            route_action=route_action,
            family_action=family_action,
            leg_count=len(actionable_legs),
            position_intents=list(dict.fromkeys(derived_intents)),
            directions=directions,
            leg_actions=leg_actions,
            execution_modes=execution_modes,
            parent_target_signal=StrategyCoordinatorService._optional_overlay_signal(metrics.get("parent_target_signal")),
            parent_current_signal=StrategyCoordinatorService._optional_overlay_signal(metrics.get("parent_current_signal")),
            parent_effective_signal=StrategyCoordinatorService._optional_overlay_signal(metrics.get("parent_effective_signal")),
            signal_source=StrategyCoordinatorService._optional_text(metrics.get("parent_exposure_signal_source")),
            close_reason=StrategyCoordinatorService._optional_text(metrics.get("close_reason")),
            book_expectancy_summary=(
                None
                if selected_candidate is None or selected_candidate.book_expectancy_summary is None
                else selected_candidate.book_expectancy_summary.model_copy(deep=True)
            ),
            book_runtime_states=StrategyCoordinatorService._book_runtime_states_for_summary(
                selected_candidate=selected_candidate,
                metrics=metrics,
            ),
            diagnostic_metric_flags=StrategyCoordinatorService._diagnostic_metric_flags_from_metrics(metrics),
        )

    @staticmethod
    def _diagnostic_metric_flags_from_metrics(metrics: dict[str, object] | None) -> dict[str, bool]:
        if not metrics:
            return {}
        normalized: dict[str, bool] = {}
        for key in (
            "emit_book_level_metrics",
            "emit_expected_vs_realized_metrics",
            "emit_close_reason_metrics",
            "emit_execution_policy_metrics",
        ):
            value = metrics.get(key)
            if value is None:
                continue
            normalized[key] = bool(value)
        return normalized

    @staticmethod
    def _book_runtime_states_for_summary(
        *,
        selected_candidate: StrategyCandidate | None,
        metrics: dict[str, object] | None = None,
    ) -> list[StrategyBookRuntimeState]:
        if selected_candidate is not None and selected_candidate.book_runtime_states:
            return [
                state.model_copy(deep=True)
                for state in selected_candidate.book_runtime_states
            ]
        raw_states = []
        if metrics is not None:
            raw_states = list(metrics.get("book_runtime_states") or [])
        normalized: list[StrategyBookRuntimeState] = []
        for item in raw_states:
            if isinstance(item, StrategyBookRuntimeState):
                normalized.append(item.model_copy(deep=True))
            elif isinstance(item, dict):
                normalized.append(StrategyBookRuntimeState.model_validate(item))
        return normalized

    def _is_protective_target(self, *, current_qty: Decimal, target_qty: Decimal) -> bool:
        current_side = self._exposure_side(to_decimal(current_qty))
        target_side = self._exposure_side(to_decimal(target_qty))
        if current_side == "flat":
            return False
        if target_side == "flat":
            return True
        if current_side != target_side:
            return False
        return abs(to_decimal(target_qty)) + EPSILON_DECIMAL_12 < abs(to_decimal(current_qty))

    def _target_notional(
        self,
        *,
        base_target: PositionTarget,
        target_qty: Decimal,
        selected: StrategyCandidate,
    ) -> Decimal:
        reference_price = self._reference_price(base_target=base_target, selected=selected)
        if reference_price <= EPSILON_DECIMAL_12:
            return Decimal("0")
        return abs(to_decimal(target_qty)) * reference_price

    def _reference_price(self, *, base_target: PositionTarget, selected: StrategyCandidate) -> Decimal:
        metrics = selected.metrics or {}
        for key in ("current_price", "spot_price", "derivatives_price"):
            if key in metrics:
                price = to_decimal(metrics[key])
                if price > EPSILON_DECIMAL_12:
                    return abs(price)
        if abs(base_target.target_position_qty) > EPSILON_DECIMAL_12 and abs(base_target.target_notional) > EPSILON_DECIMAL_12:
            return abs(to_decimal(base_target.target_notional) / to_decimal(base_target.target_position_qty))
        if abs(base_target.current_position_qty) > EPSILON_DECIMAL_12 and abs(base_target.current_notional) > EPSILON_DECIMAL_12:
            return abs(to_decimal(base_target.current_notional) / to_decimal(base_target.current_position_qty))
        latest_market = self._latest_market_snapshot(base_target.symbol)
        if latest_market is not None:
            latest_price = to_decimal(latest_market.last_price)
            if latest_price > EPSILON_DECIMAL_12:
                return latest_price
        return Decimal("0")

    @staticmethod
    def _symbol_scope(*, base_target: PositionTarget, selected: StrategyCandidate) -> tuple[str, ...]:
        return normalized_symbol_scope(
            base_target.symbol,
            *(leg.symbol for leg in selected.legs if str(leg.symbol).strip()),
        )

    def _register_sleeve(self, sleeve: StrategySleeveRecord) -> None:
        if self.strategy_sleeve_repo is None:
            return
        existing = self.strategy_sleeve_repo.get_sleeve(sleeve.sleeve_id)
        if existing is None:
            self.strategy_sleeve_repo.save_sleeve(sleeve)
            return
        merged = existing.model_copy(
            update={
                "family": sleeve.family,
                "name": sleeve.name,
                "product_scope": sleeve.product_scope,
                "margin_scope": sleeve.margin_scope,
                "symbol_scope": sleeve.symbol_scope,
                "automatic_enabled": sleeve.automatic_enabled,
                "inventory_policy": sleeve.inventory_policy,
                "status": sleeve.status,
                "metadata": {
                    **existing.metadata,
                    **sleeve.metadata,
                },
                "updated_at": sleeve.updated_at,
            }
        )
        self.strategy_sleeve_repo.save_sleeve(merged)

    @staticmethod
    def _metric_decimal(metrics: dict[str, object], key: str) -> Decimal | None:
        value = metrics.get(key)
        if value is None:
            return None
        return to_decimal(value)

    @staticmethod
    def _is_aggregate_smart_arbitrage_candidate(candidate: StrategyCandidate) -> bool:
        return candidate.family == "smart_arbitrage" and (
            str(candidate.pair_id or "") == "multi_pair" or bool((candidate.metrics or {}).get("aggregate_candidate"))
        )

    @staticmethod
    def _smart_arbitrage_pair_count(intent: StrategySleeveIntent) -> int:
        pair_ids = {
            str(leg.pair_id or "").strip()
            for leg in intent.legs
            if str(leg.pair_id or "").strip()
        }
        if pair_ids:
            return max(len(pair_ids), 1)
        metrics = intent.metrics or {}
        summaries = metrics.get("selected_pair_summaries")
        if isinstance(summaries, list) and summaries:
            return len(summaries)
        try:
            return max(int(metrics.get("pair_count_selected") or 1), 1)
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _leg_quantities_for_symbol(
        legs: list[StrategyLegIntent],
        *,
        symbol: str,
        product_type: str,
        margin_mode: str,
    ) -> tuple[Decimal, Decimal, Decimal]:
        current_qty = Decimal("0")
        target_qty = Decimal("0")
        delta_qty = Decimal("0")
        for leg in legs:
            if (
                str(leg.symbol or "").upper() != str(symbol or "").upper()
                or str(leg.product_type) != str(product_type)
                or str(leg.margin_mode) != str(margin_mode)
            ):
                continue
            leg_current_qty = to_decimal(leg.current_position_qty or Decimal("0"))
            leg_target_qty = (
                to_decimal(leg.target_position_qty)
                if leg.target_position_qty is not None
                else leg_current_qty + to_decimal(leg.delta_position_qty or Decimal("0"))
            )
            leg_delta_qty = (
                to_decimal(leg.delta_position_qty)
                if leg.delta_position_qty is not None
                else leg_target_qty - leg_current_qty
            )
            current_qty += leg_current_qty
            target_qty += leg_target_qty
            delta_qty += leg_delta_qty
        return current_qty, target_qty, delta_qty

    @staticmethod
    def _source_mix_for_allocation(allocation: PortfolioAllocationDecision) -> dict[str, float]:
        if allocation.approved_sleeve_weights:
            weights_by_family: dict[str, Decimal] = {}
            for intent in allocation.sleeve_intents:
                sleeve_weight = allocation.approved_sleeve_weights.get(intent.strategy_sleeve_id)
                if sleeve_weight is None:
                    continue
                weights_by_family.setdefault(intent.family, Decimal("0"))
                weights_by_family[intent.family] += to_decimal(sleeve_weight)
            if weights_by_family:
                return {family: float(weight) for family, weight in weights_by_family.items()}
        families = list(dict.fromkeys(allocation.approved_families))
        if not families:
            return {}
        weight = 1.0 / float(len(families))
        return {family: weight for family in families}

    @staticmethod
    def _intent_symbol_scope(*, intent: StrategySleeveIntent, base_target: PositionTarget) -> tuple[str, ...]:
        return normalized_symbol_scope(
            base_target.symbol,
            intent.symbol,
            *(leg.symbol for leg in intent.legs if str(leg.symbol).strip()),
        )

    def _budget_assignments_for_intents(
        self,
        *,
        base_target: PositionTarget,
        sleeve_intents: list[StrategySleeveIntent],
    ) -> tuple[list[SleeveBudgetProfile], list[SleeveBudgetAssignment]]:
        profiles: list[SleeveBudgetProfile] = []
        assignments: list[SleeveBudgetAssignment] = []
        for intent in sleeve_intents:
            profile = self._budget_profile_for_intent(base_target=base_target, intent=intent)
            assignment = self._budget_assignment_for_intent(intent=intent, profile=profile)
            profiles.append(profile)
            assignments.append(assignment)
        return profiles, assignments

    def _budget_profile_for_intent(
        self,
        *,
        base_target: PositionTarget,
        intent: StrategySleeveIntent,
    ) -> SleeveBudgetProfile:
        family = intent.family
        max_notional = Decimal(str(self.settings.max_notional_per_symbol))
        if family == "smart_arbitrage":
            pair_count = Decimal(str(self._smart_arbitrage_pair_count(intent)))
            quote_budget_limit = Decimal(str(self.settings.smart_arbitrage_quote_budget_per_trade)) * pair_count
            notional_cap = Decimal(str(self.settings.smart_arbitrage_max_pair_notional)) * pair_count
            allocator_base_weight = Decimal("1.15")
            hedge_priority_class = "critical_hedge"
        elif family == "spot_grid":
            quote_budget_limit = quantize_decimal(max_notional * Decimal("0.75"))
            notional_cap = quantize_decimal(max_notional * Decimal("0.75"))
            allocator_base_weight = Decimal("0.90")
            hedge_priority_class = "inventory"
        elif family == "dca":
            quote_budget_limit = Decimal(str(self.settings.dca_quote_budget_per_cycle)) * Decimal("4")
            notional_cap = quote_budget_limit
            allocator_base_weight = Decimal("0.80")
            hedge_priority_class = "inventory"
        else:
            quote_budget_limit = max_notional
            notional_cap = max_notional
            allocator_base_weight = Decimal("1.00")
            hedge_priority_class = "standard"
        margin_budget_limit = None
        if base_target.product_type == "derivatives":
            leverage = max(Decimal(str(base_target.target_leverage or 1.0)), Decimal("1"))
            margin_budget_limit = quantize_decimal(notional_cap / leverage)
        symbol_scope = self._intent_symbol_scope(intent=intent, base_target=base_target)
        now = intent.created_at
        return SleeveBudgetProfile(
            budget_profile_id=self._budget_profile_id(
                family=family,
                product_type=base_target.product_type,
                margin_mode=base_target.margin_mode,
                symbol_scope=symbol_scope,
            ),
            family=family,
            product_type=base_target.product_type,
            margin_mode=base_target.margin_mode,
            symbol_scope=symbol_scope,
            quote_budget_limit=quantize_decimal(quote_budget_limit),
            margin_budget_limit=margin_budget_limit,
            notional_cap=quantize_decimal(notional_cap),
            max_symbol_notional=quantize_decimal(notional_cap),
            max_drawdown_usdt=Decimal(str(self.settings.strategy_sleeve_auto_hard_loss_usdt)),
            allocator_base_weight=allocator_base_weight,
            hedge_priority_class=hedge_priority_class,
            metadata={
                "source": "task74_allocator_v2_phase2",
                "default_symbol": base_target.symbol,
                "pair_id": intent.pair_id,
                "pair_count": self._smart_arbitrage_pair_count(intent) if family == "smart_arbitrage" else 1,
                "opportunity_kind": intent.opportunity_kind,
                "execution_mode": intent.execution_mode,
                "state_phase": intent.state_phase,
            },
            created_at=now,
            updated_at=now,
        )

    def _budget_assignment_for_intent(
        self,
        *,
        intent: StrategySleeveIntent,
        profile: SleeveBudgetProfile,
    ) -> SleeveBudgetAssignment:
        multiplier = to_decimal(intent.budget_multiplier)
        def _scaled(value: Decimal | None) -> Decimal | None:
            if value is None:
                return None
            return quantize_decimal(value * multiplier)
        now = intent.created_at
        return SleeveBudgetAssignment(
            assignment_id=self._budget_assignment_id(
                strategy_sleeve_id=intent.strategy_sleeve_id,
                budget_profile_id=profile.budget_profile_id,
                symbol=intent.symbol,
            ),
            budget_profile_id=profile.budget_profile_id,
            strategy_sleeve_id=intent.strategy_sleeve_id,
            family=intent.family,
            symbol=intent.symbol,
            product_type=intent.product_type,
            margin_mode=intent.margin_mode,
            active_budget_multiplier=multiplier,
            allocator_base_weight=profile.allocator_base_weight,
            effective_quote_budget_limit=_scaled(profile.quote_budget_limit),
            effective_margin_budget_limit=_scaled(profile.margin_budget_limit),
            effective_notional_cap=_scaled(profile.notional_cap),
            effective_max_symbol_notional=_scaled(profile.max_symbol_notional),
            hedge_priority_class=profile.hedge_priority_class,
            reason_codes=list(dict.fromkeys([*intent.control_reason_codes, "allocator_budget_assignment_active"])),
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _budget_profile_id(
        *,
        family: StrategyFamily,
        product_type: str,
        margin_mode: str,
        symbol_scope: tuple[str, ...],
    ) -> str:
        scope_text = "|".join(symbol_scope)
        digest = hashlib.sha1(scope_text.encode("utf-8")).hexdigest()[:12]
        return f"budget:{family}:{product_type}:{margin_mode}:{digest}"

    @staticmethod
    def _budget_assignment_id(
        *,
        strategy_sleeve_id: str,
        budget_profile_id: str,
        symbol: str,
    ) -> str:
        digest = hashlib.sha1(f"{strategy_sleeve_id}|{budget_profile_id}|{symbol}".encode("utf-8")).hexdigest()[:16]
        return f"budgetassign:{digest}"
