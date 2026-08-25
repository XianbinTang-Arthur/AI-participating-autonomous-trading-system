"""Unified Research Factory policy profiles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aats.data_platform.research_factory.evidence import DatasetQualityThresholds
from aats.data_platform.research_factory.observations import (
    ObservationThresholds,
    observation_thresholds_for_profile,
)

RESEARCH_PROFILE_SCHEMA_VERSION = "research_profile_policy_v1"
ALLOWED_RESEARCH_PROFILES = frozenset(
    {
        "smoke",
        "real_factor_development",
        "real_factor_research",
        "shadow_review",
        "paper_review",
        "preapply_review",
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionEvidencePolicy:
    """Execution evidence requirements for a research profile."""

    required: bool
    allow_dataset_fingerprint_compatibility: bool
    require_exact_for_preapply: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.required, bool):
            raise ValueError("execution evidence required must be a bool")
        if not isinstance(self.allow_dataset_fingerprint_compatibility, bool):
            raise ValueError("allow_dataset_fingerprint_compatibility must be a bool")
        if not isinstance(self.require_exact_for_preapply, bool):
            raise ValueError("require_exact_for_preapply must be a bool")
        if self.require_exact_for_preapply and self.allow_dataset_fingerprint_compatibility:
            raise ValueError("preapply exact policy must not allow dataset fingerprint compatibility")


@dataclass(frozen=True, slots=True)
class ResearchProfile:
    """Named policy bundle for Research Factory stages."""

    name: str
    dataset_quality_thresholds: DatasetQualityThresholds
    candidate_gate_thresholds: Mapping[str, Any]
    observation_thresholds: ObservationThresholds
    execution_evidence_policy: ExecutionEvidencePolicy
    schema_version: str = RESEARCH_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        name = require_research_profile_name(self.name)
        if not isinstance(self.dataset_quality_thresholds, DatasetQualityThresholds):
            raise ValueError("dataset_quality_thresholds must be DatasetQualityThresholds")
        if not isinstance(self.candidate_gate_thresholds, Mapping):
            raise ValueError("candidate_gate_thresholds must be a mapping")
        if not isinstance(self.observation_thresholds, ObservationThresholds):
            raise ValueError("observation_thresholds must be ObservationThresholds")
        if not isinstance(self.execution_evidence_policy, ExecutionEvidencePolicy):
            raise ValueError("execution_evidence_policy must be ExecutionEvidencePolicy")
        if self.schema_version != RESEARCH_PROFILE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {RESEARCH_PROFILE_SCHEMA_VERSION!r}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "candidate_gate_thresholds", dict(self.candidate_gate_thresholds))

    @classmethod
    def from_name(cls, name: str) -> "ResearchProfile":
        """Build a configured research profile by name."""
        return research_profile_for_name(name)


def research_profile_for_name(name: str) -> ResearchProfile:
    """Return a deterministic Research Factory profile."""
    profile_name = require_research_profile_name(name)
    if profile_name == "smoke":
        return ResearchProfile(
            name=profile_name,
            dataset_quality_thresholds=DatasetQualityThresholds(
                min_total_bars=10,
                min_train_bars=2,
                min_valid_bars=2,
                min_test_bars=2,
            ),
            candidate_gate_thresholds=_candidate_thresholds(),
            observation_thresholds=observation_thresholds_for_profile("smoke"),
            execution_evidence_policy=ExecutionEvidencePolicy(
                required=False,
                allow_dataset_fingerprint_compatibility=True,
            ),
        )
    if profile_name in {"real_factor_development", "real_factor_research"}:
        return ResearchProfile(
            name=profile_name,
            dataset_quality_thresholds=DatasetQualityThresholds(
                min_total_bars=500,
                min_train_bars=300,
                min_valid_bars=100,
                min_test_bars=100,
            ),
            candidate_gate_thresholds=_candidate_thresholds(),
            observation_thresholds=observation_thresholds_for_profile("shadow_review"),
            execution_evidence_policy=ExecutionEvidencePolicy(
                required=profile_name == "real_factor_research",
                allow_dataset_fingerprint_compatibility=True,
            ),
        )
    if profile_name == "shadow_review":
        return ResearchProfile(
            name=profile_name,
            dataset_quality_thresholds=DatasetQualityThresholds(
                min_total_bars=500,
                min_train_bars=300,
                min_valid_bars=100,
                min_test_bars=100,
            ),
            candidate_gate_thresholds=_candidate_thresholds(),
            observation_thresholds=observation_thresholds_for_profile("shadow_review"),
            execution_evidence_policy=ExecutionEvidencePolicy(
                required=True,
                allow_dataset_fingerprint_compatibility=True,
            ),
        )
    if profile_name == "paper_review":
        return ResearchProfile(
            name=profile_name,
            dataset_quality_thresholds=DatasetQualityThresholds(
                min_total_bars=1_000,
                min_train_bars=600,
                min_valid_bars=200,
                min_test_bars=200,
            ),
            candidate_gate_thresholds=_candidate_thresholds(min_cost_adjusted_edge_bps_mean=0.2),
            observation_thresholds=observation_thresholds_for_profile("paper_review"),
            execution_evidence_policy=ExecutionEvidencePolicy(
                required=True,
                allow_dataset_fingerprint_compatibility=False,
            ),
        )
    return ResearchProfile(
        name=profile_name,
        dataset_quality_thresholds=DatasetQualityThresholds(
            min_total_bars=2_000,
            min_train_bars=1_200,
            min_valid_bars=400,
            min_test_bars=400,
        ),
        candidate_gate_thresholds=_candidate_thresholds(
            max_drawdown_limit=0.1,
            min_cost_adjusted_edge_bps_mean=0.5,
        ),
        observation_thresholds=observation_thresholds_for_profile("preapply"),
        execution_evidence_policy=ExecutionEvidencePolicy(
            required=True,
            allow_dataset_fingerprint_compatibility=False,
            require_exact_for_preapply=True,
        ),
    )


def resolve_research_profile(value: str | ResearchProfile | None) -> ResearchProfile | None:
    """Resolve optional profile input without changing default caller behavior."""
    if value is None:
        return None
    if isinstance(value, ResearchProfile):
        return value
    return research_profile_for_name(value)


def require_research_profile_name(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("research profile name must be a non-empty string")
    name = value.strip()
    if name not in ALLOWED_RESEARCH_PROFILES:
        allowed = ", ".join(sorted(ALLOWED_RESEARCH_PROFILES))
        raise ValueError(f"research profile must be one of: {allowed}")
    return name


def _candidate_thresholds(
    *,
    max_drawdown_limit: float = 0.2,
    min_cost_adjusted_edge_bps_mean: float = 0.0,
) -> dict[str, Any]:
    return {
        "min_net_annualized_return": 0.0,
        "max_drawdown_limit": max_drawdown_limit,
        "min_cost_adjusted_edge_bps_mean": min_cost_adjusted_edge_bps_mean,
        "critical_metrics": (
            "net_annualized_return",
            "max_drawdown",
            "cost_adjusted_edge_bps_mean",
        ),
    }
