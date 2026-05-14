from __future__ import annotations

from decimal import Decimal

from aats.bootstrap.settings import AATSSettings
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.strategy_engines.smart_arbitrage.schemas import ArbitrageExecutionCapability


def entry_pair_qty(
    *,
    settings: AATSSettings,
    spot_price: Decimal,
    capability: ArbitrageExecutionCapability,
    execution_mode: str,
    available_quote_budget: Decimal | None = None,
) -> Decimal:
    if spot_price <= EPSILON_DECIMAL_12:
        return Decimal("0")
    quote_budget = to_decimal(settings.smart_arbitrage_quote_budget_per_trade)
    notional_cap = to_decimal(settings.smart_arbitrage_max_pair_notional)
    positive_limits = [value for value in (quote_budget, notional_cap) if value > EPSILON_DECIMAL_12]
    if available_quote_budget is not None:
        available_quote_budget = max(to_decimal(available_quote_budget), Decimal("0"))
        if available_quote_budget <= EPSILON_DECIMAL_12:
            return Decimal("0")
        positive_limits.append(available_quote_budget)
    effective_notional = min(positive_limits) if positive_limits else Decimal("0")
    if effective_notional <= EPSILON_DECIMAL_12:
        return Decimal("0")
    desired_qty = effective_notional / spot_price
    if execution_mode == "inventory_reverse_carry":
        if capability.available_inventory_qty <= EPSILON_DECIMAL_12:
            return Decimal("0")
        desired_qty = min(desired_qty, capability.available_inventory_qty)
    return desired_qty
