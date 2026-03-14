from __future__ import annotations


class PortfolioPnLCalculator:
    def unrealized_pnl(
        self,
        *,
        position_qty: float,
        avg_entry_price: float,
        mark_price: float,
    ) -> float:
        return (mark_price - avg_entry_price) * position_qty

