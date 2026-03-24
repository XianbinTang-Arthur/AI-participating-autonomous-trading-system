from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.schemas.strategy_runtime import StrategyCandidate, StrategyLegIntent
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.base import StrategyEngineInput


def _derived_spot_symbol(symbol: str) -> str | None:
    normalized = str(symbol or "").upper()
    if not normalized:
        return None
    if normalized.endswith("-SWAP"):
        return normalized[:-5]
    tail = normalized.rsplit("-", 1)[-1]
    if tail.isdigit():
        return normalized[: -(len(tail) + 1)]
    return normalized


def _derived_derivatives_symbol(symbol: str) -> str | None:
    normalized = str(symbol or "").upper()
    if not normalized:
        return None
    if normalized.endswith("-SWAP"):
        return normalized
    tail = normalized.rsplit("-", 1)[-1]
    if tail.isdigit():
        return normalized
    return f"{normalized}-SWAP"


class SmartArbitrageStrategyEngine:
    def __init__(self, *, settings: AATSSettings, market_snapshot_loader) -> None:
        self.settings = settings
        self.market_snapshot_loader = market_snapshot_loader

    def evaluate(self, engine_input: StrategyEngineInput) -> StrategyCandidate:
        spot_symbol = (
            self.settings.smart_arbitrage_companion_spot_symbol
            or _derived_spot_symbol(engine_input.context.symbol)
        )
        derivatives_symbol = (
            self.settings.smart_arbitrage_companion_derivatives_symbol
            or _derived_derivatives_symbol(engine_input.context.symbol)
        )
        if not self.settings.smart_arbitrage_enabled:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="disabled",
                enabled=False,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Smart arbitrage is disabled.",
                recommended_symbol=spot_symbol or engine_input.context.symbol,
                reason_codes=["smart_arbitrage_disabled"],
            )
        if not spot_symbol or not derivatives_symbol:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Spot and derivatives companion symbols are not configured.",
                reason_codes=["smart_arbitrage_symbol_pair_missing"],
            )
        spot_snapshot = self.market_snapshot_loader(spot_symbol)
        derivatives_snapshot = self.market_snapshot_loader(derivatives_symbol)
        if spot_snapshot is None or derivatives_snapshot is None:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Paired market snapshots are incomplete.",
                recommended_symbol=spot_symbol,
                reason_codes=["smart_arbitrage_market_pair_incomplete"],
                metrics={
                    "spot_symbol": spot_symbol,
                    "derivatives_symbol": derivatives_symbol,
                },
            )

        spot_price = to_decimal(spot_snapshot.last_price)
        derivatives_price = to_decimal(derivatives_snapshot.last_price)
        if abs(spot_price) <= EPSILON_DECIMAL_12:
            basis_bps = Decimal("0")
        else:
            basis_bps = ((derivatives_price - spot_price) / spot_price) * Decimal("10000")
        entry_threshold = Decimal(str(max(self.settings.smart_arbitrage_basis_entry_bps, 0.0)))
        estimated_cost_bps = Decimal(str(max(self.settings.smart_arbitrage_estimated_cost_bps, 0.0)))
        net_basis_bps = basis_bps.copy_abs() - estimated_cost_bps
        if basis_bps.copy_abs() < entry_threshold:
            return StrategyCandidate(
                family="smart_arbitrage",
                state="inactive",
                enabled=True,
                selectable=False,
                execution_compatible=False,
                route_action="advisory_only",
                headline="Basis is below the configured entry threshold.",
                recommended_symbol=spot_symbol,
                reason_codes=["smart_arbitrage_basis_below_entry_threshold"],
                metrics={
                    "spot_symbol": spot_symbol,
                    "derivatives_symbol": derivatives_symbol,
                    "spot_price": spot_price,
                    "derivatives_price": derivatives_price,
                    "basis_bps": basis_bps,
                    "net_basis_bps": net_basis_bps,
                },
            )

        if basis_bps >= Decimal("0"):
            legs = [
                StrategyLegIntent(
                    symbol=spot_symbol,
                    product_type="spot",
                    side="buy",
                    role="primary",
                    note="Long the spot leg.",
                ),
                StrategyLegIntent(
                    symbol=derivatives_symbol,
                    product_type="derivatives",
                    side="sell",
                    role="hedge",
                    note="Short the derivatives leg as hedge.",
                ),
            ]
            reason_codes = ["smart_arbitrage_positive_basis", "smart_arbitrage_dual_leg_runtime_required"]
            headline = "Positive basis detected."
        else:
            legs = [
                StrategyLegIntent(
                    symbol=derivatives_symbol,
                    product_type="derivatives",
                    side="buy",
                    role="primary",
                    note="Long the derivatives leg.",
                ),
                StrategyLegIntent(
                    symbol=spot_symbol,
                    product_type="spot",
                    side="sell",
                    role="hedge",
                    note="Sell the spot leg as hedge.",
                ),
            ]
            reason_codes = ["smart_arbitrage_negative_basis", "smart_arbitrage_dual_leg_runtime_required"]
            headline = "Negative basis detected."
        score = float(max(net_basis_bps, Decimal("0")) / max(entry_threshold, Decimal("1")))
        confidence = min(0.95, 0.45 + (min(abs(float(basis_bps)), 120.0) / 200.0))
        return StrategyCandidate(
            family="smart_arbitrage",
            state="advisory_only",
            enabled=True,
            selectable=True,
            execution_compatible=False,
            route_action="advisory_only",
            headline=f"{headline} Current runtime keeps this strategy advisory-only until paired execution is supported.",
            recommended_symbol=spot_symbol,
            score=score,
            confidence=confidence,
            urgency="medium",
            reason_codes=reason_codes,
            metrics={
                "spot_symbol": spot_symbol,
                "derivatives_symbol": derivatives_symbol,
                "spot_price": spot_price,
                "derivatives_price": derivatives_price,
                "basis_bps": basis_bps,
                "net_basis_bps": net_basis_bps,
                "estimated_cost_bps": estimated_cost_bps,
            },
            legs=legs,
        )
