from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from aats.data_platform.replay.derivatives_backtest import (
    DerivativesBacktestContractError,
    DerivativesBacktestScopeV1,
    ExecutionFeeScheduleV1,
    FundingRateScheduleV1,
    LinearPerpetualContractV1,
    LiquidityRoleV1,
    MarginModeV1,
    OpeningAccountStateV1,
    PositionModeV1,
    PositionSideV1,
    PositionStateV1,
    PositionTierV1,
    canonical_accounting_decimal,
    parse_canonical_accounting_decimal,
)
from aats.domain.instrument_contract import InstrumentContract


def linear_contract() -> LinearPerpetualContractV1:
    return LinearPerpetualContractV1(
        contract_value="1e-2",
        contract_multiplier="1e0",
        lot_size="1e0",
        min_size="1e0",
        tick_size="1e-1",
    )


def test_scope_is_permanently_narrow() -> None:
    scope = DerivativesBacktestScopeV1(linear_contract())

    assert scope.symbol == "BTC-USDT-SWAP"
    assert scope.family == "independent"
    assert scope.timeframe == "15m"
    assert scope.margin_mode is MarginModeV1.ISOLATED
    assert scope.position_mode is PositionModeV1.SINGLE_POSITION


def test_scope_rejects_other_instruments() -> None:
    untrusted = InstrumentContract(
        symbol="ETH-USDT-SWAP",
        instrument_type="SWAP",
        contract_type="linear",
        base_currency="ETH",
        quote_currency="USDT",
        settle_currency="USDT",
        contract_value=Decimal("0.01"),
        contract_multiplier=Decimal("1"),
        contract_value_currency="ETH",
        lot_size=Decimal("1"),
        min_size=Decimal("1"),
        tick_size=Decimal("0.1"),
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        DerivativesBacktestScopeV1(untrusted)  # type: ignore[arg-type]

    assert exc_info.value.code == "instrument_contract_invalid"


def test_position_tier_freezes_tier_one_and_zero_deduction() -> None:
    tier = PositionTierV1(
        tier_id=1,
        minimum_notional_inclusive=Decimal("-0.00"),
        maximum_notional_inclusive=Decimal("1000000"),
        maximum_leverage=Decimal("100"),
        maintenance_margin_rate=Decimal("0.005"),
        maintenance_margin_deduction=Decimal("-0.00"),
        liquidation_fee_rate=Decimal("0.0025"),
    )

    assert tier.tier_id == 1
    assert canonical_accounting_decimal(
        tier.maintenance_margin_deduction,
        "maintenance_margin_deduction",
    ) == "0"

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        PositionTierV1(
            tier_id=1,
            minimum_notional_inclusive=Decimal("0"),
            maximum_notional_inclusive=Decimal("1000000"),
            maximum_leverage=Decimal("100"),
            maintenance_margin_rate=Decimal("0.005"),
            maintenance_margin_deduction=Decimal("0.01"),
            liquidation_fee_rate=Decimal("0.0025"),
        )

    assert exc_info.value.code == "position_tier_deduction_out_of_v1_scope"

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        PositionTierV1(
            tier_id=1,
            minimum_notional_inclusive=Decimal("0"),
            maximum_notional_inclusive=Decimal("1000000"),
            maximum_leverage=Decimal("100"),
            maintenance_margin_rate=Decimal("0.005"),
            maintenance_margin_deduction=Decimal("-0.01"),
            liquidation_fee_rate=Decimal("0.0025"),
        )

    assert exc_info.value.code == "position_tier_deduction_out_of_v1_scope"


@pytest.mark.parametrize("value", [True, False, 1, 1.0, "1"])
def test_economic_fields_reject_implicit_numeric_coercion(value: object) -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        OpeningAccountStateV1(
            total_account_cash_before_transfer_usdt=value,  # type: ignore[arg-type]
            isolated_transfer_in_usdt=Decimal("1"),
        )

    assert exc_info.value.code == "economic_decimal_type_invalid"
    assert exc_info.value.field == "total_account_cash_before_transfer_usdt"


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
def test_economic_fields_reject_non_finite_decimal(value: Decimal) -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        OpeningAccountStateV1(
            total_account_cash_before_transfer_usdt=value,
            isolated_transfer_in_usdt=Decimal("1"),
        )

    assert exc_info.value.code == "economic_decimal_non_finite"


def test_fee_schedule_preserves_rebate_and_requires_usdt() -> None:
    schedule = ExecutionFeeScheduleV1(
        maker_fee_rate=Decimal("-0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        fee_asset="USDT",
    )

    assert schedule.rate_for(LiquidityRoleV1.MAKER) == Decimal("-0.0002")
    assert schedule.rate_for(LiquidityRoleV1.TAKER) == Decimal("0.0005")

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        ExecutionFeeScheduleV1(
            maker_fee_rate=Decimal("0"),
            taker_fee_rate=Decimal("0.0005"),
            fee_asset="BTC",
        )

    assert exc_info.value.code == "fee_asset_out_of_v1_scope"


@pytest.mark.parametrize(
    ("maker", "taker"),
    [
        (Decimal("-1"), Decimal("0")),
        (Decimal("0"), Decimal("1")),
    ],
)
def test_fee_schedule_rejects_closed_rate_boundaries(
    maker: Decimal,
    taker: Decimal,
) -> None:
    with pytest.raises(DerivativesBacktestContractError):
        ExecutionFeeScheduleV1(
            maker_fee_rate=maker,
            taker_fee_rate=taker,
            fee_asset="USDT",
        )


def test_funding_schedule_bounds_are_inclusive_and_fail_closed() -> None:
    schedule = FundingRateScheduleV1(
        minimum_rate_inclusive=Decimal("-0.001"),
        maximum_rate_inclusive=Decimal("0.001"),
    )

    assert schedule.validate_rate(Decimal("-0.001")) == Decimal("-0.001")
    assert schedule.validate_rate(Decimal("0.001")) == Decimal("0.001")

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        schedule.validate_rate(Decimal("0.0010001"))

    assert exc_info.value.code == "funding_rate_out_of_schedule"


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [
        (Decimal("-1"), Decimal("0")),
        (Decimal("0"), Decimal("1")),
    ],
)
def test_funding_schedule_rejects_closed_absolute_rate_boundaries(
    minimum: Decimal,
    maximum: Decimal,
) -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        FundingRateScheduleV1(
            minimum_rate_inclusive=minimum,
            maximum_rate_inclusive=maximum,
        )

    assert exc_info.value.code == "funding_rate_schedule_bounds_invalid"


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("0", Decimal("0")),
        ("1e0", Decimal("1")),
        ("-2e-4", Decimal("-0.0002")),
        ("123e-2", Decimal("1.23")),
    ],
)
def test_canonical_decimal_wire_parser_accepts_only_exact_identity(
    wire: str,
    expected: Decimal,
) -> None:
    assert parse_canonical_accounting_decimal(wire, "rate") == expected


