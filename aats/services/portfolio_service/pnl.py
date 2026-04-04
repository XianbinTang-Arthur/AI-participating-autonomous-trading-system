from __future__ import annotations

from decimal import Decimal

from aats.services.portfolio_service.decimals import to_decimal


class PortfolioPnLCalculator:
    def unrealized_pnl(
        self,
        *,
        position_qty: Decimal | float,
        avg_entry_price: Decimal | float,
        mark_price: Decimal | float,
    ) -> Decimal:
        """Compute unrealized PnL as (mark - entry) * qty.

        ``position_qty`` is the raw signed quantity (positive=long, negative=short),
        **not** adjusted by leverage.  Leverage only affects margin requirements
        (handled separately in snapshot building), not PnL magnitude.
        """
        return (to_decimal(mark_price) - to_decimal(avg_entry_price)) * to_decimal(position_qty)
