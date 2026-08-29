"""Bounded, restartable file source for derivatives replay event sets.

The reader deliberately performs two independent filesystem passes.  The
first pass validates every byte; a subsequent verification pass reopens the
same immutable paths, validates any restart cursor from byte zero, and
recomputes the complete identity while records are consumed.  Verification
records are explicitly uncommitted: they may not drive economic mutation,
checkpoint advancement, or publishing until a later transactional spool or
rollback boundary is implemented.  This module has no database, network,
workflow, or live runtime integration and accepts only synthetic,
non-promotable v1 inputs.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Callable

from aats.data_platform.governance.research_artifact_contract import (
    decode_strict_json_artifact,
    read_stable_regular_artifact_file,
)
from aats.data_platform.governance.typed_json_identity import (
    canonical_typed_json_bytes,
)

from .contracts import DerivativesBacktestContractError
from .event_set import (
    DERIVATIVES_EVENT_SET_MAX_EVENTS,
    DERIVATIVES_EVENT_STREAM_MAX_BYTES,
    DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
    DERIVATIVES_JSONL_RECORD_MAX_BYTES,
    DERIVATIVES_MANIFEST_MAX_BYTES,
    DerivativesEventSetManifestV1,
    DerivativesEventSetRefV1,
    DerivativesEventStreamCursorV1,
    DerivativesEventStreamRefV1,
    EventStreamBoundaryKeyV1,
    EventStreamIntegritySummaryV1,
    event_stream_semantic_seed,
    update_event_stream_semantic_digest,
)
from .events import (
    EXPECTED_EVENT_STREAM_ID_V1,
    SINGLETON_EVENT_KINDS_PER_TIMESTAMP_V1,
    ContractTierEffectiveEventV1,
    DerivativeEventKindV1,
    DerivativeReplayEventV1,
    FundingSettlementEventV1,
    parse_derivative_replay_event,
)
from .funding import FundingSettlementStreamValidatorV1
from .snapshot_loader import (
    LoadedDerivativesSnapshotSetV1,
    LoadedSnapshotArtifactV1,
    load_non_promotable_derivatives_snapshot_set,
)


_READ_CHUNK_BYTES = 64 * 1024
_MAX_DIRECTORY_ENTRIES_SCANNED = 100_000
DERIVATIVES_EVENT_SOURCE_MAX_UNIQUE_SNAPSHOT_BYTES = 512 * 1024 * 1024
_REPARSE_POINT_ATTRIBUTE = 0x400
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_STABLE_FILE_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_size",
    "st_mtime_ns",
)
_STABLE_DIRECTORY_FIELDS = ("st_dev", "st_ino", "st_mode")


@dataclass(frozen=True, slots=True)
class _PathEntryIdentity:
    name: str
    stable_values: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _ResolvedFileIdentity:
    path: Path
    directory_chain: tuple[_PathEntryIdentity, ...]
    file_values: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class UncommittedDerivativeEventRecordV1:
    """One provisionally verified event and its non-committed read cursor."""

    event: DerivativeReplayEventV1
    cursor_after: DerivativesEventStreamCursorV1


@dataclass(frozen=True, slots=True)
class _StreamScanResult:
    cursor: DerivativesEventStreamCursorV1
    file_identity: _ResolvedFileIdentity


def _stable_values(value: os.stat_result, fields: tuple[str, ...]) -> tuple[int, ...]:
    return tuple(int(getattr(value, field, 0)) for field in fields)


def _is_reparse_or_junction(path: Path, value: os.stat_result) -> bool:
    if bool(int(getattr(value, "st_file_attributes", 0)) & _REPARSE_POINT_ATTRIBUTE):
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _scan_exact_directory_chain(root: Path) -> tuple[_PathEntryIdentity, ...]:
    """Resolve an absolute directory without accepting case aliases or links."""

    if not isinstance(root, Path) or not root.is_absolute():
        raise DerivativesBacktestContractError("event_source_root_invalid")
    anchor = Path(root.anchor)
    if not anchor.is_absolute():
        raise DerivativesBacktestContractError("event_source_root_invalid")
    cursor = anchor
    chain: list[_PathEntryIdentity] = []
    scanned = 0
    try:
        anchor_stat = anchor.lstat()
    except OSError as exc:
        raise DerivativesBacktestContractError("event_source_root_invalid") from exc
    if (
        not stat.S_ISDIR(anchor_stat.st_mode)
        or anchor.is_symlink()
        or _is_reparse_or_junction(anchor, anchor_stat)
    ):
        raise DerivativesBacktestContractError("event_source_root_invalid")
    for expected in root.parts[1:]:
        matches: list[tuple[str, os.stat_result]] = []
        try:
            with os.scandir(cursor) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _MAX_DIRECTORY_ENTRIES_SCANNED:
                        raise DerivativesBacktestContractError(
                            "event_source_directory_entry_limit_exceeded"
                        )
                    if entry.name.casefold() != expected.casefold():
                        continue
                    entry_stat = entry.stat(follow_symlinks=False)
                    entry_path = cursor / entry.name
                    if (
                        entry.is_symlink()
                        or _is_reparse_or_junction(entry_path, entry_stat)
                        or not stat.S_ISDIR(entry_stat.st_mode)
                    ):
                        raise DerivativesBacktestContractError(
                            "event_source_root_invalid"
                        )
                    matches.append((entry.name, entry_stat))
        except DerivativesBacktestContractError:
            raise
        except OSError as exc:
            raise DerivativesBacktestContractError("event_source_root_invalid") from exc
        if [name for name, _value in matches] != [expected]:
            code = "event_source_root_case_mismatch" if matches else "event_source_root_invalid"
            raise DerivativesBacktestContractError(code)
        next_cursor = cursor / expected
        try:
            entry_stat = next_cursor.lstat()
        except OSError as exc:
            raise DerivativesBacktestContractError("event_source_root_invalid") from exc
        chain.append(
            _PathEntryIdentity(
                name=expected,
                stable_values=_stable_values(entry_stat, _STABLE_DIRECTORY_FIELDS),
            )
        )
        cursor = next_cursor
    try:
        root_stat = cursor.lstat()
        resolved = cursor.resolve(strict=True)
    except OSError as exc:
        raise DerivativesBacktestContractError("event_source_root_invalid") from exc
    if (
        cursor != root
        or resolved != root
        or not stat.S_ISDIR(root_stat.st_mode)
        or root.is_symlink()
        or _is_reparse_or_junction(root, root_stat)
    ):
        raise DerivativesBacktestContractError("event_source_root_invalid")
    return tuple(chain)


def _resolve_exact_file(
    root: Path,
    relative_path: str,
) -> _ResolvedFileIdentity:
    relative = PurePosixPath(relative_path)
    cursor = root
    chain = list(_scan_exact_directory_chain(root))
    scanned = 0
    final_index = len(relative.parts) - 1
    for index, expected in enumerate(relative.parts):
        matches: list[tuple[str, os.stat_result]] = []
        try:
            with os.scandir(cursor) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _MAX_DIRECTORY_ENTRIES_SCANNED:
                        raise DerivativesBacktestContractError(
                            "event_source_directory_entry_limit_exceeded"
                        )
                    if entry.name.casefold() != expected.casefold():
                        continue
                    entry_stat = entry.stat(follow_symlinks=False)
                    entry_path = cursor / entry.name
                    invalid_link = entry.is_symlink() or _is_reparse_or_junction(
                        entry_path,
                        entry_stat,
                    )
                    expected_type = (
                        stat.S_ISREG(entry_stat.st_mode)
                        if index == final_index
                        else stat.S_ISDIR(entry_stat.st_mode)
                    )
                    if invalid_link or not expected_type:
                        raise DerivativesBacktestContractError(
                            "event_source_path_invalid"
                        )
                    matches.append((entry.name, entry_stat))
        except DerivativesBacktestContractError:
            raise
        except OSError as exc:
            raise DerivativesBacktestContractError("event_source_path_invalid") from exc
        if [name for name, _value in matches] != [expected]:
            code = "event_source_path_case_mismatch" if matches else "event_source_path_invalid"
            raise DerivativesBacktestContractError(code)
        cursor /= expected
        try:
            entry_stat = cursor.lstat()
            if not cursor.resolve(strict=True).is_relative_to(root):
                raise DerivativesBacktestContractError("event_source_path_invalid")
        except OSError as exc:
            raise DerivativesBacktestContractError("event_source_path_invalid") from exc
        if index != final_index:
            chain.append(
                _PathEntryIdentity(
                    name=expected,
                    stable_values=_stable_values(
                        entry_stat,
                        _STABLE_DIRECTORY_FIELDS,
                    ),
                )
            )
    return _ResolvedFileIdentity(
        path=cursor,
        directory_chain=tuple(chain),
        file_values=_stable_values(entry_stat, _STABLE_FILE_FIELDS),
    )


def _require_same_file_identity(
    observed: _ResolvedFileIdentity,
    expected: _ResolvedFileIdentity,
    *,
    changed_code: str,
) -> None:
    if observed != expected:
        raise DerivativesBacktestContractError(changed_code)


def _read_manifest(
    ref: DerivativesEventSetRefV1,
    *,
    event_root: Path,
    expected_identity: _ResolvedFileIdentity | None = None,
    changed_code: str | None = None,
) -> tuple[
    DerivativesEventSetManifestV1,
    bytes,
    _ResolvedFileIdentity,
]:
    identity = _resolve_exact_file(event_root, ref.manifest_relative_path)
    if expected_identity is not None:
        _require_same_file_identity(
            identity,
            expected_identity,
            changed_code=changed_code or "event_set_identity_changed",
        )
    if identity.file_values[3] > DERIVATIVES_MANIFEST_MAX_BYTES:
        raise DerivativesBacktestContractError("resource_limit_exceeded")
    try:
        raw = read_stable_regular_artifact_file(
            identity.path,
            parent=identity.path.parent,
            max_bytes=DERIVATIVES_MANIFEST_MAX_BYTES,
        )
    except ValueError as exc:
        raise DerivativesBacktestContractError(
            changed_code or "event_set_manifest_stable_read_failed"
        ) from exc
    after_identity = _resolve_exact_file(event_root, ref.manifest_relative_path)
    if after_identity != identity:
        raise DerivativesBacktestContractError(
            changed_code or "event_set_manifest_stable_read_failed"
        )
    try:
        decoded = decode_strict_json_artifact(raw, expected_type=dict)
        canonical = canonical_typed_json_bytes(decoded)
    except ValueError as exc:
        raise DerivativesBacktestContractError(
            changed_code or "event_set_manifest_json_invalid"
        ) from exc
    if raw != canonical:
        raise DerivativesBacktestContractError(
            changed_code or "event_set_manifest_bytes_noncanonical"
        )
    try:
        manifest = DerivativesEventSetManifestV1.from_dict(decoded)
        ref.validate_manifest(
            manifest,
            observed_relative_path=ref.manifest_relative_path,
            manifest_bytes=raw,
        )
    except DerivativesBacktestContractError as exc:
        if changed_code is not None:
            raise DerivativesBacktestContractError(changed_code) from exc
        raise
    return manifest, raw, identity


def _event_boundary(event: DerivativeReplayEventV1) -> EventStreamBoundaryKeyV1:
    return EventStreamBoundaryKeyV1(
        ts=event.header.ts,
        source_sequence=event.header.source_sequence,
        event_id=event.header.event_id,
    )


def _validate_event_against_stream(
    event: DerivativeReplayEventV1,
    stream: DerivativesEventStreamRefV1,
    *,
    previous: EventStreamBoundaryKeyV1 | None,
    ordinal: int,
    manifest: DerivativesEventSetManifestV1,
) -> EventStreamBoundaryKeyV1:
    header = event.header
    if (
        header.event_type is not stream.kind
        or header.source_ref.stream_id != stream.stream_id
        or stream.stream_id != EXPECTED_EVENT_STREAM_ID_V1[stream.kind]
    ):
        raise DerivativesBacktestContractError("event_stream_membership_mismatch")
    if (
        header.source_ref.source_registry_id not in stream.source_registry_ids
        or header.source_ref.parent_artifact_sha256
        not in stream.parent_raw_partition_sha256s
    ):
        raise DerivativesBacktestContractError("event_stream_lineage_mismatch")
    if not stream.coverage_start_ts <= header.ts < stream.coverage_end_ts:
        raise DerivativesBacktestContractError("event_stream_coverage_mismatch")
    current = _event_boundary(event)
    if previous is not None and current.order_key <= previous.order_key:
        raise DerivativesBacktestContractError("event_stream_order_invalid")
    if (
        previous is not None
        and stream.kind in SINGLETON_EVENT_KINDS_PER_TIMESTAMP_V1
        and current.ts == previous.ts
    ):
        raise DerivativesBacktestContractError(
            "event_stream_singleton_timestamp_invalid"
        )
    if stream.kind is DerivativeEventKindV1.BAR_CLOSE:
        expected_ts = stream.coverage_start_ts + timedelta(
            minutes=15 * (ordinal + 1)
        )
        if current.ts != expected_ts:
            raise DerivativesBacktestContractError("event_stream_gap_detected")
    if stream.kind is DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE:
        expected_catalog_index = ordinal + 1
        if expected_catalog_index >= len(manifest.snapshot_catalog):
            raise DerivativesBacktestContractError(
                "snapshot_catalog_event_count_mismatch"
            )
        expected_entry = manifest.snapshot_catalog[expected_catalog_index]
        if (
            type(event) is not ContractTierEffectiveEventV1
            or event.header.ts != expected_entry.activation_ts
            or event.snapshot_refs != expected_entry.refs
        ):
            raise DerivativesBacktestContractError(
                "snapshot_catalog_event_mismatch"
            )
    return current


def _expected_bar_count(stream: DerivativesEventStreamRefV1) -> int:
    duration = stream.coverage_end_ts - stream.coverage_start_ts
    duration_microseconds = (
        (duration.days * 24 * 60 * 60 + duration.seconds) * 1_000_000
        + duration.microseconds
    )
    slots = duration_microseconds // (15 * 60 * 1_000_000)
    return max(0, slots - 1)


def _assert_stream_result(
    stream: DerivativesEventStreamRefV1,
    *,
    raw_digest: str,
    byte_count: int,
    event_count: int,
    semantic_digest: str,
    first_key: EventStreamBoundaryKeyV1 | None,
    last_key: EventStreamBoundaryKeyV1 | None,
    source_registry_ids: set[str],
    parent_raw_partition_sha256s: set[str],
) -> None:
    if stream.kind is DerivativeEventKindV1.BAR_CLOSE and event_count != _expected_bar_count(
        stream
    ):
        raise DerivativesBacktestContractError("event_stream_gap_detected")
    integrity = EventStreamIntegritySummaryV1.create(
        kind=stream.kind,
        coverage_start_ts=stream.coverage_start_ts,
        coverage_end_ts=stream.coverage_end_ts,
        checked_event_count=event_count,
        semantic_event_digest=semantic_digest,
    )
    if (
        byte_count != stream.size_bytes
        or raw_digest != stream.raw_sha256
        or event_count != stream.event_count
        or semantic_digest != stream.semantic_event_digest
        or first_key != stream.first_key
        or last_key != stream.last_key
        or (
            event_count > 0
            and source_registry_ids != set(stream.source_registry_ids)
        )
        or (
            event_count > 0
            and parent_raw_partition_sha256s
            != set(stream.parent_raw_partition_sha256s)
        )
        or integrity != stream.integrity
    ):
        raise DerivativesBacktestContractError("event_stream_identity_mismatch")


def _iter_stream_records(
    stream: DerivativesEventStreamRefV1,
    *,
    event_root: Path,
    manifest: DerivativesEventSetManifestV1,
    cursor: DerivativesEventStreamCursorV1,
    expected_file_identity: _ResolvedFileIdentity | None,
    changed_code: str | None,
    emit_records: bool,
    on_event: Callable[[DerivativeReplayEventV1], None] | None,
    on_complete: Callable[[_StreamScanResult], None],
) -> Iterator[UncommittedDerivativeEventRecordV1]:
    identity = _resolve_exact_file(event_root, stream.relative_path)
    if expected_file_identity is not None:
        _require_same_file_identity(
            identity,
            expected_file_identity,
            changed_code=changed_code or "event_set_identity_changed",
        )
    declared_size = int(identity.file_values[3])
    if declared_size > DERIVATIVES_EVENT_STREAM_MAX_BYTES:
        raise DerivativesBacktestContractError("resource_limit_exceeded")
    if declared_size != stream.size_bytes:
        raise DerivativesBacktestContractError(
            changed_code or "event_stream_identity_mismatch"
        )
    cursor.validate_against(stream)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(identity.path, flags)
    except OSError as exc:
        raise DerivativesBacktestContractError(
            changed_code or "event_stream_stable_read_failed"
        ) from exc
    raw_hasher = hashlib.sha256()
    semantic_digest = event_stream_semantic_seed(stream.kind)
    buffer = bytearray()
    byte_count = 0
    event_count = 0
    first_key: EventStreamBoundaryKeyV1 | None = None
    last_key: EventStreamBoundaryKeyV1 | None = None
    source_registry_ids: set[str] = set()
    parent_raw_partition_sha256s: set[str] = set()
    cursor_checked = cursor.committed_event_count == 0
    if cursor_checked and (
        cursor.next_byte_offset != 0
        or cursor.raw_prefix_sha256 != _EMPTY_SHA256
        or cursor.semantic_prefix_sha256 != semantic_digest
        or cursor.last_committed_key is not None
    ):
        os.close(descriptor)
        raise DerivativesBacktestContractError("event_stream_cursor_mismatch")
    normal_eof = False
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or _stable_values(before, _STABLE_FILE_FIELDS) != identity.file_values
        ):
            raise DerivativesBacktestContractError(
                changed_code or "event_stream_stable_read_failed"
            )
        while True:
            remaining = DERIVATIVES_EVENT_STREAM_MAX_BYTES + 1 - byte_count - len(buffer)
            if remaining <= 0:
                raise DerivativesBacktestContractError("resource_limit_exceeded")
            try:
                chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            except OSError as exc:
                raise DerivativesBacktestContractError(
                    changed_code or "event_stream_stable_read_failed"
                ) from exc
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    if len(buffer) > DERIVATIVES_JSONL_RECORD_MAX_BYTES:
                        raise DerivativesBacktestContractError(
                            "resource_limit_exceeded"
                        )
                    break
                record_size = newline + 1
                if record_size > DERIVATIVES_JSONL_RECORD_MAX_BYTES:
                    raise DerivativesBacktestContractError("resource_limit_exceeded")
                raw_record = bytes(buffer[:record_size])
                del buffer[:record_size]
                payload = raw_record[:-1]
                if not payload:
                    raise DerivativesBacktestContractError(
                        changed_code or "event_stream_jsonl_invalid"
                    )
                if payload.startswith(b"\xef\xbb\xbf") or b"\r" in payload:
                    raise DerivativesBacktestContractError(
                        changed_code or "event_stream_jsonl_invalid"
                    )
                try:
                    decoded = decode_strict_json_artifact(
                        payload,
                        expected_type=dict,
                    )
                    if canonical_typed_json_bytes(decoded) != payload:
                        raise DerivativesBacktestContractError(
                            changed_code or "event_stream_bytes_noncanonical"
                        )
                    event = parse_derivative_replay_event(decoded)
                except DerivativesBacktestContractError as exc:
                    if changed_code is not None:
                        raise DerivativesBacktestContractError(changed_code) from exc
                    raise
                except ValueError as exc:
                    raise DerivativesBacktestContractError(
                        changed_code or "event_stream_json_invalid"
                    ) from exc
                if event_count >= DERIVATIVES_EVENT_STREAM_MAX_EVENTS:
                    raise DerivativesBacktestContractError("resource_limit_exceeded")
                current = _validate_event_against_stream(
                    event,
                    stream,
                    previous=last_key,
                    ordinal=event_count,
                    manifest=manifest,
                )
                event_count += 1
                if event_count > stream.event_count:
                    raise DerivativesBacktestContractError(
                        changed_code or "event_stream_identity_mismatch"
                    )
                byte_count += record_size
                raw_hasher.update(raw_record)
                semantic_digest = update_event_stream_semantic_digest(
                    semantic_digest,
                    event_id=current.event_id,
                )
                if first_key is None:
                    first_key = current
                last_key = current
                source_registry_ids.add(
                    event.header.source_ref.source_registry_id
                )
                parent_raw_partition_sha256s.add(
                    event.header.source_ref.parent_artifact_sha256
                )
                if on_event is not None:
                    on_event(event)
                cursor_after = DerivativesEventStreamCursorV1(
                    stream_fingerprint=stream.fingerprint,
                    next_byte_offset=byte_count,
                    committed_event_count=event_count,
                    raw_prefix_sha256=raw_hasher.hexdigest(),
                    semantic_prefix_sha256=semantic_digest,
                    last_committed_key=current,
                )
                if event_count == cursor.committed_event_count:
                    if cursor_after != cursor:
                        raise DerivativesBacktestContractError(
                            "event_stream_cursor_mismatch"
                        )
                    cursor_checked = True
                if emit_records and event_count > cursor.committed_event_count:
                    yield UncommittedDerivativeEventRecordV1(
                        event=event,
                        cursor_after=cursor_after,
                    )
        if buffer:
            raise DerivativesBacktestContractError(
                changed_code or "event_stream_final_lf_missing"
            )
        if not cursor_checked:
            raise DerivativesBacktestContractError(
                "event_stream_cursor_mismatch"
            )
        try:
            after = os.fstat(descriptor)
        except OSError as exc:
            raise DerivativesBacktestContractError(
                changed_code or "event_stream_stable_read_failed"
            ) from exc
        if _stable_values(after, _STABLE_FILE_FIELDS) != identity.file_values:
            raise DerivativesBacktestContractError(
                changed_code or "event_stream_stable_read_failed"
            )
        normal_eof = True
    finally:
        os.close(descriptor)
    if not normal_eof:
        return
    after_identity = _resolve_exact_file(event_root, stream.relative_path)
    if after_identity != identity:
        raise DerivativesBacktestContractError(
            changed_code or "event_stream_stable_read_failed"
        )
    _assert_stream_result(
        stream,
        raw_digest=raw_hasher.hexdigest(),
        byte_count=byte_count,
        event_count=event_count,
        semantic_digest=semantic_digest,
        first_key=first_key,
        last_key=last_key,
        source_registry_ids=source_registry_ids,
        parent_raw_partition_sha256s=parent_raw_partition_sha256s,
    )
    completed = DerivativesEventStreamCursorV1(
        stream_fingerprint=stream.fingerprint,
        next_byte_offset=byte_count,
        committed_event_count=event_count,
        raw_prefix_sha256=raw_hasher.hexdigest(),
        semantic_prefix_sha256=semantic_digest,
        last_committed_key=last_key,
    )
    completed.validate_against(stream)
    on_complete(
        _StreamScanResult(
            cursor=completed,
            file_identity=identity,
        )
    )


def _load_snapshot_segments(
    manifest: DerivativesEventSetManifestV1,
    *,
    snapshot_root: Path,
    existing_raw_bytes: Mapping[str, bytes] | None = None,
) -> tuple[LoadedDerivativesSnapshotSetV1, ...]:
    segments: list[LoadedDerivativesSnapshotSetV1] = []
    interned_raw_bytes = (
        {} if existing_raw_bytes is None else dict(existing_raw_bytes)
    )
    materialized_bytes = sum(len(raw) for raw in interned_raw_bytes.values())
    if materialized_bytes > DERIVATIVES_EVENT_SOURCE_MAX_UNIQUE_SNAPSHOT_BYTES:
        raise DerivativesBacktestContractError("resource_limit_exceeded")
    for index, entry in enumerate(manifest.snapshot_catalog):
        segment_end = (
            manifest.snapshot_catalog[index + 1].activation_ts
            if index + 1 < len(manifest.snapshot_catalog)
            else manifest.end_ts
        )
        loaded = load_non_promotable_derivatives_snapshot_set(
            entry.refs,
            snapshot_root=snapshot_root,
            start_ts=entry.activation_ts,
            end_ts=segment_end,
        )
        artifacts: list[LoadedSnapshotArtifactV1] = []
        for artifact in loaded.artifacts:
            identity = artifact.ref.fingerprint
            canonical_raw = interned_raw_bytes.get(identity)
            if canonical_raw is None:
                materialized_bytes += len(artifact.raw_bytes)
                if materialized_bytes > (
                    DERIVATIVES_EVENT_SOURCE_MAX_UNIQUE_SNAPSHOT_BYTES
                ):
                    raise DerivativesBacktestContractError(
                        "resource_limit_exceeded"
                    )
                interned_raw_bytes[identity] = artifact.raw_bytes
                canonical_raw = artifact.raw_bytes
            elif canonical_raw != artifact.raw_bytes:
                raise DerivativesBacktestContractError(
                    "event_set_identity_changed"
                )
            artifacts.append(
                LoadedSnapshotArtifactV1(
                    ref=artifact.ref,
                    raw_bytes=canonical_raw,
                )
            )
        segments.append(
            LoadedDerivativesSnapshotSetV1(
                refs=loaded.refs,
                replay_start_ts=loaded.replay_start_ts,
                replay_end_ts=loaded.replay_end_ts,
                instrument_contract=loaded.instrument_contract,
                position_tier=loaded.position_tier,
                execution_fee=loaded.execution_fee,
                funding_schedule=loaded.funding_schedule,
                artifacts=tuple(artifacts),
                snapshot_set_fingerprint=loaded.snapshot_set_fingerprint,
                authority_status=loaded.authority_status,
                capital_promotion_eligible=loaded.capital_promotion_eligible,
            )
        )
    return tuple(segments)


def _snapshot_file_identities(
    manifest: DerivativesEventSetManifestV1,
    *,
    snapshot_root: Path,
) -> Mapping[str, _ResolvedFileIdentity]:
    identities: dict[str, _ResolvedFileIdentity] = {}
    for entry in manifest.snapshot_catalog:
        for ref in (
            entry.refs.instrument,
            entry.refs.position_tier,
            entry.refs.execution_fee,
            entry.refs.funding_schedule,
        ):
            identity = _resolve_exact_file(snapshot_root, ref.relative_path)
            known = identities.setdefault(ref.relative_path.casefold(), identity)
            if known != identity:
                raise DerivativesBacktestContractError(
                    "event_set_identity_changed"
                )
    return MappingProxyType(identities)


class DerivativesEventVerificationPassV1:
    """Restartable identity verification; never an economic execution pass."""

    def __init__(
        self,
        source: PreflightedDerivativesEventSourceV1,
        *,
        cursors: Mapping[
            DerivativeEventKindV1,
            DerivativesEventStreamCursorV1,
        ]
        | None,
        snapshot_sets: tuple[LoadedDerivativesSnapshotSetV1, ...],
    ) -> None:
        self._source = source
        self._streams = {stream.kind: stream for stream in source.manifest.streams}
        if cursors is None:
            self._cursors = {
                kind: DerivativesEventStreamCursorV1.empty(stream)
                for kind, stream in self._streams.items()
            }
        else:
            if type(cursors) is not dict or set(cursors) != set(self._streams):
                raise DerivativesBacktestContractError(
                    "event_stream_cursor_set_invalid"
                )
            self._cursors = {}
            for kind, stream in self._streams.items():
                raw_cursor = cursors[kind]
                if type(raw_cursor) is not DerivativesEventStreamCursorV1:
                    raise DerivativesBacktestContractError(
                        "event_stream_cursor_set_invalid"
                    )
                cursor = DerivativesEventStreamCursorV1.from_dict(
                    raw_cursor.to_dict()
                )
                cursor.validate_against(stream)
                self._cursors[kind] = cursor
        self._snapshot_sets = snapshot_sets
        self._opened: set[DerivativeEventKindV1] = set()
        self._completed: dict[DerivativeEventKindV1, _StreamScanResult] = {}
        self._funding_validator = FundingSettlementStreamValidatorV1(
            snapshot_sets,
            start_ts=source.manifest.warmup_start_ts,
            end_ts=source.manifest.end_ts,
        )
        self._finished = False

    @property
    def snapshot_sets(self) -> tuple[LoadedDerivativesSnapshotSetV1, ...]:
        return self._snapshot_sets

    @property
    def economic_mutation_allowed(self) -> bool:
        return False

    def open_stream(
        self,
        kind: DerivativeEventKindV1,
    ) -> Iterator[UncommittedDerivativeEventRecordV1]:
        """Yield uncommitted records; callers must not mutate economic state."""
        if self._finished:
            raise DerivativesBacktestContractError("event_read_pass_finished")
        if type(kind) is not DerivativeEventKindV1 or kind not in self._streams:
            raise DerivativesBacktestContractError("event_stream_kind_invalid")
        if kind in self._opened:
            raise DerivativesBacktestContractError("event_stream_already_opened")
        self._opened.add(kind)
        stream = self._streams[kind]
        expected_identity = self._source._stream_file_identities[kind]

        def capture_event(event: DerivativeReplayEventV1) -> None:
            if kind is DerivativeEventKindV1.FUNDING_SETTLEMENT:
                if type(event) is not FundingSettlementEventV1:
                    raise DerivativesBacktestContractError(
                        "funding_event_type_invalid"
                    )
                self._funding_validator.consume(event)

        def capture_complete(result: _StreamScanResult) -> None:
            self._completed[kind] = result

        return _iter_stream_records(
            stream,
            event_root=self._source.event_root,
            manifest=self._source.manifest,
            cursor=self._cursors[kind],
            expected_file_identity=expected_identity,
            changed_code="event_set_identity_changed",
            emit_records=True,
            on_event=capture_event,
            on_complete=capture_complete,
        )

    def finish(self) -> tuple[DerivativesEventStreamCursorV1, ...]:
        if self._finished:
            raise DerivativesBacktestContractError("event_read_pass_finished")
        if set(self._completed) != set(self._streams):
            raise DerivativesBacktestContractError("event_read_pass_incomplete")
        self._funding_validator.finish()
        self._source._assert_artifact_set_identity_unchanged()
        self._finished = True
        return tuple(
            self._completed[stream.kind].cursor
            for stream in self._source.manifest.streams
        )


@dataclass(frozen=True, slots=True)
class PreflightedDerivativesEventSourceV1:
    """First-pass evidence that can create verification-only passes."""

    event_set_ref: DerivativesEventSetRefV1
    manifest: DerivativesEventSetManifestV1
    event_root: Path
    snapshot_root: Path
    preflight_cursors: tuple[DerivativesEventStreamCursorV1, ...]
    snapshot_sets: tuple[LoadedDerivativesSnapshotSetV1, ...]
    _event_root_identity: tuple[_PathEntryIdentity, ...]
    _snapshot_root_identity: tuple[_PathEntryIdentity, ...]
    _manifest_bytes: bytes
    _manifest_file_identity: _ResolvedFileIdentity
    _stream_file_identities: Mapping[
        DerivativeEventKindV1,
        _ResolvedFileIdentity,
    ]
    _snapshot_file_identities: Mapping[str, _ResolvedFileIdentity]

    @property
    def capital_promotion_eligible(self) -> bool:
        return False

    @property
    def economic_mutation_allowed(self) -> bool:
        return False

    def _assert_manifest_identity_unchanged(self) -> None:
        if (
            _scan_exact_directory_chain(self.event_root)
            != self._event_root_identity
        ):
            raise DerivativesBacktestContractError("event_set_identity_changed")
        manifest, raw, identity = _read_manifest(
            self.event_set_ref,
            event_root=self.event_root,
            expected_identity=self._manifest_file_identity,
            changed_code="event_set_identity_changed",
        )
        if raw != self._manifest_bytes or manifest != self.manifest or identity != self._manifest_file_identity:
            raise DerivativesBacktestContractError("event_set_identity_changed")

    def _assert_artifact_set_identity_unchanged(
        self,
    ) -> tuple[LoadedDerivativesSnapshotSetV1, ...]:
        """Re-seal every declared artifact before returning verified evidence."""

        self._assert_manifest_identity_unchanged()
        for stream in self.manifest.streams:
            _require_same_file_identity(
                _resolve_exact_file(self.event_root, stream.relative_path),
                self._stream_file_identities[stream.kind],
                changed_code="event_set_identity_changed",
            )
        if (
            _scan_exact_directory_chain(self.snapshot_root)
            != self._snapshot_root_identity
        ):
            raise DerivativesBacktestContractError("event_set_identity_changed")
        if (
            _snapshot_file_identities(
                self.manifest,
                snapshot_root=self.snapshot_root,
            )
            != self._snapshot_file_identities
        ):
            raise DerivativesBacktestContractError("event_set_identity_changed")
        try:
            existing_raw_bytes = {
                artifact.ref.fingerprint: artifact.raw_bytes
                for snapshot_set in self.snapshot_sets
                for artifact in snapshot_set.artifacts
            }
            snapshot_sets = _load_snapshot_segments(
                self.manifest,
                snapshot_root=self.snapshot_root,
                existing_raw_bytes=existing_raw_bytes,
            )
        except DerivativesBacktestContractError as exc:
            raise DerivativesBacktestContractError(
                "event_set_identity_changed"
            ) from exc
        if snapshot_sets != self.snapshot_sets:
            raise DerivativesBacktestContractError("event_set_identity_changed")
        if (
            _snapshot_file_identities(
                self.manifest,
                snapshot_root=self.snapshot_root,
            )
            != self._snapshot_file_identities
        ):
            raise DerivativesBacktestContractError("event_set_identity_changed")
        return snapshot_sets

    def start_verification_pass(
        self,
        *,
        cursors: Mapping[
            DerivativeEventKindV1,
            DerivativesEventStreamCursorV1,
        ]
        | None = None,
    ) -> DerivativesEventVerificationPassV1:
        snapshot_sets = self._assert_artifact_set_identity_unchanged()
        return DerivativesEventVerificationPassV1(
            self,
            cursors=cursors,
            snapshot_sets=snapshot_sets,
        )


def preflight_non_promotable_derivatives_event_source(
    event_set_ref: DerivativesEventSetRefV1,
    *,
    event_root: Path,
    snapshot_root: Path,
) -> PreflightedDerivativesEventSourceV1:
    """Validate an entire synthetic event-set before economic state exists."""

    if type(event_set_ref) is not DerivativesEventSetRefV1:
        raise DerivativesBacktestContractError("event_set_ref_invalid")
    ref = DerivativesEventSetRefV1.from_dict(event_set_ref.to_dict())
    event_root_identity = _scan_exact_directory_chain(event_root)
    snapshot_root_identity = _scan_exact_directory_chain(snapshot_root)
    manifest, manifest_bytes, manifest_identity = _read_manifest(
        ref,
        event_root=event_root,
    )
    snapshot_sets = _load_snapshot_segments(
        manifest,
        snapshot_root=snapshot_root,
    )
    snapshot_file_identities = _snapshot_file_identities(
        manifest,
        snapshot_root=snapshot_root,
    )
    results: dict[DerivativeEventKindV1, _StreamScanResult] = {}
    funding_validator = FundingSettlementStreamValidatorV1(
        snapshot_sets,
        start_ts=manifest.warmup_start_ts,
        end_ts=manifest.end_ts,
    )

    for stream in manifest.streams:
        def capture_event(
            event: DerivativeReplayEventV1,
            *,
            kind: DerivativeEventKindV1 = stream.kind,
        ) -> None:
            if kind is DerivativeEventKindV1.FUNDING_SETTLEMENT:
                if type(event) is not FundingSettlementEventV1:
                    raise DerivativesBacktestContractError(
                        "funding_event_type_invalid"
                    )
                funding_validator.consume(event)

        def capture_complete(
            result: _StreamScanResult,
            *,
            kind: DerivativeEventKindV1 = stream.kind,
        ) -> None:
            results[kind] = result

        for _record in _iter_stream_records(
            stream,
            event_root=event_root,
            manifest=manifest,
            cursor=DerivativesEventStreamCursorV1.empty(stream),
            expected_file_identity=None,
            changed_code=None,
            emit_records=False,
            on_event=capture_event,
            on_complete=capture_complete,
        ):
            raise AssertionError("preflight reader must not emit records")
    if set(results) != {stream.kind for stream in manifest.streams}:
        raise DerivativesBacktestContractError("event_set_preflight_incomplete")
    if sum(result.cursor.committed_event_count for result in results.values()) > (
        DERIVATIVES_EVENT_SET_MAX_EVENTS
    ):
        raise DerivativesBacktestContractError("resource_limit_exceeded")
    funding_validator.finish()
    # Re-read the manifest after all streams and snapshot artifacts.  This
    # closes replacement races around the first-pass identity as a whole.
    final_manifest, final_bytes, final_identity = _read_manifest(
        ref,
        event_root=event_root,
        expected_identity=manifest_identity,
        changed_code="event_set_identity_changed",
    )
    if final_manifest != manifest or final_bytes != manifest_bytes:
        raise DerivativesBacktestContractError("event_set_identity_changed")
    if (
        _snapshot_file_identities(
            manifest,
            snapshot_root=snapshot_root,
        )
        != snapshot_file_identities
    ):
        raise DerivativesBacktestContractError("event_set_identity_changed")
    return PreflightedDerivativesEventSourceV1(
        event_set_ref=ref,
        manifest=manifest,
        event_root=event_root,
        snapshot_root=snapshot_root,
        preflight_cursors=tuple(
            results[stream.kind].cursor for stream in manifest.streams
        ),
        snapshot_sets=snapshot_sets,
        _event_root_identity=event_root_identity,
        _snapshot_root_identity=snapshot_root_identity,
        _manifest_bytes=manifest_bytes,
        _manifest_file_identity=final_identity,
        _stream_file_identities=MappingProxyType(
            {
                kind: result.file_identity
                for kind, result in results.items()
            }
        ),
        _snapshot_file_identities=snapshot_file_identities,
    )


__all__ = [
    "DERIVATIVES_EVENT_SOURCE_MAX_UNIQUE_SNAPSHOT_BYTES",
    "DerivativesEventVerificationPassV1",
    "PreflightedDerivativesEventSourceV1",
    "UncommittedDerivativeEventRecordV1",
    "preflight_non_promotable_derivatives_event_source",
]
