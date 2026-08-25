"""Fail-closed eligibility gate for 15-minute microstructure research windows.

The Silver merger deliberately persists empty rows so operators can distinguish
"no data" from "pipeline did not run".  That persistence contract is useful for
operations but it does *not* make a window suitable for research.  This module
turns the row-level facts and collector evidence into an immutable eligibility
decision that downstream replay and capital-eligibility checks can verify.

Liquidations are sparse by nature: zero liquidation events is valid when the
liquidation collector is proven healthy.  Continuous channels fail closed when
their samples, required values, lineage, or quality flags are incomplete.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence


_UTC = timezone.utc
_REQUIRED_DATASETS = ("orderbook", "trades", "oi_funding", "liquidations")
_FATAL_FLAGS_BY_DATASET = {
    "orderbook": frozenset(
        {
            "etl_failed",
            "partial_data",
            "gap_filled_with_nulls",
            "stale_source",
            "orderbook_bbo_no_data",
            "orderbook_books5_no_data",
        }
    ),
    "trades": frozenset(
        {
            "etl_failed",
            "partial_data",
            "gap_filled_with_nulls",
            "stale_source",
            "trades_no_data",
        }
    ),
    "oi_funding": frozenset(
        {
            "etl_failed",
            "partial_data",
            "gap_filled_with_nulls",
            "stale_source",
            "oi_no_data",
            "funding_no_data",
            "mark_no_data",
        }
    ),
    # liquidation_no_data is intentionally non-fatal for this sparse channel.
    "liquidations": frozenset(
        {
            "etl_failed",
            "partial_data",
            "gap_filled_with_nulls",
            "stale_source",
        }
    ),
}


def _utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}_must_be_timezone_aware")
    return value.astimezone(_UTC)


def _normalise_string_map(
    value: Mapping[str, str | None],
    *,
    field_name: str,
) -> dict[str, str | None]:
    unexpected = sorted(set(value) - set(_REQUIRED_DATASETS))
    if unexpected:
        raise ValueError(f"{field_name}_unexpected_keys:{unexpected}")
    return {
        name: str(value[name]).strip() if value.get(name) is not None else None
        for name in _REQUIRED_DATASETS
    }


def _normalise_flags(
    value: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    unexpected = sorted(set(value) - set(_REQUIRED_DATASETS))
    if unexpected:
        raise ValueError(f"quality_flags_unexpected_keys:{unexpected}")
    return {
        name: tuple(sorted({str(flag).strip() for flag in value.get(name, ()) if flag}))
        for name in _REQUIRED_DATASETS
    }


@dataclass(frozen=True)
class MicrostructureEligibilityPolicy:
    """Versioned minimum coverage contract for one 15-minute window."""

    policy_version: str = "microstructure-15m-v1"
    window_seconds: int = 900
    min_bbo_samples: int = 720
    min_books5_samples: int = 720
    min_trade_count: int = 1
    min_oi_samples: int = 1
    max_window_age_seconds: int | None = None
    max_future_clock_skew_seconds: int = 5

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("policy_version_required")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds_must_be_positive")
        if self.max_window_age_seconds is not None and self.max_window_age_seconds <= 0:
            raise ValueError("max_window_age_seconds_must_be_positive")
        if self.max_future_clock_skew_seconds < 0:
            raise ValueError("max_future_clock_skew_seconds_must_be_non_negative")
        for name in (
            "min_bbo_samples",
            "min_books5_samples",
            "min_trade_count",
            "min_oi_samples",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name}_must_be_non_negative")


@dataclass(frozen=True)
class MicrostructureWindowObservation:
    """Code-derived Silver and runtime facts for a single aligned window."""

    symbol: str
    window_start: datetime
    window_end: datetime
    bbo_samples_n: int
    books5_samples_n: int
    trade_count: int
    oi_samples_n: int
    funding_rate_present: bool
    mark_price_present: bool
    liquidation_event_count: int
    microstructure_collector_fresh: bool
    liquidations_collector_fresh: bool
    dataset_versions: Mapping[str, str | None]
    ingest_run_ids: Mapping[str, str | None]
    quality_flags: Mapping[str, Sequence[str]]

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol_required")
        start = _utc_datetime(self.window_start, field_name="window_start")
        end = _utc_datetime(self.window_end, field_name="window_end")
        if end <= start:
            raise ValueError("window_end_must_follow_window_start")
        for name in (
            "bbo_samples_n",
            "books5_samples_n",
            "trade_count",
            "oi_samples_n",
            "liquidation_event_count",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name}_must_be_non_negative")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        object.__setattr__(
            self,
            "dataset_versions",
            _normalise_string_map(self.dataset_versions, field_name="dataset_versions"),
        )
        object.__setattr__(
            self,
            "ingest_run_ids",
            _normalise_string_map(self.ingest_run_ids, field_name="ingest_run_ids"),
        )
        object.__setattr__(self, "quality_flags", _normalise_flags(self.quality_flags))


@dataclass(frozen=True)
class MicrostructureEligibilityReport:
    """Immutable, fingerprinted output consumed by replay/readiness tooling."""

    format_version: int
    evaluated_at: datetime
    policy: MicrostructureEligibilityPolicy
    observation: MicrostructureWindowObservation
    eligible_for_research: bool
    reason_codes: tuple[str, ...]
    evidence_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evaluated_at"] = self.evaluated_at.isoformat()
        payload["observation"]["window_start"] = self.observation.window_start.isoformat()
        payload["observation"]["window_end"] = self.observation.window_end.isoformat()
        payload["reason_codes"] = list(self.reason_codes)
        for name, flags in payload["observation"]["quality_flags"].items():
            payload["observation"]["quality_flags"][name] = list(flags)
        return payload


def _fingerprint_payload(
    *,
    policy: MicrostructureEligibilityPolicy,
    observation: MicrostructureWindowObservation,
    eligible: bool,
    reason_codes: Sequence[str],
) -> str:
    payload = {
        "format_version": 1,
        "policy": asdict(policy),
        "observation": asdict(observation),
        "eligible_for_research": eligible,
        "reason_codes": list(reason_codes),
    }
    payload["observation"]["window_start"] = observation.window_start.isoformat()
    payload["observation"]["window_end"] = observation.window_end.isoformat()
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_microstructure_window(
    observation: MicrostructureWindowObservation,
    *,
    policy: MicrostructureEligibilityPolicy | None = None,
    evaluated_at: datetime | None = None,
) -> MicrostructureEligibilityReport:
    """Evaluate one window without mutating storage or runtime state."""

    selected_policy = policy or MicrostructureEligibilityPolicy()
    now = _utc_datetime(
        evaluated_at or datetime.now(_UTC),
        field_name="evaluated_at",
    )
    reasons: set[str] = set()
    expected_end = observation.window_start + timedelta(
        seconds=selected_policy.window_seconds
    )
    if observation.window_end != expected_end:
        reasons.add("window_duration_mismatch")
    if int(observation.window_start.timestamp()) % selected_policy.window_seconds:
        reasons.add("window_not_utc_aligned")
    window_age_seconds = (now - observation.window_end).total_seconds()
    if window_age_seconds < -selected_policy.max_future_clock_skew_seconds:
        reasons.add("window_end_in_future")
    if (
        selected_policy.max_window_age_seconds is not None
        and window_age_seconds > selected_policy.max_window_age_seconds
    ):
        reasons.add("window_stale")

    threshold_checks = (
        ("bbo_samples_below_minimum", observation.bbo_samples_n, selected_policy.min_bbo_samples),
        (
            "books5_samples_below_minimum",
            observation.books5_samples_n,
            selected_policy.min_books5_samples,
        ),
        ("trade_count_below_minimum", observation.trade_count, selected_policy.min_trade_count),
        ("oi_samples_below_minimum", observation.oi_samples_n, selected_policy.min_oi_samples),
    )
    for reason, actual, minimum in threshold_checks:
        if actual < minimum:
            reasons.add(reason)
    if not observation.funding_rate_present:
        reasons.add("funding_rate_missing")
    if not observation.mark_price_present:
        reasons.add("mark_price_missing")
    if not observation.microstructure_collector_fresh:
        reasons.add("microstructure_collector_not_fresh")
    if not observation.liquidations_collector_fresh:
        reasons.add("liquidations_collector_not_fresh")

    dataset_versions = observation.dataset_versions
    missing_versions = sorted(name for name, value in dataset_versions.items() if not value)
    if missing_versions:
        reasons.update(f"dataset_version_missing:{name}" for name in missing_versions)
    non_empty_versions = {value for value in dataset_versions.values() if value}
    if len(non_empty_versions) > 1:
        reasons.add("dataset_version_mismatch")

    ingest_run_ids = observation.ingest_run_ids
    missing_runs = sorted(name for name, value in ingest_run_ids.items() if not value)
    if missing_runs:
        reasons.update(f"ingest_run_id_missing:{name}" for name in missing_runs)
    non_empty_runs = {value for value in ingest_run_ids.values() if value}
    if len(non_empty_runs) > 1:
        reasons.add("ingest_run_id_mismatch")

    for dataset_name, flags in observation.quality_flags.items():
        fatal_flags = _FATAL_FLAGS_BY_DATASET[dataset_name].intersection(flags)
        reasons.update(
            f"fatal_quality_flag:{dataset_name}:{flag}" for flag in fatal_flags
        )

    ordered_reasons = tuple(sorted(reasons))
    eligible = not ordered_reasons
    fingerprint = _fingerprint_payload(
        policy=selected_policy,
        observation=observation,
        eligible=eligible,
        reason_codes=ordered_reasons,
    )
    return MicrostructureEligibilityReport(
        format_version=1,
        evaluated_at=now,
        policy=selected_policy,
        observation=observation,
        eligible_for_research=eligible,
        reason_codes=ordered_reasons,
        evidence_fingerprint=fingerprint,
    )
