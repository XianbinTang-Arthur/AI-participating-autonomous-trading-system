from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from aats.schemas.exchange import InstrumentMetadata
from aats.services.execution_engine.okx_rest import infer_okx_derivatives_inst_type
from aats.services.portfolio_service.decimals import to_decimal


_DERIVATIVE_INST_TYPES = {"SWAP", "FUTURES"}


def round_down_to_step(*, value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    ratio = value / step
    return ratio.quantize(Decimal("1"), rounding=ROUND_DOWN) * step


def exchange_quantity_from_internal(
    *,
    symbol: str,
    quantity: Decimal,
    instrument: InstrumentMetadata | None,
) -> Decimal:
    quantity = to_decimal(quantity)
    if instrument is None or not _uses_contract_quantity(symbol=symbol, instrument=instrument):
        return quantity
    contract_value = max(to_decimal(instrument.contract_value), Decimal("0"))
    if contract_value <= 0:
        return quantity
    return quantity / contract_value


def internal_quantity_from_exchange(
    *,
    symbol: str,
    quantity: Decimal,
    instrument: InstrumentMetadata | None,
) -> Decimal:
    quantity = to_decimal(quantity)
    if instrument is None or not _uses_contract_quantity(symbol=symbol, instrument=instrument):
        return quantity
    contract_value = max(to_decimal(instrument.contract_value), Decimal("0"))
    if contract_value <= 0:
        return quantity
    return quantity * contract_value


def minimum_exchange_order_quantity(*, instrument: InstrumentMetadata | None) -> Decimal:
    if instrument is None:
        return Decimal("0")
    return max(
        to_decimal(instrument.min_size),
        to_decimal(instrument.lot_size),
        Decimal("0"),
    )


def minimum_internal_order_quantity(
    *,
    symbol: str,
    instrument: InstrumentMetadata | None,
) -> Decimal:
    minimum_exchange_quantity = minimum_exchange_order_quantity(instrument=instrument)
    if minimum_exchange_quantity <= 0:
        return Decimal("0")
    return internal_quantity_from_exchange(
        symbol=symbol,
        quantity=minimum_exchange_quantity,
        instrument=instrument,
    )


def quantized_internal_quantity(
    *,
    symbol: str,
    quantity: Decimal,
    instrument: InstrumentMetadata | None,
) -> Decimal:
    quantity = to_decimal(quantity)
    if instrument is None:
        return quantity
    exchange_quantity = exchange_quantity_from_internal(
        symbol=symbol,
        quantity=quantity,
        instrument=instrument,
    )
    sign = Decimal("-1") if exchange_quantity < 0 else Decimal("1")
    rounded_exchange_quantity = round_down_to_step(
        value=abs(exchange_quantity),
        step=max(to_decimal(instrument.lot_size), Decimal("0")),
    )
    return sign * internal_quantity_from_exchange(
        symbol=symbol,
        quantity=rounded_exchange_quantity,
        instrument=instrument,
    )


def _uses_contract_quantity(*, symbol: str, instrument: InstrumentMetadata) -> bool:
    instrument_type = str(getattr(instrument, "instrument_type", "") or "").upper()
    inferred_inst_type = infer_okx_derivatives_inst_type(symbol)
    return instrument_type in _DERIVATIVE_INST_TYPES or inferred_inst_type in _DERIVATIVE_INST_TYPES
