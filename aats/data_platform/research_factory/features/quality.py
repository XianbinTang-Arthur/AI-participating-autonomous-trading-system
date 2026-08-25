"""Fail-closed completeness evidence for factor input fields."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aats.data_platform.research_factory.datasets.gold_bars import (
    PreparedGoldBarDataset,
)
from aats.data_platform.research_factory.features.expressions import (
    ALLOWED_FACTOR_FIELDS,
)

FEATURE_INPUT_QUALITY_SCHEMA = "research_factor_input_quality_v1"


@dataclass(frozen=True, slots=True)
class FactorInputQualityReport:
    """Field-level completeness gate over every configured dataset segment."""

    required_fields: tuple[str, ...]
    row_count: int
    non_null_counts: Mapping[str, int]
    missing_counts: Mapping[str, int]
    missing_ratios: Mapping[str, float]
    segment_missing_ratios: Mapping[str, Mapping[str, float]]
    max_missing_ratio: float
    passed: bool
    failures: tuple[str, ...]
    created_at: datetime
    schema_version: str = FEATURE_INPUT_QUALITY_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "required_fields": list(self.required_fields),
            "row_count": self.row_count,
            "non_null_counts": dict(self.non_null_counts),
            "missing_counts": dict(self.missing_counts),
            "missing_ratios": dict(self.missing_ratios),
            "segment_missing_ratios": {
                name: dict(ratios)
                for name, ratios in self.segment_missing_ratios.items()
            },
            "max_missing_ratio": self.max_missing_ratio,
            "passed": self.passed,
            "failures": list(self.failures),
            "created_at": self.created_at.isoformat(),
        }


def build_factor_input_quality_report(
    prepared: PreparedGoldBarDataset,
    *,
    required_fields: Sequence[str],
    max_missing_ratio: float,
    created_at: datetime | None = None,
) -> FactorInputQualityReport:
    """Measure only inputs referenced by the executed factor expression."""
    if not isinstance(prepared, PreparedGoldBarDataset):
        raise ValueError("prepared must be a PreparedGoldBarDataset")
    fields = tuple(dict.fromkeys(required_fields))
    if not fields:
        raise ValueError("required_fields must not be empty")
    unsupported = sorted(set(fields) - ALLOWED_FACTOR_FIELDS)
    if unsupported:
        raise ValueError(f"unsupported factor input fields: {unsupported}")
    if (
        isinstance(max_missing_ratio, bool)
        or not isinstance(max_missing_ratio, int | float)
        or not math.isfinite(float(max_missing_ratio))
        or not 0.0 <= float(max_missing_ratio) <= 1.0
    ):
        raise ValueError("max_missing_ratio must be between zero and one")
    evaluated_at = created_at or datetime.now(UTC)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")

    rows_by_segment = {
        segment.name: prepared.rows_for_segment(segment.name)
        for segment in prepared.dataset_spec.segments
    }
    all_rows = tuple(
        row
        for segment_name, rows in rows_by_segment.items()
        if segment_name != "replay"
        for row in rows
    )
    row_count = len(all_rows)
    non_null_counts = {
        field: sum(row.get(field) is not None for row in all_rows)
        for field in fields
    }
    missing_counts = {
        field: row_count - non_null_counts[field]
        for field in fields
    }
    missing_ratios = {
        field: missing_counts[field] / row_count if row_count else 1.0
        for field in fields
    }
    segment_missing_ratios: dict[str, dict[str, float]] = {}
    failures: list[str] = []
    threshold = float(max_missing_ratio)
    for segment_name, rows in rows_by_segment.items():
        if segment_name == "replay":
            continue
        segment_count = len(rows)
        ratios = {
            field: (
                sum(row.get(field) is None for row in rows) / segment_count
                if segment_count
                else 1.0
            )
            for field in fields
        }
        segment_missing_ratios[segment_name] = ratios
        for field, ratio in ratios.items():
            if ratio > threshold:
                failures.append(
                    f"{segment_name}.{field}_missing_ratio={ratio:.6f} > "
                    f"max_missing_ratio={threshold:.6f}"
                )

    return FactorInputQualityReport(
        required_fields=fields,
        row_count=row_count,
        non_null_counts=non_null_counts,
        missing_counts=missing_counts,
        missing_ratios=missing_ratios,
        segment_missing_ratios=segment_missing_ratios,
        max_missing_ratio=threshold,
        passed=not failures,
        failures=tuple(failures),
        created_at=evaluated_at.astimezone(UTC),
    )
