from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .replay import IndependentDecisionSnapshot


@dataclass(frozen=True, slots=True)
class IndependentScoreDrawdownSweepSample:
    decision_id: str
    leg: str
    score: float
    entry_threshold: float
    support_count: int
    downward_drawdown_bps: float
    expected_net_edge_bps: float | None = None


@dataclass(frozen=True, slots=True)
class IndependentScoreDrawdownSweepSummary:
    threshold_bps: float
    sample_count: int
    qualifying_signal_count: int
    support_ready_signal_count: int
    stable_signal_count: int
    blocked_by_drawdown_count: int
    released_vs_baseline_count: int
    avg_downward_drawdown_bps: float | None = None
    stable_avg_expected_net_edge_bps: float | None = None


def score_drawdown_sample_from_decision_snapshot(
    snapshot: IndependentDecisionSnapshot,
) -> IndependentScoreDrawdownSweepSample | None:
    threshold_snapshot = snapshot.threshold_snapshot or {}
    score_stability_metrics = snapshot.score_stability_metrics or {}
    expectancy_snapshot = snapshot.expectancy_snapshot or {}
    score = snapshot.adjusted_score if snapshot.adjusted_score is not None else snapshot.raw_score
    entry_threshold = threshold_snapshot.get("effective_entry_threshold")
    if entry_threshold is None:
        entry_threshold = threshold_snapshot.get("entry_threshold")
    support_count = score_stability_metrics.get("support_count")
    downward_drawdown_bps = score_stability_metrics.get("downward_drawdown_bps")
    if score is None or entry_threshold is None or support_count is None or downward_drawdown_bps is None:
        return None
    return IndependentScoreDrawdownSweepSample(
        decision_id=snapshot.decision_id,
        leg=snapshot.leg,
        score=float(score),
        entry_threshold=float(entry_threshold),
        support_count=int(support_count),
        downward_drawdown_bps=float(downward_drawdown_bps),
        expected_net_edge_bps=(
            None
            if expectancy_snapshot.get("expected_net_edge_bps") is None
            else float(expectancy_snapshot["expected_net_edge_bps"])
        ),
    )


def summarize_score_drawdown_threshold_sweep(
    *,
    samples: Sequence[IndependentScoreDrawdownSweepSample],
    thresholds_bps: Sequence[float],
    min_confirm_ticks: int,
    baseline_threshold_bps: float | None = None,
) -> tuple[IndependentScoreDrawdownSweepSummary, ...]:
    unique_thresholds = tuple(sorted({max(float(item), 0.0) for item in thresholds_bps}))
    if not unique_thresholds:
        return ()
    effective_min_confirm_ticks = max(int(min_confirm_ticks), 1)
    qualifying_samples = tuple(
        sample for sample in samples if sample.score + 1e-9 >= sample.entry_threshold
    )
    support_ready_samples = tuple(
        sample for sample in qualifying_samples if sample.support_count >= effective_min_confirm_ticks
    )
    baseline = min(unique_thresholds) if baseline_threshold_bps is None else max(float(baseline_threshold_bps), 0.0)
    baseline_ids = {
        (sample.decision_id, sample.leg)
        for sample in support_ready_samples
        if sample.downward_drawdown_bps <= baseline + 1e-9
    }
    avg_downward_drawdown_bps = (
        None
        if not support_ready_samples
        else sum(sample.downward_drawdown_bps for sample in support_ready_samples) / len(support_ready_samples)
    )
    summaries: list[IndependentScoreDrawdownSweepSummary] = []
    for threshold_bps in unique_thresholds:
        stable_samples = tuple(
            sample for sample in support_ready_samples if sample.downward_drawdown_bps <= threshold_bps + 1e-9
        )
        stable_ids = {(sample.decision_id, sample.leg) for sample in stable_samples}
        stable_expected_edges = [
            float(sample.expected_net_edge_bps)
            for sample in stable_samples
            if sample.expected_net_edge_bps is not None
        ]
        summaries.append(
            IndependentScoreDrawdownSweepSummary(
                threshold_bps=threshold_bps,
                sample_count=len(samples),
                qualifying_signal_count=len(qualifying_samples),
                support_ready_signal_count=len(support_ready_samples),
                stable_signal_count=len(stable_samples),
                blocked_by_drawdown_count=max(len(support_ready_samples) - len(stable_samples), 0),
                released_vs_baseline_count=len(stable_ids - baseline_ids),
                avg_downward_drawdown_bps=avg_downward_drawdown_bps,
                stable_avg_expected_net_edge_bps=(
                    None if not stable_expected_edges else sum(stable_expected_edges) / len(stable_expected_edges)
                ),
            )
        )
    return tuple(summaries)
