from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from aats.schemas.exchange import ExchangePosition
from aats.schemas.portfolio import InstrumentPositionState, Position, PositionLegState
from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal
from aats.services.portfolio_service.position_keys import (
    build_position_key,
    exposure_side_from_quantity,
    normalize_position_mode,
    normalize_position_side,
    position_key_for_snapshot_position,
    signed_quantity_for_position_side,
)


def position_leg_state_from_snapshot_position(position: Position) -> PositionLegState:
    return PositionLegState(
        symbol=position.symbol,
        position_key=position_key_for_snapshot_position(position),
        position_qty=to_decimal(position.position_qty),
        position_notional=to_decimal(position.position_notional),
        avg_entry_price=to_decimal(position.avg_entry_price),
        unrealized_pnl=to_decimal(position.unrealized_pnl),
        product_type=position.product_type,
        exposure_side=position.exposure_side or exposure_side_from_quantity(position.position_qty),
        target_leverage=position.target_leverage,
        margin_mode=position.margin_mode,
        position_mode=position.position_mode,
        pos_side=position.pos_side,
        instrument_family=position.instrument_family,
        settle_currency=position.settle_currency,
        margin_allocated=to_decimal(position.margin_allocated),
        maintenance_margin=to_decimal(position.maintenance_margin),
        margin_ratio=None if position.margin_ratio in {None, ""} else to_decimal(position.margin_ratio),
        liquidation_price=None if position.liquidation_price in {None, ""} else to_decimal(position.liquidation_price),
        margin_source=position.margin_source,
    )


def position_leg_state_from_exchange_position(
    position: ExchangePosition,
    *,
    position_mode: object | None = None,
    product_type: str = "derivatives",
) -> PositionLegState:
    normalized_mode = normalize_position_mode(position_mode)
    normalized_side = normalize_position_side(getattr(position, "side", None), position_mode=normalized_mode)
    quantity = signed_quantity_for_position_side(
        position.quantity,
        pos_side=getattr(position, "side", None),
        position_mode=normalized_mode,
    )
    notional = _signed_notional_for_exchange_position(position=position, signed_quantity=quantity)
    position_key = build_position_key(
        symbol=position.symbol,
        product_type=product_type,
        margin_mode=getattr(position, "margin_mode", None),
        position_mode=normalized_mode,
        pos_side=getattr(position, "side", None),
    )
    return PositionLegState(
        symbol=position.symbol,
        position_key=position_key,
        position_qty=quantity,
        position_notional=notional,
        avg_entry_price=to_decimal(getattr(position, "average_entry_price", None) or 0),
        unrealized_pnl=to_decimal(getattr(position, "unrealized_pnl", None) or 0),
        product_type=product_type,  # type: ignore[arg-type]
        exposure_side=exposure_side_from_quantity(quantity),
        target_leverage=float(getattr(position, "leverage", None) or 1.0),
        margin_mode=getattr(position, "margin_mode", None) or "cash",  # type: ignore[arg-type]
        position_mode=normalized_mode,  # type: ignore[arg-type]
        pos_side=normalized_side,  # type: ignore[arg-type]
        instrument_family=getattr(position, "instrument_family", None),
        settle_currency=getattr(position, "settle_currency", None),
        margin_allocated=to_decimal(getattr(position, "margin_allocated", None) or 0),
        maintenance_margin=to_decimal(getattr(position, "maintenance_margin", None) or 0),
        margin_ratio=None if getattr(position, "margin_ratio", None) in {None, ""} else to_decimal(position.margin_ratio),
        liquidation_price=(
            None
            if getattr(position, "liquidation_price", None) in {None, ""}
            else to_decimal(position.liquidation_price)
        ),
        margin_source="exchange",
    )


def instrument_position_states_from_snapshot_positions(
    positions: Iterable[Position],
) -> list[InstrumentPositionState]:
    return instrument_position_states_from_legs(
        position_leg_state_from_snapshot_position(position)
        for position in positions
    )


def instrument_position_states_from_exchange_positions(
    positions: Iterable[ExchangePosition],
    *,
    position_mode: object | None = None,
    product_type: str = "derivatives",
) -> list[InstrumentPositionState]:
    return instrument_position_states_from_legs(
        position_leg_state_from_exchange_position(
            position,
            position_mode=position_mode,
            product_type=product_type,
        )
        for position in positions
    )


