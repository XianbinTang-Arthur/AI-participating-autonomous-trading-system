from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from aats.data_platform.data_governance.archive import (
    ArchiveScope,
    _recover_existing_artifact,
    _write_parquet_immutable,
    archive_partition,
    verify_archive_artifact,
)


UTC = timezone.utc


def _scope() -> ArchiveScope:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    return ArchiveScope(
        source_id="00000000-0000-0000-0000-000000000001",
        dataset_name="bronze.market_trades",
        table="bronze.market_trades",
        symbol="BTC-USDT-SWAP",
        coverage_start=start,
        coverage_end=start + timedelta(days=1),
    )


def test_parquet_archive_is_multi_batch_verifiable_and_non_overwriting(tmp_path: Path) -> None:
    scope = _scope()
    target = tmp_path / "part-00000.parquet"
    manifest = target.with_suffix(".manifest.json")
    rows = iter(
        [
            [{"symbol": scope.symbol, "ts": scope.coverage_start, "payload_sequence": 10, "px": 100.0}],
            [{"symbol": scope.symbol, "ts": scope.coverage_start + timedelta(seconds=1), "payload_sequence": 11, "px": 101.0}],
        ]
    )

    artifact = _write_parquet_immutable(rows, target, manifest, scope)
    verified = verify_archive_artifact(
        target,
        expected_sha256=artifact.sha256,
        expected_rows=2,
    )

    assert verified.row_count == 2
    assert verified.min_sequence == 10
    assert verified.max_sequence == 11
    assert pq.read_table(target).to_pylist()[1]["px"] == 101.0
    with pytest.raises(FileExistsError, match="archive_target_exists"):
        _write_parquet_immutable(iter([[{"ts": scope.coverage_start}]]), target, manifest, scope)


def test_archive_explicit_schema_preserves_late_nullable_and_varying_json_fields(
    tmp_path: Path,
) -> None:
    scope = _scope()
    target = tmp_path / "part-schema.parquet"
    schema = pa.schema(
        [
            pa.field("symbol", pa.string()),
            pa.field("ts", pa.timestamp("us", tz="UTC")),
            pa.field("optional_value", pa.float64()),
            pa.field("raw_payload", pa.string()),
            pa.field("ingest_run_id", pa.string()),
        ]
    )
    rows = iter(
        [
            [
                {
                    "symbol": scope.symbol,
                    "ts": scope.coverage_start,
                    "optional_value": None,
                    "raw_payload": {"first": 1},
                    "ingest_run_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
                }
            ],
            [
                {
                    "symbol": scope.symbol,
                    "ts": scope.coverage_start + timedelta(seconds=1),
                    "optional_value": 2.5,
                    "raw_payload": {"second": 2},
                    "ingest_run_id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
                }
            ],
        ]
    )

    _write_parquet_immutable(
        rows,
        target,
        target.with_suffix(".manifest.json"),
        scope,
        arrow_schema=schema,
        json_columns=frozenset({"raw_payload"}),
        string_columns=frozenset({"ingest_run_id"}),
    )

    restored = pq.read_table(target).to_pylist()
    assert restored[1]["optional_value"] == 2.5
    assert json.loads(restored[0]["raw_payload"]) == {"first": 1}
    assert json.loads(restored[1]["raw_payload"]) == {"second": 2}
    assert restored[1]["ingest_run_id"] == "00000000-0000-0000-0000-000000000002"


def test_archive_verification_detects_tamper(tmp_path: Path) -> None:
    scope = _scope()
    target = tmp_path / "part-00000.parquet"
    artifact = _write_parquet_immutable(
        iter([[{"symbol": scope.symbol, "ts": scope.coverage_start, "px": 100.0}]]),
        target,
        target.with_suffix(".manifest.json"),
        scope,
    )
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    with target.open("ab") as handle:
        handle.write(b"tamper")
        handle.flush()
        os.fsync(handle.fileno())

    with pytest.raises(RuntimeError, match="archive_checksum_mismatch"):
        verify_archive_artifact(
            target,
            expected_sha256=artifact.sha256,
            expected_rows=1,
        )


def test_verified_orphan_artifact_can_resume_database_state_transition(
    tmp_path: Path,
) -> None:
    scope = _scope()
    target = tmp_path / "part-00000.parquet"
    manifest = target.with_suffix(".manifest.json")
    written = _write_parquet_immutable(
        iter([[{"symbol": scope.symbol, "ts": scope.coverage_start, "px": 100.0}]]),
        target,
        manifest,
        scope,
    )

    recovered = _recover_existing_artifact(target, manifest)

    assert recovered is not None
    assert recovered.sha256 == written.sha256
    assert recovered.row_count == 1


def test_partial_orphan_artifact_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "part-00000.parquet"
    target.write_bytes(b"partial")

    with pytest.raises(
        RuntimeError,
        match="archive_partial_artifact_requires_operator_quarantine",
    ):
        _recover_existing_artifact(target, target.with_suffix(".manifest.json"))


def test_archive_partition_rejects_relative_root_before_db_access() -> None:
    called = False

    def session_factory():
        nonlocal called
        called = True
        raise AssertionError("database must not be touched")

    with pytest.raises(ValueError, match="archive_root_must_be_absolute"):
        archive_partition(session_factory, _scope(), Path("relative/archive"))
    assert called is False


def test_archive_scope_is_exact_one_utc_day_and_dataset_matches_table() -> None:
    scope = _scope()
    with pytest.raises(ValueError, match="archive_dataset_must_match_source_table"):
        ArchiveScope(**{**scope.__dict__, "dataset_name": "bronze.other"})
    with pytest.raises(ValueError, match="archive_scope_must_start_at_utc_day_boundary"):
        ArchiveScope(
            **{
                **scope.__dict__,
                "coverage_start": scope.coverage_start + timedelta(hours=1),
                "coverage_end": scope.coverage_end + timedelta(hours=1),
            }
        )


def test_archive_verification_binds_manifest_to_expected_scope(tmp_path: Path) -> None:
    scope = _scope()
    target = tmp_path / "part-00000.parquet"
    artifact = _write_parquet_immutable(
        iter([[{"symbol": scope.symbol, "ts": scope.coverage_start, "px": 100.0}]]),
        target,
        target.with_suffix(".manifest.json"),
        scope,
    )
    other_scope = ArchiveScope(
        **{**scope.__dict__, "symbol": "ETH-USDT-SWAP"}
    )

    with pytest.raises(RuntimeError, match="archive_manifest_scope_mismatch"):
        verify_archive_artifact(
            target,
            expected_sha256=artifact.sha256,
            expected_rows=1,
            expected_scope=other_scope,
        )
