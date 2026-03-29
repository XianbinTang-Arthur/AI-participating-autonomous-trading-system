from __future__ import annotations

from decimal import Decimal
import hashlib

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.common import new_id
from aats.schemas.decision import BaselineAssessment, DecisionContext, PositionTarget
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.market import MarketSnapshot
from aats.schemas.strategy_runtime import (
    PortfolioAllocationDecision,
    SleeveBudgetAssignment,
    SleeveBudgetProfile,
    StrategyCandidate,
    StrategyCoordinatorSnapshot,
    StrategyFamily,
    StrategyLegIntent,
    StrategyRouteAction,
    StrategySleeveIntent,
    StrategySleeveRecord,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, quantize_decimal, to_decimal
from aats.services.runtime_scope import latest_snapshot_for_scope, runtime_state_scope
from aats.services.strategy_engines.allocator import PortfolioAllocatorV2Phase1
from aats.services.strategy_engines.auto_parallel import StrategySleeveAutoController
from aats.services.strategy_engines.base import StrategyEngineInput, StrategyTargetHistory
from aats.services.strategy_engines.dca import DcaStrategyEngine
from aats.services.strategy_engines.smart_arbitrage import (
    SmartArbitrageStrategyEngine,
    _derived_derivatives_symbol,
    _derived_spot_symbol,
    configured_market_symbols,
)
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

    def evaluate(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        directional_target: PositionTarget,
    ) -> StrategyCoordinatorSnapshot:
        self.sleeve_inventory_service.reset()
        engine_input = StrategyEngineInput(
            context=context,
            baseline=baseline,
            directional_target=directional_target,
            latest_snapshot=latest_snapshot_for_scope(self.portfolio_repo, self.state_scope),
            latest_account_snapshot=self._latest_account_snapshot(),
            latest_market_snapshot=self._latest_market_snapshot(context.symbol),
            recent_market_snapshots=self._recent_market_snapshots(
                symbols=self._recent_market_symbols(context.symbol),
            ),
            recent_targets_by_family=self._recent_targets_by_family(symbol=context.symbol),
        )
        candidates_by_family: dict[StrategyFamily, StrategyCandidate] = {
            "directional": self._directional_candidate(directional_target),
            "smart_arbitrage": self.smart_arbitrage_engine.evaluate(engine_input),
            "spot_grid": self.spot_grid_engine.evaluate(engine_input),
            "dca": self.dca_engine.evaluate(engine_input),
        }
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
            for family in ("smart_arbitrage", "spot_grid", "dca", "directional")
            if family != primary_family
        ]
        return StrategyCoordinatorSnapshot(
            decision_id=context.decision_id,
            symbol=context.symbol,
            timeframe=context.timeframe,
            product_type=context.product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=self.settings.expanded_allowed_symbols(),
            active_family=self.settings.strategy_family_active,
            selected_family=primary_family,
            selected_state=selected_candidate.state,
            selected_route_action=allocation_decision.route_action,
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
        allocation = snapshot.allocation_decision
        applied_route_action: StrategyRouteAction = snapshot.selected_route_action
        reason_codes = list(dict.fromkeys(snapshot.selection_reason_codes + list(selected.reason_codes)))
        target_qty = to_decimal(base_target.target_position_qty)
        urgency = base_target.urgency
        source_mix = dict(base_target.source_mix)
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
        position_intent = self._position_intent(
            current_position_qty=base_target.current_position_qty,
            target_position_qty=target_qty,
        )
        decision_outcome = base_target.decision_outcome
        if decision_outcome is not None:
            decision_outcome = decision_outcome.model_copy(
                update={
                    "selected_strategy_family": snapshot.selected_family,
                    "selected_strategy_sleeve_id": strategy_sleeve_id,
                    "selected_strategy_route_action": applied_route_action,
                    "allocation_id": allocation_id,
                    "strategy_selection_reason_codes": list(dict.fromkeys(reason_codes)),
                    "strategy_selection_headline": snapshot.selected_headline,
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
            "urgency": urgency,
            "rebalance_reason": rebalance_reason,
            "source_mix": source_mix,
            "strategy_family": snapshot.selected_family,
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
        for family in ("directional", "smart_arbitrage", "spot_grid", "dca"):
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
                intent = StrategySleeveIntent(
                    decision_id=base_target.decision_id,
                    family=family,
                    strategy_sleeve_id=strategy_sleeve_id,
                    state=candidate.state,
                    symbol=base_target.symbol,
                    product_type=base_target.product_type,
                    margin_mode=base_target.margin_mode,
                    inventory_policy="account_net_inventory",
                    route_action="hold_current"
                    if abs(to_decimal(base_target.delta_position_qty)) <= EPSILON_DECIMAL_12
                    else candidate.route_action,
                    headline=candidate.headline,
                    selectable=candidate.selectable,
                    execution_compatible=candidate.execution_compatible,
                    current_position_qty=to_decimal(base_target.current_position_qty),
                    target_position_qty=to_decimal(base_target.target_position_qty),
                    delta_position_qty=to_decimal(base_target.delta_position_qty),
                    account_current_position_qty=to_decimal(base_target.current_position_qty),
                    account_target_position_qty=to_decimal(base_target.target_position_qty),
                    target_notional=to_decimal(base_target.target_notional),
                    priority_score=candidate.score,
                    reason_codes=list(candidate.reason_codes),
                    pair_id=candidate.pair_id,
                    opportunity_kind=candidate.opportunity_kind,
                    execution_mode=candidate.execution_mode,
                    state_phase=candidate.state_phase,
                    blocking_reasons=list(candidate.blocking_reasons),
                    metrics=dict(candidate.metrics or {}),
                    legs=[
                        leg.model_copy(
                            deep=True,
                            update={
                                "family": family,
                                "strategy_sleeve_id": leg.strategy_sleeve_id or strategy_sleeve_id,
                            },
                        )
                        for leg in (base_target.strategy_execution_legs or [])
                    ],
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

    def _latest_account_snapshot(self) -> ExchangeAccountSnapshot | None:
        if self.account_service is None:
            return None
        getter = getattr(self.account_service, "latest_snapshot", None)
        if not callable(getter):
            return None
        snapshot = getter()
        return snapshot if isinstance(snapshot, ExchangeAccountSnapshot) else None

    def _recent_market_snapshots(self, *, symbols: set[str]) -> dict[str, list[MarketSnapshot]]:
        rows: dict[str, list[MarketSnapshot]] = {}
        limit = max(self.settings.spot_grid_anchor_lookback_snapshots, 1)
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

    def _recent_market_symbols(self, symbol: str) -> set[str]:
        symbols = configured_market_symbols(self.settings, symbol)
        derived_spot = _derived_spot_symbol(symbol)
        derived_derivatives = _derived_derivatives_symbol(symbol)
        if derived_spot:
            symbols.add(derived_spot)
        if derived_derivatives:
            symbols.add(derived_derivatives)
        return symbols

    def _recent_targets_by_family(self, *, symbol: str) -> dict[str, list[StrategyTargetHistory]]:
        rows: dict[str, list[StrategyTargetHistory]] = {
            "directional": [],
            "smart_arbitrage": [],
            "spot_grid": [],
            "dca": [],
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
        if not self.settings.strategy_family_auto_selection_enabled:
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

        priority_order: tuple[StrategyFamily, ...] = ("smart_arbitrage", "spot_grid", "dca", "directional")
        for family in priority_order:
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
        for family in priority_order:
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