def spot_balance_position_state(
    *,
    symbol: str,
    quantity: Decimal | float | int | str,
) -> InstrumentPositionState | None:
    resolved_quantity = to_decimal(quantity)
    if abs(resolved_quantity) <= EPSILON_DECIMAL_12:
        return None
    leg = PositionLegState(
        symbol=symbol,
        position_key=symbol,
        position_qty=resolved_quantity,
        product_type="spot",
        exposure_side=exposure_side_from_quantity(resolved_quantity),
        margin_mode="cash",
        margin_source="estimated",
    )
    return instrument_position_state_from_legs(symbol=symbol, legs=[leg])


def instrument_position_state_for_symbol(
    states: Iterable[InstrumentPositionState],
    symbol: str,
) -> InstrumentPositionState | None:
    for state in states:
        if state.symbol == symbol:
            return state
    return None


def instrument_position_states_from_legs(
    legs: Iterable[PositionLegState],
) -> list[InstrumentPositionState]:
    grouped: dict[str, list[PositionLegState]] = {}
    for leg in legs:
        grouped.setdefault(leg.symbol, []).append(leg)
    states = [
        instrument_position_state_from_legs(symbol=symbol, legs=symbol_legs)
        for symbol, symbol_legs in grouped.items()
    ]
    states.sort(key=lambda item: str(item.symbol))
    return states


def instrument_position_state_from_legs(
    *,
    symbol: str,
    legs: list[PositionLegState],
) -> InstrumentPositionState:
    net_qty = Decimal("0")
    gross_qty = Decimal("0")
    long_qty = Decimal("0")
    short_qty = Decimal("0")
    net_notional = Decimal("0")
    gross_notional = Decimal("0")
    long_notional = Decimal("0")
    short_notional = Decimal("0")
    unrealized_pnl = Decimal("0")
    target_leverage = 1.0
    has_long_leg = False
    has_short_leg = False

    for leg in legs:
        quantity = to_decimal(leg.position_qty)
        notional = to_decimal(leg.position_notional)
        net_qty += quantity
        gross_qty += abs(quantity)
        net_notional += notional
        gross_notional += abs(notional)
        unrealized_pnl += to_decimal(leg.unrealized_pnl)
        target_leverage = max(target_leverage, float(leg.target_leverage))

        resolved_side = leg.pos_side or _side_from_leg(leg)
        if resolved_side == "short":
            has_short_leg = True
            short_qty += abs(quantity)
            short_notional += abs(notional)
        elif resolved_side == "long":
            has_long_leg = True
            long_qty += abs(quantity)
            long_notional += abs(notional)
        elif quantity < -EPSILON_DECIMAL_12:
            has_short_leg = True
            short_qty += abs(quantity)
            short_notional += abs(notional)
        elif quantity > EPSILON_DECIMAL_12:
            has_long_leg = True
            long_qty += abs(quantity)
            long_notional += abs(notional)

    first_leg = legs[0] if legs else None
    return InstrumentPositionState(
        symbol=symbol,
        product_type="spot" if first_leg is None else first_leg.product_type,
        margin_mode="cash" if first_leg is None else first_leg.margin_mode,
        position_mode=None if first_leg is None else first_leg.position_mode,
        exposure_side=exposure_side_from_quantity(net_qty),
        leg_count=len(legs),
        has_long_leg=has_long_leg,
        has_short_leg=has_short_leg,
        dual_legged=has_long_leg and has_short_leg,
        net_position_qty=net_qty,
        gross_position_qty=gross_qty,
        long_position_qty=long_qty,
        short_position_qty=short_qty,
        net_position_notional=net_notional,
        gross_position_notional=gross_notional,
        long_position_notional=long_notional,
        short_position_notional=short_notional,
        unrealized_pnl=unrealized_pnl,
        target_leverage=target_leverage,
        legs=list(legs),
    )


def _side_from_leg(leg: PositionLegState) -> str:
    if leg.exposure_side in {"long", "short"}:
        return leg.exposure_side
    return exposure_side_from_quantity(leg.position_qty)


def _signed_notional_for_exchange_position(
    *,
    position: ExchangePosition,
    signed_quantity: Decimal,
) -> Decimal:
    if getattr(position, "notional_usd", None) not in {None, ""}:
        absolute_notional = abs(to_decimal(position.notional_usd))
        if signed_quantity < -EPSILON_DECIMAL_12:
            return -absolute_notional
        if signed_quantity > EPSILON_DECIMAL_12:
            return absolute_notional
        return to_decimal(position.notional_usd)
    average_entry_price = to_decimal(getattr(position, "average_entry_price", None) or 0)
    return signed_quantity * average_entry_price
