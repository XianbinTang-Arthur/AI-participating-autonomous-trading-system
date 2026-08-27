from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from aats.domain.instrument_contract import InstrumentContractError
from aats.schemas.exchange import InstrumentMetadata
from aats.services.execution_engine.quantity_rules import (
    exchange_quantity_from_internal,
    internal_quantity_from_exchange,
    minimum_internal_order_quantity,
    quantized_internal_quantity,
    round_down_to_step,
)


def _instrument(
    *,
    symbol: str = "BTC-USDT-SWAP",
    contract_type: str = "linear",
    contract_value: Decimal | None = Decimal("0.01"),
    contract_multiplier: Decimal | None = Decimal("1"),
    contract_value_currency: str | None = "BTC",
    settle_currency: str = "USDT",
    instrument_type: str = "SWAP",
    lot_size: Decimal = Decimal("0.1"),
    min_size: Decimal = Decimal("0.1"),
) -> InstrumentMetadata:
    parts = symbol.split("-")
    return InstrumentMetadata(
        instrument_id=symbol,
        symbol=symbol,
        base_currency=parts[0],
        quote_currency=parts[1],
        lot_size=lot_size,
        tick_size=Decimal("0.1"),
        min_size=min_size,
        contract_value=contract_value,
        contract_multiplier=contract_multiplier,
        contract_type=contract_type,
        instrument_type=instrument_type,
        settle_currency=settle_currency,
        contract_value_currency=contract_value_currency,
        state="live",
    )


def test_spot_quantity_is_base_quantity_and_uses_base_lot_size() -> None:
    instrument = InstrumentMetadata(
        instrument_id="BTC-USDT",
        symbol="BTC-USDT",
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        min_size=Decimal("0.001"),
        instrument_type="SPOT",
        state="live",
    )

    assert exchange_quantity_from_internal(
        symbol=instrument.symbol,
        quantity=Decimal("0.0129"),
        instrument=instrument,
    ) == Decimal("0.0129")
    assert quantized_internal_quantity(
        symbol=instrument.symbol,
        quantity=Decimal("0.0129"),
        instrument=instrument,
    ) == Decimal("0.012")


def test_linear_quantity_uses_contract_value_times_multiplier() -> None:
    instrument = _instrument(
        contract_value=Decimal("0.01"),
        contract_multiplier=Decimal("2"),
    )

    assert exchange_quantity_from_internal(
        symbol=instrument.symbol,
        quantity=Decimal("0.06"),
        instrument=instrument,
    ) == Decimal("3")
    assert internal_quantity_from_exchange(
        symbol=instrument.symbol,
        quantity=Decimal("3"),
        instrument=instrument,
    ) == Decimal("0.06")
    assert minimum_internal_order_quantity(
        symbol=instrument.symbol,
        instrument=instrument,
    ) == Decimal("0.002")


def test_linear_quantity_round_trip_preserves_direction_and_contract_lot() -> None:
    instrument = _instrument(lot_size=Decimal("0.1"))

    assert quantized_internal_quantity(
        symbol=instrument.symbol,
        quantity=Decimal("-0.0129"),
        instrument=instrument,
    ) == Decimal("-0.012")


@pytest.mark.parametrize(
    "instrument",
    [
        None,
        _instrument(contract_value=None),
        _instrument(contract_multiplier=None),
        _instrument(contract_value=Decimal("0")),
        _instrument(contract_value_currency="USD"),
    ],
)
def test_derivative_missing_or_invalid_metadata_never_uses_identity_fallback(
    instrument: InstrumentMetadata | None,
) -> None:
    with pytest.raises(InstrumentContractError):
        exchange_quantity_from_internal(
            symbol="BTC-USDT-SWAP",
            quantity=Decimal("0.01"),
            instrument=instrument,
        )


def test_inverse_execution_quantity_is_explicitly_unsupported() -> None:
    instrument = _instrument(
        symbol="BTC-USD-SWAP",
        contract_type="inverse",
        contract_value=Decimal("100"),
        contract_value_currency="USD",
        settle_currency="BTC",
        lot_size=Decimal("0.1"),
        min_size=Decimal("0.1"),
    )

    with pytest.raises(
        InstrumentContractError,
        match="inverse_execution_quantity_unsupported",
    ):
        internal_quantity_from_exchange(
            symbol=instrument.symbol,
            quantity=Decimal("3"),
            instrument=instrument,
        )


def test_requested_symbol_must_match_metadata_symbol() -> None:
    with pytest.raises(InstrumentContractError, match="instrument_identity_mismatch"):
        exchange_quantity_from_internal(
            symbol="ETH-USDT-SWAP",
            quantity=Decimal("0.1"),
            instrument=_instrument(),
        )


def test_derivative_minimum_quantity_requires_instrument_metadata() -> None:
    with pytest.raises(
        InstrumentContractError,
        match="derivative_instrument_metadata_required",
    ):
        minimum_internal_order_quantity(
            symbol="BTC-USDT-SWAP",
            instrument=None,
        )


def test_step_rounding_is_exact_and_independent_of_global_decimal_context() -> None:
    spot_instrument = InstrumentMetadata(
        instrument_id="BTC-USDT",
        symbol="BTC-USDT",
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.01"),
        tick_size=Decimal("0.1"),
        min_size=Decimal("0.01"),
        instrument_type="SPOT",
        state="live",
    )
    with localcontext() as context:
        context.prec = 2
        low_precision = round_down_to_step(
            value=Decimal("0.0129"),
            step=Decimal("0.001"),
        )
        large_value = round_down_to_step(
            value=Decimal("999"),
            step=Decimal("0.01"),
        )
        quantized_internal = quantized_internal_quantity(
            symbol="BTC-USDT-SWAP",
            quantity=Decimal("0.0129"),
            instrument=_instrument(lot_size=Decimal("0.1")),
        )
        large_spot_internal = quantized_internal_quantity(
            symbol="BTC-USDT",
            quantity=Decimal("-12345.678"),
            instrument=spot_instrument,
        )
        large_linear_internal = quantized_internal_quantity(
            symbol="BTC-USDT-SWAP",
            quantity=Decimal("123.4567"),
            instrument=_instrument(lot_size=Decimal("0.1")),
        )
    with localcontext() as context:
        context.prec = 28
        high_precision = round_down_to_step(
            value=Decimal("0.0129"),
            step=Decimal("0.001"),
        )

    assert low_precision == high_precision == Decimal("0.012")
    assert large_value == Decimal("999.00")
    assert quantized_internal == Decimal("0.012")
    assert large_spot_internal == Decimal("-12345.67")
    assert large_linear_internal == Decimal("123.456")


@pytest.mark.parametrize("step", [Decimal("0"), Decimal("NaN")])
def test_step_rounding_invalid_step_uses_stable_domain_error(step: Decimal) -> None:
    with pytest.raises(
        InstrumentContractError,
        match="quantity_step_rounding_invalid",
    ):
        round_down_to_step(value=Decimal("1"), step=step)
