from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.services.portfolio_service.decimals import to_decimal
from aats.services.strategy_engines.smart_arbitrage.schemas import ArbitrageCostBreakdown


def build_cost_breakdown(
    *,
    settings: AATSSettings,
    basis_bps: Decimal,
    execution_mode: str | None,
) -> ArbitrageCostBreakdown:
    if not settings.smart_arbitrage_cost_model_enabled:
        total_cost_bps = to_decimal(settings.smart_arbitrage_estimated_cost_bps)
        return ArbitrageCostBreakdown(
            estimated_total_cost_bps=total_cost_bps,
            net_edge_bps=basis_bps.copy_abs() - total_cost_bps,
        )

    fee_bps = to_decimal(settings.smart_arbitrage_estimated_fee_bps)
    slippage_bps = to_decimal(settings.smart_arbitrage_estimated_slippage_bps)
    funding_bps = (
        to_decimal(settings.smart_arbitrage_estimated_funding_bps)
        if settings.smart_arbitrage_funding_cost_enabled
        else Decimal("0")
    )
    borrow_bps = (
        to_decimal(settings.smart_arbitrage_estimated_borrow_bps)
        if settings.smart_arbitrage_borrow_cost_enabled and execution_mode == "margin_reverse_carry"
        else Decimal("0")
    )
    inventory_cost_bps = Decimal("0")
    total_cost_bps = fee_bps + slippage_bps + funding_bps + borrow_bps + inventory_cost_bps
    if total_cost_bps <= Decimal("0"):
        total_cost_bps = to_decimal(settings.smart_arbitrage_estimated_cost_bps)
    return ArbitrageCostBreakdown(
        estimated_fee_bps=fee_bps,
        estimated_slippage_bps=slippage_bps,
        estimated_funding_bps=funding_bps,
        estimated_borrow_bps=borrow_bps,
        estimated_inventory_cost_bps=inventory_cost_bps,
        estimated_total_cost_bps=total_cost_bps,
        net_edge_bps=basis_bps.copy_abs() - total_cost_bps,
    )
