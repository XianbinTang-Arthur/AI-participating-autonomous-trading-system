"""Strict domain contracts for the first derivatives backtest slice.

This module deliberately contains no event, persistence, configuration, or
runtime integration.  The v1 scope is a single ``BTC-USDT-SWAP`` linear,
USDT-settled, isolated position and must not be widened in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal

from aats.domain.instrument_contract import (
    InstrumentContract,
    InstrumentContractError,
    canonical_decimal_identity,
)


DERIVATIVES_BACKTEST_ACCOUNTING_POLICY_ID = "derivatives-accounting/v1"
DERIVATIVES_BACKTEST_SCHEMA_VERSION = "derivatives-backtest-run/v1"
DERIVATIVES_BACKTEST_SYMBOL = "BTC-USDT-SWAP"
DERIVATIVES_BACKTEST_FAMILY = "independent"
DERIVATIVES_BACKTEST_TIMEFRAME = "15m"
DERIVATIVES_BACKTEST_FEE_RATE_UNIT = "fraction"
_MAX_CANONICAL_DECIMAL_DIGITS = 256
_MAX_CANONICAL_DECIMAL_EXPONENT = 256
_MAX_CANONICAL_DECIMAL_WIRE_CHARS = 520


class DerivativesBacktestContractError(ValueError):
    """Stable failure for an unsupported or malformed v1 domain value."""

    def __init__(self, code: str, *, field: str | None = None) -> None:
        self.code = code
        self.field = field
        super().__init__(code if field is None else f"{code}:{field}")


def _instrument_contract_error(
    exc: InstrumentContractError,
    *,
    fallback: str,
) -> DerivativesBacktestContractError:
    """Map the shared arithmetic boundary into the LF-B public taxonomy."""

    code = str(exc).strip()
    return DerivativesBacktestContractError(code or fallback)


class MarginModeV1(StrEnum):
    """The only margin mode supported by the first vertical slice."""

    ISOLATED = "isolated"


class PositionModeV1(StrEnum):
    """The only position topology supported by the first vertical slice."""

    SINGLE_POSITION = "single_position"


class PositionSideV1(StrEnum):
    FLAT = "flat"
    LONG = "long"
    SHORT = "short"


class LiquidityRoleV1(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


def require_finite_decimal(value: Decimal, field_name: str) -> Decimal:
    """Return an exact finite ``Decimal`` and reject implicit coercion.

    Exact type checking is intentional.  In particular, ``bool``, ``int`` and
    binary ``float`` must not acquire a capital-accounting identity by being
    silently converted.
    """

    if type(value) is not Decimal:
        raise DerivativesBacktestContractError(
            "economic_decimal_type_invalid",
            field=field_name,
        )
    if not value.is_finite():
        raise DerivativesBacktestContractError(
            "economic_decimal_non_finite",
            field=field_name,
        )
    if value.is_zero():
        return Decimal("0")
    _sign, digits, exponent = value.as_tuple()
    if len(digits) > _MAX_CANONICAL_DECIMAL_WIRE_CHARS:
        raise DerivativesBacktestContractError(
            "economic_decimal_out_of_bounds",
            field=field_name,
        )
    canonical_digits = list(digits)
    while canonical_digits[-1] == 0:
        canonical_digits.pop()
        exponent += 1
    if (
        len(canonical_digits) > _MAX_CANONICAL_DECIMAL_DIGITS
        or abs(exponent) > _MAX_CANONICAL_DECIMAL_EXPONENT
    ):
        raise DerivativesBacktestContractError(
            "economic_decimal_out_of_bounds",
            field=field_name,
        )
    return value


def require_positive_decimal(value: Decimal, field_name: str) -> Decimal:
    resolved = require_finite_decimal(value, field_name)
    if resolved <= 0:
        raise DerivativesBacktestContractError(f"{field_name}_invalid")
    return resolved


def require_non_negative_decimal(value: Decimal, field_name: str) -> Decimal:
    resolved = require_finite_decimal(value, field_name)
    if resolved < 0:
        raise DerivativesBacktestContractError(f"{field_name}_invalid")
    return resolved


def canonical_accounting_decimal(value: Decimal, field_name: str) -> str:
    """Return the repository's canonical Decimal identity after strict input validation."""

    return canonical_decimal_identity(require_finite_decimal(value, field_name))


