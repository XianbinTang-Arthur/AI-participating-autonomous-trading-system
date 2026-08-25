from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from aats.data_platform.execution_realism.l2_event_replay import (
    L2_EVENT_REPLAY_MODEL_VERSION,
    L2OrderBookSnapshot,
    L2OrderRequest,
    L2ReplayPolicy,
    L2TradeEvent,
    OrderBookLevel,
    replay_l2_orders,
)


START = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _levels(values: list[tuple[str, str]]) -> tuple[OrderBookLevel, ...]:
    return tuple(OrderBookLevel(Decimal(price), Decimal(quantity)) for price, quantity in values)


def _snapshot(sequence: int = 1, *, offset_ms: int = 0) -> L2OrderBookSnapshot:
    return L2OrderBookSnapshot(
        symbol="BTC-USDT-SWAP",
        ts=START + timedelta(milliseconds=offset_ms),
        collector_sequence=sequence,
        bids=_levels([("99", "2"), ("98", "3"), ("97", "4")]),
        asks=_levels([("101", "1"), ("102", "2"), ("103", "4")]),
        payload_hash="sha256:" + f"{sequence:064x}",
    )


def _request(
    order_id: str,
    *,
    side: str = "buy",
    kind: str = "market",
    qty: str = "2",
    limit: str | None = None,
    offset_ms: int = 0,
    max_wait_ms: int = 2_000,
) -> L2OrderRequest:
    return L2OrderRequest(
        order_id=order_id,
        symbol="BTC-USDT-SWAP",
        submitted_at=START + timedelta(milliseconds=offset_ms),
        side=side,  # type: ignore[arg-type]
        order_kind=kind,  # type: ignore[arg-type]
        target_quantity=Decimal(qty),
        limit_price=Decimal(limit) if limit is not None else None,
        expected_edge_bps=20.0,
        max_wait_ms=max_wait_ms,
    )


def test_market_order_walks_top5_depth_and_reports_weighted_cost() -> None:
    evidence = replay_l2_orders(
        snapshots=[_snapshot()],
        trades=[],
        requests=[_request("market-1")],
        microstructure_eligibility_fingerprint="micro_" + "a" * 64,
    )
    result = evidence.results[0]
    assert evidence.model_version == L2_EVENT_REPLAY_MODEL_VERSION
    assert result.status == "filled"
    assert result.average_fill_price == Decimal("101.5")
    assert result.fills[0].quantity == Decimal("1")
    assert result.fills[1].quantity == Decimal("1")
    assert result.slippage_bps == pytest.approx(150.0)
    assert result.total_cost_bps == pytest.approx(155.0)


def test_depth_is_shared_and_never_extrapolated_beyond_top5() -> None:
    evidence = replay_l2_orders(
        snapshots=[_snapshot()],
        trades=[],
        requests=[_request("first", qty="5"), _request("second", qty="5")],
        microstructure_eligibility_fingerprint="micro_" + "a" * 64,
    )
    assert evidence.results[0].filled_quantity == Decimal("5")
    assert evidence.results[1].filled_quantity == Decimal("2")
    assert evidence.results[1].status == "partial_fill"
    assert evidence.results[1].reason_code == "top5_depth_exhausted"


def test_ioc_limit_stops_before_price_outside_limit() -> None:
    evidence = replay_l2_orders(
        snapshots=[_snapshot()],
        trades=[],
        requests=[_request("limit", kind="ioc_limit", qty="3", limit="101")],
        microstructure_eligibility_fingerprint="micro_" + "a" * 64,
    )
    result = evidence.results[0]
    assert result.filled_quantity == Decimal("1")
    assert result.status == "partial_fill"


def test_post_only_rejects_marketable_order() -> None:
    evidence = replay_l2_orders(
        snapshots=[_snapshot()],
        trades=[],
        requests=[_request("post-cross", kind="post_only", limit="101")],
        microstructure_eligibility_fingerprint="micro_" + "a" * 64,
    )
    result = evidence.results[0]
    assert result.status == "rejected"
    assert result.reason_code == "post_only_would_cross"


def test_post_only_waits_for_queue_then_partial_fills_from_later_trade() -> None:
    trades = [
        L2TradeEvent(
            symbol="BTC-USDT-SWAP",
            ts=START + timedelta(milliseconds=500),
            trade_id="sell-1",
            price=Decimal("99"),
            quantity=Decimal("3"),
            aggressor_side="sell",
        )
    ]
    evidence = replay_l2_orders(
        snapshots=[_snapshot()],
        trades=trades,
        requests=[_request("post", kind="post_only", qty="2", limit="99")],
        microstructure_eligibility_fingerprint="micro_" + "a" * 64,
    )
    result = evidence.results[0]
    assert result.status == "partial_fill"
    assert result.filled_quantity == Decimal("1")
    assert result.fills[0].liquidity == "maker"
    assert result.reason_code == "passive_queue_partial_fill"


def test_post_only_does_not_use_same_timestamp_or_wrong_aggressor_trade() -> None:
    trades = [
        L2TradeEvent(
            symbol="BTC-USDT-SWAP",
            ts=START,
            trade_id="same-ts",
            price=Decimal("99"),
            quantity=Decimal("100"),
            aggressor_side="sell",
        ),
        L2TradeEvent(
            symbol="BTC-USDT-SWAP",
            ts=START + timedelta(milliseconds=100),
            trade_id="wrong-side",
            price=Decimal("99"),
            quantity=Decimal("100"),
            aggressor_side="buy",
        ),
    ]
    evidence = replay_l2_orders(
        snapshots=[_snapshot()],
        trades=trades,
        requests=[_request("post", kind="post_only", limit="99")],
        microstructure_eligibility_fingerprint="micro_" + "a" * 64,
    )
    assert evidence.results[0].status == "no_fill"
    assert evidence.results[0].reason_code == "passive_queue_not_cleared"


def test_missing_fresh_arrival_snapshot_fails_closed() -> None:
    evidence = replay_l2_orders(
        snapshots=[_snapshot(offset_ms=3_000)],
        trades=[],
        requests=[_request("late")],
        microstructure_eligibility_fingerprint="micro_" + "a" * 64,
    )
    assert evidence.results[0].status == "no_fill"
    assert evidence.results[0].reason_code == "fresh_arrival_snapshot_missing"


def test_fingerprint_is_deterministic_and_summary_is_l2_identified() -> None:
    kwargs = {
        "snapshots": [_snapshot()],
        "trades": [],
        "requests": [_request("market-1")],
        "microstructure_eligibility_fingerprint": "micro_" + "a" * 64,
        "policy": L2ReplayPolicy(),
    }
    first = replay_l2_orders(**kwargs)
    second = replay_l2_orders(**kwargs)
    assert first.evidence_fingerprint == second.evidence_fingerprint
    summary = first.execution_cost_summary(
        plan_id="v2replay_example",
        timeframe="15m",
        benchmark_segment="valid",
        dataset_fingerprint="rfds_" + "b" * 64,
    )
    assert summary["model_version"] == L2_EVENT_REPLAY_MODEL_VERSION
    assert summary["plan_id"] == "v2replay_example"
    assert summary["l2_execution_evidence_fingerprint"] == first.evidence_fingerprint
