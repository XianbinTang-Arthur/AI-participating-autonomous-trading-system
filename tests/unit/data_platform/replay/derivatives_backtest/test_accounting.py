from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext

import pytest

from aats.data_platform.replay.derivatives_backtest import (
    DerivativesAccountingError,
    ExecutionFeeScheduleV1,
    FundingRateScheduleV1,
    LinearPerpetualContractV1,
    LiquidityRoleV1,
    OpeningAccountStateV1,
    PositionTierV1,
    account_equity,
    conservative_liquidation_price,
    fee_amount_for_role,
    forced_close_cost,
    forced_close_rate,
    funding_payment,
    initial_margin_rate,
    initial_margin_required,
    is_liquidatable,
    isolated_equity,
    maintenance_margin_required,
    net_pnl,
    opening_account_state,
    quote_notional,
    raw_liquidation_price,
    realized_pnl,
    signed_base_exposure,
    unrealized_pnl,
)


def linear_contract() -> LinearPerpetualContractV1:
    return LinearPerpetualContractV1(
        contract_value="1e-2",
        contract_multiplier="1e0",
        lot_size="1e0",
        min_size="1e0",
        tick_size="1e-1",
    )


def tier(
    *,
    maximum_leverage: str = "100",
    maintenance_margin_rate: str = "0.005",
    liquidation_fee_rate: str = "0.0025",
) -> PositionTierV1:
    return PositionTierV1(
        tier_id=1,
        minimum_notional_inclusive=Decimal("0"),
        maximum_notional_inclusive=Decimal("1000000"),
        maximum_leverage=Decimal(maximum_leverage),
        maintenance_margin_rate=Decimal(maintenance_margin_rate),
        maintenance_margin_deduction=Decimal("0"),
        liquidation_fee_rate=Decimal(liquidation_fee_rate),
    )


def fee_schedule(*, taker: str = "0.0005") -> ExecutionFeeScheduleV1:
    return ExecutionFeeScheduleV1(
        maker_fee_rate=Decimal("-0.0002"),
        taker_fee_rate=Decimal(taker),
        fee_asset="USDT",
    )


def funding_schedule() -> FundingRateScheduleV1:
    return FundingRateScheduleV1(
        minimum_rate_inclusive=Decimal("-0.001"),
        maximum_rate_inclusive=Decimal("0.001"),
    )


def test_linear_unit_notional_pnl_and_fee_golden_vectors() -> None:
    contract = linear_contract()

    assert signed_base_exposure(contract, position_contracts=Decimal("3")) == Decimal("0.03")
    assert quote_notional(
        contract,
        position_contracts=Decimal("3"),
        price=Decimal("61000"),
    ) == Decimal("1830.00")
    assert unrealized_pnl(
        contract,
        position_contracts=Decimal("3"),
        average_entry_price=Decimal("60000"),
        mark_price=Decimal("61000"),
    ) == Decimal("30.00")
    assert unrealized_pnl(
        contract,
        position_contracts=Decimal("-3"),
        average_entry_price=Decimal("60000"),
        mark_price=Decimal("61000"),
    ) == Decimal("-30.00")
    fees = fee_schedule()
    assert fee_amount_for_role(
        contract,
        filled_contracts=Decimal("3"),
        fill_price=Decimal("60000"),
        fee_schedule=fees,
        liquidity_role=LiquidityRoleV1.TAKER,
    ) == Decimal("0.900000")
    assert fee_amount_for_role(
        contract,
        filled_contracts=Decimal("3"),
        fill_price=Decimal("60000"),
        fee_schedule=fees,
        liquidity_role=LiquidityRoleV1.MAKER,
    ) == Decimal("-0.360000")
    assert contract.add_settlement_amounts(
        Decimal("100"),
        -fee_amount_for_role(
            contract,
            filled_contracts=Decimal("3"),
            fill_price=Decimal("60000"),
            fee_schedule=fees,
            liquidity_role=LiquidityRoleV1.MAKER,
        ),
    ) == Decimal("100.36")


