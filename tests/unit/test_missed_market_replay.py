from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from aats.services.operator.missed_market_replay import (
    BaselineAssessmentRow,
    BucketSummary,
    CostGateCandidateRow,
    DirectionalIntentRow,
    MarketTick,
    TargetEvent,
    bucket_majority_events,
    build_bucket_summaries,
    cost_candidate_events,
    count_budget_reasons,
    parse_replay_timestamp,
    simulate_target_events,
)


def _ts(minute: int) -> datetime:
    return datetime(2026, 5, 6, 12, minute, tzinfo=timezone.utc)


def test_parse_replay_timestamp_requires_timezone() -> None:
    assert parse_replay_timestamp("2026-05-06T20:30:00+08:00").utcoffset() is not None
    assert parse_replay_timestamp("2026-05-06T12:30:00Z").utcoffset() is not None

    with pytest.raises(ValueError, match="timezone"):
        parse_replay_timestamp("2026-05-06T20:30:00")


def test_simulate_target_events_accounts_for_turnover_cost_and_mark_to_market() -> None:
    market = (
        MarketTick(ts=_ts(0), price=Decimal("100")),
        MarketTick(ts=_ts(15), price=Decimal("90")),
        MarketTick(ts=_ts(30), price=Decimal("95")),
    )
    result = simulate_target_events(
        market,
        [TargetEvent(ts=_ts(0), target_qty=Decimal("-1"), cost_bps=Decimal("10"))],
        label="short_episode",
        flatten_at_end=True,
    )

    assert result.target_changes == 2
    assert result.turnover == Decimal("195")
    assert result.gross_pnl == Decimal("5")
    assert result.estimated_cost == Decimal("0.195")
    assert result.net_pnl == Decimal("4.805")
    assert result.end_qty == Decimal("0")


def test_build_bucket_summaries_counts_bias_intents_and_suppression() -> None:
    market = (
        MarketTick(ts=_ts(0), price=Decimal("100")),
        MarketTick(ts=_ts(10), price=Decimal("98")),
        MarketTick(ts=_ts(20), price=Decimal("97")),
    )
    baselines = (
        BaselineAssessmentRow(ts=_ts(1), direction_bias="short"),
        BaselineAssessmentRow(ts=_ts(2), direction_bias="flat"),
        BaselineAssessmentRow(ts=_ts(20), direction_bias="long"),
    )
    intents = (
        _intent(ts=_ts(3), delta=Decimal("-0.01"), suppressed=True),
        _intent(ts=_ts(4), delta=Decimal("0"), suppressed=False),
        _intent(ts=_ts(20), delta=Decimal("0.01"), suppressed=True),
    )

    buckets = build_bucket_summaries(
        market=market,
        baselines=baselines,
        intents=intents,
        bucket_minutes=15,
    )

    assert len(buckets) == 2
    assert buckets[0].price_delta == Decimal("-2")
    assert buckets[0].baseline_short == 1
    assert buckets[0].baseline_flat == 1
    assert buckets[0].intent_short == 1
    assert buckets[0].intent_zero == 1
    assert buckets[0].suppressed == 1
    assert buckets[1].baseline_long == 1
    assert buckets[1].intent_long == 1


def test_bucket_majority_events_emit_low_churn_targets() -> None:
    buckets = (
        BucketSummary(
            bucket=_ts(0),
            first_price=Decimal("100"),
            last_price=Decimal("90"),
            intent_short=3,
            intent_zero=1,
        ),
        BucketSummary(
            bucket=_ts(15),
            first_price=Decimal("90"),
            last_price=Decimal("91"),
            intent_short=1,
            intent_zero=4,
        ),
    )
    events = bucket_majority_events(
        buckets=buckets,
        target_notional=Decimal("1000"),
        cost_bps=Decimal("10"),
    )

    assert [event.target_qty for event in events] == [Decimal("-10"), Decimal("0")]


def test_cost_candidate_events_normalize_sell_target_to_short() -> None:
    events = cost_candidate_events(
        [
            CostGateCandidateRow(
                ts=_ts(0),
                side="sell",
                candidate_target_qty=Decimal("0.25"),
                expected_cost_bps=Decimal("12"),
                signal_edge_bps=Decimal("14"),
                required_edge_bps=Decimal("16"),
            )
        ]
    )

    assert events[0].target_qty == Decimal("-0.25")
    assert events[0].cost_bps == Decimal("12")


def test_count_budget_reasons_counts_each_control_reason() -> None:
    reasons = count_budget_reasons(
        [
            _intent(
                ts=_ts(0),
                delta=Decimal("-0.01"),
                suppressed=True,
                reasons=("pnl_contraction_active", "budget_contracted_to_zero"),
            ),
            _intent(
                ts=_ts(1),
                delta=Decimal("-0.02"),
                suppressed=True,
                reasons=("pnl_contraction_active",),
            ),
        ]
    )

    assert reasons == {
        "budget_contracted_to_zero": 1,
        "pnl_contraction_active": 2,
    }


def _intent(
    *,
    ts: datetime,
    delta: Decimal,
    suppressed: bool,
    reasons: tuple[str, ...] = (),
) -> DirectionalIntentRow:
    behavior = "suppressed_after_approval" if suppressed else "hold_current"
    return DirectionalIntentRow(
        ts=ts,
        requested_target_qty=delta,
        requested_delta_qty=delta,
        target_notional=abs(delta) * Decimal("100000"),
        execution_behavior=behavior,
        execution_control_mode="budget_zero_suppressed" if suppressed else "approved",
        expected_cost_bps=Decimal("11.5"),
        control_reason_codes=reasons,
    )

