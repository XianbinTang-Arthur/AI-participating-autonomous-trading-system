from aats.data_platform.research_factory.profiles import (
    ResearchProfile,
    research_profile_for_name,
    resolve_research_profile,
)


def test_research_profile_smoke_keeps_low_sample_thresholds() -> None:
    profile = research_profile_for_name("smoke")

    assert profile.name == "smoke"
    assert profile.dataset_quality_thresholds.min_total_bars == 10
    assert profile.observation_thresholds.min_observed_bars == 4
    assert profile.execution_evidence_policy.required is False


def test_real_factor_development_keeps_thresholds_without_claiming_l2() -> None:
    development = research_profile_for_name("real_factor_development")
    evidence_complete = research_profile_for_name("real_factor_research")

    assert development.dataset_quality_thresholds == (
        evidence_complete.dataset_quality_thresholds
    )
    assert development.candidate_gate_thresholds == (
        evidence_complete.candidate_gate_thresholds
    )
    assert development.execution_evidence_policy.required is False
    assert evidence_complete.execution_evidence_policy.required is True


def test_research_profile_preapply_requires_strict_execution_identity() -> None:
    profile = research_profile_for_name("preapply_review")

    assert profile.dataset_quality_thresholds.min_total_bars == 2_000
    assert profile.candidate_gate_thresholds["max_drawdown_limit"] == 0.1
    assert profile.candidate_gate_thresholds["min_cost_adjusted_edge_bps_mean"] == 0.5
    assert profile.execution_evidence_policy.required is True
    assert profile.execution_evidence_policy.allow_dataset_fingerprint_compatibility is False
    assert profile.execution_evidence_policy.require_exact_for_preapply is True


def test_resolve_research_profile_accepts_existing_profile() -> None:
    profile = research_profile_for_name("paper_review")

    assert resolve_research_profile(profile) is profile
    assert isinstance(resolve_research_profile("paper_review"), ResearchProfile)


def test_unknown_research_profile_fails_closed() -> None:
    try:
        research_profile_for_name("prod_apply")
    except ValueError as exc:
        assert "research profile must be one of" in str(exc)
    else:
        raise AssertionError("unknown profile should fail closed")