@pytest.mark.parametrize(
    "wire",
    ["", " 1e0", "1", "1.0", "01e0", "+1e0", "0e0", "NaN", "Infinity"],
)
def test_canonical_decimal_wire_parser_rejects_alternate_spellings(wire: str) -> None:
    with pytest.raises(DerivativesBacktestContractError):
        parse_canonical_accounting_decimal(wire, "rate")


def test_canonical_decimal_wire_parser_is_bounded_before_decimal_parse() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        parse_canonical_accounting_decimal("1" * 100_000 + "e0", "rate")

    assert exc_info.value.code == "economic_decimal_non_canonical"


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1e-256"),
        Decimal("1e256"),
        Decimal("9" * 256),
    ],
)
def test_canonical_decimal_writer_and_parser_close_at_domain_boundary(
    value: Decimal,
) -> None:
    wire = canonical_accounting_decimal(value, "boundary")

    assert parse_canonical_accounting_decimal(wire, "boundary") == value


def test_canonical_decimal_bounds_are_applied_after_trailing_zero_reduction() -> None:
    equivalent_boundary = Decimal("1" + "0" * 256)

    assert canonical_accounting_decimal(equivalent_boundary, "boundary") == "1e256"
    assert parse_canonical_accounting_decimal("1e256", "boundary") == Decimal(
        "1e256"
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        canonical_accounting_decimal(Decimal("1.0e257"), "boundary")

    assert exc_info.value.code == "economic_decimal_out_of_bounds"


@pytest.mark.parametrize(
    "value",
    [
        Decimal("1e-257"),
        Decimal("1e257"),
        Decimal("9" * 257),
    ],
)
def test_canonical_decimal_writer_rejects_values_parser_cannot_consume(
    value: Decimal,
) -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        canonical_accounting_decimal(value, "boundary")

    assert exc_info.value.code == "economic_decimal_out_of_bounds"


def test_canonical_decimal_rejects_decimal_subclasses() -> None:
    class DecimalSubclass(Decimal):
        pass

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        canonical_accounting_decimal(DecimalSubclass("1"), "value")

    assert exc_info.value.code == "economic_decimal_type_invalid"


def test_linear_contract_requires_canonical_wire_values() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        LinearPerpetualContractV1(
            contract_value=0.01,  # type: ignore[arg-type]
            contract_multiplier="1e0",
            lot_size="1e0",
            min_size="1e0",
            tick_size="1e-1",
        )

    assert exc_info.value.code == "economic_decimal_wire_type_invalid"
    assert exc_info.value.field == "contract_value"


@pytest.mark.parametrize(
    "operation",
    [
        lambda contract: contract.validate_exchange_quantity(1),
        lambda contract: contract.validate_exchange_price(50000.0),
        lambda contract: contract.base_quantity(1.0),
        lambda contract: contract.quote_notional(
            Decimal("1"),
            price=50000.0,
        ),
        lambda contract: contract.settlement_pnl(
            Decimal("1"),
            entry_price=Decimal("50000"),
            exit_price=51000.0,
        ),
        lambda contract: contract.add_settlement_amounts(Decimal("1"), 2.0),
        lambda contract: contract.execution_price(
            Decimal("50000"),
            side="buy",
            slippage_bps=0.0,
        ),
    ],
)
def test_linear_contract_arithmetic_facade_rejects_implicit_numeric_coercion(
    operation: Callable[[LinearPerpetualContractV1], object],
) -> None:
    contract = linear_contract()

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        operation(contract)

    assert exc_info.value.code == "economic_decimal_type_invalid"
    assert not hasattr(contract, "instrument")
    assert not hasattr(contract, "settlement_fee")


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    [
        (
            lambda contract: contract.validate_exchange_quantity(Decimal("1.5")),
            "exchange_quantity_lot_misaligned",
        ),
        (
            lambda contract: contract.validate_exchange_price(Decimal("50000.05")),
            "exchange_price_tick_misaligned",
        ),
        (
            lambda contract: contract.execution_price(
                Decimal("50000"),
                side="hold",  # type: ignore[arg-type]
                slippage_bps=Decimal("0"),
            ),
            "execution_side_invalid",
        ),
    ],
)
def test_linear_contract_facade_never_leaks_shared_contract_errors(
    operation: Callable[[LinearPerpetualContractV1], object],
    expected_code: str,
) -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        operation(linear_contract())

    assert type(exc_info.value) is DerivativesBacktestContractError
    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("field_name", "value", "expected_code"),
    [
        ("contract_value", "0", "contract_face_value_invalid"),
        ("contract_multiplier", "-1e0", "contract_face_value_invalid"),
        ("lot_size", "0", "lot_size_invalid"),
        ("tick_size", "-1e-1", "tick_size_invalid"),
    ],
)
def test_linear_contract_constructor_maps_shared_validation_errors(
    field_name: str,
    value: str,
    expected_code: str,
) -> None:
    fields = {
        "contract_value": "1e-2",
        "contract_multiplier": "1e0",
        "lot_size": "1e0",
        "min_size": "1e0",
        "tick_size": "1e-1",
    }
    fields[field_name] = value

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        LinearPerpetualContractV1(**fields)

    assert type(exc_info.value) is DerivativesBacktestContractError
    assert exc_info.value.code == expected_code


