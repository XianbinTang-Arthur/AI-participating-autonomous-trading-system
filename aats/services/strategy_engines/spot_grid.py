from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.strategy_runtime import StrategyCandidate
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.base import StrategyEngineInput
from aats.services.trade_costs import TradeCostService


class SpotGridStrategyEngine:
    def __init__(self, *, settings: AATSSettings, sleeve_inventory_loader=None, account_service=None) -> None:
        self.settings = settings
        self.sleeve_inventory_loader = sleeve_inventory_loader
        self.trade_cost_service = TradeCostService(settings=settings, account_service=account_service)

    def evaluate(self, engine_input: StrategyEngineInput) -> StrategyCandidate:
        if not self.settings.spot_grid_enabled:
            return StrategyCandidate(
                family="spot_grid",
                state="disabled",
                enabled=False,
                selectable=False,
                execution_compatible=False,
                route_action="hold_current",
                headline="Spot grid is disabled.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["spot_grid_disabled"],
            )
        if engine_input.context.product_type != "spot":
            return StrategyCandidate(
                family="spot_grid",
                state="incompatible",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="hold_current",
                headline="Spot grid only supports spot runtime.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["spot_grid_spot_runtime_required"],
            )
        if self.settings.spot_grid_breakout_guard_enabled and engine_input.baseline.regime not in {"range", "uncertain"}:
            return StrategyCandidate(
                family="spot_grid",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=True,
                route_action="hold_current",
                headline="Market regime is not range-bound enough for spot grid.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["spot_grid_regime_blocked"],
                metrics={"market_regime": engine_input.baseline.regime},
            )
        price = None if engine_input.latest_market_snapshot is None else to_decimal(engine_input.latest_market_snapshot.last_price)
        recent_snapshots = engine_input.recent_market_snapshots.get(engine_input.context.symbol, [])
        if price is None or not recent_snapshots:
            return StrategyCandidate(
                family="spot_grid",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=True,
                route_action="hold_current",
                headline="Anchor history is insufficient for spot grid.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["spot_grid_anchor_history_insufficient"],
            )
        anchor = sum((to_decimal(item.last_price) for item in recent_snapshots), start=Decimal("0")) / Decimal(
            len(recent_snapshots)
        )
        band_bps = Decimal(str(max(self.settings.spot_grid_band_bps, 1.0)))
        band_width = anchor * band_bps / Decimal("10000")
        if band_width <= EPSILON_DECIMAL_12:
            return StrategyCandidate(
                family="spot_grid",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=True,
                route_action="hold_current",
                headline="Spot grid band width is invalid.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["spot_grid_band_invalid"],
            )

        max_position_qty = to_decimal(self.settings.max_abs_position_qty)
        floor_fraction = Decimal(str(max(min(self.settings.spot_grid_inventory_floor_fraction, 1.0), 0.0)))
        ceiling_fraction = Decimal(str(max(min(self.settings.spot_grid_inventory_ceiling_fraction, 1.0), 0.0)))
        if ceiling_fraction < floor_fraction:
            ceiling_fraction = floor_fraction
        min_inventory = max_position_qty * floor_fraction
        max_inventory = max_position_qty * ceiling_fraction
        lower = anchor - band_width
        upper = anchor + band_width
        clamped_price = min(max(price, lower), upper)
        distance_from_upper = upper - clamped_price
        target_fraction = distance_from_upper / (upper - lower) if upper > lower else Decimal("0.5")
        target_qty = min_inventory + ((max_inventory - min_inventory) * target_fraction)
        account_current_qty = to_decimal(engine_input.context.current_position_qty)
        sleeve_current_qty = self._current_sleeve_quantity(engine_input)
        sleeve_delta_qty = target_qty - sleeve_current_qty
        account_target_qty = account_current_qty + sleeve_delta_qty
        min_rebalance_qty = max_position_qty * Decimal(
            str(max(min(self.settings.spot_grid_rebalance_min_fraction_of_max_qty, 1.0), 0.0))
        )
        if abs(sleeve_delta_qty) < max(min_rebalance_qty, Decimal("1e-8")):
            state = "inactive"
            selectable = False
            headline = "Current inventory is already close to the grid target."
            reason_codes = ["spot_grid_rebalance_not_required"]
        else:
            state = "ready"
            selectable = True
            headline = "Spot grid inventory target is ready."
            reason_codes = ["spot_grid_inventory_target_ready", f"regime_{engine_input.baseline.regime}"]
        score = float(abs(price - anchor) / band_width)
        confidence = min(0.92, 0.50 + (engine_input.baseline.confidence * 0.25) + (score * 0.1))
        cost_estimate = self.trade_cost_service.estimate_single_leg_entry(
            model_name="spot_grid_inventory_rebalance",
            symbol=engine_input.context.symbol,
            product_type="spot",
            margin_mode="cash",
            include_spread=True,
        )
        return StrategyCandidate(
            family="spot_grid",
            state=state,
            enabled=True,
            selectable=selectable,
            execution_compatible=True,
            route_action="override_target" if selectable else "hold_current",
            headline=headline,
            recommended_symbol=engine_input.context.symbol,
            target_position_qty=account_target_qty,
            delta_position_qty=sleeve_delta_qty,
            score=score,
            confidence=confidence,
            urgency="medium" if selectable else "low",
            reason_codes=reason_codes,
            metrics={
                "anchor_price": anchor,
                "current_price": price,
                "lower_band": lower,
                "upper_band": upper,
                "current_account_position_qty": account_current_qty,
                "current_sleeve_position_qty": sleeve_current_qty,
                "inventory_floor_qty": min_inventory,
                "inventory_ceiling_qty": max_inventory,
                "target_inventory_fraction": target_fraction,
                "target_sleeve_position_qty": target_qty,
                "target_account_position_qty": account_target_qty,
                "expected_cost_bps": cost_estimate.executable_total_drag_bps,
                "ideal_cost_bps": cost_estimate.ideal_total_cost_bps,
                "executable_cost_bps": cost_estimate.executable_total_drag_bps,
            },
        )

    def _current_sleeve_quantity(self, engine_input: StrategyEngineInput) -> Decimal:
        if self.sleeve_inventory_loader is None:
            return to_decimal(engine_input.context.current_position_qty)
        return to_decimal(
            self.sleeve_inventory_loader.quantity_for_strategy(
                family="spot_grid",
                primary_symbol=engine_input.context.symbol,
                product_scope=engine_input.context.product_type,
                margin_scope=self.settings.margin_mode,
                symbol_scope=(engine_input.context.symbol,),
                symbol=engine_input.context.symbol,
                product_type=engine_input.context.product_type,
                margin_mode=self.settings.margin_mode,
            )
        )
