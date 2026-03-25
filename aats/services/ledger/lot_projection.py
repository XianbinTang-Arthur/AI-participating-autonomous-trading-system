from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
import hashlib

from aats.schemas.execution import FillEvent
from aats.services.accounting import fill_fee_cost_in_quote
from aats.services.execution_engine.fill_ordering import fill_processing_sort_key
from aats.services.portfolio_service.decimals import is_effectively_zero, to_decimal
from aats.services.portfolio_service.position_keys import (
    exposure_side_from_quantity,
    normalize_position_mode,
    normalize_position_side,
    position_key_for_fill,
)
from aats.services.portfolio_service.positions import PortfolioState, PositionRecord


@dataclass(slots=True, frozen=True)
class PositionLot:
    lot_id: str
    symbol: str
    position_key: str
    quantity: Decimal
    entry_price: Decimal
    product_type: str
    target_leverage: float
    margin_mode: str
    source_fill_id: str
    exposure_side: str
    position_mode: str | None
    pos_side: str | None
    instrument_family: str | None
    settle_currency: str | None
    strategy_sleeve_id: str | None
    allocation_id: str | None
    opened_at: object
    closed_at: object | None = None


@dataclass(slots=True, frozen=True)
class LotEventRecord:
    event_id: str
    fill_id: str
    lot_id: str
    symbol: str
    event_type: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal | None
    realized_pnl_delta: Decimal
    strategy_sleeve_id: str | None
    allocation_id: str | None
    created_at: object
    payload: dict


@dataclass(slots=True, frozen=True)
class LotBookSnapshot:
    lots: list[dict]
    events: list[dict]
    realized_pnl: Decimal
    total_fees_paid: Decimal
    applied_fill_ids: set[str]
    positions: dict[str, PositionRecord]