def parse_canonical_accounting_decimal(value: str, field_name: str) -> Decimal:
    """Parse the exact coefficient/exponent wire identity without coercion."""

    if type(value) is not str:
        raise DerivativesBacktestContractError(
            "economic_decimal_wire_type_invalid",
            field=field_name,
        )
    if (
        not value
        or len(value) > _MAX_CANONICAL_DECIMAL_WIRE_CHARS
        or value != value.strip()
    ):
        raise DerivativesBacktestContractError(
            "economic_decimal_non_canonical",
            field=field_name,
        )
    try:
        resolved = Decimal(value)
    except InvalidOperation as exc:
        raise DerivativesBacktestContractError(
            "economic_decimal_non_canonical",
            field=field_name,
        ) from exc
    if not resolved.is_finite():
        raise DerivativesBacktestContractError(
            "economic_decimal_non_finite",
            field=field_name,
        )
    try:
        resolved = require_finite_decimal(resolved, field_name)
    except DerivativesBacktestContractError:
        raise
    if canonical_decimal_identity(resolved) != value:
        raise DerivativesBacktestContractError(
            "economic_decimal_non_canonical",
            field=field_name,
        )
    return Decimal("0") if resolved.is_zero() else resolved


def _validate_raw_v1_instrument_contract(
    contract: InstrumentContract,
) -> InstrumentContract:
    """Require the exact instrument class supported by LF-B1.1."""

    if type(contract) is not InstrumentContract:
        raise DerivativesBacktestContractError("instrument_contract_invalid")
    expected = {
        "symbol": DERIVATIVES_BACKTEST_SYMBOL,
        "instrument_type": "SWAP",
        "contract_type": "linear",
        "base_currency": "BTC",
        "quote_currency": "USDT",
        "settle_currency": "USDT",
        "contract_value_currency": "BTC",
    }
    if any(getattr(contract, field) != value for field, value in expected.items()):
        raise DerivativesBacktestContractError(
            "derivatives_backtest_instrument_scope_unsupported"
        )
    for field in (
        "contract_value",
        "contract_multiplier",
        "lot_size",
        "min_size",
        "tick_size",
    ):
        require_positive_decimal(getattr(contract, field), f"instrument_{field}")
    try:
        contract.validate_exchange_quantity(contract.min_size)
        contract.validate_exchange_price(contract.tick_size)
    except InstrumentContractError as exc:
        raise DerivativesBacktestContractError(
            "instrument_contract_discrete_rules_invalid"
        ) from exc
    try:
        face_value = contract.face_value
    except InstrumentContractError as exc:
        raise _instrument_contract_error(
            exc,
            fallback="instrument_contract_face_value_invalid",
        ) from exc
    require_positive_decimal(face_value, "instrument_face_value")
    return contract


