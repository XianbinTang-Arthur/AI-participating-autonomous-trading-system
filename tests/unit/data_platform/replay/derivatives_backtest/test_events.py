from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from aats.data_platform.replay.derivatives_backtest.contracts import (
    DerivativesBacktestContractError,
    LiquidityRoleV1,
)
from aats.data_platform.replay.derivatives_backtest.events import (
    DERIVATIVES_MAX_SOURCE_SEQUENCE,
    BarCloseEventV1,
    DerivativeEventKindV1,
    EVENT_PHASE_PRIORITY_V1,
    FundingSettlementEventV1,
    IndexPriceEventV1,
    MarkPriceEventV1,
    TradableEventV1,
    event_order_key,
    parse_derivative_replay_event,
)
from tests.unit.data_platform.replay.derivatives_backtest._event_helpers import (
    BASE_TS,
    all_events,
    snapshot_refs,
    source_ref,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_refs import (
    DerivativesSnapshotRefsV1,
)


@pytest.mark.parametrize(
    "name",
    ["contract", "index", "mark", "funding", "tradable", "bar"],
)
def test_every_event_round_trips_with_content_derived_identity(name: str) -> None:
    event = all_events()[name]

    restored = parse_derivative_replay_event(event.to_dict())

    assert restored == event
    assert restored.to_dict() == event.to_dict()


def test_event_id_changes_when_economic_content_changes() -> None:
    first = MarkPriceEventV1.create(
        ts=BASE_TS,
        source_sequence=1,
        source_ref=source_ref("mark"),
        price=Decimal("50000"),
    )
    second = MarkPriceEventV1.create(
        ts=BASE_TS,
        source_sequence=1,
        source_ref=source_ref("mark"),
        price=Decimal("50001"),
    )

    assert first.header.event_id != second.header.event_id


def test_event_parser_rejects_tampered_content_derived_id() -> None:
    payload = all_events()["mark"].to_dict()
    payload["price"] = "50002e0"

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        parse_derivative_replay_event(payload)

    assert exc_info.value.code in {
        "economic_decimal_non_canonical",
        "event_id_mismatch",
    }


def test_event_parser_rejects_unknown_field() -> None:
    payload = all_events()["index"].to_dict()
    payload["trusted"] = True

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        parse_derivative_replay_event(payload)

    assert exc_info.value.code == "event_shape_invalid"


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-01-02T00:15:00Z",
        "2026-01-02T00:15:00.000000+00:00",
        "2026-01-01T19:15:00.000000-05:00",
    ],
)
def test_event_parser_rejects_noncanonical_timestamp(timestamp: str) -> None:
    payload = all_events()["index"].to_dict()
    payload["ts"] = timestamp

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        parse_derivative_replay_event(payload)

    assert exc_info.value.code == "timestamp_non_canonical"


def test_event_parser_rejects_bool_sequence() -> None:
    payload = all_events()["index"].to_dict()
    payload["source_sequence"] = True

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        parse_derivative_replay_event(payload)

    assert exc_info.value.code == "integer_out_of_bounds"


def test_event_accepts_frozen_signed_int64_sequence_boundary() -> None:
    event = MarkPriceEventV1.create(
        ts=BASE_TS,
        source_sequence=DERIVATIVES_MAX_SOURCE_SEQUENCE,
        source_ref=source_ref("mark"),
        price=Decimal("50000"),
    )

    assert event.header.source_sequence == DERIVATIVES_MAX_SOURCE_SEQUENCE


@pytest.mark.parametrize(
    "source_sequence",
    [DERIVATIVES_MAX_SOURCE_SEQUENCE + 1, 10**5000],
    ids=["int64-plus-one", "huge-integer"],
)
def test_event_create_rejects_sequence_above_frozen_wire_range(
    source_sequence: int,
) -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        MarkPriceEventV1.create(
            ts=BASE_TS,
            source_sequence=source_sequence,
            source_ref=source_ref("mark"),
            price=Decimal("50000"),
        )

    assert exc_info.value.code == "integer_out_of_bounds"


