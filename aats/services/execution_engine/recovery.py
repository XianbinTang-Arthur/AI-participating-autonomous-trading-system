from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from aats.schemas.portfolio import PortfolioSnapshot
from aats.schemas.execution import FillEvent
from aats.schemas.system import RecoveryStatus
from aats.services.governance_engine.kill_switch import KillSwitch
from aats.services.portfolio_service.positions import PortfolioState
from aats.services.portfolio_service.reconstruction import PortfolioReconstructionService
from aats.storage.base import ExecutionRepository, PortfolioRepository


@dataclass(slots=True)
class RecoveryArtifacts:
    status: RecoveryStatus
    rebuilt_snapshot_saved: bool = False


class ExecutionRecoveryService:
    def __init__(
        self,
        *,
        execution_repo: ExecutionRepository,
        portfolio_repo: PortfolioRepository,
        reconstruction_service: PortfolioReconstructionService,
        price_provider: Callable[[str], float],
        kill_switch: KillSwitch,
        bootstrap_portfolio_from_exchange: bool,
    ) -> None:
        self.execution_repo = execution_repo
        self.portfolio_repo = portfolio_repo
        self.reconstruction_service = reconstruction_service
        self.price_provider = price_provider
        self.kill_switch = kill_switch
        self.bootstrap_portfolio_from_exchange = bootstrap_portfolio_from_exchange

    def recover(self, *, portfolio_state: PortfolioState) -> RecoveryArtifacts:
        fills = self.execution_repo.fills()
        latest_snapshot = self.portfolio_repo.latest()
        open_orders = self.execution_repo.open_order_states()
        notes: list[str] = []
        rebuilt_snapshot_saved = False
        divergence_count = 0
        recovery_action: str | None = None

        if latest_snapshot is not None:
            portfolio_state.load_portfolio_snapshot(
                latest_snapshot,
                applied_fill_ids={fill.fill_id for fill in fills},
                total_fees_paid=sum(fill.fee_amount for fill in fills),
            )
            if self.bootstrap_portfolio_from_exchange:
                notes.append("reconstruction_validation_skipped_bootstrap_exchange")
            else:
                rebuilt = self.reconstruction_service.rebuild_snapshot(
                    fills=fills,
                    price_provider=self._recovery_price_provider(latest_snapshot),
                )
                divergence_count = self._divergence_count(latest_snapshot, rebuilt)
                if divergence_count:
                    self.kill_switch.halt(reason="recovery_portfolio_divergence")
                    recovery_action = "halted_for_portfolio_divergence"
                    notes.append("stored_snapshot_differs_from_fill_reconstruction")
        elif fills:
            if self.bootstrap_portfolio_from_exchange:
                self.kill_switch.halt(reason="recovery_snapshot_missing")
                recovery_action = "halted_missing_bootstrap_snapshot"
                notes.append("bootstrap_exchange_snapshot_missing")
            else:
                rebuilt_snapshot = self.reconstruction_service.rebuild_snapshot(
                    fills=fills,
                    price_provider=self.price_provider,
                )
                portfolio_state.load_portfolio_snapshot(
                    rebuilt_snapshot,
                    applied_fill_ids={fill.fill_id for fill in fills},
                    total_fees_paid=sum(fill.fee_amount for fill in fills),
                )
                self.portfolio_repo.save_snapshot(rebuilt_snapshot)
                rebuilt_snapshot_saved = True
                notes.append("portfolio_rebuilt_from_fills")
        else:
            notes.append("cold_start_no_execution_state")

        status = RecoveryStatus(
            status=(
                "recovered_halted"
                if self.kill_switch.halted
                else "recovered"
                if latest_snapshot is not None or fills
                else "cold_start"
            ),
            recovered_order_count=len(self.execution_repo.order_states()),
            recovered_fill_count=len(fills),
            recovered_snapshot_available=self.portfolio_repo.latest() is not None,
            open_order_count=len(open_orders),
            divergence_count=divergence_count,
            halted=self.kill_switch.halted,
            recovery_action=recovery_action,
            notes=notes,
        )
        return RecoveryArtifacts(status=status, rebuilt_snapshot_saved=rebuilt_snapshot_saved)

    def _recovery_price_provider(self, snapshot: PortfolioSnapshot) -> Callable[[str], float]:
        snapshot_marks: dict[str, float] = {}
        for position in snapshot.positions:
            if abs(position.position_qty) > 1e-12:
                snapshot_marks[position.symbol] = position.position_notional / position.position_qty
            elif position.avg_entry_price > 0.0:
                snapshot_marks[position.symbol] = position.avg_entry_price

        def provider(symbol: str) -> float:
            live_price = self.price_provider(symbol)
            if live_price > 0.0:
                return live_price
            return snapshot_marks.get(symbol, 0.0)

        return provider

    @staticmethod
    def _divergence_count(left, right) -> int:
        count = 0
        if ExecutionRecoveryService._dict_diverges(left.balances, right.balances):
            count += 1
        left_positions = {position.symbol: (position.position_qty, position.avg_entry_price) for position in left.positions}
        right_positions = {position.symbol: (position.position_qty, position.avg_entry_price) for position in right.positions}
        if ExecutionRecoveryService._position_diverges(left_positions, right_positions):
            count += 1
        for field_name in (
            "realized_pnl",
            "unrealized_pnl",
            "total_equity",
            "gross_exposure",
            "net_exposure",
        ):
            if abs(getattr(left, field_name) - getattr(right, field_name)) > 1e-9:
                count += 1
        return count

    @staticmethod
    def _dict_diverges(left: dict[str, float], right: dict[str, float]) -> bool:
        keys = set(left) | set(right)
        return any(abs(left.get(key, 0.0) - right.get(key, 0.0)) > 1e-9 for key in keys)

    @staticmethod
    def _position_diverges(
        left: dict[str, tuple[float, float]],
        right: dict[str, tuple[float, float]],
    ) -> bool:
        keys = set(left) | set(right)
        for key in keys:
            left_qty, left_avg = left.get(key, (0.0, 0.0))
            right_qty, right_avg = right.get(key, (0.0, 0.0))
            if abs(left_qty - right_qty) > 1e-9 or abs(left_avg - right_avg) > 1e-9:
                return True
        return False