@dataclass(frozen=True, slots=True, init=False)
class LinearPerpetualContractV1:
    """Strict canonical-wire facade over the shared instrument arithmetic.

    The shared ``InstrumentContract`` intentionally supports trusted Python
    callers and may coerce numeric constructor values.  LF-B cannot accept
    that provenance ambiguity, so its engine-facing contract can only be
    created from canonical Decimal strings and a fixed instrument identity.
    """

    _instrument: InstrumentContract = dataclass_field(repr=False)

    def __init__(
        self,
        *,
        contract_value: str,
        contract_multiplier: str,
        lot_size: str,
        min_size: str,
        tick_size: str,
    ) -> None:
        try:
            instrument = InstrumentContract(
                symbol=DERIVATIVES_BACKTEST_SYMBOL,
                instrument_type="SWAP",
                contract_type="linear",
                base_currency="BTC",
                quote_currency="USDT",
                settle_currency="USDT",
                contract_value=parse_canonical_accounting_decimal(
                    contract_value,
                    "contract_value",
                ),
                contract_multiplier=parse_canonical_accounting_decimal(
                    contract_multiplier,
                    "contract_multiplier",
                ),
                contract_value_currency="BTC",
                lot_size=parse_canonical_accounting_decimal(lot_size, "lot_size"),
                min_size=parse_canonical_accounting_decimal(min_size, "min_size"),
                tick_size=parse_canonical_accounting_decimal(tick_size, "tick_size"),
            )
        except InstrumentContractError as exc:
            raise _instrument_contract_error(
                exc,
                fallback="instrument_contract_invalid",
            ) from exc
        _validate_raw_v1_instrument_contract(instrument)
        object.__setattr__(self, "_instrument", instrument)

    @property
    def fingerprint(self) -> str:
        return self._instrument.fingerprint

    @property
    def face_value(self) -> Decimal:
        try:
            value = self._instrument.face_value
        except InstrumentContractError as exc:
            raise _instrument_contract_error(
                exc,
                fallback="contract_face_value_invalid",
            ) from exc
        return require_positive_decimal(value, "contract_face_value")

    @property
    def min_size(self) -> Decimal:
        return require_positive_decimal(self._instrument.min_size, "min_size")

    def validate_exchange_quantity(self, quantity: Decimal) -> Decimal:
        resolved = require_positive_decimal(quantity, "exchange_quantity")
        try:
            validated = self._instrument.validate_exchange_quantity(resolved)
        except InstrumentContractError as exc:
            raise _instrument_contract_error(
                exc,
                fallback="exchange_quantity_invalid",
            ) from exc
        return require_positive_decimal(validated, "exchange_quantity")

    def validate_exchange_price(self, price: Decimal) -> Decimal:
        resolved = require_positive_decimal(price, "exchange_price")
        try:
            validated = self._instrument.validate_exchange_price(resolved)
        except InstrumentContractError as exc:
            raise _instrument_contract_error(
                exc,
                fallback="exchange_price_invalid",
            ) from exc
        return require_positive_decimal(validated, "exchange_price")

    def base_quantity(self, exchange_quantity: Decimal) -> Decimal:
        resolved = require_finite_decimal(exchange_quantity, "exchange_quantity")
        try:
            result = self._instrument.base_quantity(resolved)
        except InstrumentContractError as exc:
            raise _instrument_contract_error(
                exc,
                fallback="base_quantity_invalid",
            ) from exc
        return require_finite_decimal(result, "base_quantity")

    def quote_notional(
        self,
        exchange_quantity: Decimal,
        *,
        price: Decimal,
    ) -> Decimal:
        quantity = require_finite_decimal(exchange_quantity, "exchange_quantity")
        resolved_price = require_positive_decimal(price, "price")
        try:
            result = self._instrument.quote_notional(quantity, price=resolved_price)
        except InstrumentContractError as exc:
            raise _instrument_contract_error(
                exc,
                fallback="quote_notional_invalid",
            ) from exc
        return require_non_negative_decimal(result, "quote_notional")

    def _settlement_fee_for_rate(
        self,
        exchange_quantity: Decimal,
        *,
        price: Decimal,
        fee_bps: Decimal,
    ) -> Decimal:
        quantity = require_finite_decimal(exchange_quantity, "exchange_quantity")
        resolved_price = require_positive_decimal(price, "price")
        resolved_fee_bps = require_finite_decimal(fee_bps, "fee_bps")
        try:
            result = self._instrument.settlement_fee(
                quantity,
                price=resolved_price,
                fee_bps=resolved_fee_bps,
            )
        except InstrumentContractError as exc:
            raise _instrument_contract_error(
                exc,
                fallback="settlement_fee_invalid",
            ) from exc
        return require_finite_decimal(result, "settlement_fee")

    def settlement_pnl(
        self,
        exchange_quantity: Decimal,
        *,
        entry_price: Decimal,
        exit_price: Decimal,
    ) -> Decimal:
        quantity = require_finite_decimal(exchange_quantity, "exchange_quantity")
        entry = require_positive_decimal(entry_price, "entry_price")
        exit_ = require_positive_decimal(exit_price, "exit_price")
        try:
            result = self._instrument.settlement_pnl(
                quantity,
                entry_price=entry,
                exit_price=exit_,
            )
        except InstrumentContractError as exc:
            raise _instrument_contract_error(
                exc,
                fallback="settlement_pnl_invalid",
            ) from exc
        return require_finite_decimal(result, "settlement_pnl")

    def add_settlement_amounts(self, *values: Decimal) -> Decimal:
        resolved = tuple(
            require_finite_decimal(value, "settlement_amount") for value in values
        )
        try:
            result = self._instrument.add_settlement_amounts(*resolved)
        except InstrumentContractError as exc:
            raise _instrument_contract_error(
                exc,
                fallback="settlement_amount_invalid",
            ) from exc
        return require_finite_decimal(result, "settlement_amount")

    def execution_price(
        self,
        reference_price: Decimal,
        *,
        side: Literal["buy", "sell"],
        slippage_bps: Decimal,
    ) -> Decimal:
        price = require_positive_decimal(reference_price, "reference_price")
        slippage = require_non_negative_decimal(slippage_bps, "slippage_bps")
        if type(side) is not str or side not in {"buy", "sell"}:
            raise DerivativesBacktestContractError("execution_side_invalid")
        try:
            result = self._instrument.execution_price(
                price,
                side=side,
                slippage_bps=slippage,
            )
        except InstrumentContractError as exc:
            raise _instrument_contract_error(
                exc,
                fallback="execution_price_invalid",
            ) from exc
        return require_positive_decimal(result, "execution_price")