def test_long_short_realized_pnl_golden_vectors() -> None:
    contract = linear_contract()

    assert realized_pnl(
        contract,
        position_contracts_before=Decimal("20"),
        closing_contracts=Decimal("5"),
        average_entry_price=Decimal("51000"),
        exit_price=Decimal("53000"),
    ) == Decimal("100.00")
    assert realized_pnl(
        contract,
        position_contracts_before=Decimal("-10"),
        closing_contracts=Decimal("4"),
        average_entry_price=Decimal("50000"),
        exit_price=Decimal("48000"),
    ) == Decimal("80.00")


def test_funding_signs_and_schedule_bounds() -> None:
    contract = linear_contract()
    schedule = funding_schedule()

    assert funding_payment(
        contract,
        position_contracts_before=Decimal("1"),
        mark_price=Decimal("50000"),
        funding_rate=Decimal("0.0001"),
        funding_schedule=schedule,
    ) == Decimal("0.050000")
    assert funding_payment(
        contract,
        position_contracts_before=Decimal("-1"),
        mark_price=Decimal("50000"),
        funding_rate=Decimal("0.0001"),
        funding_schedule=schedule,
    ) == Decimal("-0.050000")
    assert funding_payment(
        contract,
        position_contracts_before=Decimal("1"),
        mark_price=Decimal("50000"),
        funding_rate=Decimal("-0.0001"),
        funding_schedule=schedule,
    ) == Decimal("-0.050000")
    assert funding_payment(
        contract,
        position_contracts_before=Decimal("-1"),
        mark_price=Decimal("50000"),
        funding_rate=Decimal("-0.0001"),
        funding_schedule=schedule,
    ) == Decimal("0.050000")
    assert funding_payment(
        contract,
        position_contracts_before=Decimal("0"),
        mark_price=Decimal("50000.03"),
        funding_rate=Decimal("0.0001"),
        funding_schedule=schedule,
    ) == Decimal("0")

    with pytest.raises(DerivativesAccountingError) as exc_info:
        funding_payment(
            contract,
            position_contracts_before=Decimal("1"),
            mark_price=Decimal("50000"),
            funding_rate=Decimal("0.002"),
            funding_schedule=schedule,
        )

    assert exc_info.value.code == "funding_rate_out_of_schedule"


def test_margin_rate_rejects_mmr_equal_to_imr_and_rho_equal_to_one() -> None:
    equal_rate_tier = tier(
        maximum_leverage="100",
        maintenance_margin_rate="0.01",
    )

    with pytest.raises(DerivativesAccountingError) as exc_info:
        initial_margin_rate(leverage=Decimal("100"), tier=equal_rate_tier)

    assert exc_info.value.code == "initial_margin_rate_invalid"

    rho_one_tier = tier(
        maximum_leverage="1",
        maintenance_margin_rate="0.5",
        liquidation_fee_rate="0",
    )
    with pytest.raises(DerivativesAccountingError) as exc_info:
        forced_close_rate(
            tier=rho_one_tier,
            fee_schedule=fee_schedule(taker="0.5"),
        )

    assert exc_info.value.code == "liquidation_rate_invalid"


def test_one_x_initial_margin_boundary_and_maintenance_margin() -> None:
    contract = linear_contract()
    risk_tier = tier(maximum_leverage="10")

    assert initial_margin_required(
        contract,
        position_contracts=Decimal("1"),
        mark_price=Decimal("50000"),
        leverage=Decimal("1"),
        tier=risk_tier,
    ) == Decimal("500.00")
    assert maintenance_margin_required(
        contract,
        position_contracts=Decimal("1"),
        mark_price=Decimal("50000"),
        tier=risk_tier,
    ) == Decimal("2.50000")

    with pytest.raises(DerivativesAccountingError) as exc_info:
        initial_margin_required(
            contract,
            position_contracts=Decimal("1"),
            mark_price=Decimal("50000"),
            leverage=Decimal("10.0001"),
            tier=risk_tier,
        )

    assert exc_info.value.code == "leverage_out_of_tier_scope"


