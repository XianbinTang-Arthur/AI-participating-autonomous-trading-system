from __future__ import annotations

from decimal import Decimal

ZERO_DECIMAL = Decimal("0")
EPSILON_DECIMAL_12 = Decimal("1e-12")
EPSILON_DECIMAL_9 = Decimal("1e-9")
SNAPSHOT_QUANTUM = Decimal("0.000000000001")


def clamp_float(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def to_decimal(value: Decimal | float | int | str | None) -> Decimal:
    if value is None:
        return ZERO_DECIMAL
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def to_decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def to_float(value: Decimal | float | int) -> float:
    return float(value)


def abs_decimal(value: Decimal | float | int | str | None) -> Decimal:
    return abs(to_decimal(value))


def is_effectively_zero(
    value: Decimal | float | int | str | None,
    *,
    epsilon: Decimal = EPSILON_DECIMAL_12,
) -> bool:
    return abs_decimal(value) <= epsilon


def quantize_decimal(
    value: Decimal | float | int | str | None,
    *,
    quantum: Decimal = SNAPSHOT_QUANTUM,
) -> Decimal:
    decimal_value = to_decimal(value)
    if is_effectively_zero(decimal_value):
        return Decimal("0")
    return decimal_value.quantize(quantum)