def test_event_create_revalidates_nested_source_reference_before_hashing() -> None:
    ref = source_ref("mark")
    object.__setattr__(ref, "parent_artifact_sha256", "BAD")

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        MarkPriceEventV1.create(
            ts=BASE_TS,
            source_sequence=1,
            source_ref=ref,
            price=Decimal("50000"),
        )

    assert exc_info.value.code == "sha256_non_canonical"


def test_event_replace_revalidates_mutated_header_sequence() -> None:
    event = all_events()["mark"]
    object.__setattr__(
        event.header,
        "source_sequence",
        DERIVATIVES_MAX_SOURCE_SEQUENCE + 1,
    )

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        replace(event)

    assert exc_info.value.code == "integer_out_of_bounds"


def test_events_defensively_copy_nested_source_and_snapshot_refs() -> None:
    mark_source = source_ref("mark")
    mark = MarkPriceEventV1.create(
        ts=BASE_TS,
        source_sequence=1,
        source_ref=mark_source,
        price=Decimal("50000"),
    )
    refs = snapshot_refs()
    contract = type(all_events()["contract"]).create(
        ts=BASE_TS,
        source_sequence=1,
        source_ref=source_ref("contract"),
        snapshot_refs=refs,
    )
    original_source_digest = mark.header.source_ref.parent_artifact_sha256
    original_snapshot_digest = contract.snapshot_refs.instrument.raw_sha256

    object.__setattr__(mark_source, "parent_artifact_sha256", "0" * 64)
    object.__setattr__(refs.instrument, "raw_sha256", "0" * 64)

    assert mark.header.source_ref.parent_artifact_sha256 == original_source_digest
    assert contract.snapshot_refs.instrument.raw_sha256 == original_snapshot_digest


def test_event_parser_rejects_json_number_for_economic_decimal() -> None:
    payload = all_events()["mark"].to_dict()
    payload["price"] = 50000

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        parse_derivative_replay_event(payload)

    assert exc_info.value.code == "economic_decimal_wire_type_invalid"


def test_contract_activation_requires_atomic_effective_timestamp() -> None:
    refs = snapshot_refs(effective_from=BASE_TS)
    payload = all_events()["contract"].to_dict()
    payload["snapshot_refs"]["execution_fee"]["effective_window"]["start"] = (
        "2026-01-02T00:15:00.000001Z"
    )
    # The nested identity mismatch is detected before it could be used as a
    # schedule switch; callers cannot repair it with a new event ID.
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        parse_derivative_replay_event(payload)

    assert exc_info.value.code in {
        "event_id_mismatch",
        "snapshot_not_effective_at_activation",
    }
    assert refs.execution_fee.effective_from == BASE_TS


def test_funding_rejects_future_observation() -> None:
    refs = snapshot_refs()

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        FundingSettlementEventV1.create(
            ts=BASE_TS,
            source_sequence=1,
            source_ref=source_ref("funding"),
            rate=Decimal("0.0001"),
            schedule_ref=refs.funding_schedule,
            observed_at_ts=BASE_TS + timedelta(microseconds=1),
        )

    assert exc_info.value.code == "funding_observation_in_future"


def test_tradable_v1_is_taker_ioc_only() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        TradableEventV1.create(
            ts=BASE_TS,
            source_sequence=1,
            source_ref=source_ref("tradable"),
            reference_price=Decimal("50000"),
            available_contracts=Decimal("1"),
            liquidity_role=LiquidityRoleV1.MAKER,
        )

    assert exc_info.value.code == "liquidity_role_out_of_v1_scope"


