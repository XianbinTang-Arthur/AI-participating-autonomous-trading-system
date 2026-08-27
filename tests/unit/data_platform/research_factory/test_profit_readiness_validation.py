from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from aats.data_platform.research_factory.validation import capital_eligibility
from aats.data_platform.research_factory.contract_lineage import (
    ContractAwareArtifactLineage,
)
from aats.data_platform.research_factory.validation.capital_eligibility import (
    CAPITAL_ELIGIBLE_EXECUTION_MODEL,
    CURRENT_SELECTION_PROTOCOL,
    CapitalEligibilityEvidence,
    evaluate_capital_eligibility,
    legacy_candidate_reasons,
)
from aats.data_platform.research_factory.validation.statistics import (
    build_purged_walk_forward_splits,
    evaluate_statistical_evidence,
    evaluate_walk_forward,
    holm_bonferroni,
    moving_block_bootstrap_mean,
)


def _capital_evidence(**overrides: object) -> CapitalEligibilityEvidence:
    values: dict[str, object] = {
        "candidate_id": "cand_v2",
        "dataset_fingerprint": "rfds_" + "a" * 64,
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "1h",
        "selection_protocol_version": CURRENT_SELECTION_PROTOCOL,
        "benchmark_segment": "valid",
        "candidate_gate_passed": True,
        "development_evidence_passed": True,
        "microstructure_eligible": True,
        "walk_forward_passed": True,
        "statistical_evidence_passed": True,
        "execution_model": CAPITAL_ELIGIBLE_EXECUTION_MODEL,
        "execution_calibration_passed": True,
        "holdout_status": "evaluated_pass",
        "holdout_passed": True,
        "evidence_refs": {
            "candidate": "candidate.json",
            "development": "development.json",
            "microstructure": "microstructure.json",
            "walk_forward": "walk_forward.json",
            "statistics": "statistics.json",
            "l2_execution": "l2.json",
            "execution_calibration": "calibration.json",
            "holdout": "holdout.json",
        },
        "source_contract_lineage": ContractAwareArtifactLineage(
            artifact_output_fingerprint="b" * 64,
            instrument_snapshot_digest="c" * 64,
            instrument_snapshot_source_ref=(
                "meta.data_source_registry/instrument-snapshot-test"
            ),
            verification_ref="meta.historical_research_artifacts/test-artifact",
            symbol="BTC-USDT-SWAP",
            timeframe="1h",
            coverage_start="2026-08-01T00:00:00+00:00",
            coverage_end="2026-08-02T00:00:00+00:00",
            verified=True,
        ),
    }
    values.update(overrides)
    return CapitalEligibilityEvidence(**values)  # type: ignore[arg-type]


