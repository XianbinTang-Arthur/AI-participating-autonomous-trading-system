from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from aats.domain.instrument_contract import (
    InstrumentContractError,
    instrument_contract_from_metadata,
)
from aats.schemas.exchange import InstrumentMetadata
from aats.services.execution_engine.okx_account import OKXAccountService


def _instrument(
    *,
    symbol: str,
    base: str,
    quote: str,
    settle: str,
    contract_type: str | None,
    contract_value: str,
    contract_value_currency: str,
    contract_multiplier: str = "1",
    instrument_type: str = "SWAP",
    lot_size: str = "0.01",
) -> InstrumentMetadata:
    return InstrumentMetadata(
        instrument_id=symbol,
        symbol=symbol,
        base_currency=base,
        quote_currency=quote,
        lot_size=Decimal(lot_size),
        tick_size=Decimal("0.1"),
        min_size=Decimal("0.01"),
        contract_value=Decimal(contract_value),
        contract_multiplier=Decimal(contract_multiplier),
        contract_type=contract_type,
        instrument_type=instrument_type,
        settle_currency=settle,
        contract_value_currency=contract_value_currency,
        state="live",
    )


def test_linear_contract_golden_quantity_notional_and_pnl() -> None:
    contract = instrument_contract_from_metadata(
        _instrument(
            symbol="BTC-USDT-SWAP",
            base="BTC",
            quote="USDT",
            settle="USDT",
            contract_type="linear",
            contract_value="0.01",
            contract_value_currency="BTC",
        )
    )

    assert contract.face_value == Decimal("0.01")
    assert contract.base_quantity(Decimal("3")) == Decimal("0.03")
    assert contract.exchange_quantity(Decimal("0.03")) == Decimal("3")
    assert contract.quote_notional(Decimal("3"), price=Decimal("60000")) == Decimal("1800.00")
    assert contract.settlement_pnl(
        Decimal("3"),
        entry_price=Decimal("60000"),
        exit_price=Decimal("61000"),
    ) == Decimal("30.00")
    assert contract.settlement_pnl(
        Decimal("-3"),
        entry_price=Decimal("60000"),
        exit_price=Decimal("61000"),
    ) == Decimal("-30.00")
    assert contract.settlement_pnl_currency == "USDT"


def test_inverse_contract_golden_quantity_notional_and_pnl() -> None:
    contract = instrument_contract_from_metadata(
        _instrument(
            symbol="BTC-USD-SWAP",
            base="BTC",
            quote="USD",
            settle="BTC",
            contract_type="inverse",
            contract_value="100",
            contract_value_currency="USD",
            lot_size="1",
        )
    )

    assert contract.base_quantity(
        Decimal("3"),
        reference_price=Decimal("60000"),
    ) == Decimal("0.005")
    assert contract.exchange_quantity(
        Decimal("0.005"),
        reference_price=Decimal("60000"),
    ) == Decimal("3.000")
    assert contract.quote_notional(Decimal("3"), price=Decimal("60000")) == Decimal("300")
    assert contract.settlement_pnl(
        Decimal("3"),
        entry_price=Decimal("60000"),
        exit_price=Decimal("61000"),
    ) == Decimal(
        "0.000081967213114754098360655737704918032786885245901700"
    )
    assert contract.settlement_pnl_currency == "BTC"


def test_spot_contract_uses_base_quantity_and_quote_pnl() -> None:
    contract = instrument_contract_from_metadata(
        _instrument(
            symbol="BTC-USDT",
            base="BTC",
            quote="USDT",
            settle="USDT",
            contract_type=None,
            contract_value="1",
            contract_value_currency="BTC",
            instrument_type="SPOT",
        )
    )

    assert contract.contract_type == "spot"
    assert contract.base_quantity(Decimal("0.25")) == Decimal("0.25")
    assert contract.quote_notional(
        Decimal("-0.25"),
        price=Decimal("60000"),
    ) == Decimal("15000.00")
    assert contract.settlement_pnl(
        Decimal("0.25"),
        entry_price=Decimal("60000"),
        exit_price=Decimal("61000"),
    ) == Decimal("250.00")


def test_derivative_contract_type_must_be_explicit_even_when_currencies_are_consistent() -> None:
    for metadata in (
        _instrument(
            symbol="ETH-USDT-SWAP",
            base="ETH",
            quote="USDT",
            settle="USDT",
            contract_type=None,
            contract_value="0.1",
            contract_value_currency="ETH",
        ),
        _instrument(
            symbol="ETH-USD-SWAP",
            base="ETH",
            quote="USD",
            settle="ETH",
            contract_type=None,
            contract_value="10",
            contract_value_currency="USD",
        ),
    ):
        with pytest.raises(
            InstrumentContractError,
            match="contract_type_unknown_or_inconsistent",
        ):
            instrument_contract_from_metadata(metadata)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"contract_type": None, "contract_value_currency": ""}, "derivative_instrument_metadata_required"),
        ({"contract_type": "inverse"}, "contract_value_currency_inconsistent"),
        ({"contract_type": "quanto"}, "contract_type_unknown_or_inconsistent"),
        ({"contract_value": "0"}, "contract_face_value_invalid"),
        ({"contract_multiplier": "NaN"}, "finite number"),
    ],
)
def test_invalid_or_contradictory_derivative_metadata_fails_closed(
    overrides: dict[str, str | None],
    reason: str,
) -> None:
    values: dict[str, str | None] = {
        "symbol": "BTC-USDT-SWAP",
        "base": "BTC",
        "quote": "USDT",
        "settle": "USDT",
        "contract_type": "linear",
        "contract_value": "0.01",
        "contract_value_currency": "BTC",
        "contract_multiplier": "1",
    }
    values.update(overrides)

    with pytest.raises((InstrumentContractError, ValueError), match=reason):
        instrument_contract_from_metadata(_instrument(**values))  # type: ignore[arg-type]


