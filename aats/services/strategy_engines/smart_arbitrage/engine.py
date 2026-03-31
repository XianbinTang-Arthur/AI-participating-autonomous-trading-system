from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.market import MarketSnapshot
from aats.schemas.strategy_runtime import StrategyCandidate
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.base import StrategyEngineInput
from aats.services.strategy_engines.smart_arbitrage.capabilities import resolve_execution_capability
from aats.services.strategy_engines.smart_arbitrage.cost_model import build_cost_breakdown
from aats.services.strategy_engines.smart_arbitrage.discovery import basis_bps, load_market_pair
from aats.services.strategy_engines.smart_arbitrage.leg_planner import build_legs
from aats.services.strategy_engines.smart_arbitrage.schemas import ArbitrageOpportunity
from aats.services.strategy_engines.smart_arbitrage.sizer import entry_pair_qty
from aats.services.strategy_engines.smart_arbitrage.state_machine import resolve_pair_state


class SmartArbitrageStrategyEngine:
    def __init__(
        self,
        *,
        settings: AATSSettings,
        market_snapshot_loader,
        account_snapshot_loader=None,
        sleeve_inventory_loader=None,
        account_service=None,
    ) -> None:
        self.settings = settings
        self.market_snapshot_loader = market_snapshot_loader
        self.account_snapshot_loader = account_snapshot_loader
        self.sleeve_inventory_loader = sleeve_inventory_loader
        self.account_service = account_service

    def evaluate(self, engine_input: StrategyEngineInput) -> StrategyCandidate:
        pair_definitions = tuple(engine_input.resolved_pair_definitions_by_family.get("smart_arbitrage", ()))
        resolved_pair_metrics = self._resolved_pair_configuration_metrics(pair_definitions)
        if not self.settings.smart_arbitrage_enabled:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="disabled",
                enabled=False,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Smart arbitrage is disabled.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["smart_arbitrage_disabled"],
                state_phase="inactive",
                metrics=resolved_pair_metrics,
            )
        if engine_input.context.product_type != "derivatives":
            return StrategyCandidate(
                family="smart_arbitrage",
                state="incompatible",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Smart arbitrage auto execution currently runs on derivatives runtime only.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["smart_arbitrage_derivatives_runtime_required"],
                state_phase="inactive",
                metrics=resolved_pair_metrics,
            )

        if not pair_definitions:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Spot and derivatives companion symbols are not configured in the family input contract.",
                reason_codes=["smart_arbitrage_symbol_pair_missing"],
                state_phase="inactive",
                metrics=resolved_pair_metrics,
            )

        candidates = [self._evaluate_pair(pair=pair, engine_input=engine_input) for pair in pair_definitions]
        selected_pairs = self._select_candidates(candidates)
        selected = self._aggregate_candidates(candidates=candidates, selected_pairs=selected_pairs)
        selected.metrics = {
            **selected.metrics,
            **resolved_pair_metrics,
            "evaluated_pairs": [
                {
                    "pair_id": item.pair_id,
                    "recommended_symbol": item.recommended_symbol,
                    "state": item.state,
                    "state_phase": item.state_phase,
                    "opportunity_kind": item.opportunity_kind,
                    "execution_mode": item.execution_mode,
                    "score": item.score,
                    "confidence": item.confidence,
                    "reason_codes": list(item.reason_codes),
                    "blocking_reasons": list(item.blocking_reasons),
                }
                for item in candidates
            ],
            "pair_count_evaluated": len(candidates),
            "selected_pairs": [
                {
                    "pair_id": item.pair_id,
                    "recommended_symbol": item.recommended_symbol,
                    "state": item.state,
                    "state_phase": item.state_phase,
                    "opportunity_kind": item.opportunity_kind,
                    "execution_mode": item.execution_mode,
                    "score": item.score,
                    "confidence": item.confidence,
                    "reason_codes": list(item.reason_codes),
                    "blocking_reasons": list(item.blocking_reasons),
                }
                for item in selected_pairs
            ],
            "pair_count_selected": len(selected_pairs),
        }
        return selected

    @staticmethod
    def _resolved_pair_configuration_metrics(pair_definitions) -> dict[str, object]:
        serialized_pairs = [
            pair.model_dump(mode="json")
            for pair in pair_definitions
        ]
        warning_codes = list(
            dict.fromkeys(
                code
                for pair in pair_definitions
                for code in pair.metadata.get("configuration_warning_codes", [])
                if str(code).strip()
            )
        )
        error_codes = list(
            dict.fromkeys(
                code
                for pair in pair_definitions
                for code in pair.metadata.get("configuration_error_codes", [])
                if str(code).strip()
            )
        )
        return {
            "pair_definitions": serialized_pairs,
            "pair_registry_warning_codes": warning_codes,
            "pair_registry_error_codes": error_codes,
            "pair_registry_source": "coordinator_resolved",
        }

    def _load_market_pair(
        self,
        *,
        pair,
        engine_input: StrategyEngineInput,
    ) -> tuple[MarketSnapshot | None, MarketSnapshot | None]:
        family_market_snapshots = engine_input.latest_market_snapshots_by_symbol
        if family_market_snapshots:
            return (
                family_market_snapshots.get(pair.spot_symbol),
                family_market_snapshots.get(pair.hedge_symbol),
            )
        return load_market_pair(
            pair=pair,
            market_snapshot_loader=self.market_snapshot_loader,
        )

    def _evaluate_pair(self, *, pair, engine_input: StrategyEngineInput) -> StrategyCandidate:
        spot_snapshot, hedge_snapshot = self._load_market_pair(
            pair=pair,
            engine_input=engine_input,
        )
        if spot_snapshot is None or hedge_snapshot is None:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Paired market snapshots are incomplete.",
                recommended_symbol=pair.spot_symbol,
                pair_id=pair.pair_id,
                opportunity_kind="market_unavailable",
                state_phase="inactive",
                reason_codes=["smart_arbitrage_market_pair_incomplete"],
                metrics={
                    "pair_id": pair.pair_id,
                    "spot_symbol": pair.spot_symbol,
                    "derivatives_symbol": pair.hedge_symbol,
                },
            )

        spot_price = to_decimal(spot_snapshot.last_price)
        hedge_price = to_decimal(hedge_snapshot.last_price)
        pair_basis_bps = basis_bps(spot_snapshot=spot_snapshot, hedge_snapshot=hedge_snapshot)
        entry_threshold = Decimal(str(max(self.settings.smart_arbitrage_basis_entry_bps, 0.0)))
        exit_threshold = Decimal(str(max(self.settings.smart_arbitrage_basis_exit_bps, 0.0)))
        account_snapshot = engine_input.latest_account_snapshot or self._latest_account_snapshot()
        account_cash_spot_qty = self._current_spot_quantity(
            snapshot=account_snapshot,
            engine_input=engine_input,
            spot_symbol=pair.spot_symbol,
            margin_mode="cash",
        )
        account_margin_spot_qty = self._current_spot_quantity(
            snapshot=account_snapshot,
            engine_input=engine_input,
            spot_symbol=pair.spot_symbol,
            margin_mode=self.settings.smart_arbitrage_margin_short_spot_margin_mode,
        )
        account_hedge_qty = self._current_hedge_quantity(
            snapshot=account_snapshot,
            engine_input=engine_input,
            hedge_symbol=pair.hedge_symbol,
        )
        sleeve_product_scope = str(engine_input.directional_target.product_type or engine_input.context.product_type)
        sleeve_margin_scope = str(engine_input.directional_target.margin_mode or self.settings.margin_mode)
        sleeve_cash_spot_qty = self._current_sleeve_quantity(
            engine_input=engine_input,
            primary_symbol=engine_input.context.symbol,
            sleeve_product_scope=sleeve_product_scope,
            sleeve_margin_scope=sleeve_margin_scope,
            symbol_scope=(pair.spot_symbol, pair.hedge_symbol),
            symbol=pair.spot_symbol,
            leg_product_type="spot",
            leg_margin_mode="cash",
        )
        sleeve_margin_spot_qty = self._current_sleeve_quantity(
            engine_input=engine_input,
            primary_symbol=engine_input.context.symbol,
            sleeve_product_scope=sleeve_product_scope,
            sleeve_margin_scope=sleeve_margin_scope,
            symbol_scope=(pair.spot_symbol, pair.hedge_symbol),
            symbol=pair.spot_symbol,
            leg_product_type="spot",
            leg_margin_mode=self.settings.smart_arbitrage_margin_short_spot_margin_mode,
        )
        sleeve_hedge_qty = self._current_sleeve_quantity(
            engine_input=engine_input,
            primary_symbol=engine_input.context.symbol,
            sleeve_product_scope=sleeve_product_scope,
            sleeve_margin_scope=sleeve_margin_scope,
            symbol_scope=(pair.spot_symbol, pair.hedge_symbol),
            symbol=pair.hedge_symbol,
            leg_product_type="derivatives",
            leg_margin_mode=sleeve_margin_scope,
        )
        pair_state = resolve_pair_state(
            pair_id=pair.pair_id,
            account_spot_qty=account_cash_spot_qty + account_margin_spot_qty,
            account_hedge_qty=account_hedge_qty,
            sleeve_spot_qty=sleeve_cash_spot_qty + sleeve_margin_spot_qty,
            sleeve_hedge_qty=sleeve_hedge_qty,
            basis_bps=pair_basis_bps,
            exit_threshold_bps=exit_threshold,
            account_cash_spot_qty=account_cash_spot_qty,
            account_margin_spot_qty=account_margin_spot_qty,
            sleeve_cash_spot_qty=sleeve_cash_spot_qty,
            sleeve_margin_spot_qty=sleeve_margin_spot_qty,
        )
        capability = resolve_execution_capability(
            settings=self.settings,
            pair=pair,
            account_spot_qty=account_cash_spot_qty,
        )
        directional_target_qty = to_decimal(engine_input.directional_target.target_position_qty)
        protective_directional_exit = (
            pair_state.current_direction == "flat"
            and abs(account_hedge_qty) > EPSILON_DECIMAL_12
            and abs(directional_target_qty) + EPSILON_DECIMAL_12 < abs(account_hedge_qty)
        )
        if protective_directional_exit:
            opportunity = ArbitrageOpportunity(
                pair_id=pair.pair_id,
                spot_symbol=pair.spot_symbol,
                hedge_symbol=pair.hedge_symbol,
                opportunity_kind="protective_exit",
                direction="neutral",
                state_phase="advisory",
                basis_bps=pair_basis_bps,
                entry_threshold_bps=entry_threshold,
                exit_threshold_bps=exit_threshold,
                score=float(max(pair_basis_bps.copy_abs(), Decimal("0")) / max(entry_threshold or Decimal("1"), Decimal("1"))),
                confidence=min(0.95, 0.45 + (min(abs(float(pair_basis_bps)), 120.0) / 200.0)),
                urgency="medium",
                reason_codes=[
                    "smart_arbitrage_protective_directional_exit_retained",
                    "smart_arbitrage_existing_unpaired_exposure",
                ],
                blocking_reasons=["smart_arbitrage_protective_directional_exit_retained"],
                route_action="advisory_only",
                metadata={"directional_target_qty": directional_target_qty},
            )
        else:
            opportunity = self._build_opportunity(
                pair=pair,
                pair_basis_bps=pair_basis_bps,
                entry_threshold=entry_threshold,
                exit_threshold=exit_threshold,
                pair_state=pair_state,
                capability=capability,
                spot_price=spot_price,
                reference_ts=engine_input.context.as_of_ts,
            )
        return self._candidate_from_opportunity(
            pair=pair,
            pair_state=pair_state,
            opportunity=opportunity,
            account_cash_spot_qty=account_cash_spot_qty,
            account_margin_spot_qty=account_margin_spot_qty,
            account_hedge_qty=account_hedge_qty,
            sleeve_cash_spot_qty=sleeve_cash_spot_qty,
            sleeve_margin_spot_qty=sleeve_margin_spot_qty,
            sleeve_hedge_qty=sleeve_hedge_qty,
            spot_price=spot_price,
            hedge_price=hedge_price,
            capability=capability,
        )

    def _build_opportunity(
        self,
        *,
        pair,
        pair_basis_bps: Decimal,
        entry_threshold: Decimal,
        exit_threshold: Decimal,
        pair_state,
        capability,
        spot_price: Decimal,
        reference_ts,
    ) -> ArbitrageOpportunity:
        positive_basis_active = pair_basis_bps >= entry_threshold
        negative_basis_active = pair_basis_bps <= -entry_threshold
        if pair_state.current_direction == "mixed":
            cost_breakdown = build_cost_breakdown(
                settings=self.settings,
                basis_bps=pair_basis_bps,
                execution_mode=None,
                reference_ts=reference_ts,
                spot_symbol=pair.spot_symbol,
                hedge_symbol=pair.hedge_symbol,
                account_service=self.account_service,
            )
            return ArbitrageOpportunity(
                pair_id=pair.pair_id,
                spot_symbol=pair.spot_symbol,
                hedge_symbol=pair.hedge_symbol,
                opportunity_kind="pair_recovery",
                direction="neutral",
                state_phase="blocked",
                basis_bps=pair_basis_bps,
                entry_threshold_bps=entry_threshold,
                exit_threshold_bps=exit_threshold,
                score=float(
                    max(cost_breakdown.executable_edge_bps, Decimal("0"))
                    / max(entry_threshold or Decimal("1"), Decimal("1"))
                ),
                confidence=0.40,
                urgency="high",
                reason_codes=["smart_arbitrage_mixed_pair_direction_detected"],
                blocking_reasons=list(pair_state.blocking_reasons),
                route_action="advisory_only",
                cost_breakdown=cost_breakdown,
            )
        if pair_state.current_direction == "positive_carry":
            execution_mode = "spot_carry"
            existing_mode_not_allowed = not self._pair_supports_execution_mode(pair=pair, execution_mode=execution_mode)
            if pair_state.unwind_required:
                desired_pair_qty = Decimal("0")
                state_phase = "unwinding"
                opportunity_kind = "pair_exit"
                route_action = "override_target"
                reason_codes = ["smart_arbitrage_exit_ready"]
                urgency = "high"
                direction = "neutral"
            elif pair_state.recovery_required:
                desired_pair_qty = max(pair_state.current_spot_qty, pair_state.current_short_qty)
                state_phase = "recovery"
                opportunity_kind = "pair_recovery"
                route_action = "override_target"
                reason_codes = ["smart_arbitrage_positive_basis", "smart_arbitrage_partial_fill_recovery"]
                urgency = "high"
                direction = "positive_basis"
            else:
                desired_pair_qty = max(pair_state.current_spot_qty, pair_state.current_short_qty)
                state_phase = "active"
                opportunity_kind = "pair_hold"
                route_action = "hold_current"
                reason_codes = ["smart_arbitrage_pair_active_waiting_exit"]
                urgency = "low"
                direction = "positive_basis"
            if existing_mode_not_allowed:
                reason_codes = [*reason_codes, "smart_arbitrage_existing_pair_mode_not_allowed_by_config"]
            target_spot_qty = desired_pair_qty
            target_hedge_qty = -desired_pair_qty
        elif pair_state.current_direction == "reverse_carry":
            execution_mode = self._active_reverse_execution_mode(pair_state)
            existing_mode_not_allowed = not self._pair_supports_execution_mode(pair=pair, execution_mode=execution_mode)
            if pair_state.unwind_required:
                desired_pair_qty = Decimal("0")
                state_phase = "unwinding"
                opportunity_kind = "pair_exit"
                route_action = "override_target"
                reason_codes = ["smart_arbitrage_exit_ready"]
                urgency = "high"
                direction = "neutral"
            elif pair_state.recovery_required:
                desired_pair_qty = max(abs(min(pair_state.current_spot_qty, Decimal("0"))), pair_state.current_long_qty)
                state_phase = "recovery"
                opportunity_kind = "pair_recovery"
                route_action = "override_target"
                reason_codes = ["smart_arbitrage_negative_basis", "smart_arbitrage_partial_fill_recovery"]
                urgency = "high"
                direction = "negative_basis"
            else:
                desired_pair_qty = max(pair_state.current_reverse_pair_qty, pair_state.current_long_qty)
                state_phase = "active"
                opportunity_kind = "pair_hold"
                route_action = "hold_current"
                reason_codes = ["smart_arbitrage_pair_active_waiting_exit"]
                urgency = "low"
                direction = "negative_basis"
            if existing_mode_not_allowed:
                reason_codes = [*reason_codes, "smart_arbitrage_existing_pair_mode_not_allowed_by_config"]
            target_spot_qty = -desired_pair_qty
            target_hedge_qty = desired_pair_qty
        elif positive_basis_active:
            execution_mode = "spot_carry"
            if not self._pair_supports_execution_mode(pair=pair, execution_mode=execution_mode):
                return self._unsupported_execution_mode_opportunity(
                    pair=pair,
                    pair_basis_bps=pair_basis_bps,
                    entry_threshold=entry_threshold,
                    exit_threshold=exit_threshold,
                    execution_mode=execution_mode,
                    reference_ts=reference_ts,
                    direction="positive_basis",
                    reason_code="smart_arbitrage_spot_carry_not_allowed",
                )
            desired_pair_qty = entry_pair_qty(
                settings=self.settings,
                spot_price=spot_price,
                capability=capability,
                execution_mode=execution_mode,
            )
            target_spot_qty = desired_pair_qty
            target_hedge_qty = -desired_pair_qty
            state_phase = "opening"
            opportunity_kind = "positive_basis"
            route_action = "override_target"
            reason_codes = ["smart_arbitrage_positive_basis"]
            urgency = "medium"
            direction = "positive_basis"
        elif negative_basis_active:
            return self._build_negative_basis_opportunity(
                pair=pair,
                pair_basis_bps=pair_basis_bps,
                entry_threshold=entry_threshold,
                exit_threshold=exit_threshold,
                capability=capability,
                spot_price=spot_price,
                reference_ts=reference_ts,
            )
        else:
            observation_mode = (
                "spot_carry"
                if pair_basis_bps >= Decimal("0")
                else (
                    "margin_reverse_carry"
                    if self.settings.smart_arbitrage_negative_basis_mode == "margin_backed"
                    else "inventory_reverse_carry"
                    if self.settings.smart_arbitrage_negative_basis_mode == "inventory_backed"
                    else None
                )
            )
            cost_breakdown = build_cost_breakdown(
                settings=self.settings,
                basis_bps=pair_basis_bps,
                execution_mode=observation_mode,
                reference_ts=reference_ts,
                spot_symbol=pair.spot_symbol,
                hedge_symbol=pair.hedge_symbol,
                account_service=self.account_service,
            )
            return ArbitrageOpportunity(
                pair_id=pair.pair_id,
                spot_symbol=pair.spot_symbol,
                hedge_symbol=pair.hedge_symbol,
                opportunity_kind="pair_hold",
                direction="neutral",
                state_phase="inactive",
                basis_bps=pair_basis_bps,
                entry_threshold_bps=entry_threshold,
                exit_threshold_bps=exit_threshold,
                reason_codes=["smart_arbitrage_basis_below_entry_threshold"],
                route_action="hold_current",
                urgency="low",
                cost_breakdown=cost_breakdown,
            )
        cost_breakdown = build_cost_breakdown(
            settings=self.settings,
            basis_bps=pair_basis_bps,
            execution_mode=execution_mode,
            reference_ts=reference_ts,
            spot_symbol=pair.spot_symbol,
            hedge_symbol=pair.hedge_symbol,
            account_service=self.account_service,
        )
        opening_block_reason = None
        if state_phase == "opening" and cost_breakdown.executable_edge_bps <= Decimal("0"):
            state_phase = "blocked"
            route_action = "advisory_only"
            opening_block_reason = self._drag_blocking_reason(cost_breakdown=cost_breakdown)
            reason_codes = list(dict.fromkeys([*reason_codes, opening_block_reason]))
        score = float(
            max(cost_breakdown.executable_edge_bps, Decimal("0"))
            / max(entry_threshold or Decimal("1"), Decimal("1"))
        )
        confidence = min(0.96, 0.50 + (min(abs(float(pair_basis_bps)), 120.0) / 180.0))
        return ArbitrageOpportunity(
            pair_id=pair.pair_id,
            spot_symbol=pair.spot_symbol,
            hedge_symbol=pair.hedge_symbol,
            opportunity_kind=opportunity_kind,
            direction=direction,  # type: ignore[arg-type]
            execution_mode=execution_mode,  # type: ignore[arg-type]
            state_phase=state_phase,  # type: ignore[arg-type]
            basis_bps=pair_basis_bps,
            entry_threshold_bps=entry_threshold,
            exit_threshold_bps=exit_threshold,
            desired_pair_qty=desired_pair_qty,
            target_spot_qty=target_spot_qty,
            target_hedge_qty=target_hedge_qty,
            score=score,
            confidence=confidence,
            urgency=urgency,  # type: ignore[arg-type]
            reason_codes=reason_codes,
            blocking_reasons=[] if opening_block_reason is None else [opening_block_reason],
            route_action=route_action,
            cost_breakdown=cost_breakdown,
        )

    def _build_negative_basis_opportunity(
        self,
        *,
        pair,
        pair_basis_bps: Decimal,
        entry_threshold: Decimal,
        exit_threshold: Decimal,
        capability,
        spot_price: Decimal,
        reference_ts,
    ) -> ArbitrageOpportunity:
        requested_mode = self.settings.smart_arbitrage_negative_basis_mode
        if requested_mode in {"disabled", "advisory_only"}:
            execution_mode = None
            state_phase = "advisory"
            reason_codes = ["smart_arbitrage_negative_basis", "smart_arbitrage_spot_short_not_supported"]
            blocking_reasons = ["smart_arbitrage_negative_basis_advisory_only"]
            desired_pair_qty = Decimal("0")
            target_spot_qty = Decimal("0")
            target_hedge_qty = Decimal("0")
        elif requested_mode == "inventory_backed":
            execution_mode = "inventory_reverse_carry"
            if not self._pair_supports_execution_mode(
                pair=pair,
                execution_mode=execution_mode,
            ):
                return self._unsupported_execution_mode_opportunity(
                    pair=pair,
                    pair_basis_bps=pair_basis_bps,
                    entry_threshold=entry_threshold,
                    exit_threshold=exit_threshold,
                    execution_mode=execution_mode,
                    reference_ts=reference_ts,
                    direction="negative_basis",
                    reason_code="smart_arbitrage_inventory_reverse_carry_not_allowed",
                )
            execution_mode = "inventory_reverse_carry"
            desired_pair_qty = entry_pair_qty(
                settings=self.settings,
                spot_price=spot_price,
                capability=capability,
                execution_mode=execution_mode,
            )
            target_spot_qty = -desired_pair_qty
            target_hedge_qty = desired_pair_qty
            minimum_ratio = Decimal(str(max(self.settings.smart_arbitrage_min_inventory_backed_ratio, 0.0)))
            supported_ratio = (
                Decimal("0")
                if desired_pair_qty <= EPSILON_DECIMAL_12
                else capability.available_inventory_qty / desired_pair_qty
            )
            inventory_blocking_reasons = list(capability.blocking_reasons)
            if desired_pair_qty <= EPSILON_DECIMAL_12 or supported_ratio + EPSILON_DECIMAL_12 < minimum_ratio:
                state_phase = "blocked"
                if not inventory_blocking_reasons:
                    inventory_blocking_reasons = ["smart_arbitrage_inventory_backed_insufficient"]
                reason_codes = ["smart_arbitrage_negative_basis", *inventory_blocking_reasons]
                blocking_reasons = list(dict.fromkeys(inventory_blocking_reasons))
            else:
                state_phase = "opening"
                reason_codes = ["smart_arbitrage_negative_basis", "smart_arbitrage_inventory_backed_ready"]
                blocking_reasons = []
        elif requested_mode == "margin_backed":
            execution_mode = "margin_reverse_carry"
            if not self._pair_supports_execution_mode(
                pair=pair,
                execution_mode=execution_mode,
            ):
                return self._unsupported_execution_mode_opportunity(
                    pair=pair,
                    pair_basis_bps=pair_basis_bps,
                    entry_threshold=entry_threshold,
                    exit_threshold=exit_threshold,
                    execution_mode=execution_mode,
                    reference_ts=reference_ts,
                    direction="negative_basis",
                    reason_code="smart_arbitrage_margin_reverse_carry_not_allowed",
                )
            desired_pair_qty = entry_pair_qty(
                settings=self.settings,
                spot_price=spot_price,
                capability=capability,
                execution_mode=execution_mode,
            )
            target_spot_qty = -desired_pair_qty
            target_hedge_qty = desired_pair_qty
            margin_blocking_reasons = list(capability.blocking_reasons)
            if (
                desired_pair_qty <= EPSILON_DECIMAL_12
                or not capability.spot_margin_short_supported
                or not capability.margin_short_execution_ready
            ):
                state_phase = "blocked"
                if not margin_blocking_reasons:
                    margin_blocking_reasons = ["smart_arbitrage_margin_short_disabled"]
                reason_codes = ["smart_arbitrage_negative_basis", *margin_blocking_reasons]
                blocking_reasons = list(dict.fromkeys(margin_blocking_reasons))
            else:
                state_phase = "opening"
                reason_codes = ["smart_arbitrage_negative_basis", "smart_arbitrage_margin_short_ready"]
                blocking_reasons = []
        else:
            execution_mode = None
            desired_pair_qty = Decimal("0")
            target_spot_qty = Decimal("0")
            target_hedge_qty = Decimal("0")
            state_phase = "advisory"
            reason_codes = ["smart_arbitrage_negative_basis", "smart_arbitrage_spot_short_not_supported"]
            blocking_reasons = ["smart_arbitrage_spot_short_not_supported"]
        cost_breakdown = build_cost_breakdown(
            settings=self.settings,
            basis_bps=pair_basis_bps,
            execution_mode=execution_mode,
            reference_ts=reference_ts,
            spot_symbol=pair.spot_symbol,
            hedge_symbol=pair.hedge_symbol,
            account_service=self.account_service,
        )
        if state_phase == "opening" and cost_breakdown.executable_edge_bps <= Decimal("0"):
            state_phase = "blocked"
            route_action = "advisory_only"
            drag_reason = self._drag_blocking_reason(cost_breakdown=cost_breakdown)
            reason_codes = list(dict.fromkeys([*reason_codes, drag_reason]))
            blocking_reasons = list(dict.fromkeys([*blocking_reasons, drag_reason]))
        score = float(
            max(cost_breakdown.executable_edge_bps, Decimal("0"))
            / max(entry_threshold or Decimal("1"), Decimal("1"))
        )
        confidence = min(0.95, 0.45 + (min(abs(float(pair_basis_bps)), 120.0) / 200.0))
        route_action = "override_target" if state_phase == "opening" else "advisory_only"
        return ArbitrageOpportunity(
            pair_id=pair.pair_id,
            spot_symbol=pair.spot_symbol,
            hedge_symbol=pair.hedge_symbol,
            opportunity_kind="negative_basis",
            direction="negative_basis",
            execution_mode=execution_mode,  # type: ignore[arg-type]
            state_phase=state_phase,  # type: ignore[arg-type]
            basis_bps=pair_basis_bps,
            entry_threshold_bps=entry_threshold,
            exit_threshold_bps=exit_threshold,
            desired_pair_qty=desired_pair_qty,
            target_spot_qty=target_spot_qty,
            target_hedge_qty=target_hedge_qty,
            score=score,
            confidence=confidence,
            urgency="medium",
            reason_codes=reason_codes,
            blocking_reasons=blocking_reasons,
            route_action=route_action,  # type: ignore[arg-type]
            cost_breakdown=cost_breakdown,
        )

    def _candidate_from_opportunity(
        self,
        *,
        pair,
        pair_state,
        opportunity: ArbitrageOpportunity,
        account_cash_spot_qty: Decimal,
        account_margin_spot_qty: Decimal,
        account_hedge_qty: Decimal,
        sleeve_cash_spot_qty: Decimal,
        sleeve_margin_spot_qty: Decimal,
        sleeve_hedge_qty: Decimal,
        spot_price: Decimal,
        hedge_price: Decimal,
        capability,
    ) -> StrategyCandidate:
        account_spot_qty, sleeve_spot_qty = self._selected_spot_quantities(
            opportunity=opportunity,
            pair_state=pair_state,
            account_cash_spot_qty=account_cash_spot_qty,
            account_margin_spot_qty=account_margin_spot_qty,
            sleeve_cash_spot_qty=sleeve_cash_spot_qty,
            sleeve_margin_spot_qty=sleeve_margin_spot_qty,
        )
        spot_delta_qty = to_decimal(opportunity.target_spot_qty) - sleeve_spot_qty
        hedge_delta_qty = to_decimal(opportunity.target_hedge_qty) - to_decimal(sleeve_hedge_qty)
        target_account_spot_qty = account_spot_qty + spot_delta_qty
        target_account_hedge_qty = to_decimal(account_hedge_qty) + hedge_delta_qty
        execution_compatible = opportunity.state_phase in {"opening", "active", "recovery", "unwinding"}
        route_action = opportunity.route_action
        if execution_compatible and route_action == "hold_current" and (
            abs(spot_delta_qty) > EPSILON_DECIMAL_12 or abs(hedge_delta_qty) > EPSILON_DECIMAL_12
        ):
            route_action = "override_target"
        legs = build_legs(
            settings=self.settings,
            pair=pair,
            opportunity=opportunity.model_copy(
                update={
                    "target_account_spot_qty": target_account_spot_qty,
                    "target_account_hedge_qty": target_account_hedge_qty,
                    "route_action": route_action,
                }
            ),
            account_spot_qty=account_spot_qty,
            account_hedge_qty=account_hedge_qty,
            sleeve_spot_qty=sleeve_spot_qty,
            sleeve_hedge_qty=sleeve_hedge_qty,
            spot_price=spot_price,
            hedge_price=hedge_price,
        )
        if not execution_compatible:
            legs = []
        state = {
            "inactive": "inactive",
            "advisory": "advisory_only",
            "blocked": "blocked",
            "opening": "opening",
            "active": "active",
            "recovery": "recovery",
            "unwinding": "unwinding",
        }.get(opportunity.state_phase, "inactive")
        reason_codes = list(dict.fromkeys([*opportunity.reason_codes, *pair_state.blocking_reasons]))
        blocking_reasons = list(
            dict.fromkeys([*opportunity.blocking_reasons, *capability.blocking_reasons, *pair_state.blocking_reasons])
        )
        return StrategyCandidate(
            family="smart_arbitrage",
            state=state,  # type: ignore[arg-type]
            enabled=True,
            selectable=execution_compatible and (route_action == "override_target" or pair_state.current_pair_qty > EPSILON_DECIMAL_12),
            execution_compatible=execution_compatible,
            route_action=route_action,
            headline=self._headline_for(opportunity),
            recommended_symbol=pair.hedge_symbol,
            target_position_qty=target_account_hedge_qty,
            delta_position_qty=hedge_delta_qty,
            score=opportunity.score,
            confidence=opportunity.confidence,
            urgency=opportunity.urgency,
            reason_codes=reason_codes,
            pair_id=pair.pair_id,
            opportunity_kind=opportunity.opportunity_kind,
            execution_mode=opportunity.execution_mode,
            state_phase=opportunity.state_phase,
            blocking_reasons=blocking_reasons,
            metrics={
                "pair_id": pair.pair_id,
                "spot_symbol": pair.spot_symbol,
                "derivatives_symbol": pair.hedge_symbol,
                "spot_price": spot_price,
                "derivatives_price": hedge_price,
                "basis_bps": opportunity.basis_bps,
                "entry_threshold_bps": opportunity.entry_threshold_bps,
                "exit_threshold_bps": opportunity.exit_threshold_bps,
                "net_basis_bps": opportunity.cost_breakdown.executable_edge_bps,
                "ideal_cost_bps": opportunity.cost_breakdown.ideal_total_cost_bps,
                "executable_cost_bps": opportunity.cost_breakdown.executable_total_drag_bps,
                "ideal_edge_bps": opportunity.cost_breakdown.ideal_edge_bps,
                "executable_edge_bps": opportunity.cost_breakdown.executable_edge_bps,
                "breakeven_basis_bps": opportunity.cost_breakdown.breakeven_basis_bps,
                "ideal_open_fee_bps": opportunity.cost_breakdown.ideal_open_fee_bps,
                "ideal_close_fee_bps": opportunity.cost_breakdown.ideal_close_fee_bps,
                "ideal_total_fee_bps": opportunity.cost_breakdown.ideal_total_fee_bps,
                "executable_spread_bps": opportunity.cost_breakdown.executable_spread_bps,
                "executable_slippage_bps": opportunity.cost_breakdown.executable_slippage_bps,
                "execution_mismatch_bps": opportunity.cost_breakdown.execution_mismatch_bps,
                "funding_cost_bps": opportunity.cost_breakdown.funding_cost_bps,
                "borrow_cost_bps": opportunity.cost_breakdown.borrow_cost_bps,
                "transfer_cost_bps": opportunity.cost_breakdown.transfer_cost_bps,
                "time_decay_cost_bps": opportunity.cost_breakdown.time_decay_cost_bps,
                "expected_hold_hours": opportunity.cost_breakdown.expected_hold_hours,
                "expected_funding_events": opportunity.cost_breakdown.expected_funding_events,
                "borrow_hour_windows": opportunity.cost_breakdown.borrow_hour_windows,
                "cost_confidence": opportunity.cost_breakdown.cost_confidence,
                "cost_source_flags": list(opportunity.cost_breakdown.cost_source_flags),
                "estimated_cost_bps": opportunity.cost_breakdown.estimated_total_cost_bps,
                "estimated_fee_bps": opportunity.cost_breakdown.estimated_fee_bps,
                "estimated_slippage_bps": opportunity.cost_breakdown.estimated_slippage_bps,
                "estimated_funding_bps": opportunity.cost_breakdown.estimated_funding_bps,
                "estimated_borrow_bps": opportunity.cost_breakdown.estimated_borrow_bps,
                "current_account_spot_qty": account_spot_qty,
                "current_account_cash_spot_qty": account_cash_spot_qty,
                "current_account_margin_spot_qty": account_margin_spot_qty,
                "current_account_derivatives_qty": account_hedge_qty,
                "current_sleeve_spot_qty": sleeve_spot_qty,
                "current_sleeve_cash_spot_qty": sleeve_cash_spot_qty,
                "current_sleeve_margin_spot_qty": sleeve_margin_spot_qty,
                "current_sleeve_derivatives_qty": sleeve_hedge_qty,
                "foreign_spot_qty": pair_state.foreign_spot_qty,
                "foreign_derivatives_qty": pair_state.foreign_hedge_qty,
                "paired_qty": pair_state.current_pair_qty,
                "positive_pair_qty": pair_state.current_positive_pair_qty,
                "reverse_pair_qty": pair_state.current_reverse_pair_qty,
                "inventory_reverse_pair_qty": pair_state.current_inventory_reverse_pair_qty,
                "margin_reverse_pair_qty": pair_state.current_margin_reverse_pair_qty,
                "target_pair_qty": opportunity.desired_pair_qty,
                "target_account_spot_qty": target_account_spot_qty,
                "target_account_derivatives_qty": target_account_hedge_qty,
                "target_sleeve_spot_qty": opportunity.target_spot_qty,
                "target_sleeve_derivatives_qty": opportunity.target_hedge_qty,
                "route_action": route_action,
                "state_phase": opportunity.state_phase,
                "execution_mode": opportunity.execution_mode,
                "opportunity_kind": opportunity.opportunity_kind,
                "blocking_reasons": blocking_reasons,
                "pair_configuration_warning_codes": self._pair_configuration_warning_codes(pair),
                "pair_configuration_error_codes": self._pair_configuration_error_codes(pair),
                "inventory_backed_available_qty": capability.available_inventory_qty,
                "margin_short_execution_ready": capability.margin_short_execution_ready,
                "spot_margin_mode": capability.spot_margin_mode,
            },
            legs=legs,
        )

    @staticmethod
    def _headline_for(opportunity: ArbitrageOpportunity) -> str:
        reason_codes = set(opportunity.reason_codes or [])
        if opportunity.opportunity_kind == "protective_exit":
            return "Current posture looks like a protective directional exit; smart arbitrage will not take over this cycle."
        if opportunity.opportunity_kind == "market_unavailable":
            return "Paired market snapshots are incomplete."
        if opportunity.opportunity_kind == "positive_basis":
            if opportunity.state_phase == "blocked":
                if "smart_arbitrage_funding_window_unfavorable" in reason_codes:
                    return "Positive basis exists, but the upcoming funding window makes the executable edge unattractive."
                if "smart_arbitrage_borrow_window_unfavorable" in reason_codes:
                    return "Positive basis exists, but the borrow/holding window makes the executable edge unattractive."
                if {
                    "smart_arbitrage_drag_exceeds_basis",
                    "smart_arbitrage_executable_edge_negative",
                } & reason_codes:
                    return "Positive basis exists, but executable drag consumes the edge."
                return "Positive basis is detected, but this pair is not allowed to auto open."
            return "Positive basis pair is ready."
        if opportunity.opportunity_kind == "negative_basis":
            if opportunity.state_phase == "opening":
                if opportunity.execution_mode == "margin_reverse_carry":
                    return "Negative basis reverse carry is ready with margin-backed spot execution."
                return "Negative basis reverse carry is ready with inventory-backed spot execution."
            if opportunity.state_phase == "blocked":
                if "smart_arbitrage_funding_window_unfavorable" in reason_codes:
                    return "Negative basis exists, but the upcoming funding window makes reverse carry unattractive."
                if "smart_arbitrage_borrow_window_unfavorable" in reason_codes:
                    return "Negative basis exists, but the borrow window makes reverse carry unattractive."
                if {
                    "smart_arbitrage_drag_exceeds_basis",
                    "smart_arbitrage_executable_edge_negative",
                } & reason_codes:
                    return "Negative basis exists, but executable drag consumes the edge."
                return "Negative basis is detected, but the configured reverse-carry execution path is blocked."
            return "Negative basis is detected, but reverse-carry auto execution is not available."
        if opportunity.opportunity_kind == "pair_recovery":
            return "Arbitrage pair is imbalanced; recover the missing leg."
        if opportunity.opportunity_kind == "pair_exit":
            return "Basis has normalized or the hedge posture is inconsistent; unwind the pair."
        if opportunity.state_phase == "active":
            return "Basis remains above the exit threshold; keep the pair open."
        return "Basis is below the configured entry threshold."

    @staticmethod
    def _pair_supports_execution_mode(*, pair, execution_mode: str) -> bool:
        allowed_modes = {str(mode) for mode in (pair.execution_modes or ())}
        return execution_mode in allowed_modes

    @staticmethod
    def _pair_configuration_warning_codes(pair) -> list[str]:
        return [str(code) for code in pair.metadata.get("configuration_warning_codes", []) if str(code).strip()]

    @staticmethod
    def _pair_configuration_error_codes(pair) -> list[str]:
        return [str(code) for code in pair.metadata.get("configuration_error_codes", []) if str(code).strip()]

    def _unsupported_execution_mode_opportunity(
        self,
        *,
        pair,
        pair_basis_bps: Decimal,
        entry_threshold: Decimal,
        exit_threshold: Decimal,
        execution_mode: str,
        reference_ts,
        direction: str,
        reason_code: str,
    ) -> ArbitrageOpportunity:
        cost_breakdown = build_cost_breakdown(
            settings=self.settings,
            basis_bps=pair_basis_bps,
            execution_mode=execution_mode,
            reference_ts=reference_ts,
            spot_symbol=pair.spot_symbol,
            hedge_symbol=pair.hedge_symbol,
            account_service=self.account_service,
        )
        score = float(
            max(cost_breakdown.executable_edge_bps, Decimal("0"))
            / max(entry_threshold or Decimal("1"), Decimal("1"))
        )
        confidence = min(0.90, 0.40 + (min(abs(float(pair_basis_bps)), 120.0) / 220.0))
        return ArbitrageOpportunity(
            pair_id=pair.pair_id,
            spot_symbol=pair.spot_symbol,
            hedge_symbol=pair.hedge_symbol,
            opportunity_kind="positive_basis" if direction == "positive_basis" else "negative_basis",
            direction=direction,  # type: ignore[arg-type]
            execution_mode=execution_mode,  # type: ignore[arg-type]
            state_phase="blocked",
            basis_bps=pair_basis_bps,
            entry_threshold_bps=entry_threshold,
            exit_threshold_bps=exit_threshold,
            score=score,
            confidence=confidence,
            urgency="medium",
            reason_codes=[f"smart_arbitrage_{direction}", reason_code],
            blocking_reasons=[reason_code],
            route_action="advisory_only",
            cost_breakdown=cost_breakdown,
        )

    def _select_candidates(self, candidates: list[StrategyCandidate]) -> list[StrategyCandidate]:
        ranked = self._ranked_candidates(candidates)
        active_pairs: list[StrategyCandidate] = []
        opening_pairs: list[StrategyCandidate] = []
        seen_pair_ids: set[str] = set()
        for candidate in ranked:
            pair_id = str(candidate.pair_id or "")
            if pair_id and pair_id in seen_pair_ids:
                continue
            state_phase = str(candidate.state_phase or "inactive")
            if state_phase in {"active", "recovery", "unwinding"}:
                active_pairs.append(candidate)
                if pair_id:
                    seen_pair_ids.add(pair_id)
                continue
            if state_phase == "opening" and candidate.execution_compatible and candidate.route_action == "override_target":
                opening_pairs.append(candidate)
                if pair_id:
                    seen_pair_ids.add(pair_id)
        max_pairs = max(int(self.settings.smart_arbitrage_max_concurrent_pairs or 1), 1)
        if active_pairs:
            safe_opening_pairs = self._parallel_safe_candidates(
                candidates=opening_pairs,
                existing=active_pairs,
                limit=max_pairs - len(active_pairs),
            )
            max_pairs = max(int(self.settings.smart_arbitrage_max_concurrent_pairs or 1), 1)
            if len(active_pairs) >= max_pairs:
                return active_pairs
            return [*active_pairs, *safe_opening_pairs]
        if opening_pairs:
            return self._parallel_safe_candidates(candidates=opening_pairs, existing=[], limit=max_pairs)
        return ranked[:1]

    def _aggregate_candidates(
        self,
        *,
        candidates: list[StrategyCandidate],
        selected_pairs: list[StrategyCandidate],
    ) -> StrategyCandidate:
        if not selected_pairs:
            return self._ranked_candidates(candidates)[0]
        if len(selected_pairs) == 1:
            return selected_pairs[0]
        top = self._ranked_candidates(selected_pairs)[0]
        selected_execution_modes = {
            item.execution_mode
            for item in selected_pairs
            if item.execution_mode not in {None, ""}
        }
        aggregate_metrics = dict(top.metrics or {})
        overlap_detected = self._selected_pairs_have_overlapping_symbol_scope(selected_pairs)
        selected_pair_summaries = [
            {
                "pair_id": item.pair_id,
                "spot_symbol": item.metrics.get("spot_symbol"),
                "derivatives_symbol": item.metrics.get("derivatives_symbol"),
                "basis_bps": item.metrics.get("basis_bps"),
                "ideal_cost_bps": item.metrics.get("ideal_cost_bps"),
                "executable_cost_bps": item.metrics.get("executable_cost_bps"),
                "ideal_edge_bps": item.metrics.get("ideal_edge_bps"),
                "executable_edge_bps": item.metrics.get("executable_edge_bps"),
                "breakeven_basis_bps": item.metrics.get("breakeven_basis_bps"),
                "execution_mode": item.execution_mode,
                "state_phase": item.state_phase,
                "opportunity_kind": item.opportunity_kind,
                "reason_codes": list(item.reason_codes),
                "blocking_reasons": list(item.blocking_reasons),
            }
            for item in selected_pairs
        ]
        aggregate_metrics.update(
            {
                "aggregate_candidate": True,
                "selected_pair_summaries": selected_pair_summaries,
                "selected_spot_symbols": list(
                    dict.fromkeys(
                        str(item.metrics.get("spot_symbol")).upper()
                        for item in selected_pairs
                        if str(item.metrics.get("spot_symbol") or "").strip()
                    )
                ),
                "selected_derivatives_symbols": list(
                    dict.fromkeys(
                        str(item.metrics.get("derivatives_symbol")).upper()
                        for item in selected_pairs
                        if str(item.metrics.get("derivatives_symbol") or "").strip()
                    )
                ),
                "target_pair_qty": self._sum_metric(selected_pairs, "target_pair_qty"),
                "paired_qty": self._sum_metric(selected_pairs, "paired_qty"),
                "positive_pair_qty": self._sum_metric(selected_pairs, "positive_pair_qty"),
                "reverse_pair_qty": self._sum_metric(selected_pairs, "reverse_pair_qty"),
                "inventory_reverse_pair_qty": self._sum_metric(selected_pairs, "inventory_reverse_pair_qty"),
                "margin_reverse_pair_qty": self._sum_metric(selected_pairs, "margin_reverse_pair_qty"),
                "aggregate_requested_notional": sum(
                    (self._candidate_requested_notional(item) for item in selected_pairs),
                    start=Decimal("0"),
                ),
                "parallel_scope_overlap_detected": overlap_detected,
                "basis_bps": self._weighted_average_metric(selected_pairs, "basis_bps"),
                "net_basis_bps": self._weighted_average_metric(selected_pairs, "net_basis_bps"),
                "ideal_cost_bps": self._weighted_average_metric(selected_pairs, "ideal_cost_bps"),
                "executable_cost_bps": self._weighted_average_metric(selected_pairs, "executable_cost_bps"),
                "ideal_edge_bps": self._weighted_average_metric(selected_pairs, "ideal_edge_bps"),
                "executable_edge_bps": self._weighted_average_metric(selected_pairs, "executable_edge_bps"),
                "breakeven_basis_bps": self._weighted_average_metric(selected_pairs, "breakeven_basis_bps"),
                "entry_threshold_bps": self._weighted_average_metric(selected_pairs, "entry_threshold_bps"),
                "exit_threshold_bps": self._weighted_average_metric(selected_pairs, "exit_threshold_bps"),
                "cost_confidence": self._weighted_average_metric(selected_pairs, "cost_confidence"),
                "aggregate_cost_source_flags": sorted(
                    {
                        str(flag)
                        for item in selected_pairs
                        for flag in (item.metrics.get("cost_source_flags") or [])
                        if str(flag).strip()
                    }
                ),
            }
        )
        for key in (
            "spot_symbol",
            "derivatives_symbol",
            "spot_price",
            "derivatives_price",
            "current_account_spot_qty",
            "current_account_cash_spot_qty",
            "current_account_margin_spot_qty",
            "current_account_derivatives_qty",
            "current_sleeve_spot_qty",
            "current_sleeve_cash_spot_qty",
            "current_sleeve_margin_spot_qty",
            "current_sleeve_derivatives_qty",
            "target_account_spot_qty",
            "target_account_derivatives_qty",
            "target_sleeve_spot_qty",
            "target_sleeve_derivatives_qty",
            "foreign_spot_qty",
            "foreign_derivatives_qty",
        ):
            aggregate_metrics.pop(key, None)
        return StrategyCandidate(
            family="smart_arbitrage",
            state=top.state,
            enabled=True,
            selectable=any(item.selectable for item in selected_pairs),
            execution_compatible=any(item.execution_compatible for item in selected_pairs),
            route_action=(
                "override_target"
                if any(item.route_action == "override_target" for item in selected_pairs)
                else "hold_current"
            ),
            headline=(
                "Managing multiple smart arbitrage pairs across non-overlapping companion markets."
                if not overlap_detected
                else "Managing multiple smart arbitrage pairs, but some pair scopes overlap and are being summarized conservatively."
            ),
            recommended_symbol=None,
            target_position_qty=None,
            delta_position_qty=None,
            score=max((item.score for item in selected_pairs), default=top.score),
            confidence=max((item.confidence for item in selected_pairs), default=top.confidence),
            urgency=self._highest_urgency(selected_pairs),
            reason_codes=list(dict.fromkeys(reason for item in selected_pairs for reason in item.reason_codes)),
            pair_id="multi_pair",
            opportunity_kind=top.opportunity_kind,
            execution_mode=next(iter(selected_execution_modes)) if len(selected_execution_modes) == 1 else None,
            state_phase=top.state_phase,
            blocking_reasons=list(dict.fromkeys(reason for item in selected_pairs for reason in item.blocking_reasons)),
            metrics=aggregate_metrics,
            legs=[leg for item in selected_pairs for leg in item.legs],
        )

    def _ranked_candidates(self, candidates: list[StrategyCandidate]) -> list[StrategyCandidate]:
        return sorted(candidates, key=self._candidate_sort_key, reverse=True)

    def _candidate_sort_key(self, candidate: StrategyCandidate) -> tuple[int, int, float]:
        phase_rank = {
            "opening": 7,
            "recovery": 6,
            "unwinding": 5,
            "active": 4,
            "blocked": 3,
            "advisory": 2,
            "inactive": 1,
        }.get(str(candidate.state_phase or "inactive"), 0)
        route_rank = {
            "override_target": 3,
            "hold_current": 2,
            "advisory_only": 1,
        }.get(candidate.route_action, 0)
        if self.settings.smart_arbitrage_pair_priority_mode == "basis_abs":
            priority_metric = abs(float(candidate.metrics.get("basis_bps") or 0.0))
        elif self.settings.smart_arbitrage_pair_priority_mode == "ideal_edge":
            priority_metric = float(candidate.metrics.get("ideal_edge_bps") or candidate.score or 0.0)
        else:
            priority_metric = float(
                candidate.metrics.get("executable_edge_bps")
                or candidate.metrics.get("net_basis_bps")
                or candidate.score
            )
        return (phase_rank, route_rank, priority_metric)

    @staticmethod
    def _sum_metric(candidates: list[StrategyCandidate], key: str) -> Decimal:
        return sum((to_decimal(item.metrics.get(key) or Decimal("0")) for item in candidates), start=Decimal("0"))

    @staticmethod
    def _average_metric(candidates: list[StrategyCandidate], key: str) -> Decimal | None:
        values = [
            to_decimal(item.metrics.get(key))
            for item in candidates
            if item.metrics.get(key) is not None
        ]
        if not values:
            return None
        return sum(values, start=Decimal("0")) / Decimal(len(values))

    @staticmethod
    def _candidate_requested_notional(candidate: StrategyCandidate) -> Decimal:
        pair_notionals: dict[str, Decimal] = {}
        unscoped_total = Decimal("0")
        for leg in candidate.legs:
            delta_qty = abs(to_decimal(leg.delta_position_qty or Decimal("0")))
            reference_price = abs(to_decimal(leg.reference_price or Decimal("0")))
            if delta_qty <= EPSILON_DECIMAL_12 or reference_price <= EPSILON_DECIMAL_12:
                continue
            leg_notional = delta_qty * reference_price
            pair_id = str(leg.pair_id or candidate.pair_id or "").strip()
            if pair_id:
                pair_notionals[pair_id] = max(pair_notionals.get(pair_id, Decimal("0")), leg_notional)
            else:
                unscoped_total += leg_notional
        total = sum(pair_notionals.values(), start=Decimal("0")) + unscoped_total
        if total > EPSILON_DECIMAL_12:
            return total
        target_pair_qty = to_decimal((candidate.metrics or {}).get("target_pair_qty") or Decimal("0"))
        if target_pair_qty <= EPSILON_DECIMAL_12:
            return Decimal("0")
        spot_price = abs(to_decimal((candidate.metrics or {}).get("spot_price") or Decimal("0")))
        hedge_price = abs(to_decimal((candidate.metrics or {}).get("derivatives_price") or Decimal("0")))
        reference_price = max(spot_price, hedge_price)
        if reference_price <= EPSILON_DECIMAL_12:
            return Decimal("0")
        return target_pair_qty * reference_price

    @classmethod
    def _weighted_average_metric(cls, candidates: list[StrategyCandidate], key: str) -> Decimal | None:
        weighted_sum = Decimal("0")
        total_weight = Decimal("0")
        for item in candidates:
            value = (item.metrics or {}).get(key)
            if value is None:
                continue
            weight = cls._candidate_requested_notional(item)
            if weight <= EPSILON_DECIMAL_12:
                continue
            weighted_sum += weight * to_decimal(value)
            total_weight += weight
        if total_weight <= EPSILON_DECIMAL_12:
            return cls._average_metric(candidates, key)
        return weighted_sum / total_weight

    @staticmethod
    def _highest_urgency(candidates: list[StrategyCandidate]) -> str:
        rank = {"low": 1, "medium": 2, "high": 3}
        return max(candidates, key=lambda item: rank.get(str(item.urgency), 0)).urgency

    def _active_reverse_execution_mode(self, pair_state) -> str:
        if (
            pair_state.current_margin_reverse_pair_qty > EPSILON_DECIMAL_12
            or pair_state.current_margin_spot_qty < -EPSILON_DECIMAL_12
        ):
            return "margin_reverse_carry"
        if (
            pair_state.current_inventory_reverse_pair_qty > EPSILON_DECIMAL_12
            or pair_state.current_cash_spot_qty < -EPSILON_DECIMAL_12
        ):
            return "inventory_reverse_carry"
        if self.settings.smart_arbitrage_negative_basis_mode == "margin_backed":
            return "margin_reverse_carry"
        return "inventory_reverse_carry"

    @staticmethod
    def _drag_blocking_reason(*, cost_breakdown) -> str:
        if cost_breakdown.borrow_cost_bps >= max(
            cost_breakdown.funding_cost_bps,
            cost_breakdown.executable_spread_bps + cost_breakdown.executable_slippage_bps,
            Decimal("0"),
        ) and cost_breakdown.borrow_cost_bps > Decimal("0"):
            return "smart_arbitrage_borrow_window_unfavorable"
        if cost_breakdown.funding_cost_bps >= max(
            cost_breakdown.borrow_cost_bps,
            cost_breakdown.executable_spread_bps + cost_breakdown.executable_slippage_bps,
            Decimal("0"),
        ) and cost_breakdown.funding_cost_bps > Decimal("0"):
            return "smart_arbitrage_funding_window_unfavorable"
        return "smart_arbitrage_drag_exceeds_basis"

    @staticmethod
    def _candidate_symbol_scope(candidate: StrategyCandidate) -> tuple[str, ...]:
        metrics = dict(candidate.metrics or {})
        symbols = [
            str(symbol).upper()
            for symbol in (
                metrics.get("spot_symbol"),
                metrics.get("derivatives_symbol"),
                candidate.recommended_symbol,
            )
            if str(symbol or "").strip()
        ]
        return tuple(dict.fromkeys(symbols))

    def _parallel_safe_candidates(
        self,
        *,
        candidates: list[StrategyCandidate],
        existing: list[StrategyCandidate],
        limit: int,
    ) -> list[StrategyCandidate]:
        if limit <= 0:
            return []
        selected: list[StrategyCandidate] = []
        seen_symbols = {
            symbol
            for candidate in existing
            for symbol in self._candidate_symbol_scope(candidate)
        }
        for candidate in candidates:
            scope = set(self._candidate_symbol_scope(candidate))
            if seen_symbols & scope:
                continue
            selected.append(candidate)
            seen_symbols.update(scope)
            if len(selected) >= limit:
                break
        return selected

    def _selected_pairs_have_overlapping_symbol_scope(self, candidates: list[StrategyCandidate]) -> bool:
        seen_symbols: set[str] = set()
        for candidate in candidates:
            scope = {
                symbol
                for symbol in self._candidate_symbol_scope(candidate)
                if str(symbol).strip()
            }
            if seen_symbols & scope:
                return True
            seen_symbols.update(scope)
        return False

    @staticmethod
    def _selected_spot_quantities(
        *,
        opportunity: ArbitrageOpportunity,
        pair_state,
        account_cash_spot_qty: Decimal,
        account_margin_spot_qty: Decimal,
        sleeve_cash_spot_qty: Decimal,
        sleeve_margin_spot_qty: Decimal,
    ) -> tuple[Decimal, Decimal]:
        execution_mode = opportunity.execution_mode
        if execution_mode is None and pair_state.current_direction == "reverse_carry":
            execution_mode = (
                "margin_reverse_carry"
                if pair_state.current_margin_reverse_pair_qty > EPSILON_DECIMAL_12
                else "inventory_reverse_carry"
            )
        if execution_mode == "margin_reverse_carry":
            return to_decimal(account_margin_spot_qty), to_decimal(sleeve_margin_spot_qty)
        return to_decimal(account_cash_spot_qty), to_decimal(sleeve_cash_spot_qty)

    def _latest_account_snapshot(self) -> ExchangeAccountSnapshot | None:
        if self.account_snapshot_loader is None:
            return None
        snapshot = self.account_snapshot_loader()
        return snapshot if isinstance(snapshot, ExchangeAccountSnapshot) or snapshot is None else None

    @staticmethod
    def _current_spot_quantity(
        *,
        snapshot: ExchangeAccountSnapshot | None,
        engine_input: StrategyEngineInput,
        spot_symbol: str,
        margin_mode: str,
    ) -> Decimal:
        if engine_input.latest_snapshot is not None:
            quantity, matched_positions = SmartArbitrageStrategyEngine._spot_quantity_from_portfolio_snapshot(
                engine_input=engine_input,
                spot_symbol=spot_symbol,
                margin_mode=margin_mode,
            )
            if matched_positions:
                return quantity
            if margin_mode == "cash":
                base_currency = spot_symbol.split("-", 1)[0]
                if base_currency in engine_input.latest_snapshot.balances:
                    return to_decimal(engine_input.latest_snapshot.balances[base_currency])
        if snapshot is not None:
            quantity, matched_positions = SmartArbitrageStrategyEngine._spot_quantity_from_account_snapshot(
                snapshot=snapshot,
                spot_symbol=spot_symbol,
                margin_mode=margin_mode,
            )
            if matched_positions:
                return quantity
            if margin_mode == "cash":
                base_currency = spot_symbol.split("-", 1)[0]
                for balance in snapshot.balances:
                    if balance.currency.upper() == base_currency.upper():
                        return to_decimal(balance.total)
        return Decimal("0")

    @staticmethod
    def _current_hedge_quantity(
        *,
        snapshot: ExchangeAccountSnapshot | None,
        engine_input: StrategyEngineInput,
        hedge_symbol: str,
    ) -> Decimal:
        if snapshot is not None:
            quantity = Decimal("0")
            for position in snapshot.positions:
                if position.symbol != hedge_symbol:
                    continue
                signed_qty = to_decimal(position.quantity)
                if str(position.side or "net").lower() == "short" and signed_qty > 0:
                    signed_qty = -signed_qty
                quantity += signed_qty
            if abs(quantity) > EPSILON_DECIMAL_12:
                return quantity
        if hedge_symbol == engine_input.context.symbol:
            return to_decimal(engine_input.context.current_position_qty)
        if engine_input.latest_snapshot is not None:
            quantity = sum(
                (
                    to_decimal(position.position_qty)
                    for position in engine_input.latest_snapshot.positions
                    if position.symbol == hedge_symbol
                ),
                start=Decimal("0"),
            )
            if abs(quantity) > EPSILON_DECIMAL_12:
                return quantity
        return Decimal("0")

    def _current_sleeve_quantity(
        self,
        *,
        engine_input: StrategyEngineInput,
        primary_symbol: str,
        sleeve_product_scope: str,
        sleeve_margin_scope: str,
        symbol_scope: tuple[str, ...],
        symbol: str,
        leg_product_type: str,
        leg_margin_mode: str,
    ) -> Decimal:
        if self.sleeve_inventory_loader is None:
            if symbol == engine_input.context.symbol:
                return to_decimal(engine_input.context.current_position_qty)
            return Decimal("0")
        return to_decimal(
            self.sleeve_inventory_loader.quantity_for_strategy(
                family="smart_arbitrage",
                primary_symbol=primary_symbol,
                product_scope=sleeve_product_scope,
                margin_scope=sleeve_margin_scope,
                symbol_scope=symbol_scope,
                symbol=symbol,
                product_type=leg_product_type,
                margin_mode=leg_margin_mode,
            )
        )

    @staticmethod
    def _spot_quantity_from_portfolio_snapshot(
        *,
        engine_input: StrategyEngineInput,
        spot_symbol: str,
        margin_mode: str,
    ) -> tuple[Decimal, bool]:
        if engine_input.latest_snapshot is None:
            return Decimal("0"), False
        positions = [
            position
            for position in engine_input.latest_snapshot.positions
            if position.symbol == spot_symbol
            and position.product_type == "spot"
            and SmartArbitrageStrategyEngine._spot_margin_mode_matches(
                requested_margin_mode=margin_mode,
                position_margin_mode=position.margin_mode,
            )
        ]
        if not positions:
            return Decimal("0"), False
        return sum((to_decimal(position.position_qty) for position in positions), start=Decimal("0")), True

    @staticmethod
    def _spot_quantity_from_account_snapshot(
        *,
        snapshot: ExchangeAccountSnapshot,
        spot_symbol: str,
        margin_mode: str,
    ) -> tuple[Decimal, bool]:
        quantity = Decimal("0")
        matched = False
        for position in snapshot.positions:
            if position.symbol != spot_symbol:
                continue
            if not SmartArbitrageStrategyEngine._spot_margin_mode_matches(
                requested_margin_mode=margin_mode,
                position_margin_mode=getattr(position, "margin_mode", None),
            ):
                continue
            matched = True
            signed_qty = to_decimal(position.quantity)
            if str(position.side or "net").lower() == "short" and signed_qty > 0:
                signed_qty = -signed_qty
            quantity += signed_qty
        return quantity, matched

    @staticmethod
    def _spot_margin_mode_matches(
        *,
        requested_margin_mode: str,
        position_margin_mode: str | None,
    ) -> bool:
        resolved_requested = str(requested_margin_mode or "cash").strip().lower()
        resolved_position_mode = str(position_margin_mode or "cash").strip().lower()
        return resolved_requested == resolved_position_mode
