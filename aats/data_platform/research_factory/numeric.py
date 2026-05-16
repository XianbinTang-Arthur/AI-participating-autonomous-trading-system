"""Finite numeric validation helpers for Research Factory inputs."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any


def require_finite_number(value: Any, field_name: str) -> float:
    """Return a finite float for supported numeric inputs."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ValueError(f"{field_name} must be numeric")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{field_name} must be finite")
        return float(value)
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result
