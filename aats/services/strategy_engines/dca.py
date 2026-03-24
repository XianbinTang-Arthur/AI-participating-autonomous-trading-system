from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.strategy_runtime import StrategyCandidate
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.base import StrategyEngineInput


class DcaStrategyEngine:
    def __init__(self, *, settings: AATSSettings) -> None:
        self.settings = settings

    def evaluate(self, engine_input: StrategyEngineInput) -> StrategyCandidate:
        if not self.settings.dca_enabled:
            return StrategyCandidate(
                family="dca",
                state="disabled",
                enabled=False,
                selectable=False,
                execution_compatible=False,
                route_action="hold_current",
                headline="DCA is disabled.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["dca_disabled"],
            )
        if engine_input.context.product_type != "spot":
            return StrategyCandidate(
                family="dca",
                state="incompatible",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="hold_current",
                headline="DCA only supports spot runtime.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["dca_spot_runtime_required"],
            )
        price = None if engine_input.latest_market_snapshot is None else to_decimal(engine_input.latest_market_snapshot.last_price)
        if price is None or price <= EPSILON_DECIMAL_12:
            return StrategyCandidate(
                family="dca",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=True,
                route_action="hold_current",
                headline="Current price is unavailable for DCA.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["dca_price_missing"],
            )
        max_position_qty = to_decimal(self.settings.max_abs_position_qty) * Decimal(
            str(max(min(self.settings.dca_max_position_fraction_of_limit, 1.0), 0.0))
        )
        current_qty = to_decimal(engine_input.context.current_position_qty)
        if current_qty >= max_position_qty - Decimal("1e-8"):
            return StrategyCandidate(
                family="dca",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=True,
                route_action="hold_current",
                headline="DCA position cap is already reached.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["dca_position_cap_reached"],
                metrics={"position_cap_qty": max_position_qty},
            )
        last_dca_target_at = self._last_dca_target_at(engine_input)
        interval_seconds = max(self.settings.dca_interval_seconds, 0.0)
        if last_dca_target_at is not None and interval_seconds > 0.0:
            elapsed_seconds = (engine_input.context.as_of_ts - last_dca_target_at).total_seconds()
            if elapsed_seconds < interval_seconds:
                remaining = max(interval_seconds - elapsed_seconds, 0.0)
                return StrategyCandidate(
                    family="dca",
                    state="inactive",
                    enabled=True,
                    selectable=False,
                    execution_compatible=True,
                    route_action="hold_current",
                    headline="The DCA interval has not elapsed yet.",
                    recommended_symbol=engine_input.context.symbol,
                    reason_codes=["dca_interval_not_elapsed"],
                    metrics={
                        "elapsed_seconds": elapsed_seconds,
                        "remaining_seconds": remaining,
                        "interval_seconds": interval_seconds,
                    },
                )
        recent_snapshots = engine_input.recent_market_snapshots.get(engine_input.context.symbol, [])
        if self.settings.dca_pullback_only_enabled and recent_snapshots:
            anchor = sum((to_decimal(item.last_price) for item in recent_snapshots), start=Decimal("0")) / Decimal(
                len(recent_snapshots)
            )
            pullback_bps = Decimal(str(max(self.settings.dca_pullback_entry_bps, 0.0)))
            required_price = anchor * (Decimal("1") - (pullback_bps / Decimal("10000")))
            if price > required_price:
                return StrategyCandidate(
                    family="dca",
                    state="inactive",
                    enabled=True,
                    selectable=False,
                    execution_compatible=True,
                    route_action="hold_current",
                    headline="Pullback-only DCA trigger is not met.",
                    recommended_symbol=engine_input.context.symbol,
                    reason_codes=["dca_pullback_not_met"],
                    metrics={
                        "anchor_price": anchor,
                        "current_price": price,
                        "required_price": required_price,
                    },
                )
        quote_budget = Decimal(str(max(self.settings.dca_quote_budget_per_cycle, 0.0)))
        tranche_qty = (quote_budget / price) if price > EPSILON_DECIMAL_12 else Decimal("0")
        if tranche_qty <= Decimal("1e-8"):
            return StrategyCandidate(
                family="dca",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=True,
                route_action="hold_current",
                headline="Configured DCA tranche is too small.",
                recommended_symbol=engine_input.context.symbol,
                reason_codes=["dca_tranche_too_small"],
                metrics={"quote_budget": quote_budget, "current_price": price},
            )
        target_qty = min(current_qty + tranche_qty, max_position_qty)
        delta_qty = target_qty - current_qty
        confidence = min(0.90, 0.55 + (engine_input.baseline.confidence * 0.15))
        return StrategyCandidate(
            family="dca",
            state="ready",
            enabled=True,
            selectable=True,
            execution_compatible=True,
            route_action="override_target",
            headline="DCA interval is ready and a new tranche can be accumulated.",
            recommended_symbol=engine_input.context.symbol,
            target_position_qty=target_qty,
            delta_position_qty=delta_qty,
            score=float(delta_qty / max(max_position_qty, Decimal("1e-8"))),
            confidence=confidence,
            urgency="low",
            reason_codes=["dca_interval_elapsed", "dca_budget_ready"],
            metrics={
                "quote_budget": quote_budget,
                "current_price": price,
                "tranche_qty": tranche_qty,
                "target_position_qty": target_qty,
                "position_cap_qty": max_position_qty,
            },
        )

    @staticmethod
    def _last_dca_target_at(engine_input: StrategyEngineInput) -> datetime | None:
        history = engine_input.recent_targets_by_family.get("dca") or []
        for item in history:
            target = item.target
            if str(getattr(target, "strategy_route_action", "")) != "override_target":
                continue
            if abs(to_decimal(getattr(target, "delta_position_qty", Decimal("0")))) <= EPSILON_DECIMAL_12:
                continue
            created_at = item.created_at
            if isinstance(created_at, datetime):
                return created_at
        return None