def test_main_golden_vector_reserves_entry_fee_before_margin() -> None:
    contract = linear_contract()
    risk_tier = tier()
    fees = fee_schedule()
    opening = opening_account_state(
        contract,
        OpeningAccountStateV1(
            total_account_cash_before_transfer_usdt=Decimal("1002.5"),
            isolated_transfer_in_usdt=Decimal("1002.5"),
        )
    )
    entry_fee = fee_amount_for_role(
        contract,
        filled_contracts=Decimal("10"),
        fill_price=Decimal("50000"),
        fee_schedule=fees,
        liquidity_role=LiquidityRoleV1.TAKER,
    )
    post_entry_balance = contract.add_settlement_amounts(
        opening.isolated_balance_usdt,
        -entry_fee,
    )

    assert entry_fee == Decimal("2.500000")
    assert post_entry_balance == Decimal("1000")
    assert initial_margin_required(
        contract,
        position_contracts=Decimal("10"),
        mark_price=Decimal("50000"),
        leverage=Decimal("5"),
        tier=risk_tier,
    ) == Decimal("1000.00")
    assert maintenance_margin_required(
        contract,
        position_contracts=Decimal("10"),
        mark_price=Decimal("51000"),
        tier=risk_tier,
    ) == Decimal("25.50000")

    unrealized = unrealized_pnl(
        contract,
        position_contracts=Decimal("10"),
        average_entry_price=Decimal("50000"),
        mark_price=Decimal("51000"),
    )
    assert isolated_equity(
        contract,
        isolated_balance_usdt=post_entry_balance,
        unrealized_pnl_usdt=unrealized,
    ) == Decimal("1100.00")

    funding = funding_payment(
        contract,
        position_contracts_before=Decimal("10"),
        mark_price=Decimal("51000"),
        funding_rate=Decimal("0.0001"),
        funding_schedule=funding_schedule(),
    )
    realized = realized_pnl(
        contract,
        position_contracts_before=Decimal("10"),
        closing_contracts=Decimal("10"),
        average_entry_price=Decimal("50000"),
        exit_price=Decimal("52000"),
    )
    close_fee = fee_amount_for_role(
        contract,
        filled_contracts=Decimal("10"),
        fill_price=Decimal("52000"),
        fee_schedule=fees,
        liquidity_role=LiquidityRoleV1.TAKER,
    )
    final_balance = contract.add_settlement_amounts(
        post_entry_balance,
        -funding,
        realized,
        -close_fee,
    )
    final_equity = account_equity(
        contract,
        free_cash_usdt=opening.free_cash_usdt,
        isolated_equity_usdt=final_balance,
    )

    assert funding == Decimal("0.510000")
    assert realized == Decimal("200.00")
    assert close_fee == Decimal("2.600000")
    assert final_equity == Decimal("1196.890000")
    assert net_pnl(
        contract,
        account_equity_usdt=final_equity,
        initial_total_capital_usdt=Decimal("1002.5"),
    ) == Decimal("194.390000")
    assert raw_liquidation_price(
        contract,
        position_contracts=Decimal("10"),
        average_entry_price=Decimal("50000"),
        isolated_balance_usdt=post_entry_balance,
        tier=risk_tier,
        fee_schedule=fees,
    ) == Decimal(
        "40322.580645161290322580645161290322580645161290323"
    )
    assert conservative_liquidation_price(
        contract,
        position_contracts=Decimal("10"),
        average_entry_price=Decimal("50000"),
        isolated_balance_usdt=post_entry_balance,
        tier=risk_tier,
        fee_schedule=fees,
    ) == Decimal("40322.6")


def test_liquidation_raw_and_conservative_price_vectors() -> None:
    contract = linear_contract()
    risk_tier = tier(liquidation_fee_rate="0")
    fees = fee_schedule(taker="0.0005")

    long_raw = raw_liquidation_price(
        contract,
        position_contracts=Decimal("1"),
        average_entry_price=Decimal("50000"),
        isolated_balance_usdt=Decimal("50"),
        tier=risk_tier,
        fee_schedule=fees,
    )
    short_raw = raw_liquidation_price(
        contract,
        position_contracts=Decimal("-1"),
        average_entry_price=Decimal("50000"),
        isolated_balance_usdt=Decimal("50"),
        tier=risk_tier,
        fee_schedule=fees,
    )

    assert long_raw == Decimal(
        "45248.868778280542986425339366515837104072398190045"
    )
    assert short_raw == Decimal(
        "54699.154649428145201392342118349080059671805072103"
    )
    assert conservative_liquidation_price(
        contract,
        position_contracts=Decimal("1"),
        average_entry_price=Decimal("50000"),
        isolated_balance_usdt=Decimal("50"),
        tier=risk_tier,
        fee_schedule=fees,
    ) == Decimal("45248.9")
    assert conservative_liquidation_price(
        contract,
        position_contracts=Decimal("-1"),
        average_entry_price=Decimal("50000"),
        isolated_balance_usdt=Decimal("50"),
        tier=risk_tier,
        fee_schedule=fees,
    ) == Decimal("54699.1")


