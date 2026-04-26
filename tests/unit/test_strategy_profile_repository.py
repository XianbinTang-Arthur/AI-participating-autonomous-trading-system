from __future__ import annotations

from datetime import timedelta

from aats.schemas.common import utc_now
from aats.schemas.strategy_profiles import StrategyProfileMarketRegimeAssessment, StrategyProfileRecommendation
from aats.storage.strategy_profile_repo import InMemoryStrategyProfileRepository


def _recommendation(*, recommendation_id: str, expires_delta: timedelta) -> StrategyProfileRecommendation:
    return StrategyProfileRecommendation(
        recommendation_id=recommendation_id,
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols=("BTC-USDT-SWAP",),
        active_profile_id="trend_normal",
        recommended_profile_id="trend_normal",
        confidence=0.7,
        market_regime_assessment=StrategyProfileMarketRegimeAssessment(
            regime="trend",
            volatility_state="normal",
            execution_condition="normal",
        ),
        reason_codes=["test"],
        human_summary="test recommendation",
        risk_notes=[],
        valid_for_minutes=30,
        generated_by="test",
        input_digest=recommendation_id,
        input_snapshot={},
        expires_at=utc_now() + expires_delta,
    )


def test_in_memory_repo_expires_only_matching_pending_recommendations() -> None:
    repo = InMemoryStrategyProfileRepository()
    expired = repo.save_recommendation(
        _recommendation(recommendation_id="expired_pending", expires_delta=timedelta(minutes=-1))
    )
    fresh = repo.save_recommendation(
        _recommendation(recommendation_id="fresh_pending", expires_delta=timedelta(minutes=10))
    )
    accepted = repo.save_recommendation(
        _recommendation(recommendation_id="expired_accepted", expires_delta=timedelta(minutes=-1)).model_copy(
            update={"decision_status": "accepted"}
        )
    )

    changed = repo.expire_pending_recommendations(
        product_type="derivatives",
        margin_mode="cross",
        allowed_symbols=("BTC-USDT-SWAP",),
        now=utc_now(),
    )

    assert changed == 1
    assert repo.get_recommendation(expired.recommendation_id).decision_status == "expired"
    assert repo.get_recommendation(expired.recommendation_id).decision_reason_code == "recommendation_expired"
    assert repo.get_recommendation(fresh.recommendation_id).decision_status == "pending"
    assert repo.get_recommendation(accepted.recommendation_id).decision_status == "accepted"
