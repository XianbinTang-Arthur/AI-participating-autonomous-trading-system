from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.events import topics
from aats.schemas.decision import BaselineAssessment, DecisionContext, PositionTarget
from aats.schemas.market import MarketSnapshot
from aats.schemas.strategy_runtime import (
    StrategyCandidate,
    StrategyCoordinatorSnapshot,
    StrategyFamily,
    StrategyRouteAction,
)
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.runtime_scope import latest_snapshot_for_scope, runtime_state_scope
from aats.services.strategy_engines.base import StrategyEngineInput, StrategyTargetHistory
from aats.services.strategy_engines.dca import DcaStrategyEngine
from aats.services.strategy_engines.smart_arbitrage import (
    SmartArbitrageStrategyEngine,
    _derived_derivatives_symbol,
    _derived_spot_symbol,
)
from aats.services.strategy_engines.spot_grid import SpotGridStrategyEngine


class StrategyCoordinatorService:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        event_store,
        market_gateway,
        portfolio_repo,
    ) -> None:
        self.settings = settings
        self.event_store = event_store
        self.market_gateway = market_gateway
        self.portfolio_repo = portfolio_repo
        self.state_scope = runtime_state_scope(settings)
        self.smart_arbitrage_engine = SmartArbitrageStrategyEngine(
            settings=settings,
            market_snapshot_loader=self._latest_market_snapshot,
        )
        self.spot_grid_engine = SpotGridStrategyEngine(settings=settings)
        self.dca_engine = DcaStrategyEngine(settings=settings)

    def evaluate(
        self,
        *,
        context: DecisionContext,
        baseline: BaselineAssessment,
        directional_target: PositionTarget,
    ) -> StrategyCoordinatorSnapshot:
        engine_input = StrategyEngineInput(
            context=context,
            baseline=baseline,
            directional_target=directional_target,
            latest_snapshot=latest_snapshot_for_scope(self.portfolio_repo, self.state_scope),
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
        selected_family: StrategyFamily = self.settings.strategy_family_active
        selected_candidate = candidates_by_family.get(selected_family, candidates_by_family["directional"])
        selection_reasons = [f"active_strategy_family_{selected_family}"]
        if selected_family not in candidates_by_family:
            selected_family = "directional"
            selected_candidate = candidates_by_family["directional"]
            selection_reasons.append("strategy_family_unknown_fallback_directional")
        elif selected_family == "directional":
            selection_reasons.append("directional_family_selected")
        elif selected_candidate.route_action == "override_target" and selected_candidate.selectable:
            selection_reasons.append("non_directional_family_ready")
        elif selected_candidate.route_action == "advisory_only":
            selection_reasons.append("non_directional_family_advisory_only")
        else:
            selection_reasons.append("non_directional_family_hold_current")

        candidate_order = [selected_family] + [
            family
            for family in ("smart_arbitrage", "spot_grid", "dca", "directional")
            if family != selected_family
        ]
        return StrategyCoordinatorSnapshot(
            decision_id=context.decision_id,
            symbol=context.symbol,
            timeframe=context.timeframe,
            product_type=context.product_type,
            margin_mode=self.settings.margin_mode,
            allowed_symbols=tuple(self.settings.allowed_symbols),
            active_family=self.settings.strategy_family_active,
            selected_family=selected_family,
            selected_state=selected_candidate.state,
            selected_route_action=selected_candidate.route_action,
            selected_headline=selected_candidate.headline,
            selection_reason_codes=selection_reasons + list(selected_candidate.reason_codes),
            candidates=[candidates_by_family[family] for family in candidate_order if family in candidates_by_family],
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
        applied_route_action: StrategyRouteAction = snapshot.selected_route_action
        reason_codes = list(dict.fromkeys(snapshot.selection_reason_codes + list(selected.reason_codes)))
        target_qty = base_target.target_position_qty
        urgency = base_target.urgency
        source_mix = dict(base_target.source_mix)

        if (
            selected.route_action == "override_target"
            and selected.execution_compatible
            and selected.selectable
            and selected.target_position_qty is not None
        ):
            target_qty = to_decimal(selected.target_position_qty)
            urgency = selected.urgency
            if selected.family != "directional":
                source_mix = {selected.family: 1.0}
        elif self._is_protective_target(
            current_qty=base_target.current_position_qty,
            target_qty=base_target.target_position_qty,
        ):
            target_qty = base_target.target_position_qty
            urgency = base_target.urgency
            applied_route_action = "protective_fallback"
            reason_codes.append("strategy_family_protective_fallback_retained")
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
                    "selected_strategy_route_action": applied_route_action,
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
            "target_notional": self._target_notional(
                base_target=base_target,
                target_qty=target_qty,
                selected=selected,
            ),
            "target_exposure_side": target_exposure_side,
            "position_intent": position_intent,
            "urgency": urgency,
            "rebalance_reason": rebalance_reason,
            "source_mix": source_mix,
            "strategy_family": snapshot.selected_family,
            "strategy_route_action": applied_route_action,
            "strategy_reason_codes": list(dict.fromkeys(reason_codes)),
            "strategy_headline": snapshot.selected_headline,
            "decision_outcome": decision_outcome,
        }
        if snapshot_ref is not None:
            updates["guardrail_flags"] = list(
                dict.fromkeys([*base_target.guardrail_flags, f"strategy_snapshot_ref:{snapshot_ref}"])
            )
        return base_target.model_copy(update=updates)

    def _directional_candidate(self, target: PositionTarget) -> StrategyCandidate:
        return StrategyCandidate(
            family="directional",
            state="ready",
            enabled=True,
            selectable=True,
            execution_compatible=True,
            route_action="override_target",
            headline="Use the directional strategy target.",
            recommended_symbol=target.symbol,
            target_position_qty=target.target_position_qty,
            delta_position_qty=target.delta_position_qty,
            score=max(abs(float(target.expected_net_edge_bps)), 0.0),
            confidence=min(0.95, 0.45 + max(target.expected_signal_edge_bps, 0.0) / 100.0),
            urgency=target.urgency,
            reason_codes=["directional_strategy_target"],
            metrics={
                "expected_signal_edge_bps": target.expected_signal_edge_bps,
                "expected_cost_bps": target.expected_cost_bps,
                "expected_net_edge_bps": target.expected_net_edge_bps,
            },
        )

    def _latest_market_snapshot(self, symbol: str) -> MarketSnapshot | None:
        snapshot = self.market_gateway.latest_snapshot(symbol)
        if snapshot is not None:
            return snapshot
        latest_event = self.event_store.latest(topics.MARKET_SNAPSHOTS, key=symbol)
        if latest_event is None:
            return None
        return MarketSnapshot.model_validate(latest_event.payload)

    def _recent_market_snapshots(self, *, symbols: set[str]) -> dict[str, list[MarketSnapshot]]:
        rows: dict[str, list[MarketSnapshot]] = {}
        limit = max(self.settings.spot_grid_anchor_lookback_snapshots, 1)
        events = self.event_store.by_topic(topics.MARKET_SNAPSHOTS)
        for symbol in symbols:
            symbol_rows = [
                MarketSnapshot.model_validate(item.payload)
                for item in events
                if item.key == symbol
            ]
            rows[symbol] = symbol_rows[-limit:]
        return rows

    def _recent_market_symbols(self, symbol: str) -> set[str]:
        symbols = {symbol}
        companion_spot = self.settings.smart_arbitrage_companion_spot_symbol
        companion_derivatives = self.settings.smart_arbitrage_companion_derivatives_symbol
        if companion_spot:
            symbols.add(companion_spot)
        if companion_derivatives:
            symbols.add(companion_derivatives)
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
        for event in reversed(self.event_store.by_topic(topics.POSITION_TARGETS)):
            if event.key != symbol:
                continue
            target = PositionTarget.model_validate(event.payload)
            family = str(getattr(target, "strategy_family", "directional") or "directional")
            if family not in rows or len(rows[family]) >= 10:
                continue
            rows[family].append(StrategyTargetHistory(created_at=event.event_timestamp, target=target))
        return rows

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
