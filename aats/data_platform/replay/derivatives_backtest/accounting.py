"""Pure Decimal accounting for ``derivatives-backtest-run/v1``.

All public functions are deterministic, reject implicit numeric coercion, and
have no I/O or mutable process state.  Positive fees/funding are costs; negative
values are rebates/credits.  Only the isolated balance can satisfy margin or
liquidation requirements -- free cash is deliberately excluded.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import (
    Decimal,
    DecimalException,
)

from aats.domain.instrument_contract import (
    InstrumentContractError,
    instrument_arithmetic_context,
)

from .contracts import (
    DerivativesBacktestContractError,
    ExecutionFeeScheduleV1,
    FundingRateScheduleV1,
    IsolatedAccountStateV1,
    LinearPerpetualContractV1,
    LiquidityRoleV1,
    OpeningAccountStateV1,
    PositionSideV1,
    PositionStateV1,
    PositionTierV1,
    require_finite_decimal,
    require_positive_decimal,
    validate_v1_instrument_contract,
)


class DerivativesAccountingError(DerivativesBacktestContractError):
    """Stable failure for invalid or unsupported v1 accounting operations."""


def _contract_error(exc: DerivativesBacktestContractError) -> DerivativesAccountingError:
    return DerivativesAccountingError(exc.code, field=exc.field)


def _finite(value: Decimal, field_name: str) -> Decimal:
    try:
        return require_finite_decimal(value, field_name)
    except DerivativesBacktestContractError as exc:
        raise _contract_error(exc) from exc


def _positive(value: Decimal, field_name: str) -> Decimal:
    try:
        return require_positive_decimal(value, field_name)
    except DerivativesBacktestContractError as exc:
        raise _contract_error(exc) from exc


def _require_contract(
    contract: LinearPerpetualContractV1,
) -> LinearPerpetualContractV1:
    try:
        return validate_v1_instrument_contract(contract)
    except DerivativesBacktestContractError as exc:
        raise _contract_error(exc) from exc


def _bounded_result(value: Decimal, failure: str) -> Decimal:
    try:
        return require_finite_decimal(value, failure)
    except DerivativesBacktestContractError as exc:
        raise DerivativesAccountingError(failure) from exc


def _arithmetic(operation: Callable[[], Decimal], failure: str) -> Decimal:
    try:
        with instrument_arithmetic_context():
            result = operation()
    except (DecimalException, InstrumentContractError, OverflowError) as exc:
        raise DerivativesAccountingError(failure) from exc
    return _bounded_result(result, failure)


def _price(value: Decimal, field_name: str) -> Decimal:
    return _positive(value, field_name)


def _signed_contracts(
    contract: LinearPerpetualContractV1,
    value: Decimal,
    field_name: str,
) -> Decimal:
    _require_contract(contract)
    resolved = _finite(value, field_name)
    if resolved == 0:
        return Decimal("0")
    try:
        contract.validate_exchange_quantity(resolved.copy_abs())
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError(f"{field_name}_invalid") from exc
    return resolved


def _fill_price(contract: LinearPerpetualContractV1, value: Decimal) -> Decimal:
    resolved = _price(value, "fill_price")
    try:
        return contract.validate_exchange_price(resolved)
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("fill_price_invalid") from exc


def _fee_rate(value: Decimal, field_name: str = "fee_rate") -> Decimal:
    resolved = _finite(value, field_name)
    if not Decimal("-1") < resolved < Decimal("1"):
        raise DerivativesAccountingError(f"{field_name}_invalid")
    return resolved


def _funding_rate(value: Decimal) -> Decimal:
    resolved = _finite(value, "funding_rate")
    if not Decimal("-1") < resolved < Decimal("1"):
        raise DerivativesAccountingError("funding_rate_invalid")
    return resolved


def _fraction_rate_to_bps(rate: Decimal) -> Decimal:
    """Scale a validated fraction rate exactly, before venue arithmetic."""

    sign, digits, exponent = rate.as_tuple()
    return _bounded_result(
        Decimal((sign, digits, exponent + 4)),
        "fee_rate_conversion_invalid",
    )


def _assert_tier_notional(tier: PositionTierV1, notional: Decimal) -> None:
    if type(tier) is not PositionTierV1:
        raise DerivativesAccountingError("position_tier_invalid")
    resolved = _positive(notional, "position_notional")
    if not (
        tier.minimum_notional_inclusive
        <= resolved
        <= tier.maximum_notional_inclusive
    ):
        raise DerivativesAccountingError("position_tier_out_of_v1_scope")


def _position(
    contract: LinearPerpetualContractV1,
    position_contracts: Decimal,
    average_entry_price: Decimal | None,
) -> PositionStateV1:
    contracts = _signed_contracts(
        contract,
        position_contracts,
        "position_contracts",
    )
    try:
        return PositionStateV1(
            instrument_contract=contract,
            contracts=contracts,
            average_entry_price=average_entry_price,
        )
    except DerivativesBacktestContractError as exc:
        raise _contract_error(exc) from exc


def contract_face_value(contract: LinearPerpetualContractV1) -> Decimal:
    """Return BTC represented by one supported linear contract."""

    _require_contract(contract)
    try:
        return _bounded_result(contract.face_value, "contract_face_value_invalid")
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("contract_face_value_invalid") from exc


def signed_base_exposure(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts: Decimal,
) -> Decimal:
    """Return signed BTC exposure; long is positive and short is negative."""

    contracts = _signed_contracts(
        contract,
        position_contracts,
        "position_contracts",
    )
    try:
        return _bounded_result(
            contract.base_quantity(contracts),
            "base_exposure_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("base_exposure_invalid") from exc


def quote_notional(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts: Decimal,
    price: Decimal,
) -> Decimal:
    """Return absolute USDT notional at a positive mark or fill price."""

    contracts = _signed_contracts(
        contract,
        position_contracts,
        "position_contracts",
    )
    resolved_price = _price(price, "price")
    try:
        return _bounded_result(
            contract.quote_notional(contracts, price=resolved_price),
            "quote_notional_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("quote_notional_invalid") from exc


def _fee_amount_for_rate(
    contract: LinearPerpetualContractV1,
    *,
    filled_contracts: Decimal,
    fill_price: Decimal,
    fee_rate: Decimal,
) -> Decimal:
    """Return signed USDT fee: positive cost, negative maker rebate."""

    contracts = _signed_contracts(
        contract,
        filled_contracts,
        "filled_contracts",
    )
    price = _fill_price(contract, fill_price)
    rate = _fee_rate(fee_rate)
    fee_bps = _fraction_rate_to_bps(rate)
    try:
        return _bounded_result(
            contract._settlement_fee_for_rate(
                contracts,
                price=price,
                fee_bps=fee_bps,
            ),
            "fee_amount_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("fee_amount_invalid") from exc


def fee_amount_for_role(
    contract: LinearPerpetualContractV1,
    *,
    filled_contracts: Decimal,
    fill_price: Decimal,
    fee_schedule: ExecutionFeeScheduleV1,
    liquidity_role: LiquidityRoleV1,
) -> Decimal:
    if type(fee_schedule) is not ExecutionFeeScheduleV1:
        raise DerivativesAccountingError("fee_schedule_invalid")
    try:
        rate = fee_schedule.rate_for(liquidity_role)
    except DerivativesBacktestContractError as exc:
        raise _contract_error(exc) from exc
    return _fee_amount_for_rate(
        contract,
        filled_contracts=filled_contracts,
        fill_price=fill_price,
        fee_rate=rate,
    )


def unrealized_pnl(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts: Decimal,
    average_entry_price: Decimal | None,
    mark_price: Decimal,
) -> Decimal:
    """Return signed USDT mark-to-market PnL for one net position."""

    position = _position(contract, position_contracts, average_entry_price)
    mark = _price(mark_price, "mark_price")
    if position.side is PositionSideV1.FLAT:
        return Decimal("0")
    assert position.average_entry_price is not None
    try:
        return _bounded_result(
            contract.settlement_pnl(
                position.contracts,
                entry_price=position.average_entry_price,
                exit_price=mark,
            ),
            "unrealized_pnl_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("unrealized_pnl_invalid") from exc


def realized_pnl(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts_before: Decimal,
    closing_contracts: Decimal,
    average_entry_price: Decimal,
    exit_price: Decimal,
) -> Decimal:
    """Return PnL for a reducing leg; ``closing_contracts`` is unsigned."""

    position = _position(
        contract,
        position_contracts_before,
        average_entry_price,
    )
    if position.side is PositionSideV1.FLAT:
        raise DerivativesAccountingError("realized_pnl_requires_open_position")
    close_size = _signed_contracts(
        contract,
        closing_contracts,
        "closing_contracts",
    )
    if close_size <= 0 or close_size > position.contracts.copy_abs():
        raise DerivativesAccountingError("closing_contracts_invalid")
    price = _fill_price(contract, exit_price)
    assert position.average_entry_price is not None
    signed_closing = (
        close_size
        if position.side is PositionSideV1.LONG
        else close_size.copy_negate()
    )
    try:
        return _bounded_result(
            contract.settlement_pnl(
                signed_closing,
                entry_price=position.average_entry_price,
                exit_price=price,
            ),
            "realized_pnl_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("realized_pnl_invalid") from exc


def funding_payment(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts_before: Decimal,
    mark_price: Decimal,
    funding_rate: Decimal,
    funding_schedule: FundingRateScheduleV1,
) -> Decimal:
    """Return signed settlement payment: long positive-rate funding is a cost."""

    contracts = _signed_contracts(
        contract,
        position_contracts_before,
        "position_contracts_before",
    )
    mark = _price(mark_price, "mark_price")
    if type(funding_schedule) is not FundingRateScheduleV1:
        raise DerivativesAccountingError("funding_rate_schedule_invalid")
    try:
        rate = funding_schedule.validate_rate(_funding_rate(funding_rate))
    except DerivativesBacktestContractError as exc:
        raise _contract_error(exc) from exc
    exposure = signed_base_exposure(
        contract,
        position_contracts=contracts,
    )
    return _arithmetic(
        lambda: exposure * mark * rate,
        "funding_payment_invalid",
    )


def initial_margin_rate(*, leverage: Decimal, tier: PositionTierV1) -> Decimal:
    if type(tier) is not PositionTierV1:
        raise DerivativesAccountingError("position_tier_invalid")
    resolved = _positive(leverage, "leverage")
    if resolved < 1 or resolved > tier.maximum_leverage:
        raise DerivativesAccountingError("leverage_out_of_tier_scope")
    rate = _arithmetic(lambda: Decimal("1") / resolved, "initial_margin_rate_invalid")
    if rate > 1 or tier.maintenance_margin_rate >= rate:
        raise DerivativesAccountingError("initial_margin_rate_invalid")
    return rate


def initial_margin_required(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts: Decimal,
    mark_price: Decimal,
    leverage: Decimal,
    tier: PositionTierV1,
) -> Decimal:
    contracts = _signed_contracts(
        contract,
        position_contracts,
        "position_contracts",
    )
    mark = _price(mark_price, "mark_price")
    rate = initial_margin_rate(leverage=leverage, tier=tier)
    if contracts == 0:
        return Decimal("0")
    notional = quote_notional(
        contract,
        position_contracts=contracts,
        price=mark,
    )
    _assert_tier_notional(tier, notional)
    return _arithmetic(
        lambda: notional * rate,
        "initial_margin_required_invalid",
    )


def maintenance_margin_required(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts: Decimal,
    mark_price: Decimal,
    tier: PositionTierV1,
) -> Decimal:
    contracts = _signed_contracts(
        contract,
        position_contracts,
        "position_contracts",
    )
    mark = _price(mark_price, "mark_price")
    if type(tier) is not PositionTierV1:
        raise DerivativesAccountingError("position_tier_invalid")
    if contracts == 0:
        return Decimal("0")
    notional = quote_notional(
        contract,
        position_contracts=contracts,
        price=mark,
    )
    _assert_tier_notional(tier, notional)
    amount = _arithmetic(
        lambda: notional * tier.maintenance_margin_rate
        - tier.maintenance_margin_deduction,
        "maintenance_margin_required_invalid",
    )
    return max(Decimal("0"), amount)


def isolated_equity(
    contract: LinearPerpetualContractV1,
    *,
    isolated_balance_usdt: Decimal,
    unrealized_pnl_usdt: Decimal,
) -> Decimal:
    balance = _finite(
        isolated_balance_usdt,
        "isolated_balance_usdt",
    )
    pnl = _finite(unrealized_pnl_usdt, "unrealized_pnl_usdt")
    _require_contract(contract)
    try:
        return _bounded_result(
            contract.add_settlement_amounts(balance, pnl),
            "isolated_equity_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("isolated_equity_invalid") from exc


def account_equity(
    contract: LinearPerpetualContractV1,
    *,
    free_cash_usdt: Decimal,
    isolated_equity_usdt: Decimal,
) -> Decimal:
    free_cash = _finite(free_cash_usdt, "free_cash_usdt")
    if free_cash < 0:
        raise DerivativesAccountingError("free_cash_usdt_invalid")
    isolated = _finite(
        isolated_equity_usdt,
        "isolated_equity_usdt",
    )
    _require_contract(contract)
    try:
        return _bounded_result(
            contract.add_settlement_amounts(free_cash, isolated),
            "account_equity_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("account_equity_invalid") from exc


def net_pnl(
    contract: LinearPerpetualContractV1,
    *,
    account_equity_usdt: Decimal,
    initial_total_capital_usdt: Decimal,
) -> Decimal:
    equity = _finite(account_equity_usdt, "account_equity_usdt")
    initial = _positive(
        initial_total_capital_usdt,
        "initial_total_capital_usdt",
    )
    _require_contract(contract)
    try:
        return _bounded_result(
            contract.add_settlement_amounts(equity, initial.copy_negate()),
            "net_pnl_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("net_pnl_invalid") from exc


def isolated_free_collateral(
    contract: LinearPerpetualContractV1,
    *,
    isolated_equity_usdt: Decimal,
    initial_margin_required_usdt: Decimal,
) -> Decimal:
    equity = _finite(
        isolated_equity_usdt,
        "isolated_equity_usdt",
    )
    margin = _finite(
        initial_margin_required_usdt,
        "initial_margin_required_usdt",
    )
    if margin < 0:
        raise DerivativesAccountingError("initial_margin_required_usdt_invalid")
    _require_contract(contract)
    try:
        return _bounded_result(
            contract.add_settlement_amounts(equity, margin.copy_negate()),
            "isolated_free_collateral_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("isolated_free_collateral_invalid") from exc


def opening_account_state(
    contract: LinearPerpetualContractV1,
    opening: OpeningAccountStateV1,
) -> IsolatedAccountStateV1:
    """Apply the one permitted start transfer to a flat isolated account."""

    _require_contract(contract)
    if type(opening) is not OpeningAccountStateV1:
        raise DerivativesAccountingError("opening_account_state_invalid")
    try:
        free_after = _bounded_result(
            contract.add_settlement_amounts(
                opening.total_account_cash_before_transfer_usdt,
                opening.isolated_transfer_in_usdt.copy_negate(),
            ),
            "isolated_transfer_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("isolated_transfer_invalid") from exc
    return IsolatedAccountStateV1(
        free_cash_usdt=free_after,
        isolated_balance_usdt=opening.isolated_transfer_in_usdt,
        position=PositionStateV1(
            instrument_contract=contract,
            contracts=Decimal("0"),
            average_entry_price=None,
        ),
    )


def forced_close_rate(
    *,
    tier: PositionTierV1,
    fee_schedule: ExecutionFeeScheduleV1,
) -> Decimal:
    if type(tier) is not PositionTierV1:
        raise DerivativesAccountingError("position_tier_invalid")
    if type(fee_schedule) is not ExecutionFeeScheduleV1:
        raise DerivativesAccountingError("fee_schedule_invalid")
    non_negative_taker = max(Decimal("0"), fee_schedule.taker_fee_rate)
    rate = _arithmetic(
        lambda: non_negative_taker + tier.liquidation_fee_rate,
        "forced_close_rate_invalid",
    )
    rho = _arithmetic(
        lambda: tier.maintenance_margin_rate + rate,
        "liquidation_rate_invalid",
    )
    if rho <= 0 or rho >= 1:
        raise DerivativesAccountingError("liquidation_rate_invalid")
    return rate


def forced_close_cost(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts: Decimal,
    mark_price: Decimal,
    tier: PositionTierV1,
    fee_schedule: ExecutionFeeScheduleV1,
) -> Decimal:
    contracts = _signed_contracts(
        contract,
        position_contracts,
        "position_contracts",
    )
    mark = _price(mark_price, "mark_price")
    rate = forced_close_rate(tier=tier, fee_schedule=fee_schedule)
    if contracts == 0:
        return Decimal("0")
    notional = quote_notional(
        contract,
        position_contracts=contracts,
        price=mark,
    )
    _assert_tier_notional(tier, notional)
    return _arithmetic(lambda: notional * rate, "forced_close_cost_invalid")


def liquidation_requirement(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts: Decimal,
    mark_price: Decimal,
    tier: PositionTierV1,
    fee_schedule: ExecutionFeeScheduleV1,
) -> Decimal:
    maintenance = maintenance_margin_required(
        contract,
        position_contracts=position_contracts,
        mark_price=mark_price,
        tier=tier,
    )
    close_cost = forced_close_cost(
        contract,
        position_contracts=position_contracts,
        mark_price=mark_price,
        tier=tier,
        fee_schedule=fee_schedule,
    )
    try:
        return _bounded_result(
            contract.add_settlement_amounts(maintenance, close_cost),
            "liquidation_requirement_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("liquidation_requirement_invalid") from exc


def is_liquidatable(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts: Decimal,
    average_entry_price: Decimal | None,
    mark_price: Decimal,
    isolated_balance_usdt: Decimal,
    tier: PositionTierV1,
    fee_schedule: ExecutionFeeScheduleV1,
) -> bool:
    position = _position(contract, position_contracts, average_entry_price)
    forced_close_rate(tier=tier, fee_schedule=fee_schedule)
    mark = _price(mark_price, "mark_price")
    balance = _finite(
        isolated_balance_usdt,
        "isolated_balance_usdt",
    )
    if position.side is PositionSideV1.FLAT:
        return False
    unrealized = unrealized_pnl(
        contract,
        position_contracts=position.contracts,
        average_entry_price=position.average_entry_price,
        mark_price=mark,
    )
    equity = isolated_equity(
        contract,
        isolated_balance_usdt=balance,
        unrealized_pnl_usdt=unrealized,
    )
    requirement = liquidation_requirement(
        contract,
        position_contracts=position.contracts,
        mark_price=mark,
        tier=tier,
        fee_schedule=fee_schedule,
    )
    return equity <= requirement


def raw_liquidation_price(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts: Decimal,
    average_entry_price: Decimal | None,
    isolated_balance_usdt: Decimal,
    tier: PositionTierV1,
    fee_schedule: ExecutionFeeScheduleV1,
) -> Decimal | None:
    """Solve the v1 isolated liquidation equality before tick alignment."""

    position = _position(contract, position_contracts, average_entry_price)
    close_rate = forced_close_rate(tier=tier, fee_schedule=fee_schedule)
    balance = _finite(
        isolated_balance_usdt,
        "isolated_balance_usdt",
    )
    if position.side is PositionSideV1.FLAT:
        return None
    assert position.average_entry_price is not None
    absolute_exposure = signed_base_exposure(
        contract,
        position_contracts=position.contracts,
    ).copy_abs()
    if absolute_exposure <= 0:
        raise DerivativesAccountingError("liquidation_exposure_invalid")
    rho = _arithmetic(
        lambda: tier.maintenance_margin_rate + close_rate,
        "liquidation_rate_invalid",
    )
    collateral_per_base = _arithmetic(
        lambda: (
            balance + tier.maintenance_margin_deduction
        )
        / absolute_exposure,
        "liquidation_price_invalid",
    )
    if position.side is PositionSideV1.LONG:
        numerator = _arithmetic(
            lambda: position.average_entry_price - collateral_per_base,
            "liquidation_price_invalid",
        )
        if numerator <= 0:
            return None
        price = _arithmetic(
            lambda: numerator / (Decimal("1") - rho),
            "liquidation_price_invalid",
        )
    else:
        numerator = _arithmetic(
            lambda: position.average_entry_price + collateral_per_base,
            "liquidation_price_invalid",
        )
        price = _arithmetic(
            lambda: numerator / (Decimal("1") + rho),
            "liquidation_price_invalid",
        )
    if price <= 0:
        return None
    liquidation_notional = quote_notional(
        contract,
        position_contracts=position.contracts,
        price=price,
    )
    _assert_tier_notional(tier, liquidation_notional)
    return price


def conservative_liquidation_price(
    contract: LinearPerpetualContractV1,
    *,
    position_contracts: Decimal,
    average_entry_price: Decimal | None,
    isolated_balance_usdt: Decimal,
    tier: PositionTierV1,
    fee_schedule: ExecutionFeeScheduleV1,
) -> Decimal | None:
    """Return long thresholds rounded up and short thresholds rounded down."""

    position = _position(contract, position_contracts, average_entry_price)
    raw = raw_liquidation_price(
        contract,
        position_contracts=position.contracts,
        average_entry_price=position.average_entry_price,
        isolated_balance_usdt=isolated_balance_usdt,
        tier=tier,
        fee_schedule=fee_schedule,
    )
    if raw is None:
        return None
    try:
        return _bounded_result(
            contract.execution_price(
                raw,
                side="buy" if position.side is PositionSideV1.LONG else "sell",
                slippage_bps=Decimal("0"),
            ),
            "liquidation_tick_alignment_invalid",
        )
    except (InstrumentContractError, DerivativesBacktestContractError) as exc:
        raise DerivativesAccountingError("liquidation_tick_alignment_invalid") from exc


__all__ = [
    "DerivativesAccountingError",
    "account_equity",
    "conservative_liquidation_price",
    "contract_face_value",
    "fee_amount_for_role",
    "forced_close_cost",
    "forced_close_rate",
    "funding_payment",
    "initial_margin_rate",
    "initial_margin_required",
    "is_liquidatable",
    "isolated_equity",
    "isolated_free_collateral",
    "liquidation_requirement",
    "maintenance_margin_required",
    "net_pnl",
    "opening_account_state",
    "quote_notional",
    "raw_liquidation_price",
    "realized_pnl",
    "signed_base_exposure",
    "unrealized_pnl",
]
