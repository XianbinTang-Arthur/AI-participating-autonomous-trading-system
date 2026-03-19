from __future__ import annotations

from decimal import Decimal
from typing import Callable

from aats.schemas.execution import FillEvent
from aats.schemas.portfolio import PortfolioSnapshot
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder


class PortfolioReconstructionService:
    def __init__(
        self,
        *,
        initial_usdt_balance: Decimal | float,
        snapshot_builder: PortfolioSnapshotBuilder,
    ) -> None:
        self.initial_usdt_balance = initial_usdt_balance
        self.snapshot_builder = snapshot_builder

    def rebuild_state(self, fills: list[FillEvent]) -> PortfolioState:
        state = PortfolioState(initial_usdt_balance=self.initial_usdt_balance)
        for fill in sorted(fills, key=lambda item: (item.ingestion_timestamp, item.fill_id)):
            state.apply_fill(fill)
        return state

    def rebuild_snapshot(
        self,
        *,
        fills: list[FillEvent],
        price_provider: Callable[[str], Decimal],
    ) -> PortfolioSnapshot:
        state = self.rebuild_state(fills)
        return self.snapshot_builder.build(state=state, price_provider=price_provider)
