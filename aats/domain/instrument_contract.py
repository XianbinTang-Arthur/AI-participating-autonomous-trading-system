"""Validated instrument contract arithmetic shared by execution and research.

The exchange quantity for derivatives is a number of contracts.  It must not be
treated as a base-asset quantity until the contract definition has been
validated.  This module deliberately has no database, network, settings, or
runtime-service dependency so that every consumer can use the same Decimal
arithmetic and fail-closed validation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import (
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    ROUND_HALF_EVEN,
    Underflow,
    localcontext,
)
from fractions import Fraction
from typing import Literal

from aats.schemas.exchange import InstrumentMetadata


ContractType = Literal["spot", "linear", "inverse"]
INSTRUMENT_ARITHMETIC_POLICY_ID = "instrument-arithmetic/v1"
_DERIVATIVE_INSTRUMENT_TYPES = frozenset({"SWAP", "FUTURES"})
_SPOT_QUANTITY_INSTRUMENT_TYPES = frozenset({"SPOT", "MARGIN"})
_DISCRETE_DECIMAL_COMPLEXITY_LIMIT = 10_000
_ARITHMETIC_CONTEXT = Context(
    prec=50,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
)
for _signal in (InvalidOperation, DivisionByZero, Overflow, Underflow):
    _ARITHMETIC_CONTEXT.traps[_signal] = True


class InstrumentContractError(ValueError):
    """Stable failure for missing or contradictory financial-unit metadata."""


@dataclass(frozen=True)
class InstrumentContract:
    """One validated contract definition and its deterministic arithmetic.

    ``exchange_quantity`` means base quantity for spot and contract count for
    derivatives.  Linear PnL is returned in quote/settlement currency; inverse
    PnL is returned in base/settlement currency.
    """

    symbol: str
    instrument_type: str
    contract_type: ContractType
    base_currency: str
    quote_currency: str
    settle_currency: str
    contract_value: Decimal
    contract_multiplier: Decimal
    contract_value_currency: str
    lot_size: Decimal
    min_size: Decimal
    tick_size: Decimal

    def __post_init__(self) -> None:
        symbol = str(self.symbol or "").strip().upper()
        instrument_type = str(self.instrument_type or "").strip().upper()
        base_currency = _currency(self.base_currency, "base_currency")
        quote_currency = _currency(self.quote_currency, "quote_currency")
        settle_currency = _currency(self.settle_currency, "settle_currency")
        value_currency = _currency(
            self.contract_value_currency,
            "contract_value_currency",
        )
        contract_value = _positive_decimal(self.contract_value, "contract_value")
        contract_multiplier = _positive_decimal(
            self.contract_multiplier,
            "contract_multiplier",
        )
        lot_size = _positive_decimal(self.lot_size, "lot_size")
        min_size = _positive_decimal(self.min_size, "min_size")
        tick_size = _positive_decimal(self.tick_size, "tick_size")

        if not symbol:
            raise InstrumentContractError("instrument_symbol_required")
        symbol_parts = symbol.split("-")
        if (
            len(symbol_parts) < 2
            or symbol_parts[0] != base_currency
            or symbol_parts[1] != quote_currency
        ):
            raise InstrumentContractError("instrument_identity_mismatch")
        if self.contract_type == "spot":
            if instrument_type not in _SPOT_QUANTITY_INSTRUMENT_TYPES:
                raise InstrumentContractError("contract_type_unknown_or_inconsistent")
            if len(symbol_parts) != 2:
                raise InstrumentContractError("instrument_identity_mismatch")
            if settle_currency != quote_currency or value_currency != base_currency:
                raise InstrumentContractError("contract_value_currency_inconsistent")
            if contract_value != Decimal("1") or contract_multiplier != Decimal("1"):
                raise InstrumentContractError("spot_contract_face_value_must_be_one")
        elif self.contract_type == "linear":
            if instrument_type not in _DERIVATIVE_INSTRUMENT_TYPES:
                raise InstrumentContractError("contract_type_unknown_or_inconsistent")
            if (
                (symbol.endswith("-SWAP") and instrument_type != "SWAP")
                or (instrument_type == "SWAP" and not symbol.endswith("-SWAP"))
                or (instrument_type == "FUTURES" and len(symbol_parts) < 3)
            ):
                raise InstrumentContractError("instrument_identity_mismatch")
            if value_currency != base_currency or settle_currency != quote_currency:
                raise InstrumentContractError("contract_value_currency_inconsistent")
        elif self.contract_type == "inverse":
            if instrument_type not in _DERIVATIVE_INSTRUMENT_TYPES:
                raise InstrumentContractError("contract_type_unknown_or_inconsistent")
            if (
                (symbol.endswith("-SWAP") and instrument_type != "SWAP")
                or (instrument_type == "SWAP" and not symbol.endswith("-SWAP"))
                or (instrument_type == "FUTURES" and len(symbol_parts) < 3)
            ):
                raise InstrumentContractError("instrument_identity_mismatch")
            if value_currency != quote_currency or settle_currency != base_currency:
                raise InstrumentContractError("contract_value_currency_inconsistent")
        else:  # pragma: no cover - Literal is enforced by callers and type checkers
            raise InstrumentContractError("contract_type_unknown_or_inconsistent")

        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "instrument_type", instrument_type)
        object.__setattr__(self, "base_currency", base_currency)
        object.__setattr__(self, "quote_currency", quote_currency)
        object.__setattr__(self, "settle_currency", settle_currency)
        object.__setattr__(self, "contract_value_currency", value_currency)
        object.__setattr__(self, "contract_value", contract_value)
        object.__setattr__(self, "contract_multiplier", contract_multiplier)
        object.__setattr__(self, "lot_size", lot_size)
        object.__setattr__(self, "min_size", min_size)
        object.__setattr__(self, "tick_size", tick_size)

    @property
    def face_value(self) -> Decimal:
        """Value represented by one derivative contract."""

        return _arithmetic_result(
            lambda: self.contract_value * self.contract_multiplier,
            "contract_face_value_invalid",
            require_positive=True,
        )

    @property
    def settlement_pnl_currency(self) -> str:
        return self.settle_currency

    @property
    def fingerprint(self) -> str:
        """Stable identity for the exact contract and arithmetic policy."""

        fields = (
            INSTRUMENT_ARITHMETIC_POLICY_ID,
            self.symbol,
            self.instrument_type,
            self.contract_type,
            self.base_currency,
            self.quote_currency,
            self.settle_currency,
            canonical_decimal_identity(self.contract_value),
            canonical_decimal_identity(self.contract_multiplier),
            self.contract_value_currency,
            canonical_decimal_identity(self.lot_size),
            canonical_decimal_identity(self.min_size),
            canonical_decimal_identity(self.tick_size),
        )
        return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()

    def base_quantity(
        self,
        exchange_quantity: Decimal,
        *,
        reference_price: Decimal | None = None,
    ) -> Decimal:
        """Convert exchange units to signed base-asset exposure."""

        quantity = _finite_decimal(exchange_quantity, "exchange_quantity")
        if self.contract_type == "spot":
            return quantity
        if self.contract_type == "linear":
            return _arithmetic_result(
                lambda: quantity * self.face_value,
                "contract_arithmetic_invalid",
            )
        price = _required_price(reference_price)
        return _arithmetic_result(
            lambda: quantity * self.face_value / price,
            "contract_arithmetic_invalid",
        )

    def exchange_quantity(
        self,
        base_quantity: Decimal,
        *,
        reference_price: Decimal | None = None,
    ) -> Decimal:
        """Convert signed base-asset exposure to exchange units."""

        quantity = _finite_decimal(base_quantity, "base_quantity")
        if self.contract_type == "spot":
            return quantity
        if self.contract_type == "linear":
            return _arithmetic_result(
                lambda: quantity / self.face_value,
                "contract_arithmetic_invalid",
            )
        price = _required_price(reference_price)
        return _arithmetic_result(
            lambda: quantity * price / self.face_value,
            "contract_arithmetic_invalid",
        )

    def quote_notional(
        self,
        exchange_quantity: Decimal,
        *,
        price: Decimal,
    ) -> Decimal:
        """Return absolute quote-currency notional at ``price``."""

        quantity = _finite_decimal(exchange_quantity, "exchange_quantity").copy_abs()
        resolved_price = _required_price(price)
        if self.contract_type == "spot":
            return _arithmetic_result(
                lambda: quantity * resolved_price,
                "contract_arithmetic_invalid",
            )
        if self.contract_type == "linear":
            return _arithmetic_result(
                lambda: quantity * self.face_value * resolved_price,
                "contract_arithmetic_invalid",
            )
        return _arithmetic_result(
            lambda: quantity * self.face_value,
            "contract_arithmetic_invalid",
        )

    def settlement_notional(
        self,
        exchange_quantity: Decimal,
        *,
        price: Decimal,
    ) -> Decimal:
        """Return absolute fee basis in settlement currency.

        Spot and linear contracts settle in quote currency, so their fee basis
        is the quote notional.  Inverse contracts settle in base currency and
        therefore use ``contracts * face / price``.  Keeping this conversion
        beside :meth:`settlement_pnl` prevents replay consumers from mixing a
        quote-currency fee with a base-currency inverse PnL.
        """

        quantity = _finite_decimal(exchange_quantity, "exchange_quantity").copy_abs()
        resolved_price = _required_price(price)
        if self.contract_type in {"spot", "linear"}:
            return self.quote_notional(quantity, price=resolved_price)
        return _arithmetic_result(
            lambda: quantity * self.face_value / resolved_price,
            "contract_arithmetic_invalid",
        )

    def settlement_fee(
        self,
        exchange_quantity: Decimal,
        *,
        price: Decimal,
        fee_bps: Decimal,
    ) -> Decimal:
        """Return signed fee amount in :attr:`settle_currency`.

        Positive basis points represent a cost and negative basis points a
        rebate.  The sign is deliberately retained so equity can subtract the
        returned value once for both cases.
        """

        rate = _finite_decimal(fee_bps, "fee_bps")
        return _arithmetic_result(
            lambda: self.settlement_notional(
                exchange_quantity,
                price=price,
            )
            * rate
            / Decimal("10000"),
            "contract_arithmetic_invalid",
        )

    def fee_asset_amount(
        self,
        exchange_quantity: Decimal,
        *,
        price: Decimal,
        fee_bps: Decimal,
        fee_asset: str,
    ) -> Decimal:
        """Return the signed fee amount in an explicitly declared asset.

        Replay accounting reports PnL in settlement currency, but a spot venue
        may debit a buy fee from the acquired base asset.  Keeping the charged
        asset separate from its settlement-currency valuation prevents that
        inventory debit from being silently lost.  No venue default is inferred
        here: callers must provide either the contract settlement currency or,
        for spot only, its base currency.
        """

        currency = _currency(fee_asset, "fee_asset")
        rate = _finite_decimal(fee_bps, "fee_bps")
        if currency == self.settle_currency:
            return self.settlement_fee(
                exchange_quantity,
                price=price,
                fee_bps=rate,
            )
        if self.contract_type == "spot" and currency == self.base_currency:
            quantity = _finite_decimal(
                exchange_quantity,
                "exchange_quantity",
            ).copy_abs()
            return _arithmetic_result(
                lambda: quantity * rate / Decimal("10000"),
                "contract_arithmetic_invalid",
            )
        raise InstrumentContractError("fee_asset_unsupported")

    def fee_settlement_value(
        self,
        fee_asset_quantity: Decimal,
        *,
        fee_asset: str,
        price: Decimal,
    ) -> Decimal:
        """Value a signed fee amount in settlement currency.

        This is the accounting bridge between actual fee-asset inventory and
        the single-currency replay equity series.  Unsupported assets fail
        closed rather than being assigned an implicit FX rate.
        """

        amount = _finite_decimal(fee_asset_quantity, "fee_asset_quantity")
        currency = _currency(fee_asset, "fee_asset")
        if currency == self.settle_currency:
            return amount
        if self.contract_type == "spot" and currency == self.base_currency:
            resolved_price = _required_price(price)
            return _arithmetic_result(
                lambda: amount * resolved_price,
                "contract_arithmetic_invalid",
            )
        raise InstrumentContractError("fee_asset_unsupported")

    def execution_price(
        self,
        reference_price: Decimal,
        *,
        side: Literal["buy", "sell"],
        slippage_bps: Decimal = Decimal("0"),
    ) -> Decimal:
        """Return a deterministic, conservatively tick-aligned fill price.

        Positive slippage worsens both sides: buys round upward and sells round
        downward to the exchange tick.  Keeping tick alignment inside the
        contract boundary prevents the replay caller's ambient Decimal context
        from changing an otherwise identical fill.
        """

        price = _required_price(reference_price)
        bps = _finite_decimal(slippage_bps, "slippage_bps")
        if bps < 0 or bps >= Decimal("10000"):
            raise InstrumentContractError("slippage_bps_invalid")
        if side not in {"buy", "sell"}:
            raise InstrumentContractError("execution_side_invalid")
        price_fraction = _discrete_fraction(price)
        bps_fraction = _discrete_fraction(bps)
        target = price_fraction * (
            Fraction(1)
            + (bps_fraction if side == "buy" else -bps_fraction)
            / Fraction(10_000)
        )
        if target <= 0:
            raise InstrumentContractError("contract_arithmetic_invalid")
        tick_ratio = target / _discrete_fraction(self.tick_size)
        if side == "buy":
            tick_count = -(-tick_ratio.numerator // tick_ratio.denominator)
        else:
            tick_count = tick_ratio.numerator // tick_ratio.denominator
        aligned = _integer_step_decimal(tick_count, self.tick_size)
        if aligned <= 0:
            raise InstrumentContractError("contract_arithmetic_invalid")
        return aligned

    def validate_exchange_price(self, price: Decimal) -> Decimal:
        """Return a positive tick-aligned exchange price or fail."""

        resolved = _required_price(price)
        ratio = _discrete_fraction(resolved) / _discrete_fraction(
            self.tick_size
        )
        if ratio.denominator != 1:
            raise InstrumentContractError("exchange_price_tick_misaligned")
        return resolved

    def fillable_exchange_quantity(
        self,
        requested_quantity: Decimal,
        *,
        available_quantity: Decimal,
        max_participation: Decimal,
    ) -> Decimal:
        """Return a lot-aligned fill quantity, or zero below ``min_size``."""

        requested = _positive_decimal(requested_quantity, "requested_quantity")
        available = _positive_decimal(available_quantity, "available_quantity")
        participation = _positive_decimal(max_participation, "max_participation")
        if participation > Decimal("1"):
            raise InstrumentContractError("max_participation_invalid")
        cap = _discrete_fraction(available) * _discrete_fraction(participation)
        candidate = min(_discrete_fraction(requested), cap)
        lot = _discrete_fraction(self.lot_size)
        lot_count = candidate.numerator * lot.denominator // (
            candidate.denominator * lot.numerator
        )
        aligned = _integer_step_decimal(lot_count, self.lot_size)
        return Decimal("0") if aligned < self.min_size else aligned

    def validate_exchange_quantity(self, quantity: Decimal) -> Decimal:
        """Return a positive lot/min compliant exchange quantity or fail."""

        resolved = _positive_decimal(quantity, "exchange_quantity")
        if resolved < self.min_size:
            raise InstrumentContractError("exchange_quantity_below_min_size")
        steps = _discrete_fraction(resolved) / _discrete_fraction(self.lot_size)
        if steps.denominator != 1:
            raise InstrumentContractError("exchange_quantity_lot_misaligned")
        return resolved

    def execution_slippage_bps(
        self,
        reference_price: Decimal,
        *,
        execution_price: Decimal,
        side: Literal["buy", "sell"],
    ) -> Decimal:
        """Return realized adverse slippage, including tick rounding, in bps."""

        reference = _required_price(reference_price)
        execution = _required_price(execution_price)
        if side == "buy":
            numerator = _arithmetic_result(
                lambda: execution - reference,
                "contract_arithmetic_invalid",
            )
        elif side == "sell":
            numerator = _arithmetic_result(
                lambda: reference - execution,
                "contract_arithmetic_invalid",
            )
        else:
            raise InstrumentContractError("execution_side_invalid")
        return _arithmetic_result(
            lambda: numerator / reference * Decimal("10000"),
            "contract_arithmetic_invalid",
        )

    def quantity_ratio(
        self,
        numerator: Decimal,
        *,
        denominator: Decimal,
    ) -> Decimal:
        """Divide exchange quantities under the fixed arithmetic policy."""

        top = _finite_decimal(numerator, "exchange_quantity")
        bottom = _positive_decimal(denominator, "exchange_quantity")
        return _arithmetic_result(
            lambda: top / bottom,
            "contract_arithmetic_invalid",
        )

    def settlement_basis_points(
        self,
        numerator: Decimal,
        *,
        denominator: Decimal,
    ) -> Decimal:
        """Return a settlement-currency ratio expressed in basis points."""

        top = _finite_decimal(numerator, "settlement_amount")
        bottom = _positive_decimal(denominator, "settlement_amount")
        return _arithmetic_result(
            lambda: top / bottom * Decimal("10000"),
            "contract_arithmetic_invalid",
        )

    def settlement_pnl(
        self,
        exchange_quantity: Decimal,
        *,
        entry_price: Decimal,
        exit_price: Decimal,
    ) -> Decimal:
        """Return signed PnL in :attr:`settlement_pnl_currency`.

        A positive exchange quantity is long and a negative quantity is short.
        """

        quantity = _finite_decimal(exchange_quantity, "exchange_quantity")
        entry = _required_price(entry_price)
        exit_ = _required_price(exit_price)
        if self.contract_type in {"spot", "linear"}:
            return _arithmetic_result(
                lambda: (
                    quantity
                    if self.contract_type == "spot"
                    else quantity * self.face_value
                )
                * (exit_ - entry),
                "contract_arithmetic_invalid",
            )
        return _arithmetic_result(
            lambda: quantity
            * self.face_value
            * ((Decimal("1") / entry) - (Decimal("1") / exit_)),
            "contract_arithmetic_invalid",
        )

    def combined_entry_price(
        self,
        existing_quantity: Decimal,
        *,
        existing_price: Decimal,
        added_quantity: Decimal,
        added_price: Decimal,
    ) -> Decimal:
        """Return deterministic WAC for two same-direction position lots.

        Inverse contracts require a reciprocal (harmonic) cost basis; spot and
        linear contracts use the usual arithmetic quantity weighting.
        """

        existing = _positive_decimal(existing_quantity, "existing_quantity")
        added = _positive_decimal(added_quantity, "added_quantity")
        old_price = _required_price(existing_price)
        new_price = _required_price(added_price)
        total = _arithmetic_result(
            lambda: existing + added,
            "contract_arithmetic_invalid",
            require_positive=True,
        )
        if self.contract_type == "inverse":
            return _arithmetic_result(
                lambda: total / (existing / old_price + added / new_price),
                "contract_arithmetic_invalid",
                require_positive=True,
            )
        return _arithmetic_result(
            lambda: (old_price * existing + new_price * added) / total,
            "contract_arithmetic_invalid",
            require_positive=True,
        )

    def add_exchange_quantities(self, *values: Decimal) -> Decimal:
        """Add signed exchange quantities exactly within the bounded policy.

        Fixed-precision Decimal addition can round each large operand before a
        near-total cancellation (for example, two 53-digit quantities whose
        mathematical remainder is one).  Exchange inventory must never be
        created by that rounding, so addition uses the same bounded rational
        representation as lot/tick arithmetic and converts the exact result
        back to Decimal without division.
        """

        resolved = tuple(
            _finite_decimal(value, "exchange_quantity") for value in values
        )
        return _exact_decimal_sum(resolved)

    def add_settlement_amounts(self, *values: Decimal) -> Decimal:
        """Add signed settlement amounts without cancellation rounding."""

        resolved = tuple(
            _finite_decimal(value, "settlement_amount") for value in values
        )
        return _exact_decimal_sum(resolved)


def instrument_contract_from_metadata(
    instrument: InstrumentMetadata,
) -> InstrumentContract:
    """Validate exchange metadata and return its single arithmetic contract."""

    instrument_type = str(instrument.instrument_type or "").strip().upper()
    instrument_id = str(instrument.instrument_id or "").strip().upper()
    symbol = str(instrument.symbol or "").strip().upper()
    if not instrument_id or not symbol or instrument_id != symbol:
        raise InstrumentContractError("instrument_identity_mismatch")
    base_currency, quote_currency = _metadata_currencies(instrument, symbol=symbol)
    symbol_parts = [part for part in symbol.split("-") if part]
    derivative_shaped_symbol = symbol.endswith("-SWAP") or len(symbol_parts) >= 3

    if not instrument_type:
        if derivative_shaped_symbol:
            raise InstrumentContractError("derivative_instrument_metadata_required")
        instrument_type = "SPOT"

    if instrument_type not in _DERIVATIVE_INSTRUMENT_TYPES:
        if instrument_type not in _SPOT_QUANTITY_INSTRUMENT_TYPES:
            raise InstrumentContractError("contract_type_unknown_or_inconsistent")
        if derivative_shaped_symbol:
            raise InstrumentContractError("contract_type_unknown_or_inconsistent")
        declared_contract_type = str(instrument.contract_type or "").strip().lower()
        if declared_contract_type and declared_contract_type != "spot":
            raise InstrumentContractError("contract_type_unknown_or_inconsistent")
        return InstrumentContract(
            symbol=symbol,
            instrument_type=instrument_type or "SPOT",
            contract_type="spot",
            base_currency=base_currency,
            quote_currency=quote_currency,
            settle_currency=str(instrument.settle_currency or quote_currency),
            contract_value=Decimal("1"),
            contract_multiplier=Decimal("1"),
            contract_value_currency=base_currency,
            lot_size=instrument.lot_size,
            min_size=instrument.min_size,
            tick_size=instrument.tick_size,
        )

    if not derivative_shaped_symbol:
        raise InstrumentContractError("contract_type_unknown_or_inconsistent")
    if symbol.endswith("-SWAP") and instrument_type != "SWAP":
        raise InstrumentContractError("contract_type_unknown_or_inconsistent")
    if derivative_shaped_symbol and not symbol.endswith("-SWAP") and instrument_type != "FUTURES":
        raise InstrumentContractError("contract_type_unknown_or_inconsistent")

    settle_currency = str(instrument.settle_currency or "").strip().upper()
    value_currency = str(instrument.contract_value_currency or "").strip().upper()
    if (
        not settle_currency
        or not value_currency
        or instrument.contract_value is None
        or instrument.contract_multiplier is None
    ):
        raise InstrumentContractError("derivative_instrument_metadata_required")
    contract_type = str(instrument.contract_type or "").strip().lower()
    if contract_type not in {"linear", "inverse"}:
        raise InstrumentContractError("contract_type_unknown_or_inconsistent")

    return InstrumentContract(
        symbol=symbol,
        instrument_type=instrument_type,
        contract_type=contract_type,  # type: ignore[arg-type]
        base_currency=base_currency,
        quote_currency=quote_currency,
        settle_currency=settle_currency,
        contract_value=instrument.contract_value,
        contract_multiplier=instrument.contract_multiplier,
        contract_value_currency=value_currency,
        lot_size=instrument.lot_size,
        min_size=instrument.min_size,
        tick_size=instrument.tick_size,
    )


def _metadata_currencies(
    instrument: InstrumentMetadata,
    *,
    symbol: str,
) -> tuple[str, str]:
    base_currency = str(instrument.base_currency or "").strip().upper()
    quote_currency = str(instrument.quote_currency or "").strip().upper()
    parts = [part for part in symbol.split("-") if part]
    if not base_currency and parts:
        base_currency = parts[0]
    if not quote_currency and len(parts) >= 2:
        quote_currency = parts[1]
    if len(parts) >= 2 and (
        base_currency != parts[0] or quote_currency != parts[1]
    ):
        raise InstrumentContractError("instrument_currency_identity_mismatch")
    underlying = str(instrument.underlying or "").strip().upper()
    if underlying:
        underlying_parts = [part for part in underlying.split("-") if part]
        if len(underlying_parts) < 2 or (
            underlying_parts[0] != base_currency
            or underlying_parts[1] != quote_currency
        ):
            raise InstrumentContractError("instrument_currency_identity_mismatch")
    return (
        _currency(base_currency, "base_currency"),
        _currency(quote_currency, "quote_currency"),
    )


def _currency(value: str, field_name: str) -> str:
    currency = str(value or "").strip().upper()
    if not currency:
        raise InstrumentContractError(f"{field_name}_required")
    return currency


def _finite_decimal(value: Decimal, field_name: str) -> Decimal:
    try:
        resolved = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InstrumentContractError(f"{field_name}_invalid") from exc
    if not resolved.is_finite():
        raise InstrumentContractError(f"{field_name}_invalid")
    return resolved


def canonical_decimal_identity(value: Decimal) -> str:
    """Canonical scientific identity without ambient context or zero padding."""

    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    trimmed = list(digits)
    while trimmed and trimmed[-1] == 0:
        trimmed.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in trimmed)
    prefix = "-" if sign else ""
    return f"{prefix}{coefficient}e{exponent}"


def _positive_decimal(value: Decimal, field_name: str) -> Decimal:
    resolved = _finite_decimal(value, field_name)
    if resolved <= 0:
        if field_name in {"contract_value", "contract_multiplier"}:
            raise InstrumentContractError("contract_face_value_invalid")
        raise InstrumentContractError(f"{field_name}_invalid")
    return resolved


def _required_price(value: Decimal | None) -> Decimal:
    if value is None:
        raise InstrumentContractError("inverse_quantity_requires_reference_price")
    resolved = _finite_decimal(value, "reference_price")
    if resolved <= 0:
        raise InstrumentContractError("reference_price_invalid")
    return resolved


def _discrete_fraction(value: Decimal) -> Fraction:
    """Return an exact rational for lot/tick decisions with a DoS bound.

    Fixed-precision Decimal division is unsuitable for exchange discreteness:
    rounding before floor/integrality checks can create quantity or price.  A
    bounded exact rational keeps >50-digit inputs correct while rejecting
    pathological exponents before Python materializes enormous integers.
    """

    resolved = _finite_decimal(value, "discrete_decimal")
    _sign, digits, exponent = resolved.as_tuple()
    if len(digits) + abs(exponent) > _DISCRETE_DECIMAL_COMPLEXITY_LIMIT:
        raise InstrumentContractError("contract_discrete_arithmetic_too_large")
    return Fraction(resolved)


def _integer_step_decimal(step_count: int, step: Decimal) -> Decimal:
    """Multiply a non-negative integer by a positive Decimal exactly."""

    if step_count < 0:
        raise InstrumentContractError("contract_arithmetic_invalid")
    if step_count == 0:
        return Decimal("0")
    _sign, digits, exponent = step.as_tuple()
    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    product_digits = Decimal(step_count * coefficient).as_tuple().digits
    canonical_digits = list(product_digits)
    while len(canonical_digits) > 1 and canonical_digits[-1] == 0:
        canonical_digits.pop()
        exponent += 1
    if (
        len(canonical_digits) + abs(exponent)
        > _DISCRETE_DECIMAL_COMPLEXITY_LIMIT
    ):
        raise InstrumentContractError("contract_discrete_arithmetic_too_large")
    return Decimal((0, tuple(canonical_digits), exponent))


def _exact_decimal_sum(values: tuple[Decimal, ...]) -> Decimal:
    """Return an exact, complexity-bounded sum of finite Decimal values."""

    total = sum((_discrete_fraction(value) for value in values), Fraction(0))
    if total == 0:
        return Decimal("0")

    denominator = total.denominator
    powers_of_two = 0
    powers_of_five = 0
    while denominator % 2 == 0:
        denominator //= 2
        powers_of_two += 1
    while denominator % 5 == 0:
        denominator //= 5
        powers_of_five += 1
    if denominator != 1:  # Decimal inputs make this unreachable by contract.
        raise InstrumentContractError("contract_arithmetic_invalid")

    scale = max(powers_of_two, powers_of_five)
    coefficient = total.numerator
    coefficient *= 2 ** (scale - powers_of_two)
    coefficient *= 5 ** (scale - powers_of_five)
    sign = 1 if coefficient < 0 else 0
    _unused_sign, digits, exponent = Decimal(abs(coefficient)).as_tuple()
    canonical_digits = list(digits)
    exponent -= scale
    while len(canonical_digits) > 1 and canonical_digits[-1] == 0:
        canonical_digits.pop()
        exponent += 1
    if (
        len(canonical_digits) + abs(exponent)
        > _DISCRETE_DECIMAL_COMPLEXITY_LIMIT
    ):
        raise InstrumentContractError("contract_discrete_arithmetic_too_large")
    return Decimal((sign, tuple(canonical_digits), exponent))


def _arithmetic_result(
    operation,
    reason: str,
    *,
    require_positive: bool = False,
) -> Decimal:
    try:
        with localcontext(_ARITHMETIC_CONTEXT):
            result = operation()
    except DecimalException as exc:
        raise InstrumentContractError(reason) from exc
    resolved = _finite_decimal(result, reason)
    if require_positive and resolved <= 0:
        raise InstrumentContractError(reason)
    return resolved


def instrument_arithmetic_context():
    """Return the fixed Decimal context manager used by contract arithmetic."""

    return localcontext(_ARITHMETIC_CONTEXT)


__all__ = [
    "ContractType",
    "INSTRUMENT_ARITHMETIC_POLICY_ID",
    "InstrumentContract",
    "InstrumentContractError",
    "canonical_decimal_identity",
    "instrument_arithmetic_context",
    "instrument_contract_from_metadata",
]