def test_bar_close_requires_exact_fifteen_minute_window() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        BarCloseEventV1.create(
            ts=BASE_TS,
            source_sequence=1,
            source_ref=source_ref("bar"),
            bar_start_ts=BASE_TS - timedelta(minutes=14),
            bar_end_ts=BASE_TS,
            open_price=Decimal("1"),
            high_price=Decimal("2"),
            low_price=Decimal("0.5"),
            close_price=Decimal("1.5"),
            volume_contracts=Decimal("1"),
            feature_ref=source_ref("feature"),
        )

    assert exc_info.value.code == "bar_window_invalid"


def test_bar_close_rejects_shifted_fifteen_minute_window() -> None:
    shifted_end = BASE_TS + timedelta(minutes=7, seconds=13, microseconds=1)

    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        BarCloseEventV1.create(
            ts=shifted_end,
            source_sequence=1,
            source_ref=source_ref("bar"),
            bar_start_ts=shifted_end - timedelta(minutes=15),
            bar_end_ts=shifted_end,
            open_price=Decimal("1"),
            high_price=Decimal("2"),
            low_price=Decimal("0.5"),
            close_price=Decimal("1.5"),
            volume_contracts=Decimal("1"),
            feature_ref=source_ref("feature"),
        )

    assert exc_info.value.code == "bar_window_invalid"


def test_event_order_key_uses_fixed_phase_not_caller_input() -> None:
    events = all_events()

    phases = {
        name: event_order_key(event)[1]
        for name, event in events.items()
    }

    assert phases == {
        "contract": 5,
        "index": 10,
        "mark": 20,
        "funding": 30,
        "tradable": 50,
        "bar": 60,
    }
    assert events["mark"].header.event_type is DerivativeEventKindV1.MARK_PRICE


def test_event_phase_policy_is_immutable() -> None:
    with pytest.raises(TypeError):
        EVENT_PHASE_PRIORITY_V1[DerivativeEventKindV1.MARK_PRICE] = 99  # type: ignore[index]

    assert event_order_key(all_events()["mark"])[1] == 20


def test_index_and_mark_are_distinct_closed_types() -> None:
    events = all_events()

    assert type(events["index"]) is IndexPriceEventV1
    assert type(events["mark"]) is MarkPriceEventV1


def test_mark_event_rejects_index_source_stream() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        MarkPriceEventV1.create(
            ts=BASE_TS,
            source_sequence=1,
            source_ref=source_ref("index"),
            price=Decimal("50000"),
        )

    assert exc_info.value.code == "event_source_stream_mismatch"


def test_bar_rejects_non_feature_source_ref() -> None:
    with pytest.raises(DerivativesBacktestContractError) as exc_info:
        BarCloseEventV1.create(
            ts=BASE_TS,
            source_sequence=1,
            source_ref=source_ref("bar"),
            bar_start_ts=BASE_TS - timedelta(minutes=15),
            bar_end_ts=BASE_TS,
            open_price=Decimal("1"),
            high_price=Decimal("2"),
            low_price=Decimal("0.5"),
            close_price=Decimal("1.5"),
            volume_contracts=Decimal("1"),
            feature_ref=source_ref("mark"),
        )

    assert exc_info.value.code == "bar_feature_stream_mismatch"


def test_contract_event_identity_excludes_snapshot_locator() -> None:
    original_refs = snapshot_refs()
    relocated_refs = DerivativesSnapshotRefsV1(
        instrument=replace(
            original_refs.instrument,
            relative_path="alternate/instrument.json",
        ),
        position_tier=replace(
            original_refs.position_tier,
            relative_path="alternate/position_tier.json",
        ),
        execution_fee=replace(
            original_refs.execution_fee,
            relative_path="alternate/execution_fee.json",
        ),
        funding_schedule=replace(
            original_refs.funding_schedule,
            relative_path="alternate/funding_schedule.json",
        ),
    )
    first = all_events()["contract"]
    second = type(first).create(
        ts=BASE_TS,
        source_sequence=1,
        source_ref=source_ref("contract"),
        snapshot_refs=relocated_refs,
    )

    assert first.to_dict() != second.to_dict()
    assert first.header.event_id == second.header.event_id
