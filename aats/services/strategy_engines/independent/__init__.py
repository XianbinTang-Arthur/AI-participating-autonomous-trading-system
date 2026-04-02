from __future__ import annotations

from importlib import import_module

__all__ = [
    "IndependentBookAction",
    "IndependentBookDecision",
    "IndependentBookEvaluation",
    "IndependentBookExpectancy",
    "IndependentEligibilityOutcome",
    "IndependentAdaptiveSnapshot",
    "IndependentExecutionHealthState",
    "IndependentExecutionPolicy",
    "IndependentFamilyEvaluation",
    "IndependentLeg",
    "IndependentSizingOutcome",
    "ScoreStabilityMetrics",
    "IndependentScoreDrawdownSweepSample",
    "IndependentScoreDrawdownSweepSummary",
    "aggregate_family_health",
    "build_independent_family_candidate",
    "derive_book_state",
    "derive_guard_state",
    "derive_holding_phase",
    "evaluate_leg_health",
    "evaluate_independent_book",
    "replay_snapshot_from_decision",
    "score_drawdown_sample_from_decision_snapshot",
    "summarize_score_drawdown_threshold_sweep",
    "runtime_state_from_decision",
    "transition_book_state",
    "threshold_snapshot",
]


def __getattr__(name: str) -> object:
    if name in {
        "IndependentBookAction",
        "IndependentBookDecision",
        "IndependentBookEvaluation",
        "IndependentBookExpectancy",
        "IndependentEligibilityOutcome",
        "IndependentExecutionHealthState",
        "IndependentExecutionPolicy",
        "IndependentFamilyEvaluation",
        "IndependentLeg",
        "IndependentSizingOutcome",
        "ScoreStabilityMetrics",
    }:
        return getattr(import_module(".models", __name__), name)
    if name in {"IndependentAdaptiveSnapshot", "threshold_snapshot"}:
        return getattr(import_module(".adaptive", __name__), name)
    if name in {"aggregate_family_health", "evaluate_leg_health"}:
        return getattr(import_module(".health", __name__), name)
    if name in {"build_independent_family_candidate", "evaluate_independent_book"}:
        return getattr(import_module(".engine", __name__), name)
    if name in {"runtime_state_from_decision"}:
        return getattr(import_module(".diagnostics", __name__), name)
    if name in {"replay_snapshot_from_decision"}:
        return getattr(import_module(".replay", __name__), name)
    if name in {
        "IndependentScoreDrawdownSweepSample",
        "IndependentScoreDrawdownSweepSummary",
        "score_drawdown_sample_from_decision_snapshot",
        "summarize_score_drawdown_threshold_sweep",
    }:
        return getattr(import_module(".tuning", __name__), name)
    if name in {"derive_book_state", "derive_guard_state", "derive_holding_phase", "transition_book_state"}:
        return getattr(import_module(".state_machine", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
