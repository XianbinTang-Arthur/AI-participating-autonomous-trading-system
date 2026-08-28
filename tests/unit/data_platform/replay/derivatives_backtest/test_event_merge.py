from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from aats.data_platform.replay.derivatives_backtest.contracts import (
    DerivativesBacktestContractError,
)
from aats.data_platform.replay.derivatives_backtest.event_merge import (
    DerivedLiquidationBarrierV1,
    EngineBarrierKindV1,
    engine_step_order_key,
    expand_engine_barriers,
    merge_derivative_event_streams,
)
from aats.data_platform.replay.derivatives_backtest.events import (
    DERIVATIVES_MAX_SOURCE_SEQUENCE,
    BarCloseEventV1,
    ContractTierEffectiveEventV1,
    DerivativeEventKindV1,
    FundingSettlementEventV1,
    MarkPriceEventV1,
    TradableEventV1,
    event_order_key,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_refs import (
    DerivativesSnapshotRefsV1,
)
from tests.unit.data_platform.replay.derivatives_backtest._event_helpers import (
    BASE_TS,
    all_events,
    snapshot_refs,
    source_ref,
)


def opening_refs_before_base() -> DerivativesSnapshotRefsV1:
    current = snapshot_refs()
    old = [
        replace(
            ref,
            snapshot_id=f"00000000-0000-4000-8000-{ordinal:012d}",
            effective_from=BASE_TS - timedelta(hours=1),
            effective_to=BASE_TS,
        )
        for ordinal, ref in enumerate(
            (
                current.instrument,
                current.position_tier,
                current.execution_fee,
                current.funding_schedule,
            ),
            start=101,
        )
    ]
    return DerivativesSnapshotRefsV1(
        instrument=old[0],
        position_tier=old[1],
        execution_fee=old[2],
        funding_schedule=old[3],
    )


def expand(events, *, opening_refs=None):
    return expand_engine_barriers(
        events,
        opening_snapshot_refs=opening_refs or snapshot_refs(),
    )


def streams_for(events: dict[str, object]):
    return {
        DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE: [events["contract"]],
        DerivativeEventKindV1.INDEX_PRICE: [events["index"]],
        DerivativeEventKindV1.MARK_PRICE: [events["mark"]],
        DerivativeEventKindV1.FUNDING_SETTLEMENT: [events["funding"]],
        DerivativeEventKindV1.TRADABLE: [events["tradable"]],
        DerivativeEventKindV1.BAR_CLOSE: [events["bar"]],
    }


def test_merge_uses_fixed_phase_order_independent_of_mapping_order() -> None:
    events = all_events()
    streams = streams_for(events)
    reversed_streams = dict(reversed(list(streams.items())))

    first = list(merge_derivative_event_streams(streams))
    second = list(merge_derivative_event_streams(reversed_streams))

    assert [event.header.event_id for event in first] == [
        event.header.event_id for event in second
    ]
    assert [event_order_key(event)[1] for event in first] == [5, 10, 20, 30, 50, 60]


def test_merge_rejects_missing_stream_even_when_empty_would_be_convenient() -> None:
    streams = streams_for(all_events())
    streams.pop(DerivativeEventKindV1.FUNDING_SETTLEMENT)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(merge_derivative_event_streams(streams))

    assert exc_info.value.code == "event_stream_set_invalid"


def test_merge_rejects_wrong_event_type_in_stream() -> None:
    streams = streams_for(all_events())
    streams[DerivativeEventKindV1.INDEX_PRICE] = [all_events()["mark"]]

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(merge_derivative_event_streams(streams))

    assert exc_info.value.code == "event_stream_type_mismatch"


def test_merge_revalidates_hash_bound_economic_body() -> None:
    streams = streams_for(all_events())
    tradable = streams[DerivativeEventKindV1.TRADABLE][0]
    object.__setattr__(tradable, "reference_price", Decimal("999999"))

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(merge_derivative_event_streams(streams))

    assert exc_info.value.code == "event_id_mismatch"


def test_barrier_expander_revalidates_direct_event_input() -> None:
    mark = all_events()["mark"]
    object.__setattr__(mark, "price", Decimal("999999"))

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(expand([mark]))

    assert exc_info.value.code == "event_id_mismatch"


def test_merge_maps_non_iterable_stream_to_stable_failure() -> None:
    streams = streams_for(all_events())
    streams[DerivativeEventKindV1.INDEX_PRICE] = None

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(merge_derivative_event_streams(streams))

    assert exc_info.value.code == "event_stream_iterable_invalid"


def test_merge_rejects_local_sequence_regression_without_sorting() -> None:
    first = MarkPriceEventV1.create(
        ts=BASE_TS,
        source_sequence=2,
        source_ref=source_ref("mark"),
        price=Decimal("50000"),
    )
    second = MarkPriceEventV1.create(
        ts=BASE_TS,
        source_sequence=1,
        source_ref=source_ref("mark"),
        price=Decimal("50001"),
    )
    streams = streams_for(all_events())
    streams[DerivativeEventKindV1.MARK_PRICE] = [first, second]

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(merge_derivative_event_streams(streams))

    assert exc_info.value.code == "event_stream_order_invalid"


@pytest.mark.parametrize(
    ("kind", "name", "second"),
    [
        (
            DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE,
            "contract",
            lambda: ContractTierEffectiveEventV1.create(
                ts=BASE_TS,
                source_sequence=2,
                source_ref=source_ref("contract"),
                snapshot_refs=all_events()["contract"].snapshot_refs,
            ),
        ),
        (
            DerivativeEventKindV1.BAR_CLOSE,
            "bar",
            lambda: BarCloseEventV1.create(
                ts=BASE_TS,
                source_sequence=2,
                source_ref=source_ref("bar"),
                bar_start_ts=BASE_TS - timedelta(minutes=15),
                bar_end_ts=BASE_TS,
                open_price=Decimal("1"),
                high_price=Decimal("2"),
                low_price=Decimal("0.5"),
                close_price=Decimal("1.5"),
                volume_contracts=Decimal("1"),
                feature_ref=source_ref("feature"),
            ),
        ),
    ],
)
def test_merge_rejects_singleton_event_duplicates_per_timestamp(
    kind,
    name: str,
    second,
) -> None:
    streams = streams_for(all_events())
    streams[kind] = [all_events()[name], second()]

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(merge_derivative_event_streams(streams))

    assert exc_info.value.code == "event_timestamp_cardinality_invalid"


def test_barrier_expansion_matches_frozen_phase_order() -> None:
    merged = merge_derivative_event_streams(streams_for(all_events()))

    steps = list(expand(merged, opening_refs=opening_refs_before_base()))

    assert [engine_step_order_key(step)[1] for step in steps] == [
        5,
        10,
        20,
        30,
        40,
        50,
        55,
        60,
    ]
    barriers = [step for step in steps if type(step) is DerivedLiquidationBarrierV1]
    assert [barrier.kind for barrier in barriers] == [
        EngineBarrierKindV1.PRE_FILL_LIQUIDATION,
        EngineBarrierKindV1.POST_FILL_LIQUIDATION,
    ]


def test_mark_only_timestamp_still_gets_prefill_liquidation_barrier() -> None:
    mark = MarkPriceEventV1.create(
        ts=BASE_TS,
        source_sequence=1,
        source_ref=source_ref("mark"),
        price=Decimal("50000"),
    )

    steps = list(expand([mark]))

    assert steps[0] == mark
    assert type(steps[1]) is DerivedLiquidationBarrierV1
    assert steps[1].kind is EngineBarrierKindV1.PRE_FILL_LIQUIDATION


def test_funding_precedes_derived_prefill_barrier() -> None:
    events = all_events()
    steps = list(expand([events["funding"], events["tradable"]]))

    assert steps[0] == events["funding"]
    assert type(steps[1]) is DerivedLiquidationBarrierV1
    assert steps[1].kind is EngineBarrierKindV1.PRE_FILL_LIQUIDATION
    assert steps[2] == events["tradable"]
    assert type(steps[3]) is DerivedLiquidationBarrierV1
    assert steps[3].kind is EngineBarrierKindV1.POST_FILL_LIQUIDATION


def test_funding_event_must_match_active_schedule_without_activation() -> None:
    active = snapshot_refs()
    other_schedule = replace(
        active.funding_schedule,
        snapshot_id="00000000-0000-4000-8000-000000000904",
    )
    funding = FundingSettlementEventV1.create(
        ts=BASE_TS,
        source_sequence=1,
        source_ref=source_ref("funding"),
        rate=Decimal("0.0001"),
        schedule_ref=other_schedule,
        observed_at_ts=BASE_TS,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(expand([funding], opening_refs=active))

    assert exc_info.value.code == "funding_event_active_schedule_mismatch"


def test_funding_event_rejects_old_schedule_after_atomic_activation() -> None:
    incoming = all_events()["contract"]
    opening = opening_refs_before_base()
    funding = FundingSettlementEventV1.create(
        ts=BASE_TS,
        source_sequence=1,
        source_ref=source_ref("funding"),
        rate=Decimal("0.0001"),
        schedule_ref=opening.funding_schedule,
        observed_at_ts=BASE_TS,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(expand([incoming, funding], opening_refs=opening))

    assert exc_info.value.code == "snapshot_not_effective_at_timestamp"


def test_multiple_tradable_events_at_same_timestamp_fail_closed() -> None:
    first = all_events()["tradable"]
    second = TradableEventV1.create(
        ts=BASE_TS,
        source_sequence=2,
        source_ref=source_ref("tradable"),
        reference_price=Decimal("50003"),
        available_contracts=Decimal("1"),
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(expand([first, second]))

    assert exc_info.value.code == "multiple_tradable_events_per_timestamp"


@pytest.mark.parametrize("event_name", ["funding", "bar"])
def test_expander_rejects_all_singleton_event_duplicates(
    event_name: str,
) -> None:
    first = all_events()[event_name]
    if event_name == "funding":
        second = FundingSettlementEventV1.create(
            ts=BASE_TS,
            source_sequence=2,
            source_ref=source_ref("funding"),
            rate=Decimal("0.0002"),
            schedule_ref=snapshot_refs().funding_schedule,
            observed_at_ts=BASE_TS,
        )
    else:
        second = BarCloseEventV1.create(
            ts=BASE_TS,
            source_sequence=2,
            source_ref=source_ref("bar"),
            bar_start_ts=BASE_TS - timedelta(minutes=15),
            bar_end_ts=BASE_TS,
            open_price=Decimal("1"),
            high_price=Decimal("2"),
            low_price=Decimal("0.5"),
            close_price=Decimal("1.6"),
            volume_contracts=Decimal("1"),
            feature_ref=source_ref("feature"),
        )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(expand([first, second]))

    assert exc_info.value.code == "event_timestamp_cardinality_invalid"


def test_barrier_ids_are_deterministic_and_source_bound() -> None:
    event = all_events()["mark"]

    first = list(expand([event]))[1]
    second = list(expand([event]))[1]

    assert first == second
    assert first.barrier_id == second.barrier_id


def test_expander_freezes_mark_barrier_before_external_yield() -> None:
    generator = expand([all_events()["mark"]])
    yielded_mark = next(generator)
    original_sequence = yielded_mark.header.source_sequence

    object.__setattr__(yielded_mark.header, "source_sequence", 999)
    barrier = next(generator)

    assert type(barrier) is DerivedLiquidationBarrierV1
    assert barrier.trigger_source_key[2] == original_sequence


def test_expander_freezes_active_snapshot_before_contract_yield() -> None:
    opening = opening_refs_before_base()
    expected_fingerprint = all_events()["contract"].snapshot_refs.fingerprint
    generator = expand(
        [all_events()["contract"]],
        opening_refs=opening,
    )
    yielded_contract = next(generator)

    object.__setattr__(
        yielded_contract.snapshot_refs.funding_schedule,
        "raw_sha256",
        "0" * 64,
    )
    barrier = next(generator)

    assert type(barrier) is DerivedLiquidationBarrierV1
    assert barrier.snapshot_set_fingerprint == expected_fingerprint


def test_expander_freezes_post_fill_barrier_before_tradable_yield() -> None:
    generator = expand([all_events()["tradable"]])
    pre_fill = next(generator)
    yielded_tradable = next(generator)
    original_sequence = yielded_tradable.header.source_sequence

    object.__setattr__(yielded_tradable.header, "source_sequence", 999)
    post_fill = next(generator)

    assert pre_fill.kind is EngineBarrierKindV1.PRE_FILL_LIQUIDATION
    assert post_fill.kind is EngineBarrierKindV1.POST_FILL_LIQUIDATION
    assert post_fill.trigger_source_key[2] == original_sequence


def test_barrier_rejects_trigger_sequence_outside_frozen_wire_range() -> None:
    barrier = list(expand([all_events()["mark"]]))[1]
    invalid_key = (
        barrier.ts,
        barrier.trigger_source_key[1],
        DERIVATIVES_MAX_SOURCE_SEQUENCE + 1,
        barrier.trigger_source_key[3],
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(barrier, trigger_source_key=invalid_key)

    assert exc_info.value.code == "integer_out_of_bounds"


def test_post_fill_barrier_requires_tradable_trigger_phase() -> None:
    barrier = list(expand([all_events()["tradable"]]))[-1]
    invalid_key = (
        barrier.ts,
        20,
        barrier.trigger_source_key[2],
        barrier.trigger_source_key[3],
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(barrier, trigger_source_key=invalid_key)

    assert exc_info.value.code == "post_fill_barrier_trigger_phase_invalid"


def test_barrier_id_is_bound_to_snapshot_set() -> None:
    event = all_events()["mark"]

    first = list(expand([event]))[1]
    second = list(
        expand_engine_barriers(
            [event],
            opening_snapshot_refs=snapshot_refs(
                effective_from=BASE_TS - timedelta(microseconds=1)
            ),
        )
    )[1]

    assert first.barrier_id != second.barrier_id


def test_expander_revalidates_nested_opening_snapshot_reference() -> None:
    opening = snapshot_refs()
    object.__setattr__(opening.funding_schedule, "raw_sha256", "NOT-A-SHA")

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(expand([all_events()["mark"]], opening_refs=opening))

    assert exc_info.value.code == "sha256_non_canonical"


def test_contract_activation_updates_barrier_snapshot_identity() -> None:
    events = all_events()
    opening_refs = opening_refs_before_base()

    steps = list(
        expand_engine_barriers(
            [events["contract"], events["mark"]],
            opening_snapshot_refs=opening_refs,
        )
    )
    barrier = steps[-1]

    assert type(barrier) is DerivedLiquidationBarrierV1
    assert barrier.snapshot_set_fingerprint == events["contract"].snapshot_refs.fingerprint
    assert barrier.snapshot_set_fingerprint != opening_refs.fingerprint


def test_expander_rejects_expired_active_snapshot_without_activation() -> None:
    expired = opening_refs_before_base()
    mark = MarkPriceEventV1.create(
        ts=BASE_TS + timedelta(seconds=1),
        source_sequence=1,
        source_ref=source_ref("mark"),
        price=Decimal("50000"),
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(expand([mark], opening_refs=expired))

    assert exc_info.value.code == "snapshot_not_effective_at_timestamp"


def test_expander_rejects_global_regression() -> None:
    later = MarkPriceEventV1.create(
        ts=BASE_TS + timedelta(seconds=1),
        source_sequence=1,
        source_ref=source_ref("mark"),
        price=Decimal("50000"),
    )
    earlier = all_events()["mark"]

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        list(expand([later, earlier]))

    assert exc_info.value.code == "event_global_order_invalid"
