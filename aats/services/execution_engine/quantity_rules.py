from __future__ import annotations

from decimal import Decimal

from aats.domain.instrument_contract import (
    InstrumentContract,
    InstrumentContractError,
    instrument_contract_from_metadata,
)
from aats.schemas.exchange import InstrumentMetadata
from aats.services.execution_engine.okx_rest import infer_okx_derivatives_inst_type
from aats.services.portfolio_service.decimals import to_decimal


_DERIVATIVE_INST_TYPES = {"SWAP", "FUTURES"}


def round_down_to_step(*, value: Decimal, step: Decimal) -> Decimal:
    value = to_decimal(value)
    step = to_decimal(step)
    if not value.is_finite() or not step.is_finite() or step <= 0:
        raise InstrumentContractError("quantity_step_rounding_invalid")
    if value == 0:
        return Decimal("0")

    value_tuple = value.copy_abs().as_tuple()
    step_tuple = step.as_tuple()
    exponent_delta = value_tuple.exponent - step_tuple.exponent
    if (
        len(value_tuple.digits) > 1000
        or len(step_tuple.digits) > 1000
        or abs(exponent_delta) > 1000
    ):
        raise InstrumentContractError("quantity_step_rounding_invalid")
    value_coefficient = int("".join(str(digit) for digit in value_tuple.digits))
    step_coefficient = int("".join(str(digit) for digit in step_tuple.digits))
    if exponent_delta >= 0:
        numerator = value_coefficient * (10**exponent_delta)
        denominator = step_coefficient
    else:
        numerator = value_coefficient
        denominator = step_coefficient * (10 ** (-exponent_delta))
    step_count = numerator // denominator
    result_coefficient = step_count * step_coefficient
    result_digits = tuple(int(digit) for digit in str(result_coefficient))
    return Decimal((1 if value < 0 else 0, result_digits, step_tuple.exponent))


def exchange_quantity_from_internal(
    *,
    symbol: str,
    quantity: Decimal,
    instrument: InstrumentMetadata | None,
) -> Decimal:
    quantity = to_decimal(quantity)
    contract = _execution_contract(symbol=symbol, instrument=instrument)
    if contract is None:
        return quantity
    return contract.exchange_quantity(quantity)


def internal_quantity_from_exchange(
    *,
    symbol: str,
    quantity: Decimal,
    instrument: InstrumentMetadata | None,
) -> Decimal:
    quantity = to_decimal(quantity)
    contract = _execution_contract(symbol=symbol, instrument=instrument)
    if contract is None:
        return quantity
    return contract.base_quantity(quantity)


def minimum_exchange_order_quantity(*, instrument: InstrumentMetadata | None) -> Decimal:
    if instrument is None:
        return Decimal("0")
    _execution_contract(symbol=instrument.symbol, instrument=instrument)
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
    if instrument is None and infer_okx_derivatives_inst_type(symbol) in _DERIVATIVE_INST_TYPES:
        raise InstrumentContractError("derivative_instrument_metadata_required")
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
        if infer_okx_derivatives_inst_type(symbol) in _DERIVATIVE_INST_TYPES:
            raise InstrumentContractError("derivative_instrument_metadata_required")
        return quantity
    exchange_quantity = exchange_quantity_from_internal(
        symbol=symbol,
        quantity=quantity,
        instrument=instrument,
    )
    sign = Decimal("-1") if exchange_quantity < 0 else Decimal("1")
    rounded_exchange_quantity = round_down_to_step(
        value=exchange_quantity.copy_abs(),
        step=max(to_decimal(instrument.lot_size), Decimal("0")),
    )
    rounded_internal_quantity = internal_quantity_from_exchange(
        symbol=symbol,
        quantity=rounded_exchange_quantity,
        instrument=instrument,
    )
    return (
        rounded_internal_quantity.copy_negate()
        if sign < 0
        else rounded_internal_quantity
    )


def _execution_contract(
    *,
    symbol: str,
    instrument: InstrumentMetadata | None,
) -> InstrumentContract | None:
    inferred_inst_type = infer_okx_derivatives_inst_type(symbol)
    if instrument is None:
        if inferred_inst_type in _DERIVATIVE_INST_TYPES:
            raise InstrumentContractError("derivative_instrument_metadata_required")
        return None
    if str(instrument.symbol or "").strip().upper() != str(symbol or "").strip().upper():
        raise InstrumentContractError("instrument_identity_mismatch")
    contract = instrument_contract_from_metadata(instrument)
    if contract.contract_type == "inverse":
        raise InstrumentContractError("inverse_execution_quantity_unsupported")
    return contract
