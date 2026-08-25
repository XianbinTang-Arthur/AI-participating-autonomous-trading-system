"""Profit-readiness validation primitives for research-only candidates."""

from aats.data_platform.research_factory.validation.capital_eligibility import (
    CapitalEligibilityEvidence,
    CapitalEligibilityDecision,
    evaluate_capital_eligibility,
)
from aats.data_platform.research_factory.validation.statistics import (
    StatisticalEvidence,
    WalkForwardEvidence,
    build_purged_walk_forward_splits,
    evaluate_statistical_evidence,
    evaluate_walk_forward,
)
from aats.data_platform.research_factory.validation.holdout import (
    HoldoutAccessRequest,
    HoldoutEvaluationOutcome,
    HoldoutEvaluationResult,
    SQLHoldoutLedger,
    evaluate_holdout_once,
)

__all__ = [
    "CapitalEligibilityDecision",
    "CapitalEligibilityEvidence",
    "StatisticalEvidence",
    "HoldoutAccessRequest",
    "HoldoutEvaluationOutcome",
    "HoldoutEvaluationResult",
    "SQLHoldoutLedger",
    "WalkForwardEvidence",
    "build_purged_walk_forward_splits",
    "evaluate_capital_eligibility",
    "evaluate_holdout_once",
    "evaluate_statistical_evidence",
    "evaluate_walk_forward",
]
