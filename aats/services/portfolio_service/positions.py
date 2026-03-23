from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from aats.bootstrap.logging import correlation_fields, get_logger, log_event
from aats.bootstrap.metrics import MetricsRegistry
from aats.bus.base import EventBus
from aats.events import topics
from aats.events.envelopes import parse_payload, publish_model
from aats.schemas.common import utc_now
from aats.schemas.execution import FillEvent
from aats.schemas.exchange import ExchangeAccountSnapshot
from aats.schemas.operator import ProcessingFailureRecord
from aats.schemas.portfolio import FillOutcomeRecord, PortfolioBalanceDelta, PortfolioSnapshot, PortfolioSnapshotOrigin
from aats.services.accounting import fill_fee_cost_in_quote, resolve_symbol_currencies, resolved_fee_currency
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, is_effectively_zero, to_decimal
from aats.services.portfolio_service.position_keys import (
    build_position_key,
    exposure_side_from_quantity,
    normalize_position_mode,
    normalize_position_side,
    position_key_for_fill,
    position_key_for_snapshot_position,
    signed_quantity_for_position_side,
)
from aats.storage.base import FillOutcomeRepository, PortfolioRepository
from aats.services.portfolio_service.snapshots import PortfolioSnapshotBuilder


@dataclass
class PositionRecord:
    symbol: str = ""
    position_key: str | None = None
    quantity: Decimal = Decimal("0")
    avg_entry_price: Decimal = Decimal("0")
    product_type: str = "spot"
    target_leverage: float = 1.0
    margin_mode: str = "cash"
    position_mode: str | None = None
    pos_side: str | None = None
    instrument_family: str | None = None
    settle_currency: str | None = None
    margin_allocated: Decimal = Decimal("0")
    maintenance_margin: Decimal = Decimal("0")
    margin_ratio: Decimal | None = None
    liquidation_price: Decimal | None = None
    margin_source: str = "estimated"
    exposure_side: str = "flat"


@dataclass
class FillApplicationResult:
    applied: bool
    starting_quantity: Decimal
    ending_quantity: Decimal
    starting_avg_entry_price: Decimal
    ending_avg_entry_price: Decimal
    realized_pnl_delta: Decimal
    fee_delta: Decimal


@dataclass
class PortfolioStateCheckpoint:
    positions: dict[str, PositionRecord] = field(default_factory=dict)
    balances: dict[str, Decimal] = field(default_factory=dict)
    realized_pnl: Decimal = Decimal("0")
    total_fees_paid: Decimal = Decimal("0")
    applied_fill_ids: set[str] = field(default_factory=set)
    loaded_from_exchange_snapshot: bool = False


