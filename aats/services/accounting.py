from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from aats.schemas.execution import FillEvent, OrderObligation
from aats.services.portfolio_service.decimals import to_decimal


class UnsupportedFeeCurrencyError(ValueError):
    pass


def resolve_symbol_currencies(
    symbol: str,
    *,
    instrument_lookup: Callable[[str], object | None] | None = None,
) -> tuple[str | None, str | None]:
    instrument = instrument_lookup(symbol) if instrument_lookup is not None else None
    if instrument is not None:
        base_currency = str(getattr(instrument, "base_currency", "") or "").strip()
        quote_currency = str(getattr(instrument, "quote_currency", "") or "").strip()
        if base_currency and quote_currency:
            return base_currency, quote_currency
    if "-" not in symbol:
        return symbol or None, None
    parts = [part for part in symbol.split("-") if part]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def worst_case_reference_price(
    *,
    reference_price: Decimal | float | int | str | None,
    side: str,
    max_slippage_tolerance_bps: int | None,
) -> Decimal | None:
    decimal_price = to_decimal(reference_price)
    if decimal_price <= 0:
        return None
    if max_slippage_tolerance_bps is None or max_slippage_tolerance_bps <= 0:
        return decimal_price
    slippage_fraction = to_decimal(max_slippage_tolerance_bps) / to_decimal(10_000)
    if side == "buy":
        return decimal_price * (to_decimal(1) + slippage_fraction)
    return max(decimal_price * (to_decimal(1) - slippage_fraction), to_decimal(0))


def spot_buy_quote_requirement(
    *,
    quantity: Decimal | float | int | str,
    reference_price: Decimal | float | int | str | None,
    max_slippage_tolerance_bps: int | None,
    taker_fee_bps: Decimal | float | int | str,
) -> Decimal | None:
    worst_case_price = worst_case_reference_price(
        reference_price=reference_price,
        side="buy",
        max_slippage_tolerance_bps=max_slippage_tolerance_bps,
    )
    if worst_case_price is None or worst_case_price <= Decimal("0"):
        return None
    fee_multiplier = to_decimal(1) + (to_decimal(taker_fee_bps) / to_decimal(10_000))
    return to_decimal(quantity) * worst_case_price * fee_multiplier


def derivatives_initial_margin_requirement(
    *,
    quantity: Decimal | float | int | str,
    reference_price: Decimal | float | int | str | None,
    target_leverage: Decimal | float | int | str,
    max_slippage_tolerance_bps: int | None,
) -> Decimal | None:
    resolved_quantity = to_decimal(quantity)
    worst_case_price = worst_case_reference_price(
        reference_price=reference_price,
        side="buy" if resolved_quantity >= 0 else "sell",
        max_slippage_tolerance_bps=max_slippage_tolerance_bps,
    )
    if worst_case_price is None or worst_case_price <= Decimal("0"):
        return None
    leverage = max(to_decimal(target_leverage), Decimal("1"))
    return abs(resolved_quantity) * worst_case_price / to_decimal(leverage)


def remaining_obligation_amount(obligation: OrderObligation) -> Decimal:
    return max(
        obligation.reserved_amount - obligation.consumed_amount - obligation.released_amount,
        Decimal("0"),
    )


def resolved_fee_currency(
    *,
    fill: FillEvent,
    base_currency: str | None,
    quote_currency: str | None,
) -> str | None:
    if fill.fee_currency:
        return fill.fee_currency
    if fill.venue == "OKX":
        return base_currency if fill.side == "buy" else quote_currency
    return quote_currency


def fill_fee_cost_in_quote(
    fill: FillEvent,
    *,
    base_currency: str | None = None,
    quote_currency: str | None = None,
) -> Decimal:
    return abs(
        fill_fee_delta_in_quote(
            fill,
            base_currency=base_currency,
            quote_currency=quote_currency,
        )
    )


def try_fill_fee_cost_in_quote(
    fill: FillEvent,
    *,
    base_currency: str | None = None,
    quote_currency: str | None = None,
) -> tuple[Decimal | None, UnsupportedFeeCurrencyError | None]:
    fee_delta, error = try_fill_fee_delta_in_quote(
        fill,
        base_currency=base_currency,
        quote_currency=quote_currency,
    )
    if error is not None:
        return None, error
    if fee_delta is None:
        return None, None
    return abs(fee_delta), None


def fill_fee_delta_in_quote(
    fill: FillEvent,
    *,
    base_currency: str | None = None,
    quote_currency: str | None = None,
) -> Decimal:
    resolved_base = base_currency
    resolved_quote = quote_currency
    if resolved_base is None and resolved_quote is None:
        resolved_base, resolved_quote = resolve_symbol_currencies(fill.symbol)
    fee_amount = to_decimal(fill.fee_amount)
    if fee_amount == 0:
        return Decimal("0")
    fee_currency = resolved_fee_currency(
        fill=fill,
        base_currency=resolved_base,
        quote_currency=resolved_quote,
    )
    if fee_currency == resolved_quote and fee_currency is not None:
        return fee_amount
    if fee_currency == resolved_base:
        return fee_amount * to_decimal(fill.fill_price)
    raise UnsupportedFeeCurrencyError(
        "unsupported_fill_fee_currency:"
        f"{fee_currency or 'UNRESOLVED'}:"
        f"{fill.symbol}:"
        f"{resolved_base or 'UNKNOWN'}:"
        f"{resolved_quote or 'UNKNOWN'}"
    )


def try_fill_fee_delta_in_quote(
    fill: FillEvent,
    *,
    base_currency: str | None = None,
    quote_currency: str | None = None,
) -> tuple[Decimal | None, UnsupportedFeeCurrencyError | None]:
    try:
        return (
            fill_fee_delta_in_quote(
                fill,
                base_currency=base_currency,
                quote_currency=quote_currency,
            ),
            None,
        )
    except UnsupportedFeeCurrencyError as exc:
        return None, exc


def unsupported_fee_currency_details(
    fill: FillEvent,
    *,
    base_currency: str | None = None,
    quote_currency: str | None = None,
    error: UnsupportedFeeCurrencyError | None = None,
) -> dict[str, Any]:
    resolved_base = base_currency
    resolved_quote = quote_currency
    if resolved_base is None and resolved_quote is None:
        resolved_base, resolved_quote = resolve_symbol_currencies(fill.symbol)
    fee_currency = resolved_fee_currency(
        fill=fill,
        base_currency=resolved_base,
        quote_currency=resolved_quote,
    )
    return {
        "fill_id": fill.fill_id,
        "symbol": fill.symbol,
        "fee_currency": fee_currency,
        "base_currency": resolved_base,
        "quote_currency": resolved_quote,
        "venue": fill.venue,
        "side": fill.side,
        "fill_price": str(fill.fill_price),
        "fee_amount": str(fill.fee_amount),
        "error": None if error is None else str(error),
    }
