from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from aats.services.portfolio_service.decimals import EPSILON_DECIMAL_12, to_decimal

if TYPE_CHECKING:
    from aats.schemas.exchange import ExchangePosition
    from aats.schemas.execution import FillEvent
    from aats.schemas.portfolio import Position


LONG_SHORT_POSITION_MODE = "long_short_mode"
NET_POSITION_MODE = "net_mode"
_LONG_SHORT_SIDES = {"long", "short"}
_ALL_POSITION_SIDES = {"net", "long", "short"}


def normalize_position_mode(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {LONG_SHORT_POSITION_MODE, NET_POSITION_MODE}:
        return normalized
    return None


def normalize_position_side(value: object, *, position_mode: object | None = None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized not in _ALL_POSITION_SIDES:
        return None
    if normalize_position_mode(position_mode) == LONG_SHORT_POSITION_MODE:
        return normalized if normalized in _LONG_SHORT_SIDES else None
    return normalized


def build_position_key(
    *,
    symbol: str,
    product_type: str,
    margin_mode: object | None = None,
    position_mode: object | None = None,
    pos_side: object | None = None,
) -> str:
    normalized_margin_mode = None if margin_mode is None else str(margin_mode).strip().lower()
    if product_type != "derivatives":
        if normalized_margin_mode in {"cross", "isolated"}:
            return f"{symbol}:spot:{normalized_margin_mode}"
        return symbol
    normalized_mode = normalize_position_mode(position_mode)
    normalized_side = normalize_position_side(pos_side, position_mode=normalized_mode)
    if normalized_mode == LONG_SHORT_POSITION_MODE and normalized_side in _LONG_SHORT_SIDES:
        return f"{symbol}:{normalized_side}"
    return symbol


def position_key_for_fill(fill: "FillEvent") -> str:
    return build_position_key(
        symbol=fill.symbol,
        product_type=fill.product_type,
        margin_mode=getattr(fill, "margin_mode", None),
        position_mode=fill.position_mode,
        pos_side=fill.pos_side,
    )


def position_key_for_snapshot_position(position: "Position") -> str:
    if getattr(position, "position_key", None):
        return str(position.position_key)
    return build_position_key(
        symbol=position.symbol,
        product_type=position.product_type,
        margin_mode=getattr(position, "margin_mode", None),
        position_mode=getattr(position, "position_mode", None),
        pos_side=getattr(position, "pos_side", None),
    )


def position_key_for_exchange_position(
    position: "ExchangePosition",
    *,
    position_mode: object | None = None,
    product_type: str = "derivatives",
) -> str:
    return build_position_key(
        symbol=position.symbol,
        product_type=product_type,
        margin_mode=getattr(position, "margin_mode", None),
        position_mode=position_mode,
        pos_side=getattr(position, "side", None),
    )


def signed_quantity_for_position_side(
    quantity: Decimal | float | int | str | None,
    *,
    pos_side: object | None = None,
    position_mode: object | None = None,
) -> Decimal:
    resolved = to_decimal(quantity)
    normalized_side = normalize_position_side(pos_side, position_mode=position_mode)
    if normalized_side == "short" and resolved > EPSILON_DECIMAL_12:
        return -resolved
    return resolved


def exposure_side_from_quantity(quantity: Decimal | float | int | str | None) -> str:
    resolved = to_decimal(quantity)
    if resolved > EPSILON_DECIMAL_12:
        return "long"
    if resolved < -EPSILON_DECIMAL_12:
        return "short"
    return "flat"


def symbol_from_position_key(position_key: str) -> str:
    return str(position_key).split(":", 1)[0]