def test_liquidation_uses_raw_equity_inequality_not_rounded_price() -> None:
    contract = linear_contract()
    risk_tier = tier(liquidation_fee_rate="0")
    fees = fee_schedule(taker="0.0005")

    assert forced_close_cost(
        contract,
        position_contracts=Decimal("1"),
        mark_price=Decimal("50000"),
        tier=risk_tier,
        fee_schedule=fees,
    ) == Decimal("0.250000")
    assert is_liquidatable(
        contract,
        position_contracts=Decimal("1"),
        average_entry_price=Decimal("50000"),
        mark_price=Decimal("50000"),
        isolated_balance_usdt=Decimal("2.75"),
        tier=risk_tier,
        fee_schedule=fees,
    )
    assert not is_liquidatable(
        contract,
        position_contracts=Decimal("1"),
        average_entry_price=Decimal("50000"),
        mark_price=Decimal("50000"),
        isolated_balance_usdt=Decimal("2.7500001"),
        tier=risk_tier,
        fee_schedule=fees,
    )
    assert not is_liquidatable(
        contract,
        position_contracts=Decimal("1"),
        average_entry_price=Decimal("50000"),
        mark_price=Decimal("45248.88"),
        isolated_balance_usdt=Decimal("50"),
        tier=risk_tier,
        fee_schedule=fees,
    )
    assert is_liquidatable(
        contract,
        position_contracts=Decimal("1"),
        average_entry_price=Decimal("50000"),
        mark_price=Decimal("45248.86"),
        isolated_balance_usdt=Decimal("50"),
        tier=risk_tier,
        fee_schedule=fees,
    )


def test_zero_and_flat_paths_validate_all_economic_inputs_before_return() -> None:
    contract = linear_contract()
    risk_tier = tier()
    fees = fee_schedule()

    with pytest.raises(DerivativesAccountingError):
        initial_margin_required(
            contract,
            position_contracts=Decimal("0"),
            mark_price=50000.0,  # type: ignore[arg-type]
            leverage=Decimal("1"),
            tier=risk_tier,
        )
    with pytest.raises(DerivativesAccountingError):
        maintenance_margin_required(
            contract,
            position_contracts=Decimal("0"),
            mark_price=Decimal("NaN"),
            tier=risk_tier,
        )
    with pytest.raises(DerivativesAccountingError):
        forced_close_cost(
            contract,
            position_contracts=Decimal("0"),
            mark_price=True,  # type: ignore[arg-type]
            tier=risk_tier,
            fee_schedule=fees,
        )
    with pytest.raises(DerivativesAccountingError):
        is_liquidatable(
            contract,
            position_contracts=Decimal("0"),
            average_entry_price=None,
            mark_price=50000.0,  # type: ignore[arg-type]
            isolated_balance_usdt=Decimal("0"),
            tier=risk_tier,
            fee_schedule=fees,
        )
    with pytest.raises(DerivativesAccountingError):
        raw_liquidation_price(
            contract,
            position_contracts=Decimal("0"),
            average_entry_price=None,
            isolated_balance_usdt=0.0,  # type: ignore[arg-type]
            tier=risk_tier,
            fee_schedule=fees,
        )
    with pytest.raises(DerivativesAccountingError):
        conservative_liquidation_price(
            contract,
            position_contracts=Decimal("0"),
            average_entry_price=None,
            isolated_balance_usdt=0.0,  # type: ignore[arg-type]
            tier=risk_tier,
            fee_schedule=fees,
        )