def test_inverse_quantity_requires_positive_reference_price() -> None:
    contract = instrument_contract_from_metadata(
        _instrument(
            symbol="BTC-USD-SWAP",
            base="BTC",
            quote="USD",
            settle="BTC",
            contract_type="inverse",
            contract_value="100",
            contract_value_currency="USD",
        )
    )

    with pytest.raises(
        InstrumentContractError,
        match="inverse_quantity_requires_reference_price",
    ):
        contract.base_quantity(Decimal("1"))
    with pytest.raises(InstrumentContractError, match="reference_price_invalid"):
        contract.exchange_quantity(Decimal("0.01"), reference_price=Decimal("0"))


def test_derivative_shaped_symbol_without_instrument_type_fails_closed() -> None:
    metadata = _instrument(
        symbol="BTC-USDT-SWAP",
        base="BTC",
        quote="USDT",
        settle="USDT",
        contract_type="linear",
        contract_value="0.01",
        contract_value_currency="BTC",
        instrument_type="",
    )

    with pytest.raises(
        InstrumentContractError,
        match="derivative_instrument_metadata_required",
    ):
        instrument_contract_from_metadata(metadata)


@pytest.mark.parametrize(
    ("symbol", "instrument_type"),
    [
        ("BTC-USDT-SWAP", "FUTURES"),
        ("BTC-USDT-260925", "SWAP"),
        ("BTC-USDT", "FUTURES"),
    ],
)
def test_derivative_symbol_and_instrument_type_must_agree(
    symbol: str,
    instrument_type: str,
) -> None:
    with pytest.raises(
        InstrumentContractError,
        match="contract_type_unknown_or_inconsistent",
    ):
        instrument_contract_from_metadata(
            _instrument(
                symbol=symbol,
                base="BTC",
                quote="USDT",
                settle="USDT",
                contract_type="linear",
                contract_value="0.01",
                contract_value_currency="BTC",
                instrument_type=instrument_type,
            )
        )


def test_spot_instrument_rejects_derivative_contract_type() -> None:
    with pytest.raises(
        InstrumentContractError,
        match="contract_type_unknown_or_inconsistent",
    ):
        instrument_contract_from_metadata(
            _instrument(
                symbol="BTC-USDT",
                base="BTC",
                quote="USDT",
                settle="USDT",
                contract_type="linear",
                contract_value="1",
                contract_value_currency="BTC",
                instrument_type="SPOT",
            )
        )


def test_non_unit_contract_multiplier_changes_every_contract_value() -> None:
    contract = instrument_contract_from_metadata(
        _instrument(
            symbol="ETH-USDT-SWAP",
            base="ETH",
            quote="USDT",
            settle="USDT",
            contract_type="linear",
            contract_value="0.1",
            contract_multiplier="2",
            contract_value_currency="ETH",
        )
    )

    assert contract.face_value == Decimal("0.2")
    assert contract.base_quantity(Decimal("3")) == Decimal("0.6")
    assert contract.quote_notional(
        Decimal("3"),
        price=Decimal("4000"),
    ) == Decimal("2400.0")


def test_inverse_pnl_is_independent_of_callers_decimal_context() -> None:
    contract = instrument_contract_from_metadata(
        _instrument(
            symbol="BTC-USD-SWAP",
            base="BTC",
            quote="USD",
            settle="BTC",
            contract_type="inverse",
            contract_value="100",
            contract_value_currency="USD",
        )
    )

    with localcontext() as context:
        context.prec = 10
        low_precision = contract.settlement_pnl(
            Decimal("3"),
            entry_price=Decimal("60000"),
            exit_price=Decimal("61000"),
        )
    with localcontext() as context:
        context.prec = 50
        high_precision = contract.settlement_pnl(
            Decimal("3"),
            entry_price=Decimal("60000"),
            exit_price=Decimal("61000"),
        )

    assert low_precision == high_precision


