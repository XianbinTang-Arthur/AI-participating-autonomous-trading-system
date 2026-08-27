"""Validated instrument contract arithmetic shared by execution and research.

The exchange quantity for derivatives is a number of contracts.  It must not be
treated as a base-asset quantity until the contract definition has been
validated.  This module deliberately has no database, network, settings, or
runtime-service dependency so that every consumer can use the same Decimal
arithmetic and fail-closed validation.
"""

from __future__ import annotations

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
from typing import Literal

from aats.schemas.exchange import InstrumentMetadata


ContractType = Literal["spot", "linear", "inverse"]
_DERIVATIVE_INSTRUMENT_TYPES = frozenset({"SWAP", "FUTURES"})
_SPOT_QUANTITY_INSTRUMENT_TYPES = frozenset({"SPOT", "MARGIN"})
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
        if self.contract_type == "spot":
            if instrument_type in _DERIVATIVE_INSTRUMENT_TYPES:
                raise InstrumentContractError("contract_type_unknown_or_inconsistent")
            if settle_currency != quote_currency or value_currency != base_currency:
                raise InstrumentContractError("contract_value_currency_inconsistent")
        elif self.contract_type == "linear":
            if instrument_type not in _DERIVATIVE_INSTRUMENT_TYPES:
                raise InstrumentContractError("contract_type_unknown_or_inconsistent")
            if value_currency != base_currency or settle_currency != quote_currency:
                raise InstrumentContractError("contract_value_currency_inconsistent")
        elif self.contract_type == "inverse":
            if instrument_type not in _DERIVATIVE_INSTRUMENT_TYPES:
                raise InstrumentContractError("contract_type_unknown_or_inconsistent")
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


__all__ = [
    "ContractType",
    "InstrumentContract",
    "InstrumentContractError",
    "instrument_contract_from_metadata",
]