def validate_v1_instrument_contract(
    contract: LinearPerpetualContractV1,
) -> LinearPerpetualContractV1:
    if type(contract) is not LinearPerpetualContractV1:
        raise DerivativesBacktestContractError("instrument_contract_invalid")
    _validate_raw_v1_instrument_contract(contract._instrument)
    return contract


@dataclass(frozen=True, slots=True)
class DerivativesBacktestScopeV1:
    """Frozen scope marker; fixed identities are exposed as properties."""

    instrument_contract: LinearPerpetualContractV1

    def __post_init__(self) -> None:
        validate_v1_instrument_contract(self.instrument_contract)

    @property
    def symbol(self) -> str:
        return DERIVATIVES_BACKTEST_SYMBOL

    @property
    def family(self) -> str:
        return DERIVATIVES_BACKTEST_FAMILY

    @property
    def timeframe(self) -> str:
        return DERIVATIVES_BACKTEST_TIMEFRAME

    @property
    def margin_mode(self) -> MarginModeV1:
        return MarginModeV1.ISOLATED

    @property
    def position_mode(self) -> PositionModeV1:
        return PositionModeV1.SINGLE_POSITION


@dataclass(frozen=True, slots=True)
class PositionTierV1:
    """One immutable tier-1 MMR/liquidation-fee schedule entry.

    LF-B1.1 intentionally rejects maintenance deductions and tier changes.
    Those need a later version with independently reviewed exchange semantics.
    """

    tier_id: int
    minimum_notional_inclusive: Decimal
    maximum_notional_inclusive: Decimal
    maximum_leverage: Decimal
    maintenance_margin_rate: Decimal
    maintenance_margin_deduction: Decimal
    liquidation_fee_rate: Decimal

    def __post_init__(self) -> None:
        if type(self.tier_id) is not int or self.tier_id != 1:
            raise DerivativesBacktestContractError("position_tier_out_of_v1_scope")
        lower = require_non_negative_decimal(
            self.minimum_notional_inclusive,
            "minimum_notional_inclusive",
        )
        upper = require_positive_decimal(
            self.maximum_notional_inclusive,
            "maximum_notional_inclusive",
        )
        leverage = require_positive_decimal(
            self.maximum_leverage,
            "maximum_leverage",
        )
        mmr = require_positive_decimal(
            self.maintenance_margin_rate,
            "maintenance_margin_rate",
        )
        deduction = require_finite_decimal(
            self.maintenance_margin_deduction,
            "maintenance_margin_deduction",
        )
        liquidation_fee = require_non_negative_decimal(
            self.liquidation_fee_rate,
            "liquidation_fee_rate",
        )
        if lower != 0 or upper <= lower:
            raise DerivativesBacktestContractError("position_tier_bounds_invalid")
        if leverage < 1:
            raise DerivativesBacktestContractError("maximum_leverage_invalid")
        if mmr >= 1:
            raise DerivativesBacktestContractError("maintenance_margin_rate_invalid")
        if deduction != 0:
            raise DerivativesBacktestContractError(
                "position_tier_deduction_out_of_v1_scope"
            )
        if liquidation_fee >= 1:
            raise DerivativesBacktestContractError("liquidation_fee_rate_invalid")


@dataclass(frozen=True, slots=True)
class ExecutionFeeScheduleV1:
    """Signed USDT fee rates; negative maker fees represent rebates."""

    maker_fee_rate: Decimal
    taker_fee_rate: Decimal
    fee_asset: str

    def __post_init__(self) -> None:
        maker = require_finite_decimal(self.maker_fee_rate, "maker_fee_rate")
        taker = require_finite_decimal(self.taker_fee_rate, "taker_fee_rate")
        if type(self.fee_asset) is not str or self.fee_asset != "USDT":
            raise DerivativesBacktestContractError("fee_asset_out_of_v1_scope")
        if not Decimal("-1") < maker < Decimal("1"):
            raise DerivativesBacktestContractError("maker_fee_rate_invalid")
        if not Decimal("-1") < taker < Decimal("1"):
            raise DerivativesBacktestContractError("taker_fee_rate_invalid")

    def rate_for(self, liquidity_role: LiquidityRoleV1) -> Decimal:
        if type(liquidity_role) is not LiquidityRoleV1:
            raise DerivativesBacktestContractError("liquidity_role_invalid")
        return (
            self.maker_fee_rate
            if liquidity_role is LiquidityRoleV1.MAKER
            else self.taker_fee_rate
        )