def test_symbol_currency_and_instrument_identity_mismatch_fail_closed() -> None:
    wrong_currency = _instrument(
        symbol="BTC-USDT-SWAP",
        base="ETH",
        quote="USDT",
        settle="USDT",
        contract_type="linear",
        contract_value="0.1",
        contract_value_currency="ETH",
    )
    wrong_id = wrong_currency.model_copy(
        update={"base_currency": "BTC", "contract_value_currency": "BTC", "instrument_id": "ETH-USDT-SWAP"}
    )

    with pytest.raises(
        InstrumentContractError,
        match="instrument_currency_identity_mismatch",
    ):
        instrument_contract_from_metadata(wrong_currency)
    with pytest.raises(InstrumentContractError, match="instrument_identity_mismatch"):
        instrument_contract_from_metadata(wrong_id)


def test_missing_contract_value_or_multiplier_is_not_defaulted() -> None:
    base = _instrument(
        symbol="BTC-USDT-SWAP",
        base="BTC",
        quote="USDT",
        settle="USDT",
        contract_type="linear",
        contract_value="0.01",
        contract_value_currency="BTC",
    )

    for field_name in ("contract_value", "contract_multiplier"):
        with pytest.raises(
            InstrumentContractError,
            match="derivative_instrument_metadata_required",
        ):
            instrument_contract_from_metadata(base.model_copy(update={field_name: None}))


def test_okx_parser_preserves_missing_derivative_face_fields_as_unknown() -> None:
    metadata = OKXAccountService._parse_instruments(
        {
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "instType": "SWAP",
                    "ctType": "linear",
                    "uly": "BTC-USDT",
                    "settleCcy": "USDT",
                    "ctValCcy": "BTC",
                    "lotSz": "0.01",
                    "minSz": "0.01",
                    "tickSz": "0.1",
                    "state": "live",
                }
            ]
        }
    )[0]

    assert metadata.contract_value is None
    assert metadata.contract_multiplier is None
    with pytest.raises(
        InstrumentContractError,
        match="derivative_instrument_metadata_required",
    ):
        instrument_contract_from_metadata(metadata)


def test_okx_inverse_parser_does_not_reverse_base_and_quote_currencies() -> None:
    metadata = OKXAccountService._parse_instruments(
        {
            "data": [
                {
                    "instId": "BTC-USD-SWAP",
                    "instType": "SWAP",
                    "ctType": "inverse",
                    "uly": "",
                    "settleCcy": "BTC",
                    "ctVal": "100",
                    "ctMult": "1",
                    "ctValCcy": "USD",
                    "lotSz": "0.1",
                    "minSz": "0.1",
                    "tickSz": "0.1",
                    "state": "live",
                }
            ]
        }
    )[0]

    assert metadata.base_currency == "BTC"
    assert metadata.quote_currency == "USD"
    assert instrument_contract_from_metadata(metadata).contract_type == "inverse"


@pytest.mark.parametrize("missing_field", ["lotSz", "tickSz", "minSz"])
def test_okx_parser_never_invents_missing_trading_rules(missing_field: str) -> None:
    row = {
        "instId": "BTC-USDT-SWAP",
        "instType": "SWAP",
        "ctType": "linear",
        "uly": "BTC-USDT",
        "settleCcy": "USDT",
        "ctVal": "0.01",
        "ctMult": "1",
        "ctValCcy": "BTC",
        "lotSz": "1",
        "minSz": "1",
        "tickSz": "0.1",
        "state": "live",
    }
    row.pop(missing_field)

    with pytest.raises(
        InstrumentContractError,
        match=f"instrument_trading_rule_required:{missing_field}",
    ):
        OKXAccountService._parse_instruments({"data": [row]})


def test_okx_parser_preserves_non_unit_contract_multiplier() -> None:
    metadata = OKXAccountService._parse_instruments(
        {
            "data": [
                {
                    "instId": "ETH-USDT-SWAP",
                    "instType": "SWAP",
                    "ctType": "linear",
                    "uly": "ETH-USDT",
                    "settleCcy": "USDT",
                    "ctVal": "0.1",
                    "ctMult": "2",
                    "ctValCcy": "ETH",
                    "lotSz": "1",
                    "minSz": "1",
                    "tickSz": "0.01",
                    "state": "live",
                }
            ]
        }
    )[0]

    assert metadata.contract_multiplier == Decimal("2")
    assert instrument_contract_from_metadata(metadata).face_value == Decimal("0.2")


def test_spot_metadata_without_contract_fields_has_explicit_compatibility_contract() -> None:
    metadata = InstrumentMetadata(
        instrument_id="BTC-USDT",
        symbol="BTC-USDT",
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.0001"),
        tick_size=Decimal("0.1"),
        min_size=Decimal("0.0001"),
        instrument_type="SPOT",
        state="live",
    )

    assert metadata.model_dump()["contract_value"] is None
    assert instrument_contract_from_metadata(metadata).face_value == Decimal("1")


def test_face_value_underflow_is_mapped_to_stable_domain_error() -> None:
    contract = instrument_contract_from_metadata(
        _instrument(
            symbol="BTC-USDT-SWAP",
            base="BTC",
            quote="USDT",
            settle="USDT",
            contract_type="linear",
            contract_value="1e-999999",
            contract_multiplier="1e-999999",
            contract_value_currency="BTC",
        )
    )

    with pytest.raises(
        InstrumentContractError,
        match="contract_face_value_invalid",
    ):
        _ = contract.face_value
