from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.smart_arbitrage.schemas import (
    ArbitrageExecutionCapability,
    ArbitragePairDefinition,
)


def resolve_execution_capability(
    *,
    settings: AATSSettings,
    pair: ArbitragePairDefinition,
    account_spot_qty: Decimal,
) -> ArbitrageExecutionCapability:
    del pair
    available_inventory_qty = Decimal("0")
    blocking_reasons: list[str] = []
    requested_mode = settings.smart_arbitrage_negative_basis_mode
    inventory_enabled = bool(
        settings.smart_arbitrage_inventory_reservation_enabled
        and requested_mode == "inventory_backed"
    )
    if inventory_enabled:
        available_inventory_qty = max(to_decimal(account_spot_qty), Decimal("0"))
        if available_inventory_qty <= EPSILON_DECIMAL_12:
            blocking_reasons.append("smart_arbitrage_inventory_backed_spot_balance_unavailable")
    margin_short_requested = requested_mode == "margin_backed"
    spot_margin_short_supported = bool(settings.smart_arbitrage_margin_short_enabled and margin_short_requested)
    spot_margin_mode = (
        settings.smart_arbitrage_margin_short_spot_margin_mode
        if settings.smart_arbitrage_margin_short_spot_margin_mode in {"cross", "isolated"}
        else "cross"
    )
    margin_short_execution_ready = bool(
        spot_margin_short_supported and settings.smart_arbitrage_margin_short_execution_ready
    )
    if margin_short_requested and not spot_margin_short_supported:
        blocking_reasons.append("smart_arbitrage_margin_short_disabled")
    elif margin_short_requested and not margin_short_execution_ready:
        blocking_reasons.append("smart_arbitrage_margin_short_execution_not_ready")
    return ArbitrageExecutionCapability(
        runtime_supported=True,
        inventory_backed_spot_sell_supported=inventory_enabled and available_inventory_qty > EPSILON_DECIMAL_12,
        spot_margin_short_supported=spot_margin_short_supported,
        spot_margin_mode=spot_margin_mode,  # type: ignore[arg-type]
        margin_short_execution_ready=margin_short_execution_ready,
        derivatives_short_supported=True,
        funding_data_available=bool(settings.smart_arbitrage_funding_cost_enabled),
        borrow_rate_available=bool(settings.smart_arbitrage_borrow_cost_enabled),
        multi_leg_recovery_supported=True,
        inventory_reservation_enabled=inventory_enabled,
        auto_repay_supported=bool(settings.smart_arbitrage_margin_short_auto_repay_enabled),
        available_inventory_qty=available_inventory_qty,
        blocking_reasons=blocking_reasons,
    )