class LotBasedProjectionBuilder:
    def rebuild_lot_book(
        self,
        *,
        fills: Iterable[FillEvent],
    ) -> LotBookSnapshot:
        lots_by_position_key: dict[str, deque[PositionLot]] = defaultdict(deque)
        closed_lots: list[dict] = []
        realized_pnl = Decimal("0")
        total_fees_paid = Decimal("0")
        applied_fill_ids: set[str] = set()
        lot_events: list[LotEventRecord] = []

        for fill in sorted(fills, key=fill_processing_sort_key):
            applied_fill_ids.add(fill.fill_id)
            fill_qty = to_decimal(fill.fill_qty)
            if is_effectively_zero(fill_qty):
                continue
            fill_price = to_decimal(fill.fill_price)
            signed_qty = fill_qty if fill.side == "buy" else -fill_qty
            fee_quote = to_decimal(fill_fee_cost_in_quote(fill))
            total_fees_paid += fee_quote

            position_key = position_key_for_fill(fill)
            position_mode = normalize_position_mode(fill.position_mode)
            pos_side = normalize_position_side(fill.pos_side, position_mode=position_mode)
            open_lots = lots_by_position_key[position_key]
            remaining_qty = signed_qty
            close_event_index = 0
            while open_lots and not self._same_direction(open_lots[0].quantity, remaining_qty):
                current_lot = open_lots[0]
                close_qty = min(abs(current_lot.quantity), abs(remaining_qty))
                if current_lot.quantity > 0:
                    pnl_delta = (fill_price - current_lot.entry_price) * close_qty
                    realized_pnl += pnl_delta
                    updated_lot_qty = current_lot.quantity - close_qty
                    remaining_qty += close_qty
                else:
                    pnl_delta = (current_lot.entry_price - fill_price) * close_qty
                    realized_pnl += pnl_delta
                    updated_lot_qty = current_lot.quantity + close_qty
                    remaining_qty -= close_qty
                open_lots.popleft()
                lot_events.append(
                    LotEventRecord(
                        event_id=self._stable_id("lotevt", fill.fill_id, "close", close_event_index),
                        fill_id=fill.fill_id,
                        lot_id=current_lot.lot_id,
                        symbol=current_lot.symbol,
                        event_type="close",
                        quantity=close_qty,
                        entry_price=current_lot.entry_price,
                        exit_price=fill_price,
                        realized_pnl_delta=pnl_delta,
                        strategy_sleeve_id=current_lot.strategy_sleeve_id,
                        allocation_id=current_lot.allocation_id,
                        created_at=fill.ingestion_timestamp,
                        payload={
                            "side": fill.side,
                            "position_intent": fill.position_intent,
                            "position_key": current_lot.position_key,
                            "position_mode": current_lot.position_mode,
                            "pos_side": current_lot.pos_side,
                            "instrument_family": current_lot.instrument_family,
                            "settle_currency": current_lot.settle_currency,
                            "strategy_sleeve_id": current_lot.strategy_sleeve_id,
                            "allocation_id": current_lot.allocation_id,
                        },
                    )
                )
                close_event_index += 1
                if not is_effectively_zero(updated_lot_qty):
                    open_lots.appendleft(
                        PositionLot(
                            lot_id=current_lot.lot_id,
                            symbol=current_lot.symbol,
                            position_key=current_lot.position_key,
                            quantity=updated_lot_qty,
                            entry_price=current_lot.entry_price,
                            product_type=current_lot.product_type,
                            target_leverage=current_lot.target_leverage,
                            margin_mode=current_lot.margin_mode,
                            source_fill_id=current_lot.source_fill_id,
                            exposure_side=current_lot.exposure_side,
                            position_mode=current_lot.position_mode,
                            pos_side=current_lot.pos_side,
                            instrument_family=current_lot.instrument_family,
                            settle_currency=current_lot.settle_currency,
                            strategy_sleeve_id=current_lot.strategy_sleeve_id,
                            allocation_id=current_lot.allocation_id,
                            opened_at=current_lot.opened_at,
                            closed_at=None,
                        )
                    )
                else:
                    closed_lots.append(
                        {
                            "lot_id": current_lot.lot_id,
                            "symbol": current_lot.symbol,
                            "signed_quantity_open": Decimal("0"),
                            "entry_price": current_lot.entry_price,
                            "source_fill_id": current_lot.source_fill_id,
                            "target_leverage": current_lot.target_leverage,
                            "exposure_side": current_lot.exposure_side,
                            "strategy_sleeve_id": current_lot.strategy_sleeve_id,
                            "allocation_id": current_lot.allocation_id,
                            "status": "CLOSED",
                            "opened_at": current_lot.opened_at,
                            "closed_at": fill.ingestion_timestamp,
                            "updated_at": fill.ingestion_timestamp,
                            "metadata": {
                                "position_key": current_lot.position_key,
                                "position_mode": current_lot.position_mode,
                                "pos_side": current_lot.pos_side,
                                "instrument_family": current_lot.instrument_family,
                                "settle_currency": current_lot.settle_currency,
                                "strategy_sleeve_id": current_lot.strategy_sleeve_id,
                                "allocation_id": current_lot.allocation_id,
                            },
                        }
                    )
                if is_effectively_zero(remaining_qty):
                    remaining_qty = Decimal("0")
                    break

            if not is_effectively_zero(remaining_qty):
                lot_id = self._stable_id("lot", position_key, fill.fill_id, "open")
                exposure_side = exposure_side_from_quantity(remaining_qty)
                open_lots.append(
                    PositionLot(
                        lot_id=lot_id,
                        symbol=fill.symbol,
                        position_key=position_key,
                        quantity=remaining_qty,
                        entry_price=fill_price,
                        product_type=fill.product_type,
                        target_leverage=fill.target_leverage,
                        margin_mode=fill.margin_mode,
                        source_fill_id=fill.fill_id,
                        exposure_side=exposure_side,
                        position_mode=position_mode,
                        pos_side=pos_side,
                        instrument_family=fill.instrument_family,
                        settle_currency=fill.settle_currency,
                        strategy_sleeve_id=fill.strategy_sleeve_id,
                        allocation_id=fill.allocation_id,
                        opened_at=fill.ingestion_timestamp,
                        closed_at=None,
                    )
                )
                lot_events.append(
                    LotEventRecord(
                        event_id=self._stable_id("lotevt", fill.fill_id, "open"),
                        fill_id=fill.fill_id,
                        lot_id=lot_id,
                        symbol=fill.symbol,
                        event_type="open",
                        quantity=abs(remaining_qty),
                        entry_price=fill_price,
                        exit_price=None,
                        realized_pnl_delta=Decimal("0"),
                        strategy_sleeve_id=fill.strategy_sleeve_id,
                        allocation_id=fill.allocation_id,
                        created_at=fill.ingestion_timestamp,
                        payload={
                            "side": fill.side,
                            "position_intent": fill.position_intent,
                            "position_key": position_key,
                            "position_mode": position_mode,
                            "pos_side": pos_side,
                            "instrument_family": fill.instrument_family,
                            "settle_currency": fill.settle_currency,
                            "strategy_sleeve_id": fill.strategy_sleeve_id,
                            "allocation_id": fill.allocation_id,
                        },
                    )
                )

            if not open_lots:
                lots_by_position_key.pop(position_key, None)

        positions: dict[str, PositionRecord] = {}
        persisted_lots: list[dict] = []
        for position_key, lots in lots_by_position_key.items():
            total_qty = sum((lot.quantity for lot in lots), start=Decimal("0"))
            if not is_effectively_zero(total_qty):
                notional = sum((abs(lot.quantity) * lot.entry_price for lot in lots), start=Decimal("0"))
                positions[position_key] = PositionRecord(
                    symbol=lots[-1].symbol,
                    position_key=position_key,
                    quantity=total_qty,
                    avg_entry_price=(notional / abs(total_qty)) if not is_effectively_zero(total_qty) else Decimal("0"),
                    product_type=lots[-1].product_type,
                    target_leverage=lots[-1].target_leverage,
                    margin_mode=lots[-1].margin_mode,
                    position_mode=lots[-1].position_mode,
                    pos_side=lots[-1].pos_side,
                    instrument_family=lots[-1].instrument_family,
                    settle_currency=lots[-1].settle_currency,
                    exposure_side=exposure_side_from_quantity(total_qty),
                )
            for lot in lots:
                persisted_lots.append(
                    {
                        "lot_id": lot.lot_id,
                        "symbol": lot.symbol,
                        "signed_quantity_open": lot.quantity,
                        "entry_price": lot.entry_price,
                        "source_fill_id": lot.source_fill_id,
                        "target_leverage": lot.target_leverage,
                        "exposure_side": lot.exposure_side,
                        "strategy_sleeve_id": lot.strategy_sleeve_id,
                        "allocation_id": lot.allocation_id,
                        "status": "OPEN",
                        "opened_at": lot.opened_at,
                        "closed_at": lot.closed_at,
                        "updated_at": lot.opened_at,
                        "metadata": {
                            "position_key": lot.position_key,
                            "position_mode": lot.position_mode,
                            "pos_side": lot.pos_side,
                            "instrument_family": lot.instrument_family,
                            "settle_currency": lot.settle_currency,
                        },
                    }
                )
        persisted_lots.extend(closed_lots)
        return LotBookSnapshot(
            lots=persisted_lots,
            events=[
                {
                    "event_id": event.event_id,
                    "fill_id": event.fill_id,
                    "lot_id": event.lot_id,
                    "symbol": event.symbol,
                    "event_type": event.event_type,
                    "quantity": event.quantity,
                    "entry_price": event.entry_price,
                    "exit_price": event.exit_price,
                    "realized_pnl_delta": event.realized_pnl_delta,
                    "strategy_sleeve_id": event.strategy_sleeve_id,
                    "allocation_id": event.allocation_id,
                    "created_at": event.created_at,
                    "payload": event.payload,
                }
                for event in lot_events
            ],
            realized_pnl=realized_pnl,
            total_fees_paid=total_fees_paid,
            applied_fill_ids=applied_fill_ids,
            positions=positions,
        )

    def rebuild_portfolio_state(
        self,
        *,
        fills: Iterable[FillEvent],
        balances: dict[str, Decimal],
        default_product_type: str,
        default_margin_mode: str,
    ) -> PortfolioState:
        state = PortfolioState(
            initial_usdt_balance=Decimal("0"),
            default_product_type=default_product_type,
            default_margin_mode=default_margin_mode,
        )
        state.balances = {currency: to_decimal(amount) for currency, amount in balances.items()}
        if "USDT" not in state.balances:
            state.balances["USDT"] = Decimal("0")
        lot_book = self.rebuild_lot_book(fills=fills)
        state.positions = lot_book.positions
        state.realized_pnl = lot_book.realized_pnl - lot_book.total_fees_paid
        state.total_fees_paid = lot_book.total_fees_paid
        state._applied_fill_ids = lot_book.applied_fill_ids
        return state

    @staticmethod
    def _stable_id(prefix: str, *parts: object) -> str:
        raw = "|".join(str(part) for part in parts).encode("utf-8")
        return f"{prefix}_{hashlib.sha1(raw).hexdigest()[:24]}"

    @staticmethod
    def _same_direction(left: Decimal, right: Decimal) -> bool:
        if is_effectively_zero(left) or is_effectively_zero(right):
            return True
        return (left > 0 and right > 0) or (left < 0 and right < 0)
