from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from aats.schemas.common import utc_now
from aats.schemas.portfolio import PortfolioSnapshot, Position
from aats.services.portfolio_service.pnl import PortfolioPnLCalculator

if TYPE_CHECKING:
    from aats.services.portfolio_service.positions import PortfolioState


class PortfolioSnapshotBuilder:
    def __init__(self, *, pnl_calculator: PortfolioPnLCalculator) -> None:
        self.pnl_calculator = pnl_calculator

    def build(
        self,
        *,
        state: "PortfolioState",
        price_provider: Callable[[str], float],
        decision_id: str | None = None,
        source_intent_id: str | None = None,
        source_fill_id: str | None = None,
    ) -> PortfolioSnapshot:
        positions: list[Position] = []
        gross_exposure = 0.0
        net_exposure = 0.0
        unrealized_pnl = 0.0
        marked_value = 0.0

        for symbol, record in state.positions.items():
            mark_price = price_provider(symbol)
            position_notional = record.quantity * mark_price
            position_unrealized = self.pnl_calculator.unrealized_pnl(
                position_qty=record.quantity,
                avg_entry_price=record.avg_entry_price,
                mark_price=mark_price,
            )
            positions.append(
                Position(
                    symbol=symbol,
                    position_qty=record.quantity,
                    position_notional=position_notional,
                    avg_entry_price=record.avg_entry_price,
                    unrealized_pnl=position_unrealized,
                )
            )
            gross_exposure += abs(position_notional)
            net_exposure += position_notional
            unrealized_pnl += position_unrealized
            marked_value += position_notional

        balances = dict(state.balances)
        total_equity = balances.get("USDT", 0.0) + marked_value
        return PortfolioSnapshot(
            decision_id=decision_id,
            source_intent_id=source_intent_id,
            source_fill_id=source_fill_id,
            snapshot_ts=utc_now(),
            balances=balances,
            positions=positions,
            cost_basis={symbol: record.avg_entry_price for symbol, record in state.positions.items()},
            realized_pnl=state.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_equity=total_equity,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            risk_budget_usage={"gross_exposure": gross_exposure},
        )
