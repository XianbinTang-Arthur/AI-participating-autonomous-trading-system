from datetime import datetime, timezone

import pytest

from aats.data_platform.research_factory.datasets.gold_bars import (
    GoldBarDatasetHandler,
    GoldBarRecord,
    dataset_fingerprint,
)
from aats.data_platform.research_factory.specs import DatasetSpec, ProcessorSpec, SegmentSpec


UTC = timezone.utc


def ts(hour: int) -> datetime:
    return datetime(2026, 5, 16, hour, tzinfo=UTC)


def segment(name: str, start_hour: int, end_hour: int) -> SegmentSpec:
    return SegmentSpec(
        name=name,
        start=ts(start_hour),
        end=ts(end_hour),
        purpose=f"{name} segment",
    )


def dataset_spec(
    *,
    symbol: str = "BTC-USDT-SWAP",
    timeframe: str = "1h",
    dataset_version: str = "v1",
    window_end: datetime | None = None,
    source_refs: dict[str, object] | None = None,
    segments: tuple[SegmentSpec, ...] | None = None,
) -> DatasetSpec:
    return DatasetSpec(
        dataset_id="btc_swap_1h_v1",
        symbol=symbol,
        timeframe=timeframe,
        dataset_version=dataset_version,
        window_start=ts(0),
        window_end=window_end or ts(6),
        segments=segments
        or (
            segment("train", 0, 2),
            segment("valid", 2, 4),
            segment("test", 4, 6),
        ),
        source_refs=source_refs or {"gold": "fixture"},
    )


def record(
    hour: int,
    *,
    symbol: str = "BTC-USDT-SWAP",
    timeframe: str = "1h",
    funding_rate: float | None = 0.0001,
) -> GoldBarRecord:
    base = 100.0 + hour
    return GoldBarRecord(
        symbol=symbol,
        timeframe=timeframe,
        ts=ts(hour),
        open=base,
        high=base + 1.0,
        low=base - 1.0,
        close=base + 0.5,
        volume=10_000.0 + hour,
        vwap=base + 0.25,
        funding_rate=funding_rate,
    )


def prepare(records: list[GoldBarRecord], spec: DatasetSpec | None = None):
    return GoldBarDatasetHandler().prepare(records, spec or dataset_spec())


def fingerprint(
    spec: DatasetSpec | None = None,
    *,
    source_watermark: object | None = None,
    processor_versions: object | None = None,
) -> str:
    return dataset_fingerprint(
        spec or dataset_spec(),
        source_watermark or {"gold_max_ts": ts(5).isoformat()},
        processor_versions or {"drop_missing": "v1"},
    )


def test_prepare_sorts_out_of_order_records_within_segments() -> None:
    prepared = prepare(
        [
            record(4),
            record(0),
            record(5),
            record(2),
            record(1),
            record(3),
        ]
    )

    assert [row["ts"] for row in prepared.rows_for_segment("train")] == [ts(0), ts(1)]
    assert [row["ts"] for row in prepared.rows_for_segment("valid")] == [ts(2), ts(3)]
    assert [row["ts"] for row in prepared.rows_for_segment("test")] == [ts(4), ts(5)]


def test_prepare_rejects_symbol_mismatch() -> None:
    with pytest.raises(ValueError, match="record symbol"):
        prepare([record(0), record(1), record(2), record(3), record(4, symbol="ETH-USDT-SWAP")])


def test_prepare_rejects_timeframe_mismatch() -> None:
    with pytest.raises(ValueError, match="record timeframe"):
        prepare([record(0), record(1), record(2), record(3), record(4, timeframe="15m")])


def test_gold_bar_record_rejects_non_finite_price() -> None:
    with pytest.raises(ValueError, match="finite"):
        GoldBarRecord(
            symbol="BTC-USDT-SWAP",
            timeframe="1h",
            ts=ts(0),
            open=float("nan"),
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1.0,
        )


def test_gold_bar_record_rejects_invalid_ohlcv_invariants() -> None:
    with pytest.raises(ValueError, match="record.high"):
        GoldBarRecord(
            symbol="BTC-USDT-SWAP",
            timeframe="1h",
            ts=ts(0),
            open=100.0,
            high=99.0,
            low=98.0,
            close=100.5,
            volume=1.0,
        )


def test_gold_bar_record_exposes_validated_microstructure_feature_values() -> None:
    row = GoldBarRecord(
        symbol="BTC-USDT-SWAP",
        timeframe="15m",
        ts=ts(0),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1.0,
        feature_values={"trade_flow_imbalance": 0.25, "oi_delta": None},
    ).to_row()

    assert row["trade_flow_imbalance"] == pytest.approx(0.25)
    assert row["oi_delta"] is None