def test_capital_eligibility_requires_every_independent_evidence_class(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        capital_eligibility,
        "_SOURCE_AWARE_ARTIFACT_VERIFIER_AVAILABLE",
        True,
    )
    decision = evaluate_capital_eligibility(
        _capital_evidence(symbol="BTC-USDT", source_contract_lineage=None),
        evaluated_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert decision.capital_eligible is True
    assert decision.reason_codes == ()

    failed = evaluate_capital_eligibility(
        replace(
            _capital_evidence(),
            symbol="BTC-USDT",
            source_contract_lineage=None,
            microstructure_eligible=False,
            evidence_refs={"candidate": "candidate.json"},
        )
    )
    assert failed.capital_eligible is False
    assert "microstructure_ineligible_or_unknown" in failed.reason_codes
    assert "evidence_ref_missing:holdout" in failed.reason_codes


def test_derivative_capital_eligibility_requires_verified_contract_lineage() -> None:
    missing = evaluate_capital_eligibility(
        replace(_capital_evidence(), source_contract_lineage=None)
    )
    unverified = evaluate_capital_eligibility(
        replace(
            _capital_evidence(),
            source_contract_lineage=replace(
                _capital_evidence().source_contract_lineage,
                verified=False,
            ),
        )
    )

    assert missing.capital_eligible is False
    assert "contract_aware_derivative_artifact_unavailable" in missing.reason_codes
    assert "source_contract_lineage_missing" in missing.reason_codes
    assert unverified.capital_eligible is False
    assert "source_contract_lineage_unverified" in unverified.reason_codes

    recorded_but_not_authoritative = evaluate_capital_eligibility(
        _capital_evidence()
    )
    assert recorded_but_not_authoritative.capital_eligible is False
    assert "contract_aware_derivative_artifact_unavailable" in (
        recorded_but_not_authoritative.reason_codes
    )


def test_spot_capital_eligibility_requires_source_aware_artifact_verifier() -> None:
    decision = evaluate_capital_eligibility(
        replace(
            _capital_evidence(),
            symbol="BTC-USDT",
            source_contract_lineage=None,
        )
    )

    assert decision.capital_eligible is False
    assert "source_aware_research_artifact_unavailable" in decision.reason_codes
    assert "source_contract_lineage_missing" not in decision.reason_codes


@pytest.mark.parametrize(
    "symbol",
    ("DOGE-USDT", "DOGE-USDT-SWAP", "BTC-USDT-240927"),
)
def test_unknown_instrument_scope_is_never_capital_eligible(symbol: str) -> None:
    decision = evaluate_capital_eligibility(
        replace(
            _capital_evidence(),
            symbol=symbol,
            source_contract_lineage=None,
        )
    )

    assert decision.capital_eligible is False
    assert "instrument_scope_unsupported_or_unproven" in decision.reason_codes
    assert "contract_aware_derivative_artifact_unavailable" not in (
        decision.reason_codes
    )


def test_legacy_candidate_using_test_for_selection_is_never_capital_eligible() -> None:
    reasons = legacy_candidate_reasons(
        {
            "payload": {
                "selection_protocol_version": "legacy_v1",
                "benchmark_segment": "test",
            }
        }
    )
    assert "legacy_test_used_for_selection" in reasons
    assert "selection_protocol_not_v2" in reasons
    assert "holdout_not_evaluated_pass" in reasons


def test_legacy_derivative_candidate_cannot_claim_lineage_from_dataset_fingerprint() -> None:
    reasons = legacy_candidate_reasons(
        {
            "payload": {
                "symbol": "BTC-USDT-SWAP",
                "dataset_fingerprint": "rfds_" + "a" * 64,
                "selection_protocol_version": CURRENT_SELECTION_PROTOCOL,
                "benchmark_segment": "valid",
                "execution_model": CAPITAL_ELIGIBLE_EXECUTION_MODEL,
                "holdout_status": "evaluated_pass",
            }
        }
    )

    assert "source_contract_lineage_missing" in reasons
    assert "contract_aware_derivative_artifact_unavailable" in reasons


def test_legacy_unknown_swap_suffix_does_not_prove_derivative_scope() -> None:
    reasons = legacy_candidate_reasons(
        {
            "payload": {
                "symbol": "DOGE-USDT-SWAP",
                "selection_protocol_version": CURRENT_SELECTION_PROTOCOL,
                "benchmark_segment": "valid",
                "execution_model": CAPITAL_ELIGIBLE_EXECUTION_MODEL,
                "holdout_status": "evaluated_pass",
            }
        }
    )

    assert "instrument_scope_unsupported_or_unproven" in reasons
    assert "contract_aware_derivative_artifact_unavailable" not in reasons
    assert "source_contract_lineage_missing" not in reasons


def test_purged_walk_forward_split_has_no_train_test_overlap() -> None:
    splits = build_purged_walk_forward_splits(
        120,
        initial_train_size=40,
        test_size=15,
        purge_size=3,
        embargo_size=2,
    )
    assert len(splits) >= 3
    assert all(split.train_end + 3 == split.test_start for split in splits)
    assert all(split.train_end <= split.test_start for split in splits)

    returns = [0.001] * 120
    evidence = evaluate_walk_forward(returns, splits)
    assert evidence.passed is True
    assert evidence.positive_fold_ratio == 1.0


def test_walk_forward_fails_when_most_oos_folds_lose_money() -> None:
    splits = build_purged_walk_forward_splits(
        80,
        initial_train_size=20,
        test_size=10,
        purge_size=1,
        embargo_size=1,
    )
    returns = [0.001] * 80
    for split in splits[:-1]:
        returns[split.test_start : split.test_end] = [-0.01] * 10
    evidence = evaluate_walk_forward(returns, splits)
    assert evidence.passed is False
    assert "positive_fold_ratio_below_minimum" in evidence.reason_codes


def test_moving_block_bootstrap_is_deterministic_and_detects_positive_mean() -> None:
    returns = [0.004 + (index % 3) * 0.0001 for index in range(60)]
    first = moving_block_bootstrap_mean(
        returns,
        block_size=5,
        replications=200,
        seed=7,
    )
    second = moving_block_bootstrap_mean(
        returns,
        block_size=5,
        replications=200,
        seed=7,
    )
    assert first == second
    assert first.ci_lower > 0.0


def test_holm_bonferroni_stops_rejection_after_first_failure() -> None:
    adjusted, rejected = holm_bonferroni(
        {"a": 0.001, "b": 0.03, "c": 0.04},
        alpha=0.05,
    )
    assert rejected == {"a": True, "b": False, "c": False}
    assert adjusted["a"] == pytest.approx(0.003)


def test_statistical_evidence_penalizes_multiple_trials() -> None:
    returns = [0.004 + (index % 5) * 0.0002 for index in range(80)]
    evidence = evaluate_statistical_evidence(
        returns,
        candidate_id="candidate_a",
        candidate_p_values={"candidate_a": 0.001, "candidate_b": 0.5},
        trial_count=2,
        periods_per_year=365 * 24 * 4,
        block_size=5,
        replications=200,
        seed=9,
    )
    assert evidence.passed is True
    assert evidence.holm_rejected["candidate_a"] is True
    assert evidence.deflated_sharpe_probability >= 0.95
    assert len(evidence.evidence_fingerprint) == 64
