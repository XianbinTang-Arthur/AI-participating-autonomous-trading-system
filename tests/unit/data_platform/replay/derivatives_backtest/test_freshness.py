from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from aats.data_platform.replay.derivatives_backtest.contracts import (
    DerivativesBacktestContractError,
)
from aats.data_platform.replay.derivatives_backtest.events import (
    IndexPriceEventV1,
    MarkPriceEventV1,
)
from aats.data_platform.replay.derivatives_backtest.freshness import (
    PriceFreshnessStateV1,
    PriceObservationV1,
    apply_price_event,
    empty_price_freshness_state,
    require_end_valuation_prices,
    require_prices_as_of,
)
from tests.unit.data_platform.replay.derivatives_backtest._event_helpers import (
    BASE_TS,
    source_ref,
)


def index_event(*, ts=BASE_TS, sequence: int = 1) -> IndexPriceEventV1:
    return IndexPriceEventV1.create(
        ts=ts,
        source_sequence=sequence,
        source_ref=source_ref("index"),
        price=Decimal("50000"),
    )


def mark_event(*, ts=BASE_TS, sequence: int = 1) -> MarkPriceEventV1:
    return MarkPriceEventV1.create(
        ts=ts,
        source_sequence=sequence,
        source_ref=source_ref("mark"),
        price=Decimal("50001"),
    )


def complete_state() -> PriceFreshnessStateV1:
    state = apply_price_event(empty_price_freshness_state(), index_event())
    return apply_price_event(state, mark_event())


def test_freshness_uses_event_time_and_accepts_exact_sixty_seconds() -> None:
    pair = require_prices_as_of(
        complete_state(),
        use_ts=BASE_TS + timedelta(seconds=60),
    )

    assert pair.mark.price == Decimal("50001")
    assert pair.index.price == Decimal("50000")


@pytest.mark.parametrize("kind", ["mark", "index"])
def test_freshness_rejects_each_missing_independent_source(kind: str) -> None:
    state = empty_price_freshness_state()
    if kind == "mark":
        state = apply_price_event(state, index_event())
    else:
        state = apply_price_event(state, mark_event())

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        require_prices_as_of(state, use_ts=BASE_TS)

    assert exc_info.value.code == f"{kind}_price_missing"


@pytest.mark.parametrize("kind", ["mark", "index"])
def test_freshness_rejects_sixty_seconds_plus_one_microsecond(kind: str) -> None:
    if kind == "mark":
        state = apply_price_event(empty_price_freshness_state(), mark_event())
        state = apply_price_event(
            state,
            index_event(ts=BASE_TS + timedelta(microseconds=1)),
        )
    else:
        state = apply_price_event(empty_price_freshness_state(), index_event())
        state = apply_price_event(
            state,
            mark_event(ts=BASE_TS + timedelta(microseconds=1)),
        )
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        require_prices_as_of(
            state,
            use_ts=BASE_TS + timedelta(seconds=60, microseconds=1),
        )

    assert exc_info.value.code == f"{kind}_price_stale"


def test_freshness_rejects_future_observation() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        require_prices_as_of(
            complete_state(),
            use_ts=BASE_TS - timedelta(microseconds=1),
        )

    assert exc_info.value.code == "mark_price_from_future"


def test_price_cursor_allows_same_timestamp_higher_sequence() -> None:
    state = apply_price_event(empty_price_freshness_state(), mark_event(sequence=1))

    updated = apply_price_event(state, mark_event(sequence=2))

    assert updated.latest_mark is not None
    assert updated.latest_mark.source_sequence == 2


def test_price_cursor_rejects_same_or_regressed_source_key() -> None:
    state = apply_price_event(empty_price_freshness_state(), mark_event(sequence=2))

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        apply_price_event(state, mark_event(sequence=1))

    assert exc_info.value.code == "mark_price_cursor_regressed"


def test_apply_revalidates_existing_cursor_before_order_comparison() -> None:
    state = apply_price_event(
        empty_price_freshness_state(),
        index_event(sequence=10),
    )
    assert state.latest_index is not None
    object.__setattr__(state.latest_index.event.header, "source_sequence", 0)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        apply_price_event(state, index_event(sequence=5))

    assert exc_info.value.code == "event_id_mismatch"


def test_apply_maps_mutated_cursor_timestamp_to_stable_error() -> None:
    state = apply_price_event(empty_price_freshness_state(), index_event())
    assert state.latest_index is not None
    object.__setattr__(state.latest_index.event.header, "ts", "bad")

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        apply_price_event(state, index_event(sequence=2))

    assert exc_info.value.code == "timestamp_utc_required"


def test_end_valuation_requires_both_observations_strictly_before_end() -> None:
    end_ts = BASE_TS + timedelta(seconds=30)

    pair = require_end_valuation_prices(complete_state(), end_ts=end_ts)

    assert pair.mark.ts < end_ts
    assert pair.index.ts < end_ts


def test_end_valuation_rejects_observation_at_end_boundary() -> None:
    end_ts = BASE_TS + timedelta(seconds=30)
    state = apply_price_event(empty_price_freshness_state(), index_event(ts=end_ts))
    state = apply_price_event(state, mark_event(ts=end_ts))

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        require_end_valuation_prices(state, end_ts=end_ts)

    assert exc_info.value.code == "end_mark_timestamp_invalid"


def test_price_observation_retains_hash_bound_event_identity() -> None:
    event = mark_event()

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(event, price=Decimal("999999"))

    assert exc_info.value.code == "event_id_mismatch"


def test_price_state_rejects_wrong_source_role() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        PriceFreshnessStateV1(
            latest_mark=PriceObservationV1(event=index_event()),
            latest_index=None,
        )

    assert exc_info.value.code == "mark_price_cursor_invalid"


def test_fresh_pair_replace_revalidates_freshness() -> None:
    pair = require_prices_as_of(complete_state(), use_ts=BASE_TS)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(pair, use_ts=BASE_TS + timedelta(seconds=61))

    assert exc_info.value.code == "mark_price_stale"


def test_freshness_consumer_revalidates_mutated_nested_event() -> None:
    state = complete_state()
    assert state.latest_mark is not None
    object.__setattr__(state.latest_mark.event, "price", Decimal("999999"))

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        require_prices_as_of(state, use_ts=BASE_TS)

    assert exc_info.value.code == "event_id_mismatch"


def test_fresh_pair_does_not_alias_caller_owned_event_graph() -> None:
    original_mark = mark_event()
    original_index = index_event()
    state = apply_price_event(empty_price_freshness_state(), original_index)
    state = apply_price_event(state, original_mark)
    pair = require_prices_as_of(state, use_ts=BASE_TS)

    object.__setattr__(original_mark, "price", Decimal("1"))
    object.__setattr__(original_index.header.source_ref, "stream_id", "mutated")

    assert pair.mark.price == Decimal("50001")
    assert pair.index.price == Decimal("50000")
    assert pair.index.event.header.source_ref.stream_id == "index-price"