def test_gold_bar_record_rejects_unknown_or_non_finite_feature_values() -> None:
    common = {
        "symbol": "BTC-USDT-SWAP",
        "timeframe": "15m",
        "ts": ts(0),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1.0,
    }
    with pytest.raises(ValueError, match="unsupported fields"):
        GoldBarRecord(**common, feature_values={"future_alpha": 1.0})
    with pytest.raises(ValueError, match="finite"):
        GoldBarRecord(
            **common,
            feature_values={"trade_flow_imbalance": float("nan")},
        )

    with pytest.raises(ValueError, match="record.volume"):
        GoldBarRecord(
            symbol="BTC-USDT-SWAP",
            timeframe="1h",
            ts=ts(0),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=-1.0,
        )


def test_prepare_rejects_duplicate_timestamp() -> None:
    with pytest.raises(ValueError, match="duplicate timestamp"):
        prepare([record(0), record(0), record(2), record(3), record(4), record(5)])


def test_prepare_ignores_rows_outside_dataset_window() -> None:
    before_window = GoldBarRecord(
        symbol="BTC-USDT-SWAP",
        timeframe="1h",
        ts=datetime(2026, 5, 15, 23, tzinfo=UTC),
        open=99.0,
        high=100.0,
        low=98.0,
        close=99.5,
        volume=1.0,
        funding_rate=0.0001,
    )
    window_end = record(6)

    prepared = prepare(
        [
            before_window,
            record(0),
            record(2),
            record(4),
            window_end,
        ]
    )

    all_output_ts = {
        row["ts"]
        for rows in prepared.rows_by_segment.values()
        for row in rows
    }
    assert datetime(2026, 5, 15, 23, tzinfo=UTC) not in all_output_ts
    assert ts(6) not in all_output_ts
    assert all_output_ts == {ts(0), ts(2), ts(4)}


def test_prepare_rejects_empty_segment() -> None:
    with pytest.raises(ValueError, match="segment 'valid' has no rows"):
        prepare([record(0), record(1), record(4), record(5)])


def test_prepare_records_missing_funding_reason_without_rejecting_row() -> None:
    prepared = prepare(
        [
            record(0),
            record(1),
            record(2, funding_rate=None),
            record(3),
            record(4),
            record(5),
        ]
    )

    assert prepared.rows_for_segment("valid")[0]["funding_rate"] is None
    assert prepared.missing_reasons["funding_rate"] == (
        f"{ts(2).isoformat()}: funding_rate missing",
    )


def test_prepare_groups_replay_overlap_without_leakage_failure() -> None:
    spec = dataset_spec(
        segments=(
            segment("train", 0, 2),
            segment("valid", 2, 4),
            segment("test", 4, 6),
            SegmentSpec(
                name="replay",
                start=ts(4),
                end=ts(6),
                purpose="explicit replay overlap with test",
            ),
        )
    )

    prepared = prepare([record(hour) for hour in range(6)], spec)

    assert [row["ts"] for row in prepared.rows_for_segment("test")] == [ts(4), ts(5)]
    assert [row["ts"] for row in prepared.rows_for_segment("replay")] == [ts(4), ts(5)]


def test_dataset_fingerprint_is_deterministic() -> None:
    assert fingerprint() == fingerprint()


@pytest.mark.parametrize(
    ("changed_spec", "source_watermark", "processor_versions"),
    [
        (dataset_spec(symbol="ETH-USDT-SWAP"), None, None),
        (dataset_spec(timeframe="15m"), None, None),
        (dataset_spec(dataset_version="v2"), None, None),
        (dataset_spec(window_end=ts(7)), None, None),
        (dataset_spec(source_refs={"gold": "fixture-v2"}), None, None),
        (None, {"gold_max_ts": ts(4).isoformat()}, None),
        (None, None, {"drop_missing": "v2"}),
    ],
)
def test_dataset_fingerprint_changes_when_key_material_changes(
    changed_spec: DatasetSpec | None,
    source_watermark: object | None,
    processor_versions: object | None,
) -> None:
    assert fingerprint(changed_spec, source_watermark=source_watermark, processor_versions=processor_versions) != fingerprint()


def test_dataset_fingerprint_rejects_missing_source_watermark() -> None:
    with pytest.raises(ValueError, match="source_watermark"):
        dataset_fingerprint(dataset_spec(), None, {"drop_missing": "v1"})


def test_dataset_fingerprint_does_not_expose_absolute_local_paths() -> None:
    spec = dataset_spec(
        source_refs={"gold_path": "C:\\Users\\example\\research\\gold.parquet"}
    )

    cache_key = fingerprint(spec)

    assert cache_key.startswith("rfds_")
    assert "C:" not in cache_key
    assert "Users" not in cache_key
    assert "gold.parquet" not in cache_key


def test_dataset_fingerprint_accepts_processor_specs() -> None:
    cache_key = fingerprint(
        processor_versions=[
            ProcessorSpec(name="drop_missing", params={"fields": ("close",)}, version="v1")
        ]
    )

    assert cache_key.startswith("rfds_")