class PortfolioState:
    def __init__(
        self,
        *,
        initial_usdt_balance: float | Decimal,
        default_product_type: str = "spot",
        default_margin_mode: str = "cash",
    ) -> None:
        self.positions: dict[str, PositionRecord] = {}
        self.balances: dict[str, Decimal] = {"USDT": to_decimal(initial_usdt_balance)}
        self.realized_pnl: Decimal = Decimal("0")
        self.total_fees_paid: Decimal = Decimal("0")
        self._applied_fill_ids: set[str] = set()
        self._loaded_from_exchange_snapshot = False
        self.default_product_type = default_product_type
        self.default_margin_mode = default_margin_mode

    def has_applied_fill(self, fill_id: str) -> bool:
        return fill_id in self._applied_fill_ids

    def checkpoint(self) -> PortfolioStateCheckpoint:
        return PortfolioStateCheckpoint(
            positions={
                symbol: PositionRecord(
                    symbol=record.symbol,
                    position_key=record.position_key,
                    quantity=record.quantity,
                    avg_entry_price=record.avg_entry_price,
                    product_type=record.product_type,
                    target_leverage=record.target_leverage,
                    margin_mode=record.margin_mode,
                    position_mode=record.position_mode,
                    pos_side=record.pos_side,
                    instrument_family=record.instrument_family,
                    settle_currency=record.settle_currency,
                    margin_allocated=record.margin_allocated,
                    maintenance_margin=record.maintenance_margin,
                    margin_ratio=record.margin_ratio,
                    liquidation_price=record.liquidation_price,
                    margin_source=record.margin_source,
                    exposure_side=record.exposure_side,
                )
                for symbol, record in self.positions.items()
            },
            balances=dict(self.balances),
            realized_pnl=self.realized_pnl,
            total_fees_paid=self.total_fees_paid,
            applied_fill_ids=set(self._applied_fill_ids),
            loaded_from_exchange_snapshot=self._loaded_from_exchange_snapshot,
        )

    def restore(self, checkpoint: PortfolioStateCheckpoint) -> None:
        self.positions = {
            symbol: PositionRecord(
                symbol=record.symbol,
                position_key=record.position_key,
                quantity=record.quantity,
                avg_entry_price=record.avg_entry_price,
                product_type=record.product_type,
                target_leverage=record.target_leverage,
                margin_mode=record.margin_mode,
                position_mode=record.position_mode,
                pos_side=record.pos_side,
                instrument_family=record.instrument_family,
                settle_currency=record.settle_currency,
                margin_allocated=record.margin_allocated,
                maintenance_margin=record.maintenance_margin,
                margin_ratio=record.margin_ratio,
                liquidation_price=record.liquidation_price,
                margin_source=record.margin_source,
                exposure_side=record.exposure_side,
            )
            for symbol, record in checkpoint.positions.items()
        }
        self.balances = dict(checkpoint.balances)
        self.realized_pnl = checkpoint.realized_pnl
        self.total_fees_paid = checkpoint.total_fees_paid
        self._applied_fill_ids = set(checkpoint.applied_fill_ids)
        self._loaded_from_exchange_snapshot = checkpoint.loaded_from_exchange_snapshot

    def load_exchange_snapshot(self, snapshot: ExchangeAccountSnapshot) -> None:
        self.positions = {}
        self.balances = {
            balance.currency: to_decimal(balance.total)
            for balance in snapshot.balances
            if not is_effectively_zero(balance.total)
        }
        if "USDT" not in self.balances:
            self.balances["USDT"] = Decimal("0")
        snapshot_position_mode = (
            snapshot.account_configuration.position_mode
            if snapshot.account_configuration is not None
            else snapshot.position_mode
        )
        for position in snapshot.positions:
            signed_quantity = signed_quantity_for_position_side(
                position.quantity,
                pos_side=getattr(position, "side", None),
                position_mode=snapshot_position_mode,
            )
            if is_effectively_zero(signed_quantity):
                continue
            position_key = build_position_key(
                symbol=position.symbol,
                product_type=self.default_product_type,
                position_mode=snapshot_position_mode,
                pos_side=getattr(position, "side", None),
            )
            self.positions[position_key] = PositionRecord(
                symbol=position.symbol,
                position_key=position_key,
                quantity=signed_quantity,
                avg_entry_price=to_decimal(position.average_entry_price),
                product_type=getattr(position, "product_type", self.default_product_type),
                target_leverage=float(getattr(position, "leverage", None) or 1.0),
                margin_mode=getattr(position, "margin_mode", None) or self.default_margin_mode,
                position_mode=normalize_position_mode(snapshot_position_mode),
                pos_side=normalize_position_side(getattr(position, "side", None), position_mode=snapshot_position_mode),
                instrument_family=getattr(position, "instrument_family", None),
                settle_currency=getattr(position, "settle_currency", None),
                margin_allocated=to_decimal(getattr(position, "margin_allocated", 0) or 0),
                maintenance_margin=to_decimal(getattr(position, "maintenance_margin", 0) or 0),
                margin_ratio=(
                    None
                    if getattr(position, "margin_ratio", None) in {None, ""}
                    else to_decimal(getattr(position, "margin_ratio"))
                ),
                liquidation_price=(
                    None
                    if getattr(position, "liquidation_price", None) in {None, ""}
                    else to_decimal(getattr(position, "liquidation_price"))
                ),
                margin_source="exchange",
                exposure_side=exposure_side_from_quantity(signed_quantity),
            )
        if self.default_product_type == "spot" and not snapshot.positions:
            for symbol, quantity in self._synthetic_spot_positions(snapshot).items():
                self.positions[symbol] = PositionRecord(
                    symbol=symbol,
                    position_key=symbol,
                    quantity=quantity,
                    avg_entry_price=Decimal("0"),
                    exposure_side=exposure_side_from_quantity(quantity),
                )
        self.realized_pnl = Decimal("0")
        self.total_fees_paid = Decimal("0")
        self._applied_fill_ids.clear()
        self._loaded_from_exchange_snapshot = True

    def load_portfolio_snapshot(
        self,
        snapshot: PortfolioSnapshot,
        *,
        applied_fill_ids: set[str] | None = None,
        total_fees_paid: Decimal | float | None = None,
    ) -> None:
        self.positions = {
            position_key_for_snapshot_position(position): PositionRecord(
                symbol=position.symbol,
                position_key=position_key_for_snapshot_position(position),
                quantity=to_decimal(position.position_qty),
                avg_entry_price=to_decimal(position.avg_entry_price),
                product_type=position.product_type,
                target_leverage=position.target_leverage,
                margin_mode=position.margin_mode,
                position_mode=position.position_mode,
                pos_side=position.pos_side,
                instrument_family=position.instrument_family,
                settle_currency=position.settle_currency,
                margin_allocated=to_decimal(position.margin_allocated),
                maintenance_margin=to_decimal(position.maintenance_margin),
                margin_ratio=None if position.margin_ratio in {None, ""} else to_decimal(position.margin_ratio),
                liquidation_price=(
                    None if position.liquidation_price in {None, ""} else to_decimal(position.liquidation_price)
                ),
                margin_source=position.margin_source,
                exposure_side=position.exposure_side or exposure_side_from_quantity(position.position_qty),
            )
            for position in snapshot.positions
            if not is_effectively_zero(position.position_qty)
        }
        self.balances = {currency: to_decimal(balance) for currency, balance in snapshot.balances.items()}
        if "USDT" not in self.balances:
            self.balances["USDT"] = Decimal("0")
        self.realized_pnl = to_decimal(snapshot.realized_pnl)
        self.total_fees_paid = to_decimal(total_fees_paid if total_fees_paid is not None else 0)
        self._applied_fill_ids = set(applied_fill_ids or set())
        self._loaded_from_exchange_snapshot = False

    def apply_fill(self, fill: FillEvent) -> FillApplicationResult:
        position_key = position_key_for_fill(fill)
        if fill.fill_id in self._applied_fill_ids:
            existing = self.positions.get(position_key, PositionRecord(symbol=fill.symbol, position_key=position_key))
            return FillApplicationResult(
                applied=False,
                starting_quantity=existing.quantity,
                ending_quantity=existing.quantity,
                starting_avg_entry_price=existing.avg_entry_price,
                ending_avg_entry_price=existing.avg_entry_price,
                realized_pnl_delta=Decimal("0"),
                fee_delta=Decimal("0"),
            )

        record = self.positions.setdefault(
            position_key,
            PositionRecord(
                symbol=fill.symbol,
                position_key=position_key,
            ),
        )
        record.symbol = fill.symbol
        record.position_key = position_key
        record.product_type = getattr(fill, "product_type", "spot")
        record.target_leverage = getattr(fill, "target_leverage", 1.0)
        record.margin_mode = getattr(fill, "margin_mode", "cash")
        record.position_mode = normalize_position_mode(getattr(fill, "position_mode", None))
        record.pos_side = normalize_position_side(getattr(fill, "pos_side", None), position_mode=record.position_mode)
        record.instrument_family = getattr(fill, "instrument_family", None)
        record.settle_currency = getattr(fill, "settle_currency", None)
        record.margin_allocated = Decimal("0")
        record.maintenance_margin = Decimal("0")
        record.margin_ratio = None
        record.liquidation_price = None
        record.margin_source = "estimated"
        record.exposure_side = exposure_side_from_quantity(record.quantity)
        product_type = record.product_type or self.default_product_type
        fill_qty = to_decimal(fill.fill_qty)
        fill_price = to_decimal(fill.fill_price)
        fee_amount = to_decimal(fill.fee_amount)
        signed_qty = fill_qty if fill.side == "buy" else -fill_qty
        starting_qty = to_decimal(record.quantity)
        base_currency, quote_currency = resolve_symbol_currencies(fill.symbol)
        notional = fill_qty * fill_price
        fee_currency = resolved_fee_currency(fill=fill, base_currency=base_currency, quote_currency=quote_currency)
        fee_quote_amount = fill_fee_cost_in_quote(
            fill=fill,
            base_currency=base_currency,
            quote_currency=quote_currency,
        )
        fee_quote_amount = to_decimal(fee_quote_amount)
        fee_delta = fee_quote_amount
        trading_pnl_delta = Decimal("0")
        starting_avg_entry_price = to_decimal(record.avg_entry_price)

        if product_type != "derivatives":
            if quote_currency is not None:
                quote_balance = to_decimal(self.balances.get(quote_currency, 0))
                if fill.side == "buy":
                    self.balances[quote_currency] = quote_balance - notional
                else:
                    self.balances[quote_currency] = quote_balance + notional
            if base_currency is not None:
                base_balance = to_decimal(self.balances.get(base_currency, 0))
                if fill.side == "buy":
                    self.balances[base_currency] = base_balance + fill_qty
                else:
                    self.balances[base_currency] = base_balance - fill_qty

        if self._same_direction(starting_qty, signed_qty):
            ending_qty = starting_qty + signed_qty
            current_avg_entry_price = record.avg_entry_price
            new_total_cost = (abs(starting_qty) * current_avg_entry_price) + (abs(signed_qty) * fill_price)
            record.quantity = ending_qty
            record.avg_entry_price = new_total_cost / abs(ending_qty) if not is_effectively_zero(ending_qty) else Decimal("0")
        else:
            closing_qty = min(abs(starting_qty), abs(signed_qty))
            current_avg_entry_price = record.avg_entry_price
            if starting_qty > 0:
                trading_pnl_delta += (fill_price - current_avg_entry_price) * closing_qty
            else:
                trading_pnl_delta += (current_avg_entry_price - fill_price) * closing_qty

            ending_qty = starting_qty + signed_qty
            record.quantity = ending_qty
            if is_effectively_zero(ending_qty):
                record.avg_entry_price = Decimal("0")
            elif self._same_direction(starting_qty, ending_qty):
                # Position was reduced but remained on the same side, so cost basis is unchanged.
                pass
            else:
                # Position crossed through flat and reopened in the opposite direction.
                record.avg_entry_price = fill_price

        if product_type == "derivatives" and quote_currency is not None and not is_effectively_zero(trading_pnl_delta):
            self.balances[quote_currency] = to_decimal(self.balances.get(quote_currency, 0)) + trading_pnl_delta
        if fee_currency is not None:
            self.balances[fee_currency] = to_decimal(self.balances.get(fee_currency, 0)) - fee_amount

        realized_pnl_delta = trading_pnl_delta - fee_quote_amount
        self.realized_pnl = self.realized_pnl + realized_pnl_delta
        self.total_fees_paid = self.total_fees_paid + fee_delta
        self._applied_fill_ids.add(fill.fill_id)
        self._loaded_from_exchange_snapshot = False
        record.exposure_side = exposure_side_from_quantity(record.quantity)
        self._cleanup_if_flat(position_key)
        ending_record = self.positions.get(position_key, PositionRecord(symbol=fill.symbol, position_key=position_key))
        return FillApplicationResult(
            applied=True,
            starting_quantity=starting_qty,
            ending_quantity=ending_record.quantity,
            starting_avg_entry_price=starting_avg_entry_price,
            ending_avg_entry_price=ending_record.avg_entry_price,
            realized_pnl_delta=realized_pnl_delta,
            fee_delta=fee_delta,
        )

    def _cleanup_if_flat(self, position_key: str) -> None:
        record = self.positions.get(position_key)
        if record is not None and is_effectively_zero(record.quantity):
            self.positions.pop(position_key, None)

    def position_for_fill(self, fill: FillEvent) -> PositionRecord | None:
        return self.positions.get(position_key_for_fill(fill))

    def position_quantity_for_symbol(self, symbol: str) -> Decimal:
        return sum(
            (
                to_decimal(record.quantity)
                for record in self.positions.values()
                if record.symbol == symbol
            ),
            start=Decimal("0"),
        )

    def loaded_from_exchange_snapshot(self) -> bool:
        return self._loaded_from_exchange_snapshot

    def _synthetic_spot_positions(self, snapshot: ExchangeAccountSnapshot) -> dict[str, Decimal]:
        synthetic_positions: dict[str, Decimal] = {}
        for instrument in snapshot.instruments:
            if instrument.quote_currency != "USDT":
                continue
            if instrument.symbol in self.positions:
                continue
            quantity = self.balances.get(instrument.base_currency, Decimal("0"))
            if is_effectively_zero(quantity):
                continue
            synthetic_positions[instrument.symbol] = quantity
        return synthetic_positions

    @classmethod
    def fee_cost_in_quote(cls, fill: FillEvent) -> Decimal:
        return fill_fee_cost_in_quote(fill)

    @classmethod
    def total_fee_cost_in_quote(cls, fills: list[FillEvent]) -> Decimal:
        return sum((cls.fee_cost_in_quote(fill) for fill in fills), start=Decimal("0"))

    @staticmethod
    def _same_direction(left: Decimal, right: Decimal) -> bool:
        if abs(left) <= EPSILON_DECIMAL_12 or abs(right) <= EPSILON_DECIMAL_12:
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
        fill_outcome_repo: FillOutcomeRepository,
        price_provider: Callable[[str], Decimal],
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.bus = bus
        self.state = state
        self.snapshot_builder = snapshot_builder
        self.portfolio_repo = portfolio_repo
        self.fill_outcome_repo = fill_outcome_repo
        self.price_provider = price_provider
        self.metrics = metrics
        self.logger = get_logger("aats.portfolio_service")

    async def bootstrap_snapshot(self, *, snapshot_origin: PortfolioSnapshotOrigin = "runtime_bootstrap") -> None:
        snapshot = self.snapshot_builder.build(
            state=self.state,
            price_provider=self.price_provider,
            snapshot_origin=snapshot_origin,
        )
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
        checkpoint = self.state.checkpoint()
        balances_before = dict(checkpoint.balances)
        result = self.state.apply_fill(fill)
        if not result.applied:
            return
        try:
            snapshot = self.snapshot_builder.build(
                state=self.state,
                price_provider=self.price_provider,
                decision_id=fill.decision_id,
                source_intent_id=fill.intent_id,
                source_fill_id=fill.fill_id,
                snapshot_origin="fill_derived",
            )
            self.portfolio_repo.save_snapshot(snapshot)
        except Exception as exc:
            self.state.restore(checkpoint)
            await self._emit_processing_failure(
                stage="portfolio_snapshot_persist",
                message=str(exc),
                fill=fill,
                retriable=True,
            )
            raise
        balance_delta = self._balance_delta_event(
            fill=fill,
            balances_before=balances_before,
            balances_after=self.state.balances,
            realized_pnl_delta=result.realized_pnl_delta,
            fee_delta=result.fee_delta,
        )
        try:
            self.fill_outcome_repo.save_outcome(
                FillOutcomeRecord.from_fill_and_balance_delta(
                    fill=fill,
                    balance_delta=balance_delta,
                    starting_position_qty=result.starting_quantity,
                    starting_avg_entry_price=result.starting_avg_entry_price,
                    ending_position_qty=result.ending_quantity,
                    ending_avg_entry_price=result.ending_avg_entry_price,
                )
            )
        except Exception as exc:
            log_event(
                self.logger,
                "fill_outcome_persist_failed",
                level="warning",
                **correlation_fields(
                    decision_id=fill.decision_id,
                    intent_id=fill.intent_id,
                    order_id=fill.client_order_id,
                    fill_id=fill.fill_id,
                    symbol=fill.symbol,
                    error=str(exc),
                ),
            )
            await self._emit_processing_failure(
                stage="fill_outcome_persist",
                message=str(exc),
                fill=fill,
                retriable=True,
            )
        if self.metrics is not None:
            self.metrics.increment("fills_processed")
        log_event(
            self.logger,
            "fill_applied",
            **correlation_fields(
                decision_id=fill.decision_id,
                intent_id=fill.intent_id,
                order_id=fill.client_order_id,
                fill_id=fill.fill_id,
                symbol=fill.symbol,
                ending_quantity=result.ending_quantity,
                realized_pnl_delta=result.realized_pnl_delta,
                fee_delta=result.fee_delta,
            ),
        )
        await publish_model(
            bus=self.bus,
            topic=topics.PORTFOLIO_BALANCE_DELTAS,
            key=fill.symbol,
            payload_model=balance_delta,
            source_component="portfolio_service",
        )
        await publish_model(
            bus=self.bus,
            topic=topics.PORTFOLIO_SNAPSHOTS,
            key="portfolio",
            payload_model=snapshot,
            source_component="portfolio_service",
        )

    async def _emit_processing_failure(
        self,
        *,
        stage: str,
        message: str,
        fill: FillEvent,
        retriable: bool,
    ) -> None:
        if self.metrics is not None:
            self.metrics.increment("processing_failures")
        try:
            await publish_model(
                bus=self.bus,
                topic=topics.PROCESSING_FAILURES,
                key=fill.symbol,
                payload_model=ProcessingFailureRecord(
                    subsystem="portfolio_service",
                    stage=stage,
                    severity="error",
                    message=message,
                    decision_id=fill.decision_id,
                    intent_id=fill.intent_id,
                    order_id=fill.client_order_id,
                    fill_id=fill.fill_id,
                    symbol=fill.symbol,
                    product_type=fill.product_type,
                    margin_mode=fill.margin_mode,
                    retriable=retriable,
                    observed_at=utc_now(),
                ),
                source_component="portfolio_service",
            )
        except Exception:
            pass

    @staticmethod
    def _balance_delta_event(
        *,
        fill: FillEvent,
        balances_before: dict[str, Decimal],
        balances_after: dict[str, Decimal],
        realized_pnl_delta: Decimal,
        fee_delta: Decimal,
    ) -> PortfolioBalanceDelta:
        currencies = sorted(set(balances_before) | set(balances_after))
        balance_deltas = {
            currency: to_decimal(balances_after.get(currency, 0)) - to_decimal(balances_before.get(currency, 0))
            for currency in currencies
            if to_decimal(balances_after.get(currency, 0)) != to_decimal(balances_before.get(currency, 0))
        }
        return PortfolioBalanceDelta(
            decision_id=fill.decision_id,
            intent_id=fill.intent_id,
            order_id=fill.client_order_id,
            fill_id=fill.fill_id,
            symbol=fill.symbol,
            balances_before={currency: to_decimal(value) for currency, value in balances_before.items()},
            balances_after={currency: to_decimal(value) for currency, value in balances_after.items()},
            balance_deltas=balance_deltas,
            realized_pnl_delta=realized_pnl_delta,
            fee_delta=fee_delta,
            product_type=fill.product_type,
            margin_mode=fill.margin_mode,
        )
