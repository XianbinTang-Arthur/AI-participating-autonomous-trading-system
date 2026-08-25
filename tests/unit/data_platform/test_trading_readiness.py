from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aats.data_platform.operations.trading_readiness import (
    COMMON_REQUIRED_FACTS,
    FUTURE_CANARY_REQUIRED_FACTS,
    ReadinessFact,
    evaluate_trading_readiness,
)


NOW = datetime(2026, 8, 25, 14, tzinfo=UTC)


def _facts(names: tuple[str, ...]) -> list[ReadinessFact]:
    return [
        ReadinessFact(
            name=name,
            status="PASS",
            evidence_ref=f"evidence/{name}.json",
            observed_at=NOW,
            max_age_seconds=300,
        )
        for name in names
    ]


def _evaluate(facts: list[ReadinessFact]):
    return evaluate_trading_readiness(
        target="simulation",
        profile="derivatives",
        git_commit="a" * 40,
        image_identity="sha256:" + "b" * 64,
        schema_revision="batch_b_16_profit_readiness_governance",
        facts=facts,
        evaluated_at=NOW,
    )


def test_complete_simulation_evidence_is_simulation_ready_only() -> None:
    evidence = _evaluate(_facts(COMMON_REQUIRED_FACTS))
    assert evidence.simulation_ready is True
    assert evidence.production_ready is False
    assert evidence.trading_ready is False


def test_unknown_missing_or_degraded_fact_fails_closed() -> None:
    facts = _facts(COMMON_REQUIRED_FACTS[:-1])
    facts[0] = ReadinessFact(
        name=facts[0].name,
        status="DEGRADED",
        evidence_ref="evidence/degraded.json",
        observed_at=NOW,
    )
    evidence = _evaluate(facts)
    assert evidence.simulation_ready is False
    assert "fact_degraded:git_revision" in evidence.reason_codes
    assert "fact_missing:order_reconciliation" in evidence.reason_codes


def test_stale_fact_fails_closed() -> None:
    facts = _facts(COMMON_REQUIRED_FACTS)
    facts[3] = ReadinessFact(
        name=facts[3].name,
        status="PASS",
        evidence_ref="evidence/stale.json",
        observed_at=NOW - timedelta(seconds=301),
        max_age_seconds=300,
    )
    evidence = _evaluate(facts)
    assert evidence.simulation_ready is False
    assert f"fact_stale:{facts[3].name}" in evidence.reason_codes


def test_future_or_unbounded_fact_fails_closed() -> None:
    facts = _facts(COMMON_REQUIRED_FACTS)
    facts[1] = ReadinessFact(
        name=facts[1].name,
        status="PASS",
        evidence_ref="evidence/future.json",
        observed_at=NOW + timedelta(seconds=6),
        max_age_seconds=300,
    )
    facts[2] = ReadinessFact(
        name=facts[2].name,
        status="PASS",
        evidence_ref="evidence/unbounded.json",
        observed_at=NOW,
    )
    evidence = _evaluate(facts)
    assert evidence.simulation_ready is False
    assert f"fact_observed_in_future:{facts[1].name}" in evidence.reason_codes
    assert f"fact_max_age_missing:{facts[2].name}" in evidence.reason_codes


def test_future_canary_is_hard_disabled_in_v1_even_with_all_facts() -> None:
    evidence = evaluate_trading_readiness(
        target="future_canary",
        profile="future_derivatives_canary",
        git_commit="a" * 40,
        image_identity="sha256:" + "b" * 64,
        schema_revision="batch_b_16_profit_readiness_governance",
        facts=_facts(FUTURE_CANARY_REQUIRED_FACTS),
        evaluated_at=NOW,
    )
    assert evidence.production_ready is False
    assert evidence.trading_ready is False
    assert "future_canary_activation_not_implemented" in evidence.reason_codes


def test_evidence_fingerprint_does_not_change_with_evaluation_time() -> None:
    first = _evaluate(_facts(COMMON_REQUIRED_FACTS))
    second = evaluate_trading_readiness(
        target="simulation",
        profile="derivatives",
        git_commit="a" * 40,
        image_identity="sha256:" + "b" * 64,
        schema_revision="batch_b_16_profit_readiness_governance",
        facts=_facts(COMMON_REQUIRED_FACTS),
        evaluated_at=NOW + timedelta(seconds=1),
    )
    assert first.evidence_fingerprint == second.evidence_fingerprint