@dataclass(frozen=True, slots=True)
class FundingRateScheduleV1:
    """Immutable funding-rate bounds for one effective schedule segment."""

    minimum_rate_inclusive: Decimal
    maximum_rate_inclusive: Decimal

    def __post_init__(self) -> None:
        minimum = require_finite_decimal(
            self.minimum_rate_inclusive,
            "minimum_funding_rate",
        )
        maximum = require_finite_decimal(
            self.maximum_rate_inclusive,
            "maximum_funding_rate",
        )
        if not Decimal("-1") < minimum <= maximum < Decimal("1"):
            raise DerivativesBacktestContractError(
                "funding_rate_schedule_bounds_invalid"
            )

    def validate_rate(self, value: Decimal) -> Decimal:
        rate = require_finite_decimal(value, "funding_rate")
        if not self.minimum_rate_inclusive <= rate <= self.maximum_rate_inclusive:
            raise DerivativesBacktestContractError("funding_rate_out_of_schedule")
        return rate


@dataclass(frozen=True, slots=True)
class OpeningAccountStateV1:
    """Flat carry-in and the only allowed free-cash to isolated transfer."""

    total_account_cash_before_transfer_usdt: Decimal
    isolated_transfer_in_usdt: Decimal

    def __post_init__(self) -> None:
        total_cash = require_non_negative_decimal(
            self.total_account_cash_before_transfer_usdt,
            "total_account_cash_before_transfer_usdt",
        )
        transfer = require_positive_decimal(
            self.isolated_transfer_in_usdt,
            "isolated_transfer_in_usdt",
        )
        if transfer > total_cash:
            raise DerivativesBacktestContractError(
                "isolated_transfer_exceeds_total_account_cash"
            )


@dataclass(frozen=True, slots=True)
class PositionStateV1:
    """One net position; flat state has no average entry price."""

    instrument_contract: LinearPerpetualContractV1
    contracts: Decimal
    average_entry_price: Decimal | None

    def __post_init__(self) -> None:
        contract = validate_v1_instrument_contract(self.instrument_contract)
        contracts = require_finite_decimal(self.contracts, "position_contracts")
        if contracts == 0:
            if self.average_entry_price is not None:
                raise DerivativesBacktestContractError(
                    "flat_position_average_entry_must_be_none"
                )
            object.__setattr__(self, "contracts", Decimal("0"))
            return
        try:
            contract.validate_exchange_quantity(contracts.copy_abs())
        except (InstrumentContractError, DerivativesBacktestContractError) as exc:
            raise DerivativesBacktestContractError(
                "position_contracts_invalid"
            ) from exc
        if self.average_entry_price is None:
            raise DerivativesBacktestContractError(
                "open_position_average_entry_required"
            )
        require_positive_decimal(self.average_entry_price, "average_entry_price")

    @property
    def side(self) -> PositionSideV1:
        if self.contracts > 0:
            return PositionSideV1.LONG
        if self.contracts < 0:
            return PositionSideV1.SHORT
        return PositionSideV1.FLAT


@dataclass(frozen=True, slots=True)
class IsolatedAccountStateV1:
    """Cash compartments and the single net position at one event boundary."""

    free_cash_usdt: Decimal
    isolated_balance_usdt: Decimal
    position: PositionStateV1

    def __post_init__(self) -> None:
        require_non_negative_decimal(self.free_cash_usdt, "free_cash_usdt")
        require_finite_decimal(self.isolated_balance_usdt, "isolated_balance_usdt")
        if type(self.position) is not PositionStateV1:
            raise DerivativesBacktestContractError("position_state_invalid")


__all__ = [
    "DERIVATIVES_BACKTEST_ACCOUNTING_POLICY_ID",
    "DERIVATIVES_BACKTEST_FAMILY",
    "DERIVATIVES_BACKTEST_FEE_RATE_UNIT",
    "DERIVATIVES_BACKTEST_SCHEMA_VERSION",
    "DERIVATIVES_BACKTEST_SYMBOL",
    "DERIVATIVES_BACKTEST_TIMEFRAME",
    "DerivativesBacktestContractError",
    "DerivativesBacktestScopeV1",
    "ExecutionFeeScheduleV1",
    "FundingRateScheduleV1",
    "IsolatedAccountStateV1",
    "LiquidityRoleV1",
    "LinearPerpetualContractV1",
    "MarginModeV1",
    "OpeningAccountStateV1",
    "PositionModeV1",
    "PositionSideV1",
    "PositionStateV1",
    "PositionTierV1",
    "canonical_accounting_decimal",
    "parse_canonical_accounting_decimal",
    "require_finite_decimal",
    "require_non_negative_decimal",
    "require_positive_decimal",
    "validate_v1_instrument_contract",
]