def test_accounting_outputs_remain_inside_canonical_decimal_domain() -> None:
    contract = linear_contract()

    with pytest.raises(DerivativesAccountingError) as exc_info:
        quote_notional(
            contract,
            position_contracts=Decimal("1e256"),
            price=Decimal("1e256"),
        )

    assert exc_info.value.code == "quote_notional_invalid"

    maximum = Decimal("9" * 256)
    with pytest.raises(DerivativesAccountingError) as exc_info:
        account_equity(
            contract,
            free_cash_usdt=maximum,
            isolated_equity_usdt=maximum,
        )

    assert exc_info.value.code == "account_equity_invalid"

    unit_face_contract = LinearPerpetualContractV1(
        contract_value="1e0",
        contract_multiplier="1e0",
        lot_size="1e0",
        min_size="1e0",
        tick_size="1e-1",
    )
    with pytest.raises(DerivativesAccountingError) as exc_info:
        quote_notional(
            unit_face_contract,
            position_contracts=Decimal("2e128"),
            price=Decimal("5e128"),
        )

    assert exc_info.value.code == "quote_notional_invalid"


def test_public_accounting_failures_use_accounting_error_taxonomy() -> None:
    with pytest.raises(DerivativesAccountingError) as exc_info:
        fee_amount_for_role(
            linear_contract(),
            filled_contracts=Decimal("1"),
            fill_price=50000.0,  # type: ignore[arg-type]
            fee_schedule=fee_schedule(),
            liquidity_role=LiquidityRoleV1.TAKER,
        )

    assert exc_info.value.code == "economic_decimal_type_invalid"
    assert exc_info.value.field == "fill_price"


def test_tier_upper_notional_bound_is_inclusive() -> None:
    contract = linear_contract()
    risk_tier = tier()

    assert initial_margin_required(
        contract,
        position_contracts=Decimal("2000"),
        mark_price=Decimal("50000"),
        leverage=Decimal("1"),
        tier=risk_tier,
    ) == Decimal("1000000.00")

    with pytest.raises(DerivativesAccountingError) as exc_info:
        initial_margin_required(
            contract,
            position_contracts=Decimal("2001"),
            mark_price=Decimal("50000"),
            leverage=Decimal("1"),
            tier=risk_tier,
        )

    assert exc_info.value.code == "position_tier_out_of_v1_scope"


def test_short_realized_pnl_is_independent_of_ambient_context() -> None:
    contract = LinearPerpetualContractV1(
        contract_value="1e0",
        contract_multiplier="1e0",
        lot_size="1e-2",
        min_size="1e-2",
        tick_size="1e-2",
    )
    inputs = {
        "position_contracts_before": Decimal("-1234567.89"),
        "closing_contracts": Decimal("1234567.89"),
        "average_entry_price": Decimal("2"),
        "exit_price": Decimal("1"),
    }

    with localcontext() as context:
        context.prec = 7
        context.rounding = ROUND_DOWN
        low = realized_pnl(contract, **inputs)
    with localcontext() as context:
        context.prec = 60
        context.rounding = ROUND_UP
        high = realized_pnl(contract, **inputs)

    assert low == high == Decimal("1234567.89")


def test_accounting_is_independent_of_ambient_decimal_context() -> None:
    contract = linear_contract()
    risk_tier = tier(liquidation_fee_rate="0")
    fees = fee_schedule(taker="0.0005")

    with localcontext() as context:
        context.prec = 7
        context.rounding = ROUND_DOWN
        low = raw_liquidation_price(
            contract,
            position_contracts=Decimal("1"),
            average_entry_price=Decimal("50000"),
            isolated_balance_usdt=Decimal("50"),
            tier=risk_tier,
            fee_schedule=fees,
        )
    with localcontext() as context:
        context.prec = 60
        context.rounding = ROUND_UP
        high = raw_liquidation_price(
            contract,
            position_contracts=Decimal("1"),
            average_entry_price=Decimal("50000"),
            isolated_balance_usdt=Decimal("50"),
            tier=risk_tier,
            fee_schedule=fees,
        )

    assert low == high
