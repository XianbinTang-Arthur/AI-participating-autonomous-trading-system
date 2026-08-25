"""Deterministic statistical gates for dependent trading returns.

The implementation intentionally uses only the Python standard library.  The
moving-block bootstrap preserves local serial dependence, Holm-Bonferroni
controls family-wise error across tried candidates, and the deflated Sharpe
probability penalizes repeated trials and non-normal returns.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from dataclasses import asdict, dataclass
from statistics import NormalDist
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    fold_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    purge_size: int
    embargo_size: int


@dataclass(frozen=True, slots=True)
class WalkForwardFoldResult:
    fold_index: int
    sample_count: int
    mean_return: float
    compounded_return: float
    max_drawdown: float
    positive: bool


@dataclass(frozen=True, slots=True)
class WalkForwardEvidence:
    format_version: int
    passed: bool
    reason_codes: tuple[str, ...]
    folds: tuple[WalkForwardFoldResult, ...]
    positive_fold_ratio: float
    aggregate_compounded_return: float
    evidence_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        payload["folds"] = [asdict(fold) for fold in self.folds]
        return payload


@dataclass(frozen=True, slots=True)
class BlockBootstrapResult:
    sample_count: int
    block_size: int
    replications: int
    observed_mean: float
    confidence_level: float
    ci_lower: float
    ci_upper: float
    one_sided_p_value: float
    seed: int


@dataclass(frozen=True, slots=True)
class StatisticalEvidence:
    format_version: int
    passed: bool
    reason_codes: tuple[str, ...]
    block_bootstrap: BlockBootstrapResult
    raw_p_values: Mapping[str, float]
    holm_adjusted_p_values: Mapping[str, float]
    holm_rejected: Mapping[str, bool]
    observed_sharpe: float
    deflated_sharpe_probability: float
    trial_count: int
    alpha: float
    evidence_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def _finite_values(values: Sequence[float], *, minimum: int) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) < minimum:
        raise ValueError(f"returns_require_at_least_{minimum}_samples")
    if any(not math.isfinite(value) for value in result):
        raise ValueError("returns_must_be_finite")
    if any(value <= -1.0 for value in result):
        raise ValueError("returns_must_be_greater_than_minus_one")
    return result


def build_purged_walk_forward_splits(
    sample_count: int,
    *,
    initial_train_size: int,
    test_size: int,
    purge_size: int,
    embargo_size: int,
) -> tuple[WalkForwardSplit, ...]:
    """Build expanding-window splits with explicit purge and post-test embargo."""

    if sample_count <= 0 or initial_train_size <= 0 or test_size <= 0:
        raise ValueError("sample_count_train_and_test_sizes_must_be_positive")
    if purge_size < 0 or embargo_size < 0:
        raise ValueError("purge_and_embargo_must_be_non_negative")
    splits: list[WalkForwardSplit] = []
    train_end = initial_train_size
    fold = 0
    while True:
        test_start = train_end + purge_size
        test_end = test_start + test_size
        if test_end > sample_count:
            break
        splits.append(
            WalkForwardSplit(
                fold_index=fold,
                train_start=0,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                purge_size=purge_size,
                embargo_size=embargo_size,
            )
        )
        train_end = test_end + embargo_size
        fold += 1
    if not splits:
        raise ValueError("insufficient_samples_for_walk_forward_split")
    return tuple(splits)


def _compounded_return(values: Sequence[float]) -> float:
    wealth = 1.0
    for value in values:
        wealth *= 1.0 + value
    return wealth - 1.0


def _max_drawdown(values: Sequence[float]) -> float:
    wealth = 1.0
    peak = 1.0
    maximum = 0.0
    for value in values:
        wealth *= 1.0 + value
        peak = max(peak, wealth)
        maximum = max(maximum, (peak - wealth) / peak)
    return maximum


def _walk_forward_fingerprint(
    splits: Sequence[WalkForwardSplit],
    folds: Sequence[WalkForwardFoldResult],
    reasons: Sequence[str],
) -> str:
    payload = {
        "format_version": 1,
        "splits": [asdict(item) for item in splits],
        "folds": [asdict(item) for item in folds],
        "reason_codes": list(reasons),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def evaluate_walk_forward(
    net_returns: Sequence[float],
    splits: Sequence[WalkForwardSplit],
    *,
    min_positive_fold_ratio: float = 0.6,
    max_fold_drawdown: float = 0.2,
) -> WalkForwardEvidence:
    returns = _finite_values(net_returns, minimum=2)
    if not 0.0 <= min_positive_fold_ratio <= 1.0:
        raise ValueError("min_positive_fold_ratio_out_of_range")
    if not 0.0 <= max_fold_drawdown <= 1.0:
        raise ValueError("max_fold_drawdown_out_of_range")
    if not splits:
        raise ValueError("walk_forward_splits_required")
    folds: list[WalkForwardFoldResult] = []
    reasons: set[str] = set()
    for split in splits:
        if not 0 <= split.train_start < split.train_end <= split.test_start:
            raise ValueError("invalid_walk_forward_train_test_order")
        if split.test_end > len(returns) or split.test_start >= split.test_end:
            raise ValueError("invalid_walk_forward_test_bounds")
        test_returns = returns[split.test_start : split.test_end]
        compounded = _compounded_return(test_returns)
        drawdown = _max_drawdown(test_returns)
        result = WalkForwardFoldResult(
            fold_index=split.fold_index,
            sample_count=len(test_returns),
            mean_return=statistics.fmean(test_returns),
            compounded_return=compounded,
            max_drawdown=drawdown,
            positive=compounded > 0.0,
        )
        folds.append(result)
        if drawdown > max_fold_drawdown:
            reasons.add(f"fold_drawdown_exceeded:{split.fold_index}")
    positive_ratio = sum(fold.positive for fold in folds) / len(folds)
    if positive_ratio < min_positive_fold_ratio:
        reasons.add("positive_fold_ratio_below_minimum")
    aggregate = _compounded_return(
        returns[index]
        for split in splits
        for index in range(split.test_start, split.test_end)
    )
    if aggregate <= 0.0:
        reasons.add("aggregate_oos_return_not_positive")
    ordered_reasons = tuple(sorted(reasons))
    return WalkForwardEvidence(
        format_version=1,
        passed=not ordered_reasons,
        reason_codes=ordered_reasons,
        folds=tuple(folds),
        positive_fold_ratio=positive_ratio,
        aggregate_compounded_return=aggregate,
        evidence_fingerprint=_walk_forward_fingerprint(splits, folds, ordered_reasons),
    )


def moving_block_bootstrap_mean(
    net_returns: Sequence[float],
    *,
    block_size: int,
    replications: int = 2_000,
    confidence_level: float = 0.95,
    seed: int = 0,
) -> BlockBootstrapResult:
    values = _finite_values(net_returns, minimum=20)
    if not 1 <= block_size <= len(values):
        raise ValueError("block_size_out_of_range")
    if replications < 100:
        raise ValueError("bootstrap_replications_must_be_at_least_100")
    if not 0.5 < confidence_level < 1.0:
        raise ValueError("confidence_level_out_of_range")
    rng = random.Random(seed)
    sample_means: list[float] = []
    starts = range(0, len(values) - block_size + 1)
    for _ in range(replications):
        sample: list[float] = []
        while len(sample) < len(values):
            start = rng.choice(starts)
            sample.extend(values[start : start + block_size])
        sample_means.append(statistics.fmean(sample[: len(values)]))
    sample_means.sort()
    tail = (1.0 - confidence_level) / 2.0

    def quantile(probability: float) -> float:
        position = probability * (len(sample_means) - 1)
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return sample_means[lower]
        weight = position - lower
        return sample_means[lower] * (1.0 - weight) + sample_means[upper] * weight

    non_positive = sum(value <= 0.0 for value in sample_means)
    p_value = (non_positive + 1.0) / (replications + 1.0)
    return BlockBootstrapResult(
        sample_count=len(values),
        block_size=block_size,
        replications=replications,
        observed_mean=statistics.fmean(values),
        confidence_level=confidence_level,
        ci_lower=quantile(tail),
        ci_upper=quantile(1.0 - tail),
        one_sided_p_value=p_value,
        seed=seed,
    )


def holm_bonferroni(
    p_values: Mapping[str, float],
    *,
    alpha: float = 0.05,
) -> tuple[dict[str, float], dict[str, bool]]:
    if not p_values:
        raise ValueError("p_values_required")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha_out_of_range")
    ordered: list[tuple[str, float]] = []
    for name, raw_value in p_values.items():
        value = float(raw_value)
        if not 0.0 <= value <= 1.0 or not math.isfinite(value):
            raise ValueError(f"invalid_p_value:{name}")
        ordered.append((str(name), value))
    ordered.sort(key=lambda item: (item[1], item[0]))
    adjusted: dict[str, float] = {}
    running_max = 0.0
    total = len(ordered)
    for index, (name, value) in enumerate(ordered):
        running_max = max(running_max, min(1.0, (total - index) * value))
        adjusted[name] = running_max
    rejected: dict[str, bool] = {}
    continue_rejecting = True
    for index, (name, value) in enumerate(ordered):
        threshold = alpha / (total - index)
        continue_rejecting = continue_rejecting and value <= threshold
        rejected[name] = continue_rejecting
    return adjusted, rejected


def _sample_shape(values: Sequence[float]) -> tuple[float, float]:
    mean = statistics.fmean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    if variance <= 0.0:
        return 0.0, 0.0
    stddev = math.sqrt(variance)
    skewness = sum(((value - mean) / stddev) ** 3 for value in values) / len(values)
    excess_kurtosis = (
        sum(((value - mean) / stddev) ** 4 for value in values) / len(values) - 3.0
    )
    return skewness, excess_kurtosis


def deflated_sharpe_probability(
    observed_sharpe: float,
    *,
    sample_count: int,
    skewness: float,
    excess_kurtosis: float,
    trial_count: int,
) -> float:
    """Approximate Bailey-Lopez de Prado deflated Sharpe probability."""

    if sample_count < 3 or trial_count < 1:
        raise ValueError("invalid_deflated_sharpe_counts")
    values = (observed_sharpe, skewness, excess_kurtosis)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("deflated_sharpe_inputs_must_be_finite")
    variance = (
        1.0
        - skewness * observed_sharpe
        + ((excess_kurtosis + 2.0) / 4.0) * observed_sharpe**2
    ) / (sample_count - 1)
    if variance <= 0.0:
        return 0.0
    if trial_count == 1:
        expected_max_sharpe = 0.0
    else:
        gamma = 0.5772156649015329
        normal = NormalDist()
        expected_max_sharpe = math.sqrt(variance) * (
            (1.0 - gamma) * normal.inv_cdf(1.0 - 1.0 / trial_count)
            + gamma * normal.inv_cdf(1.0 - 1.0 / (trial_count * math.e))
        )
    test_statistic = (observed_sharpe - expected_max_sharpe) / math.sqrt(variance)
    return NormalDist().cdf(test_statistic)


def _statistical_fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def evaluate_statistical_evidence(
    net_returns: Sequence[float],
    *,
    candidate_id: str,
    candidate_p_values: Mapping[str, float],
    trial_count: int,
    periods_per_year: float,
    block_size: int,
    replications: int = 2_000,
    alpha: float = 0.05,
    min_deflated_sharpe_probability: float = 0.95,
    seed: int = 0,
) -> StatisticalEvidence:
    values = _finite_values(net_returns, minimum=20)
    if candidate_id not in candidate_p_values:
        raise ValueError("candidate_id_missing_from_p_values")
    if periods_per_year <= 0.0:
        raise ValueError("periods_per_year_must_be_positive")
    if not 0.0 < min_deflated_sharpe_probability < 1.0:
        raise ValueError("min_deflated_sharpe_probability_out_of_range")
    bootstrap = moving_block_bootstrap_mean(
        values,
        block_size=block_size,
        replications=replications,
        confidence_level=1.0 - alpha,
        seed=seed,
    )
    adjusted, rejected = holm_bonferroni(candidate_p_values, alpha=alpha)
    mean = statistics.fmean(values)
    standard_deviation = statistics.stdev(values)
    observed_sharpe = (
        mean / standard_deviation * math.sqrt(periods_per_year)
        if standard_deviation > 0.0
        else 0.0
    )
    skewness, excess_kurtosis = _sample_shape(values)
    dsr_probability = deflated_sharpe_probability(
        observed_sharpe,
        sample_count=len(values),
        skewness=skewness,
        excess_kurtosis=excess_kurtosis,
        trial_count=trial_count,
    )
    reasons: set[str] = set()
    if bootstrap.ci_lower <= 0.0:
        reasons.add("bootstrap_lower_bound_not_positive")
    if bootstrap.one_sided_p_value > alpha:
        reasons.add("bootstrap_p_value_above_alpha")
    if not rejected[candidate_id]:
        reasons.add("candidate_not_significant_after_holm")
    if dsr_probability < min_deflated_sharpe_probability:
        reasons.add("deflated_sharpe_probability_below_minimum")
    ordered_reasons = tuple(sorted(reasons))
    fingerprint_payload = {
        "format_version": 1,
        "candidate_id": candidate_id,
        "block_bootstrap": asdict(bootstrap),
        "raw_p_values": dict(candidate_p_values),
        "holm_adjusted_p_values": adjusted,
        "holm_rejected": rejected,
        "observed_sharpe": observed_sharpe,
        "deflated_sharpe_probability": dsr_probability,
        "trial_count": trial_count,
        "alpha": alpha,
        "reason_codes": ordered_reasons,
    }
    return StatisticalEvidence(
        format_version=1,
        passed=not ordered_reasons,
        reason_codes=ordered_reasons,
        block_bootstrap=bootstrap,
        raw_p_values=dict(candidate_p_values),
        holm_adjusted_p_values=adjusted,
        holm_rejected=rejected,
        observed_sharpe=observed_sharpe,
        deflated_sharpe_probability=dsr_probability,
        trial_count=trial_count,
        alpha=alpha,
        evidence_fingerprint=_statistical_fingerprint(fingerprint_payload),
    )
