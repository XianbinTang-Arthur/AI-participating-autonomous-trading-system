"""Shared fail-closed thresholds for capital-promotion decisions."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

P2_MIN_OPENING_COUNT = 1
P2_MIN_POSITIVE_EDGE_RATIO = 0.2


def phase2_combo_meets_promotion_gate(stats: Mapping[str, Any]) -> bool:
    """Return whether one exact combo clears the mandatory Phase 2 gate."""

    experiments_with_openings = stats.get("experiments_with_openings")
    max_opening_count = stats.get("max_opening_count")
    mean_positive_edge_ratio = stats.get("mean_positive_edge_ratio")
    return bool(
        stats.get("available") is True
        and type(experiments_with_openings) is int
        and experiments_with_openings >= P2_MIN_OPENING_COUNT
        and type(max_opening_count) is int
        and max_opening_count >= P2_MIN_OPENING_COUNT
        and type(mean_positive_edge_ratio) in {int, float}
        and math.isfinite(float(mean_positive_edge_ratio))
        and float(mean_positive_edge_ratio) >= P2_MIN_POSITIVE_EDGE_RATIO
    )
