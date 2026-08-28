from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from aats.data_platform.replay.derivatives_backtest.events import (
    BarCloseEventV1,
    ContractTierEffectiveEventV1,
    FundingSettlementEventV1,
    IndexPriceEventV1,
    MarkPriceEventV1,
    SourceRecordRefV1,
    TradableEventV1,
)
from aats.data_platform.replay.derivatives_backtest.snapshot_refs import (
    DerivativesSnapshotRefsV1,
    ImmutableSnapshotRefV1,
    SnapshotKindV1,
)


BASE_TS = datetime(2026, 1, 2, 0, 15, tzinfo=timezone.utc)


def source_ref(stream_id: str = "mark") -> SourceRecordRefV1:
    stream_id = {
        "contract": "contract-schedule",
        "index": "index-price",
        "mark": "mark-price",
        "funding": "funding-settlement",
        "bar": "bar-close",
        "feature": "bar-features",
    }.get(stream_id, stream_id)
    return SourceRecordRefV1(
        stream_id=stream_id,
        source_registry_id="00000000-0000-4000-8000-000000000099",
        parent_artifact_sha256="a" * 64,
        source_record_sha256="b" * 64,
    )


def snapshot_ref(
    kind: SnapshotKindV1,
    *,
    ordinal: int,
    effective_from: datetime = BASE_TS,
) -> ImmutableSnapshotRefV1:
    return ImmutableSnapshotRefV1(
        kind=kind,
        snapshot_id=f"00000000-0000-4000-8000-{ordinal:012d}",
        relative_path=f"snapshots/{kind.value}.json",
        raw_sha256=f"{ordinal:x}" * 64,
        size_bytes=128 + ordinal,
        semantic_sha256=f"{ordinal + 4:x}" * 64,
        source_registry_id=f"00000000-0000-4000-8000-{ordinal + 10:012d}",
        source_seal_fingerprint=f"{ordinal + 8:x}" * 64,
        source_schema=f"aats.{kind.value}.v1",
        effective_from=effective_from,
        effective_to=None,
    )


def snapshot_refs(*, effective_from: datetime = BASE_TS) -> DerivativesSnapshotRefsV1:
    return DerivativesSnapshotRefsV1(
        instrument=snapshot_ref(
            SnapshotKindV1.INSTRUMENT,
            ordinal=1,
            effective_from=effective_from,
        ),
        position_tier=snapshot_ref(
            SnapshotKindV1.POSITION_TIER,
            ordinal=2,
            effective_from=effective_from,
        ),
        execution_fee=snapshot_ref(
            SnapshotKindV1.EXECUTION_FEE,
            ordinal=3,
            effective_from=effective_from,
        ),
        funding_schedule=snapshot_ref(
            SnapshotKindV1.FUNDING_SCHEDULE,
            ordinal=4,
            effective_from=effective_from,
        ),
    )


def all_events(ts: datetime = BASE_TS):
    refs = snapshot_refs(effective_from=ts)
    return {
        "contract": ContractTierEffectiveEventV1.create(
            ts=ts,
            source_sequence=1,
            source_ref=source_ref("contract"),
            snapshot_refs=refs,
        ),
        "index": IndexPriceEventV1.create(
            ts=ts,
            source_sequence=1,
            source_ref=source_ref("index"),
            price=Decimal("50000"),
        ),
        "mark": MarkPriceEventV1.create(
            ts=ts,
            source_sequence=1,
            source_ref=source_ref("mark"),
            price=Decimal("50001"),
        ),
        "funding": FundingSettlementEventV1.create(
            ts=ts,
            source_sequence=1,
            source_ref=source_ref("funding"),
            rate=Decimal("0.0001"),
            schedule_ref=refs.funding_schedule,
            observed_at_ts=ts,
        ),
        "tradable": TradableEventV1.create(
            ts=ts,
            source_sequence=1,
            source_ref=source_ref("tradable"),
            reference_price=Decimal("50002"),
            available_contracts=Decimal("2"),
        ),
        "bar": BarCloseEventV1.create(
            ts=ts,
            source_sequence=1,
            source_ref=source_ref("bar"),
            bar_start_ts=ts.replace(minute=0),
            bar_end_ts=ts,
            open_price=Decimal("49990"),
            high_price=Decimal("50010"),
            low_price=Decimal("49980"),
            close_price=Decimal("50000"),
            volume_contracts=Decimal("100"),
            feature_ref=source_ref("feature"),
        ),
    }
