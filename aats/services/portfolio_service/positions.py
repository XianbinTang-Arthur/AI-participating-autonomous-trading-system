from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from aats.bootstrap.logging import get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_payload, publish_model
from aats.schemas.execution import FillEvent
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.portfolio import PortfolioSnapshot
from aats.storage.base import PortfolioRepository
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder


@dataclass
class PositionRecord:
    quantity: float = 0.0
    avg_entry_price: float = 0.0


@dataclass
class FillApplicationResult:
    applied: bool
    starting_quantity: float
    ending_quantity: float
    realized_pnl_delta: float
    fee_delta: float


class PortfolioState:
    def __init__(self, *, initial_usdt_balance: float) -> None:
        self.positions: dict[str, PositionRecord] = {}
        self.balances: dict[str, float] = {"USDT": initial_usdt_balance}
        self.realized_pnl: float = 0.0
        self.total_fees_paid: float = 0.0
        self._applied_fill_ids: set[str] = set()

    def has_applied_fill(self, fill_id: str) -> bool:
        return fill_id in self._applied_fill_ids

    def load_exchange_snapshot(self, snapshot: ExchangeAccountSnapshot) -> None:
        self.positions = {}
        self.balances = {
            balance.currency: balance.total for balance in snapshot.balances if abs(balance.total) > 1e-12
        }
        if "USDT" not in self.balances:
            self.balances["USDT"] = 0.0
        for position in snapshot.positions:
            if abs(position.quantity) < 1e-12:
                continue
            self.positions[position.symbol] = PositionRecord(
                quantity=position.quantity,
                avg_entry_price=position.average_entry_price or 0.0,
            )
        self.realized_pnl = 0.0
        self.total_fees_paid = 0.0
        self._applied_fill_ids.clear()

    def load_portfolio_snapshot(
        self,
        snapshot: PortfolioSnapshot,
        *,
        applied_fill_ids: set[str] | None = None,
        total_fees_paid: float | None = None,
    ) -> None:
        self.positions = {
            position.symbol: PositionRecord(
                quantity=position.position_qty,
                avg_entry_price=position.avg_entry_price,
            )
            for position in snapshot.positions
            if abs(position.position_qty) > 1e-12
        }
        self.balances = dict(snapshot.balances)
        if "USDT" not in self.balances:
            self.balances["USDT"] = 0.0
        self.realized_pnl = snapshot.realized_pnl
        self.total_fees_paid = total_fees_paid if total_fees_paid is not None else 0.0
        self._applied_fill_ids = set(applied_fill_ids or set())

    def apply_fill(self, fill: FillEvent) -> FillApplicationResult:
        if fill.fill_id in self._applied_fill_ids:
            return FillApplicationResult(
                applied=False,
                starting_quantity=self.positions.get(fill.symbol, PositionRecord()).quantity,
                ending_quantity=self.positions.get(fill.symbol, PositionRecord()).quantity,
                realized_pnl_delta=0.0,
                fee_delta=0.0,
            )

        record = self.positions.setdefault(fill.symbol, PositionRecord())
        signed_qty = fill.fill_qty if fill.side == "buy" else -fill.fill_qty
        starting_qty = record.quantity
        fee_delta = fill.fee_amount
        realized_pnl_delta = -fee_delta

        if fill.side == "buy":
            self.balances["USDT"] = self.balances.get("USDT", 0.0) - (fill.fill_qty * fill.fill_price) - fill.fee_amount
        else:
            self.balances["USDT"] = self.balances.get("USDT", 0.0) + (fill.fill_qty * fill.fill_price) - fill.fee_amount

        if self._same_direction(starting_qty, signed_qty):
            ending_qty = starting_qty + signed_qty
            new_total_cost = (abs(starting_qty) * record.avg_entry_price) + (abs(signed_qty) * fill.fill_price)
            record.quantity = ending_qty
            record.avg_entry_price = new_total_cost / abs(record.quantity) if record.quantity else 0.0
        else:
            closing_qty = min(abs(starting_qty), abs(signed_qty))
            if starting_qty > 0:
                realized_pnl_delta += (fill.fill_price - record.avg_entry_price) * closing_qty
            else:
                realized_pnl_delta += (record.avg_entry_price - fill.fill_price) * closing_qty

            ending_qty = starting_qty + signed_qty
            record.quantity = ending_qty
            if abs(ending_qty) < 1e-12:
                record.avg_entry_price = 0.0
            elif self._same_direction(starting_qty, ending_qty):
                # Position was reduced but remained on the same side, so cost basis is unchanged.
                pass
            else:
                # Position crossed through flat and reopened in the opposite direction.
                record.avg_entry_price = fill.fill_price

        self.realized_pnl += realized_pnl_delta
        self.total_fees_paid += fee_delta
        self._applied_fill_ids.add(fill.fill_id)
        self._cleanup_if_flat(fill.symbol)
        return FillApplicationResult(
            applied=True,
            starting_quantity=starting_qty,
            ending_quantity=self.positions.get(fill.symbol, PositionRecord()).quantity,
            realized_pnl_delta=realized_pnl_delta,
            fee_delta=fee_delta,
        )

    def _cleanup_if_flat(self, symbol: str) -> None:
        record = self.positions.get(symbol)
        if record is not None and abs(record.quantity) < 1e-12:
            self.positions.pop(symbol, None)

    @staticmethod
    def _same_direction(left: float, right: float) -> bool:
        if abs(left) < 1e-12 or abs(right) < 1e-12:
            return True
        return (left > 0 and right > 0) or (left < 0 and right < 0)


class PortfolioService:
    def __init__(
        self,
        *,
        bus: EventBus,
        state: PortfolioState,
        snapshot_builder: PortfolioSnapshotBuilder,
        portfolio_repo: PortfolioRepository,
        price_provider: Callable[[str], float],
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.bus = bus
        self.state = state
        self.snapshot_builder = snapshot_builder
        self.portfolio_repo = portfolio_repo
        self.price_provider = price_provider
        self.metrics = metrics
        self.logger = get_logger("aats.portfolio_service")

    async def bootstrap_snapshot(self) -> None:
        snapshot = self.snapshot_builder.build(state=self.state, price_provider=self.price_provider)
        self.portfolio_repo.save_snapshot(snapshot)
        await publish_model(
            bus=self.bus,
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=snapshot,
            source_component="portfolio_service",
        )

    async def handle_fill_event(self, message: dict) -> None:
        fill = parse_payload(message, FillEvent)
        result = self.state.apply_fill(fill)
        if not result.applied:
            return
        if self.metrics is not None:
            self.metrics.increment("fills_processed")
        log_event(
            self.logger,
            "fill_applied",
            decision_id=fill.decision_id,
            fill_id=fill.fill_id,
            symbol=fill.symbol,
            ending_quantity=result.ending_quantity,
            realized_pnl_delta=result.realized_pnl_delta,
            fee_delta=result.fee_delta,
        )
        snapshot = self.snapshot_builder.build(
            state=self.state,
            price_provider=self.price_provider,
            decision_id=fill.decision_id,
            source_intent_id=fill.intent_id,
            source_fill_id=fill.fill_id,
        )
        self.portfolio_repo.save_snapshot(snapshot)
        await publish_model(
            bus=self.bus,
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=snapshot,
            source_component="portfolio_service",
        )