def test_linear_contract_constructor_validates_derived_face_value() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        LinearPerpetualContractV1(
            contract_value="1e-256",
            contract_multiplier="1e-256",
            lot_size="1e0",
            min_size="1e0",
            tick_size="1e-1",
        )

    assert exc_info.value.code == "economic_decimal_out_of_bounds"
    assert exc_info.value.field == "instrument_face_value"


def test_public_contract_facade_does_not_expose_raw_fee_arithmetic() -> None:
    contract = linear_contract()

    assert not hasattr(contract, "settlement_fee")


def test_opening_state_is_flat_and_only_transfers_bound_total_cash() -> None:
    opening = OpeningAccountStateV1(
        total_account_cash_before_transfer_usdt=Decimal("1002.5"),
        isolated_transfer_in_usdt=Decimal("1000"),
    )

    assert opening.total_account_cash_before_transfer_usdt == Decimal("1002.5")

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        OpeningAccountStateV1(
            total_account_cash_before_transfer_usdt=Decimal("1000"),
            isolated_transfer_in_usdt=Decimal("1000.1"),
        )

    assert exc_info.value.code == "isolated_transfer_exceeds_total_account_cash"


def test_position_state_has_one_unambiguous_side() -> None:
    contract = linear_contract()
    assert PositionStateV1(contract, Decimal("0"), None).side is PositionSideV1.FLAT
    assert (
        PositionStateV1(contract, Decimal("1"), Decimal("50000")).side
        is PositionSideV1.LONG
    )
    assert (
        PositionStateV1(contract, Decimal("-1"), Decimal("50000")).side
        is PositionSideV1.SHORT
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        PositionStateV1(contract, Decimal("0"), Decimal("50000"))

    assert exc_info.value.code == "flat_position_average_entry_must_be_none"

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        PositionStateV1(contract, Decimal("1.5"), Decimal("50000"))

    assert exc_info.value.code == "position_contracts_invalid"
