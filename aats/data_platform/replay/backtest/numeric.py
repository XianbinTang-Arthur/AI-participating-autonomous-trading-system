"""Fail-closed numeric boundaries for replay metrics and JSON artifacts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any


def finite_float(value: Any, *, reason: str) -> float:
    """Convert one numeric value to a finite float or raise a stable error."""

    if type(value) is bool:
        raise ValueError(reason)
    try:
        resolved = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(reason) from exc
    if not math.isfinite(resolved):
        raise ValueError(reason)
    if resolved == 0.0:
        return 0.0
    return resolved


def validate_finite_numbers(value: Any, *, reason: str) -> None:
    """Recursively reject non-finite floats/Decimals in an artifact payload."""

    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(reason)
        return
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(reason)
        return
    if isinstance(value, Mapping):
        for child in value.values():
            validate_finite_numbers(child, reason=reason)
        return
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for child in value:
            validate_finite_numbers(child, reason=reason)


__all__ = ["finite_float", "validate_finite_numbers"]
