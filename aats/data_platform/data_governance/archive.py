"""Immutable Parquet archive and archive-before-delete primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import text

from aats.data_platform.governance._atomic_io import immutable_json_write
from aats.data_platform.governance._atomic_io import _fsync_directory


_QUALIFIED_TABLE = re.compile(r"^(staging|bronze)\.[a-z][a-z0-9_]*$")
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,79}$")


@dataclass(frozen=True)
class ArchiveScope:
    source_id: str
    dataset_name: str
    table: str
    symbol: str
    coverage_start: datetime
    coverage_end: datetime
    time_column: str = "ts"

    def __post_init__(self) -> None:
        try:
            uuid.UUID(self.source_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("archive_source_id_invalid") from exc
        if not _QUALIFIED_TABLE.fullmatch(self.table):
            raise ValueError("archive_table_not_allowlisted")
        if self.dataset_name != self.table:
            raise ValueError("archive_dataset_must_match_source_table")
        if not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("archive_symbol_invalid")
        if (
            self.coverage_start.tzinfo is None
            or self.coverage_start.utcoffset() is None
            or self.coverage_end.tzinfo is None
            or self.coverage_end.utcoffset() is None
        ):
            raise ValueError("archive_scope_requires_timezone_aware_timestamps")
        if self.coverage_end <= self.coverage_start:
            raise ValueError("archive_scope_end_must_be_after_start")
        start_utc = self.coverage_start.astimezone(timezone.utc)
        end_utc = self.coverage_end.astimezone(timezone.utc)
        if start_utc != start_utc.replace(hour=0, minute=0, second=0, microsecond=0):
            raise ValueError("archive_scope_must_start_at_utc_day_boundary")
        if end_utc - start_utc != timedelta(days=1):
            raise ValueError("archive_scope_must_cover_one_utc_day")
        if not self.time_column.replace("_", "").isalnum():
            raise ValueError("archive_time_column_invalid")


@dataclass(frozen=True)
class ArchiveArtifact:
    parquet_path: str
    manifest_path: str
    sha256: str
    row_count: int
    min_event_ts: str
    max_event_ts: str
    min_sequence: int | None
    max_sequence: int | None
    schema: str


def archive_partition(
    session_factory: Callable[[], Any],
    scope: ArchiveScope,
    archive_root: Path,
    *,
    minimum_free_bytes: int = 5 * 1024**3,
    batch_size: int = 25_000,
) -> ArchiveArtifact:
    """Archive one exact half-open partition and mark it DELETE_ELIGIBLE.

    Existing artifacts are never overwritten. The database is moved to
    ``DELETE_ELIGIBLE`` only after Parquet metadata, content SHA-256 and source
    row count have all been verified.
    """

    expanded_root = archive_root.expanduser()
    if not expanded_root.is_absolute():
        raise ValueError("archive_root_must_be_absolute")
    if minimum_free_bytes < 0:
        raise ValueError("archive_minimum_free_bytes_must_be_non_negative")
    if batch_size <= 0:
        raise ValueError("archive_batch_size_must_be_positive")
    root = expanded_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(root).free < minimum_free_bytes:
        raise RuntimeError("archive_disk_free_below_safety_floor")
    target = _target_path(root, scope)
    manifest_path = target.with_suffix(".manifest.json")

    with session_factory() as session, session.begin():
        existing = _load_partition(session, scope)
        if existing and existing["state"] in {"VERIFIED", "DELETE_ELIGIBLE", "DELETED"}:
            artifact = verify_archive_artifact(
                Path(existing["storage_path"]),
                expected_sha256=existing["sha256"],
                expected_rows=int(existing["row_count"]),
                expected_scope=scope,
            )
            if artifact.manifest_path != str(manifest_path):
                raise RuntimeError("archive_manifest_path_mismatch")
            return artifact
        _upsert_partition_state(
            session,
            scope,
            target,
            state="ARCHIVING",
            error_message=None,
        )

    try:
        with session_factory() as session:
            _begin_repeatable_read_snapshot(session)
            source_count = _source_count(session, scope)
            artifact = _recover_existing_artifact(
                target,
                manifest_path,
                expected_scope=scope,
            )
            if artifact is None:
                rows = _stream_rows(session, scope, batch_size=batch_size)
                artifact = _write_parquet_immutable(
                    rows,
                    target,
                    manifest_path,
                    scope,
                )
        if source_count != artifact.row_count:
            raise RuntimeError(
                "archive_source_row_count_changed_during_archive:"
                f"source={source_count};archive={artifact.row_count}"
            )
        verify_archive_artifact(
            target,
            expected_sha256=artifact.sha256,
            expected_rows=source_count,
            expected_scope=scope,
        )
        with session_factory() as session, session.begin():
            _mark_verified_and_delete_eligible(session, scope, artifact)
        return artifact
    except BaseException as exc:
        with session_factory() as session, session.begin():
            _upsert_partition_state(
                session,
                scope,
                target,
                state="FAILED",
                error_message=type(exc).__name__,
            )
        raise


def _begin_repeatable_read_snapshot(session) -> None:
    bind = session.get_bind() if hasattr(session, "get_bind") else None
    if bind is not None and bind.dialect.name == "postgresql":
        session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )


def _recover_existing_artifact(
    target: Path,
    manifest_path: Path,
    *,
    expected_scope: ArchiveScope | None = None,
) -> ArchiveArtifact | None:
    """Recover an immutable artifact left before its DB state commit."""

    if not target.exists() and not manifest_path.exists():
        return None
    if not target.is_file() or not manifest_path.is_file():
        raise RuntimeError("archive_partial_artifact_requires_operator_quarantine")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_sha256 = str(manifest["sha256"])
        expected_rows = int(manifest["row_count"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("archive_existing_manifest_invalid") from exc
    return verify_archive_artifact(
        target,
        expected_sha256=expected_sha256,
        expected_rows=expected_rows,
        expected_scope=expected_scope,
    )


def verify_archive_artifact(
    parquet_path: Path,
    *,
    expected_sha256: str,
    expected_rows: int,
    expected_scope: ArchiveScope | None = None,
) -> ArchiveArtifact:
    """Read-only checksum and Parquet metadata verification."""

    path = parquet_path.resolve()
    manifest_path = path.with_suffix(".manifest.json")
    if not path.is_file() or not manifest_path.is_file():
        raise RuntimeError("archive_artifact_or_manifest_missing")
    digest = _sha256_file(path)
    if digest != expected_sha256:
        raise RuntimeError("archive_checksum_mismatch")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("archive_manifest_invalid") from exc
    if manifest.get("sha256") != digest:
        raise RuntimeError("archive_manifest_checksum_mismatch")
    if expected_scope is not None:
        expected_identity = {
            "source_id": expected_scope.source_id,
            "dataset_name": expected_scope.dataset_name,
            "table": expected_scope.table,
            "symbol": expected_scope.symbol,
            "coverage_start": expected_scope.coverage_start.astimezone(
                timezone.utc
            ).isoformat(),
            "coverage_end": expected_scope.coverage_end.astimezone(
                timezone.utc
            ).isoformat(),
        }
        if any(manifest.get(key) != value for key, value in expected_identity.items()):
            raise RuntimeError("archive_manifest_scope_mismatch")
    metadata = pq.read_metadata(path)
    if metadata.num_rows != expected_rows or manifest.get("row_count") != expected_rows:
        raise RuntimeError("archive_row_count_mismatch")
    return ArchiveArtifact(
        parquet_path=str(path),
        manifest_path=str(manifest_path),
        sha256=digest,
        row_count=int(metadata.num_rows),
        min_event_ts=str(manifest["min_event_ts"]),
        max_event_ts=str(manifest["max_event_ts"]),
        min_sequence=manifest.get("min_sequence"),
        max_sequence=manifest.get("max_sequence"),
        schema=str(manifest["schema"]),
    )


def register_local_capture_source(
    session,
    *,
    source_key: str,
    table: str,
    schema_version: str,
    timestamp_semantics: str,
) -> str:
    """Register a stable local WebSocket capture source without secrets."""

    value = session.execute(
        text(
            """
            INSERT INTO meta.data_source_registry (
                source_key, source_kind, provider, source_locator,
                schema_version, timestamp_semantics, truth_tier,
                license_usage_note, source_metadata
            ) VALUES (
                :source_key, 'aats_ws_capture', 'AATS', :table,
                :schema_version, :timestamp_semantics, 'local_observation',
                'AATS local public-market observation; not proof of pre-start history',
                CAST(:metadata AS jsonb)
            )
            ON CONFLICT (source_key) DO UPDATE SET
                source_key = EXCLUDED.source_key
            WHERE meta.data_source_registry.source_kind = EXCLUDED.source_kind
              AND meta.data_source_registry.provider = EXCLUDED.provider
              AND meta.data_source_registry.source_locator = EXCLUDED.source_locator
              AND meta.data_source_registry.schema_version = EXCLUDED.schema_version
              AND meta.data_source_registry.timestamp_semantics = EXCLUDED.timestamp_semantics
              AND meta.data_source_registry.truth_tier = EXCLUDED.truth_tier
            RETURNING source_id
            """
        ),
        {
            "source_key": source_key,
            "table": table,
            "schema_version": schema_version,
            "timestamp_semantics": timestamp_semantics,
            "metadata": json.dumps({"table": table}, sort_keys=True),
        },
    ).scalar_one_or_none()
    if value is None:
        raise RuntimeError("local_capture_source_registry_immutable_conflict")
    return str(value)


def _stream_rows(session, scope: ArchiveScope, *, batch_size: int) -> Iterator[list[dict[str, Any]]]:
    table_sql = _qualified(scope.table)
    time_sql = _identifier(scope.time_column)
    result = session.execute(
        text(
            f"SELECT * FROM {table_sql} WHERE symbol = :symbol "
            f"AND {time_sql} >= :start AND {time_sql} < :end "
            f"ORDER BY {time_sql} ASC"
        ),
        {
            "symbol": scope.symbol,
            "start": scope.coverage_start,
            "end": scope.coverage_end,
        },
    ).mappings()
    while True:
        batch = result.fetchmany(batch_size)
        if not batch:
            break
        yield [dict(row) for row in batch]


def _write_parquet_immutable(
    batches: Iterator[list[dict[str, Any]]],
    target: Path,
    manifest_path: Path,
    scope: ArchiveScope,
) -> ArchiveArtifact:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or manifest_path.exists():
        raise FileExistsError(f"archive_target_exists:{target}")
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    writer: pq.ParquetWriter | None = None
    arrow_schema: pa.Schema | None = None
    row_count = 0
    min_ts: datetime | None = None
    max_ts: datetime | None = None
    min_sequence: int | None = None
    max_sequence: int | None = None
    try:
        for rows in batches:
            if not rows:
                continue
            table = (
                pa.Table.from_pylist(rows)
                if arrow_schema is None
                else pa.Table.from_pylist(rows, schema=arrow_schema)
            )
            if writer is None:
                arrow_schema = table.schema
                writer = pq.ParquetWriter(
                    temporary,
                    table.schema,
                    compression="zstd",
                    use_dictionary=True,
                    write_statistics=True,
                )
            writer.write_table(table)
            row_count += len(rows)
            timestamps = [row[scope.time_column] for row in rows]
            batch_min = min(timestamps)
            batch_max = max(timestamps)
            min_ts = batch_min if min_ts is None else min(min_ts, batch_min)
            max_ts = batch_max if max_ts is None else max(max_ts, batch_max)
            sequences = [
                int(row["payload_sequence"])
                for row in rows
                if row.get("payload_sequence") is not None
            ]
            if sequences:
                batch_min_sequence = min(sequences)
                batch_max_sequence = max(sequences)
                min_sequence = batch_min_sequence if min_sequence is None else min(min_sequence, batch_min_sequence)
                max_sequence = batch_max_sequence if max_sequence is None else max(max_sequence, batch_max_sequence)
        if writer is None or row_count == 0 or min_ts is None or max_ts is None:
            raise RuntimeError("archive_source_partition_empty")
        writer.close()
        writer = None
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.link(temporary, target)
        _fsync_directory(target.parent)
        temporary.unlink()
        target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        digest = _sha256_file(target)
        schema = str(pq.read_schema(target))
        manifest = {
            "schema_version": "rdp-archive-manifest-v1",
            "source_id": scope.source_id,
            "dataset_name": scope.dataset_name,
            "table": scope.table,
            "symbol": scope.symbol,
            "coverage_start": scope.coverage_start.astimezone(timezone.utc).isoformat(),
            "coverage_end": scope.coverage_end.astimezone(timezone.utc).isoformat(),
            "storage_format": "parquet",
            "parquet_path": str(target.resolve()),
            "sha256": digest,
            "row_count": row_count,
            "min_event_ts": min_ts.astimezone(timezone.utc).isoformat(),
            "max_event_ts": max_ts.astimezone(timezone.utc).isoformat(),
            "min_sequence": min_sequence,
            "max_sequence": max_sequence,
            "schema": schema,
            "gap_summary": _sequence_gap_summary(min_sequence, max_sequence, row_count),
        }
        immutable_json_write(manifest, manifest_path)
        return ArchiveArtifact(
            parquet_path=str(target.resolve()),
            manifest_path=str(manifest_path.resolve()),
            sha256=digest,
            row_count=row_count,
            min_event_ts=manifest["min_event_ts"],
            max_event_ts=manifest["max_event_ts"],
            min_sequence=min_sequence,
            max_sequence=max_sequence,
            schema=schema,
        )
    finally:
        if writer is not None:
            writer.close()
        temporary.unlink(missing_ok=True)


def _load_partition(session, scope: ArchiveScope):
    return session.execute(
        text(
            "SELECT state, storage_path, sha256, row_count FROM meta.archive_partitions "
            "WHERE source_id = :source_id AND dataset_name = :dataset_name "
            "AND symbol = :symbol AND coverage_start = :start AND coverage_end = :end"
        ),
        {
            "source_id": scope.source_id,
            "dataset_name": scope.dataset_name,
            "symbol": scope.symbol,
            "start": scope.coverage_start,
            "end": scope.coverage_end,
        },
    ).mappings().one_or_none()


def _upsert_partition_state(session, scope: ArchiveScope, target: Path, *, state: str, error_message: str | None) -> None:
    session.execute(
        text(
            """
            INSERT INTO meta.archive_partitions (
                source_id, dataset_name, symbol, coverage_start, coverage_end,
                storage_path, state, error_message
            ) VALUES (
                :source_id, :dataset_name, :symbol, :start, :end,
                :storage_path, :state, :error_message
            )
            ON CONFLICT (source_id, dataset_name, symbol, coverage_start, coverage_end)
            DO UPDATE SET state = EXCLUDED.state, error_message = EXCLUDED.error_message,
                          updated_at = NOW()
            WHERE meta.archive_partitions.state NOT IN (
                'VERIFIED', 'DELETE_ELIGIBLE', 'DELETED'
            )
            """
        ),
        {
            "source_id": scope.source_id,
            "dataset_name": scope.dataset_name,
            "symbol": scope.symbol,
            "start": scope.coverage_start,
            "end": scope.coverage_end,
            "storage_path": str(target.resolve()),
            "state": state,
            "error_message": error_message,
        },
    )


def _mark_verified_and_delete_eligible(session, scope: ArchiveScope, artifact: ArchiveArtifact) -> None:
    result = session.execute(
        text(
            """
            UPDATE meta.archive_partitions SET
                storage_path = :storage_path, sha256 = :sha256, row_count = :row_count,
                min_event_ts = :min_ts, max_event_ts = :max_ts,
                min_sequence = :min_sequence, max_sequence = :max_sequence,
                manifest_payload = CAST(:manifest AS jsonb),
                state = 'DELETE_ELIGIBLE', verified_at = NOW(),
                error_message = NULL, updated_at = NOW()
            WHERE source_id = :source_id AND dataset_name = :dataset_name
              AND symbol = :symbol AND coverage_start = :start AND coverage_end = :end
              AND state = 'ARCHIVING'
            """
        ),
        {
            "storage_path": artifact.parquet_path,
            "sha256": artifact.sha256,
            "row_count": artifact.row_count,
            "min_ts": artifact.min_event_ts,
            "max_ts": artifact.max_event_ts,
            "min_sequence": artifact.min_sequence,
            "max_sequence": artifact.max_sequence,
            "manifest": json.dumps(asdict(artifact), sort_keys=True),
            "source_id": scope.source_id,
            "dataset_name": scope.dataset_name,
            "symbol": scope.symbol,
            "start": scope.coverage_start,
            "end": scope.coverage_end,
        },
    )
    if result.rowcount != 1:
        raise RuntimeError("archive_state_transition_conflict")


def _source_count(session, scope: ArchiveScope) -> int:
    value = session.execute(
        text(
            f"SELECT COUNT(*) FROM {_qualified(scope.table)} WHERE symbol = :symbol "
            f"AND {_identifier(scope.time_column)} >= :start "
            f"AND {_identifier(scope.time_column)} < :end"
        ),
        {"symbol": scope.symbol, "start": scope.coverage_start, "end": scope.coverage_end},
    ).scalar_one()
    return int(value)


def _target_path(root: Path, scope: ArchiveScope) -> Path:
    day = scope.coverage_start.astimezone(timezone.utc).strftime("%Y-%m-%d")
    dataset = scope.dataset_name.replace(".", "_")
    return root / dataset / f"symbol={scope.symbol}" / f"date={day}" / "part-00000.parquet"


def _qualified(value: str) -> str:
    schema, table = value.split(".", 1)
    return f'{_identifier(schema)}.{_identifier(table)}'


def _identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError("unsafe_sql_identifier")
    return f'"{value}"'


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sequence_gap_summary(minimum: int | None, maximum: int | None, rows: int) -> dict[str, Any]:
    if minimum is None or maximum is None:
        return {"sequence_available": False, "known_gap_count": None}
    expected = maximum - minimum + 1
    return {
        "sequence_available": True,
        "expected_sequence_span": expected,
        "observed_rows": rows,
        # Sequence identifiers are exchange/channel specific and can be shared
        # by multiple persisted rows.  A span-minus-row-count calculation would
        # therefore manufacture gaps.  Only the continuity ledger may assert
        # known gaps from explicit predecessor/drop evidence.
        "known_gap_count": None,
        "gap_inference": "continuity_ledger_required",
    }
