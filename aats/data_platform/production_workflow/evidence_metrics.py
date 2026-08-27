"""Strict numeric parsing for post-apply research evidence."""

from __future__ import annotations

import math
from typing import Any


def finite_metric(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Return an exact finite number inside the optional closed interval.

    JSON booleans are deliberately rejected even though ``bool`` subclasses
    ``int`` in Python.  Missing, stringified, NaN and infinite measurements are
    unavailable evidence, never zero/default measurements.
    """

    if type(value) not in {int, float}:
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    if minimum is not None and numeric < minimum:
        return None
    if maximum is not None and numeric > maximum:
        return None
    return numeric
