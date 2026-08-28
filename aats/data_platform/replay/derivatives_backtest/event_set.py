"""Strict identities for a restartable derivatives event-set.

This module defines only immutable contracts and streaming digest primitives.
It performs no filesystem, database, network, or runtime lookup.  A later
event-source layer must verify the referenced raw bytes twice before economic
state may change.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from aats.data_platform.governance.typed_json_identity import (
    canonical_typed_json_bytes,
    typed_json_sha256,
)

from .contracts import (
    DERIVATIVES_BACKTEST_FAMILY,
    DERIVATIVES_BACKTEST_SYMBOL,
    DERIVATIVES_BACKTEST_TIMEFRAME,
    DerivativesBacktestContractError,
)
from .events import (
    DERIVATIVES_EVENT_ORDERING_POLICY_ID,
    DERIVATIVES_MAX_SOURCE_SEQUENCE,
    DERIVATIVES_REPLAY_EVENT_SCHEMA,
    EXPECTED_EVENT_STREAM_ID_V1,
    SINGLETON_EVENT_KINDS_PER_TIMESTAMP_V1,
    DerivativeEventKindV1,
)
from .snapshot_refs import (
    DerivativesSnapshotRefsV1,
    ImmutableSnapshotRefV1,
    validate_snapshot_transition,
)
from .wire import (
    canonical_utc_timestamp,
    require_canonical_utc_timestamp,
    require_canonical_uuid,
    require_exact_int,
    require_exact_mapping_keys,
    require_identifier,
    require_safe_relative_posix_path,
    require_sha256,
    require_utc_datetime,
)


DERIVATIVES_EVENT_SET_REF_SCHEMA = "derivatives-event-set-ref/v1"
DERIVATIVES_EVENT_SET_MANIFEST_SCHEMA = "derivatives-event-set/v1"
DERIVATIVES_EVENT_STREAM_REF_SCHEMA = "derivatives-event-stream-ref/v1"
DERIVATIVES_EVENT_STREAM_DIGEST_SCHEMA = "derivatives-event-stream-digest/v1"
DERIVATIVES_EVENT_STREAM_CURSOR_SCHEMA = "derivatives-event-stream-cursor/v1"
DERIVATIVES_EVENT_STREAM_INTEGRITY_SCHEMA = (
    "derivatives-event-stream-integrity/v1"
)
DERIVATIVES_SNAPSHOT_CATALOG_ENTRY_SCHEMA = (
    "derivatives-snapshot-catalog-entry/v1"
)
DERIVATIVES_RESOURCE_POLICY_ID = "derivatives-backtest-resource-policy/v1"
DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_ID = (
    "derivatives-event-stream-integrity-policy"
)
DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_VERSION = "v1"
DERIVATIVES_EVENT_SET_AUTHORITY_SYNTHETIC = "synthetic_test_only"
DERIVATIVES_VENUE = "OKX"
DERIVATIVES_INSTRUMENT_TYPE = "SWAP"
DERIVATIVES_CONTRACT_TYPE = "linear"
DERIVATIVES_SETTLE_CURRENCY = "USDT"
DERIVATIVES_MARGIN_MODE = "isolated"
DERIVATIVES_POSITION_MODE = "single_position"

DERIVATIVES_JSONL_RECORD_MAX_BYTES = 1024 * 1024
DERIVATIVES_EVENT_STREAM_MAX_BYTES = 512 * 1024 * 1024
DERIVATIVES_EVENT_STREAM_MAX_EVENTS = 5_000_000
DERIVATIVES_EVENT_SET_MAX_EVENTS = 10_000_000
DERIVATIVES_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
DERIVATIVES_MANIFEST_ENVELOPE_RESERVE_BYTES = 64 * 1024
DERIVATIVES_MAX_SNAPSHOT_CATALOG_ENTRIES = 512
DERIVATIVES_MAX_SOURCE_REGISTRY_IDS = 64
DERIVATIVES_MAX_PARENT_RAW_PARTITIONS = 4_096

_EMPTY_RAW_SHA256 = hashlib.sha256(b"").hexdigest()
_STREAM_KINDS = tuple(DerivativeEventKindV1)
_STREAM_KIND_NAMES = frozenset(kind.value for kind in _STREAM_KINDS)
_DIGEST_DOMAIN = (DERIVATIVES_EVENT_STREAM_DIGEST_SCHEMA + "\0").encode("ascii")
_CONTINUITY_POLICY_ID_BY_KIND = {
    DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE: (
        "snapshot-catalog-transition/v1"
    ),
    DerivativeEventKindV1.INDEX_PRICE: "event-driven-price-freshness/v1",
    DerivativeEventKindV1.MARK_PRICE: "event-driven-price-freshness/v1",
    DerivativeEventKindV1.FUNDING_SETTLEMENT: "funding-schedule-continuity/v1",
    DerivativeEventKindV1.TRADABLE: "event-driven-tradable/v1",
    DerivativeEventKindV1.BAR_CLOSE: "fixed-15m-evaluation-bars/v1",
}
DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_FINGERPRINT = typed_json_sha256(
    {
        "schema": "derivatives-event-stream-integrity-policy/v1",
        "policy_id": DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_ID,
        "policy_version": DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_VERSION,
        "stream_order_key": ["ts", "source_sequence"],
        "duplicate_event_ids": "forbidden",
        "source_order_violations": "forbidden",
        "singleton_timestamp_violations": "forbidden",
        "singleton_event_kinds": sorted(
            kind.value for kind in SINGLETON_EVENT_KINDS_PER_TIMESTAMP_V1
        ),
        "continuity_policy_by_kind": {
            kind.value: policy_id
            for kind, policy_id in _CONTINUITY_POLICY_ID_BY_KIND.items()
        },
    }
)


def event_stream_semantic_seed(kind: DerivativeEventKindV1) -> str:
    if type(kind) is not DerivativeEventKindV1:
        raise DerivativesBacktestContractError("event_stream_kind_invalid")
    return hashlib.sha256(_DIGEST_DOMAIN + kind.value.encode("ascii")).hexdigest()


def update_event_stream_semantic_digest(
    current_digest: str,
    *,
    event_id: str,
) -> str:
    """Extend the domain-separated event-ID chain in O(1) memory."""

    current = require_sha256(current_digest, "current_event_stream_digest")
    identity = require_sha256(event_id, "event_id")
    return hashlib.sha256(
        _DIGEST_DOMAIN + bytes.fromhex(current) + bytes.fromhex(identity)
    ).hexdigest()


def _stream_integrity_evidence_identity(
    *,
    kind: DerivativeEventKindV1,
    coverage_start_ts: datetime,
    coverage_end_ts: datetime,
    checked_event_count: int,
    semantic_event_digest: str,
    gap_count: int,
    duplicate_event_id_count: int,
    source_order_violation_count: int,
    singleton_timestamp_violation_count: int,
) -> dict[str, Any]:
    return {
        "schema": DERIVATIVES_EVENT_STREAM_INTEGRITY_SCHEMA,
        "policy": {
            "policy_id": DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_ID,
            "policy_version": DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_VERSION,
            "policy_fingerprint": (
                DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_FINGERPRINT
            ),
            "continuity_policy_id": _CONTINUITY_POLICY_ID_BY_KIND[kind],
        },
        "kind": kind.value,
        "checked_coverage": {
            "start": canonical_utc_timestamp(
                coverage_start_ts,
                "coverage_start_ts",
            ),
            "end": canonical_utc_timestamp(
                coverage_end_ts,
                "coverage_end_ts",
            ),
        },
        "checked_event_count": checked_event_count,
        "semantic_event_digest": semantic_event_digest,
        "results": {
            "gap_count": gap_count,
            "duplicate_event_id_count": duplicate_event_id_count,
            "source_order_violation_count": source_order_violation_count,
            "singleton_timestamp_violation_count": (
                singleton_timestamp_violation_count
            ),
        },
    }


@dataclass(frozen=True, slots=True)
class EventStreamIntegritySummaryV1:
    """Expected stream-integrity result; formal readers must recompute it."""

    kind: DerivativeEventKindV1
    coverage_start_ts: datetime
    coverage_end_ts: datetime
    checked_event_count: int
    semantic_event_digest: str
    gap_count: int
    duplicate_event_id_count: int
    source_order_violation_count: int
    singleton_timestamp_violation_count: int
    evidence_digest: str

    def __post_init__(self) -> None:
        if type(self.kind) is not DerivativeEventKindV1:
            raise DerivativesBacktestContractError("event_stream_kind_invalid")
        start = require_utc_datetime(self.coverage_start_ts, "coverage_start_ts")
        end = require_utc_datetime(self.coverage_end_ts, "coverage_end_ts")
        if end <= start:
            raise DerivativesBacktestContractError(
                "event_stream_integrity_coverage_invalid"
            )
        require_exact_int(
            self.checked_event_count,
            "checked_event_count",
            minimum=0,
            maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
        )
        require_sha256(self.semantic_event_digest, "semantic_event_digest")
        for value, field_name in (
            (self.gap_count, "gap_count"),
            (self.duplicate_event_id_count, "duplicate_event_id_count"),
            (self.source_order_violation_count, "source_order_violation_count"),
            (
                self.singleton_timestamp_violation_count,
                "singleton_timestamp_violation_count",
            ),
        ):
            if require_exact_int(
                value,
                field_name,
                minimum=0,
                maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
            ) != 0:
                raise DerivativesBacktestContractError(
                    "event_stream_integrity_failed",
                    field=field_name,
                )
        require_sha256(self.evidence_digest, "integrity_evidence_digest")
        expected = typed_json_sha256(
            _stream_integrity_evidence_identity(
                kind=self.kind,
                coverage_start_ts=start,
                coverage_end_ts=end,
                checked_event_count=self.checked_event_count,
                semantic_event_digest=self.semantic_event_digest,
                gap_count=self.gap_count,
                duplicate_event_id_count=self.duplicate_event_id_count,
                source_order_violation_count=self.source_order_violation_count,
                singleton_timestamp_violation_count=(
                    self.singleton_timestamp_violation_count
                ),
            )
        )
        if self.evidence_digest != expected:
            raise DerivativesBacktestContractError(
                "event_stream_integrity_evidence_mismatch"
            )

    @classmethod
    def create(
        cls,
        *,
        kind: DerivativeEventKindV1,
        coverage_start_ts: datetime,
        coverage_end_ts: datetime,
        checked_event_count: int,
        semantic_event_digest: str,
    ) -> EventStreamIntegritySummaryV1:
        if type(kind) is not DerivativeEventKindV1:
            raise DerivativesBacktestContractError("event_stream_kind_invalid")
        identity = _stream_integrity_evidence_identity(
            kind=kind,
            coverage_start_ts=coverage_start_ts,
            coverage_end_ts=coverage_end_ts,
            checked_event_count=checked_event_count,
            semantic_event_digest=semantic_event_digest,
            gap_count=0,
            duplicate_event_id_count=0,
            source_order_violation_count=0,
            singleton_timestamp_violation_count=0,
        )
        return cls(
            kind=kind,
            coverage_start_ts=coverage_start_ts,
            coverage_end_ts=coverage_end_ts,
            checked_event_count=checked_event_count,
            semantic_event_digest=semantic_event_digest,
            gap_count=0,
            duplicate_event_id_count=0,
            source_order_violation_count=0,
            singleton_timestamp_violation_count=0,
            evidence_digest=typed_json_sha256(identity),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **_stream_integrity_evidence_identity(
                kind=self.kind,
                coverage_start_ts=self.coverage_start_ts,
                coverage_end_ts=self.coverage_end_ts,
                checked_event_count=self.checked_event_count,
                semantic_event_digest=self.semantic_event_digest,
                gap_count=self.gap_count,
                duplicate_event_id_count=self.duplicate_event_id_count,
                source_order_violation_count=self.source_order_violation_count,
                singleton_timestamp_violation_count=(
                    self.singleton_timestamp_violation_count
                ),
            ),
            "evidence_digest": self.evidence_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EventStreamIntegritySummaryV1:
        payload = require_exact_mapping_keys(
            value,
            {
                "schema",
                "policy",
                "kind",
                "checked_coverage",
                "checked_event_count",
                "semantic_event_digest",
                "results",
                "evidence_digest",
            },
            "event_stream_integrity_shape_invalid",
        )
        if payload["schema"] != DERIVATIVES_EVENT_STREAM_INTEGRITY_SCHEMA:
            raise DerivativesBacktestContractError(
                "event_stream_integrity_schema_invalid"
            )
        if type(payload["kind"]) is not str:
            raise DerivativesBacktestContractError("event_stream_kind_invalid")
        try:
            kind = DerivativeEventKindV1(payload["kind"])
        except ValueError as exc:
            raise DerivativesBacktestContractError("event_stream_kind_invalid") from exc
        policy = require_exact_mapping_keys(
            payload["policy"],
            {
                "policy_id",
                "policy_version",
                "policy_fingerprint",
                "continuity_policy_id",
            },
            "event_stream_integrity_policy_invalid",
        )
        if (
            policy["policy_id"] != DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_ID
            or policy["policy_version"]
            != DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_VERSION
            or policy["policy_fingerprint"]
            != DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_FINGERPRINT
            or policy["continuity_policy_id"]
            != _CONTINUITY_POLICY_ID_BY_KIND[kind]
        ):
            raise DerivativesBacktestContractError(
                "event_stream_integrity_policy_invalid"
            )
        coverage = require_exact_mapping_keys(
            payload["checked_coverage"],
            {"start", "end"},
            "event_stream_integrity_coverage_invalid",
        )
        results = require_exact_mapping_keys(
            payload["results"],
            {
                "gap_count",
                "duplicate_event_id_count",
                "source_order_violation_count",
                "singleton_timestamp_violation_count",
            },
            "event_stream_integrity_results_invalid",
        )
        return cls(
            kind=kind,
            coverage_start_ts=require_canonical_utc_timestamp(
                coverage["start"],
                "coverage_start_ts",
            ),
            coverage_end_ts=require_canonical_utc_timestamp(
                coverage["end"],
                "coverage_end_ts",
            ),
            checked_event_count=require_exact_int(
                payload["checked_event_count"],
                "checked_event_count",
                minimum=0,
                maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
            ),
            semantic_event_digest=require_sha256(
                payload["semantic_event_digest"],
                "semantic_event_digest",
            ),
            gap_count=require_exact_int(
                results["gap_count"],
                "gap_count",
                minimum=0,
                maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
            ),
            duplicate_event_id_count=require_exact_int(
                results["duplicate_event_id_count"],
                "duplicate_event_id_count",
                minimum=0,
                maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
            ),
            source_order_violation_count=require_exact_int(
                results["source_order_violation_count"],
                "source_order_violation_count",
                minimum=0,
                maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
            ),
            singleton_timestamp_violation_count=require_exact_int(
                results["singleton_timestamp_violation_count"],
                "singleton_timestamp_violation_count",
                minimum=0,
                maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
            ),
            evidence_digest=require_sha256(
                payload["evidence_digest"],
                "integrity_evidence_digest",
            ),
        )


@dataclass(frozen=True, slots=True)
class EventStreamBoundaryKeyV1:
    ts: datetime
    source_sequence: int
    event_id: str

    def __post_init__(self) -> None:
        require_utc_datetime(self.ts, "event_ts")
        require_exact_int(
            self.source_sequence,
            "source_sequence",
            minimum=0,
            maximum=DERIVATIVES_MAX_SOURCE_SEQUENCE,
        )
        require_sha256(self.event_id, "event_id")

    @property
    def order_key(self) -> tuple[datetime, int]:
        return self.ts, self.source_sequence

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": canonical_utc_timestamp(self.ts, "event_ts"),
            "source_sequence": self.source_sequence,
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EventStreamBoundaryKeyV1:
        payload = require_exact_mapping_keys(
            value,
            {"ts", "source_sequence", "event_id"},
            "event_stream_boundary_key_shape_invalid",
        )
        return cls(
            ts=require_canonical_utc_timestamp(payload["ts"], "event_ts"),
            source_sequence=require_exact_int(
                payload["source_sequence"],
                "source_sequence",
                minimum=0,
                maximum=DERIVATIVES_MAX_SOURCE_SEQUENCE,
            ),
            event_id=require_sha256(payload["event_id"], "event_id"),
        )


def _sorted_unique_sha256s(
    value: Any,
    *,
    field_name: str,
    maximum: int,
) -> tuple[str, ...]:
    if type(value) not in {tuple, list} or not 1 <= len(value) <= maximum:
        raise DerivativesBacktestContractError(
            "event_stream_lineage_invalid",
            field=field_name,
        )
    resolved = tuple(require_sha256(item, field_name) for item in value)
    if resolved != tuple(sorted(set(resolved))):
        raise DerivativesBacktestContractError(
            "event_stream_lineage_noncanonical",
            field=field_name,
        )
    return resolved


def _sorted_unique_uuids(
    value: Any,
    *,
    field_name: str,
    maximum: int,
) -> tuple[str, ...]:
    if type(value) not in {tuple, list} or not 1 <= len(value) <= maximum:
        raise DerivativesBacktestContractError(
            "event_stream_lineage_invalid",
            field=field_name,
        )
    resolved = tuple(require_canonical_uuid(item, field_name) for item in value)
    if resolved != tuple(sorted(set(resolved))):
        raise DerivativesBacktestContractError(
            "event_stream_lineage_noncanonical",
            field=field_name,
        )
    return resolved


@dataclass(frozen=True, slots=True)
class DerivativesEventStreamRefV1:
    kind: DerivativeEventKindV1
    stream_id: str
    relative_path: str
    size_bytes: int
    raw_sha256: str
    event_count: int
    semantic_event_digest: str
    integrity: EventStreamIntegritySummaryV1
    first_key: EventStreamBoundaryKeyV1 | None
    last_key: EventStreamBoundaryKeyV1 | None
    coverage_start_ts: datetime
    coverage_end_ts: datetime
    source_registry_ids: tuple[str, ...]
    parent_raw_partition_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.kind) is not DerivativeEventKindV1:
            raise DerivativesBacktestContractError("event_stream_kind_invalid")
        if self.stream_id != EXPECTED_EVENT_STREAM_ID_V1[self.kind]:
            raise DerivativesBacktestContractError("event_stream_id_mismatch")
        require_safe_relative_posix_path(self.relative_path, "stream_relative_path")
        require_exact_int(
            self.size_bytes,
            "stream_size_bytes",
            minimum=0,
            maximum=DERIVATIVES_EVENT_STREAM_MAX_BYTES,
        )
        require_sha256(self.raw_sha256, "stream_raw_sha256")
        count = require_exact_int(
            self.event_count,
            "event_count",
            minimum=0,
            maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
        )
        require_sha256(self.semantic_event_digest, "semantic_event_digest")
        if type(self.integrity) is not EventStreamIntegritySummaryV1:
            raise DerivativesBacktestContractError(
                "event_stream_integrity_invalid"
            )
        integrity = EventStreamIntegritySummaryV1.from_dict(
            self.integrity.to_dict()
        )
        start = require_utc_datetime(self.coverage_start_ts, "coverage_start_ts")
        end = require_utc_datetime(self.coverage_end_ts, "coverage_end_ts")
        if end <= start:
            raise DerivativesBacktestContractError("event_stream_coverage_invalid")
        registries = _sorted_unique_uuids(
            self.source_registry_ids,
            field_name="source_registry_id",
            maximum=DERIVATIVES_MAX_SOURCE_REGISTRY_IDS,
        )
        parents = _sorted_unique_sha256s(
            self.parent_raw_partition_sha256s,
            field_name="parent_raw_partition_sha256",
            maximum=DERIVATIVES_MAX_PARENT_RAW_PARTITIONS,
        )
        object.__setattr__(self, "source_registry_ids", registries)
        object.__setattr__(self, "parent_raw_partition_sha256s", parents)
        if (
            integrity.kind is not self.kind
            or integrity.coverage_start_ts != start
            or integrity.coverage_end_ts != end
            or integrity.checked_event_count != count
            or integrity.semantic_event_digest != self.semantic_event_digest
        ):
            raise DerivativesBacktestContractError(
                "event_stream_integrity_ref_mismatch"
            )
        object.__setattr__(self, "integrity", integrity)
        if count == 0:
            if (
                self.first_key is not None
                or self.last_key is not None
                or self.size_bytes != 0
                or self.raw_sha256 != _EMPTY_RAW_SHA256
                or self.semantic_event_digest != event_stream_semantic_seed(self.kind)
            ):
                raise DerivativesBacktestContractError(
                    "empty_event_stream_identity_invalid"
                )
            return
        if (
            type(self.first_key) is not EventStreamBoundaryKeyV1
            or type(self.last_key) is not EventStreamBoundaryKeyV1
            or self.size_bytes <= 0
        ):
            raise DerivativesBacktestContractError("event_stream_boundary_missing")
        first = EventStreamBoundaryKeyV1.from_dict(self.first_key.to_dict())
        last = EventStreamBoundaryKeyV1.from_dict(self.last_key.to_dict())
        if (count == 1 and first != last) or (
            count > 1 and first.order_key >= last.order_key
        ):
            raise DerivativesBacktestContractError("event_stream_boundary_order_invalid")
        if first.ts < start or first.ts >= end or last.ts < start or last.ts >= end:
            raise DerivativesBacktestContractError("event_stream_boundary_outside_coverage")
        object.__setattr__(self, "first_key", first)
        object.__setattr__(self, "last_key", last)

    def raw_identity_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "stream_id": self.stream_id,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
            "raw_sha256": self.raw_sha256,
        }

    def semantic_identity_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "stream_id": self.stream_id,
            "event_schema": DERIVATIVES_REPLAY_EVENT_SCHEMA,
            "event_count": self.event_count,
            "semantic_event_digest": self.semantic_event_digest,
            "integrity": self.integrity.to_dict(),
            "first_key": None if self.first_key is None else self.first_key.to_dict(),
            "last_key": None if self.last_key is None else self.last_key.to_dict(),
            "coverage": {
                "start": canonical_utc_timestamp(
                    self.coverage_start_ts,
                    "coverage_start_ts",
                ),
                "end": canonical_utc_timestamp(
                    self.coverage_end_ts,
                    "coverage_end_ts",
                ),
            },
            "source_registry_ids": list(self.source_registry_ids),
            "parent_raw_partition_sha256s": list(
                self.parent_raw_partition_sha256s
            ),
        }

    @property
    def fingerprint(self) -> str:
        return typed_json_sha256(
            {
                "schema": DERIVATIVES_EVENT_STREAM_REF_SCHEMA,
                "raw": self.raw_identity_dict(),
                "semantic": self.semantic_identity_dict(),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DERIVATIVES_EVENT_STREAM_REF_SCHEMA,
            **self.raw_identity_dict(),
            **self.semantic_identity_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DerivativesEventStreamRefV1:
        payload = require_exact_mapping_keys(
            value,
            {
                "schema",
                "kind",
                "stream_id",
                "relative_path",
                "size_bytes",
                "raw_sha256",
                "event_schema",
                "event_count",
                "semantic_event_digest",
                "integrity",
                "first_key",
                "last_key",
                "coverage",
                "source_registry_ids",
                "parent_raw_partition_sha256s",
            },
            "event_stream_ref_shape_invalid",
        )
        if payload["schema"] != DERIVATIVES_EVENT_STREAM_REF_SCHEMA:
            raise DerivativesBacktestContractError("event_stream_ref_schema_invalid")
        if payload["event_schema"] != DERIVATIVES_REPLAY_EVENT_SCHEMA:
            raise DerivativesBacktestContractError("event_stream_event_schema_invalid")
        if type(payload["kind"]) is not str:
            raise DerivativesBacktestContractError("event_stream_kind_invalid")
        try:
            kind = DerivativeEventKindV1(payload["kind"])
        except ValueError as exc:
            raise DerivativesBacktestContractError("event_stream_kind_invalid") from exc
        coverage = require_exact_mapping_keys(
            payload["coverage"],
            {"start", "end"},
            "event_stream_coverage_invalid",
        )
        if (
            type(payload["source_registry_ids"]) is not list
            or type(payload["parent_raw_partition_sha256s"]) is not list
        ):
            raise DerivativesBacktestContractError(
                "event_stream_lineage_wire_invalid"
            )
        return cls(
            kind=kind,
            stream_id=require_identifier(payload["stream_id"], "stream_id"),
            relative_path=require_safe_relative_posix_path(
                payload["relative_path"],
                "stream_relative_path",
            ),
            size_bytes=require_exact_int(
                payload["size_bytes"],
                "stream_size_bytes",
                minimum=0,
                maximum=DERIVATIVES_EVENT_STREAM_MAX_BYTES,
            ),
            raw_sha256=require_sha256(payload["raw_sha256"], "stream_raw_sha256"),
            event_count=require_exact_int(
                payload["event_count"],
                "event_count",
                minimum=0,
                maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
            ),
            semantic_event_digest=require_sha256(
                payload["semantic_event_digest"],
                "semantic_event_digest",
            ),
            integrity=EventStreamIntegritySummaryV1.from_dict(
                payload["integrity"]
            ),
            first_key=(
                None
                if payload["first_key"] is None
                else EventStreamBoundaryKeyV1.from_dict(payload["first_key"])
            ),
            last_key=(
                None
                if payload["last_key"] is None
                else EventStreamBoundaryKeyV1.from_dict(payload["last_key"])
            ),
            coverage_start_ts=require_canonical_utc_timestamp(
                coverage["start"],
                "coverage_start_ts",
            ),
            coverage_end_ts=require_canonical_utc_timestamp(
                coverage["end"],
                "coverage_end_ts",
            ),
            source_registry_ids=_sorted_unique_uuids(
                payload["source_registry_ids"],
                field_name="source_registry_id",
                maximum=DERIVATIVES_MAX_SOURCE_REGISTRY_IDS,
            ),
            parent_raw_partition_sha256s=_sorted_unique_sha256s(
                payload["parent_raw_partition_sha256s"],
                field_name="parent_raw_partition_sha256",
                maximum=DERIVATIVES_MAX_PARENT_RAW_PARTITIONS,
            ),
        )


@dataclass(frozen=True, slots=True)
class SnapshotSetCatalogEntryV1:
    activation_ts: datetime
    refs: DerivativesSnapshotRefsV1

    def __post_init__(self) -> None:
        point = require_utc_datetime(self.activation_ts, "snapshot_activation_ts")
        if type(self.refs) is not DerivativesSnapshotRefsV1:
            raise DerivativesBacktestContractError("snapshot_set_invalid")
        validated = DerivativesSnapshotRefsV1.from_dict(self.refs.to_dict())
        if validated != self.refs:
            raise DerivativesBacktestContractError(
                "snapshot_catalog_revalidation_mismatch"
            )
        validated.validate_at(point)
        object.__setattr__(self, "refs", validated)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DERIVATIVES_SNAPSHOT_CATALOG_ENTRY_SCHEMA,
            "activation_ts": canonical_utc_timestamp(
                self.activation_ts,
                "snapshot_activation_ts",
            ),
            "snapshot_refs": self.refs.to_dict(),
        }

    def semantic_identity_dict(self) -> dict[str, Any]:
        return {
            "schema": DERIVATIVES_SNAPSHOT_CATALOG_ENTRY_SCHEMA,
            "activation_ts": canonical_utc_timestamp(
                self.activation_ts,
                "snapshot_activation_ts",
            ),
            "snapshot_refs": self.refs.semantic_identity_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SnapshotSetCatalogEntryV1:
        payload = require_exact_mapping_keys(
            value,
            {"schema", "activation_ts", "snapshot_refs"},
            "snapshot_catalog_entry_shape_invalid",
        )
        if payload["schema"] != DERIVATIVES_SNAPSHOT_CATALOG_ENTRY_SCHEMA:
            raise DerivativesBacktestContractError(
                "snapshot_catalog_entry_schema_invalid"
            )
        return cls(
            activation_ts=require_canonical_utc_timestamp(
                payload["activation_ts"],
                "snapshot_activation_ts",
            ),
            refs=DerivativesSnapshotRefsV1.from_dict(payload["snapshot_refs"]),
        )


def derive_raw_artifact_set_fingerprint(
    streams: tuple[DerivativesEventStreamRefV1, ...],
) -> str:
    validated = _validate_stream_tuple(streams)
    ordered = sorted(
        (stream.raw_identity_dict() for stream in validated),
        key=lambda item: item["relative_path"],
    )
    return typed_json_sha256(
        {
            "schema": "derivatives-event-raw-artifact-set/v1",
            "streams": ordered,
        }
    )


def _validate_stream_tuple(
    streams: tuple[DerivativesEventStreamRefV1, ...],
) -> tuple[DerivativesEventStreamRefV1, ...]:
    if type(streams) is not tuple or len(streams) != len(_STREAM_KINDS):
        raise DerivativesBacktestContractError("event_stream_set_invalid")
    if any(type(item) is not DerivativesEventStreamRefV1 for item in streams):
        raise DerivativesBacktestContractError("event_stream_set_invalid")
    rebuilt = tuple(
        DerivativesEventStreamRefV1.from_dict(item.to_dict()) for item in streams
    )
    if tuple(item.kind for item in rebuilt) != _STREAM_KINDS:
        raise DerivativesBacktestContractError("event_stream_set_kind_mismatch")
    paths = tuple(item.relative_path for item in rebuilt)
    if len({path.casefold() for path in paths}) != len(paths):
        raise DerivativesBacktestContractError("event_stream_path_collision")
    if sum(item.event_count for item in rebuilt) > DERIVATIVES_EVENT_SET_MAX_EVENTS:
        raise DerivativesBacktestContractError("event_set_event_limit_exceeded")
    return rebuilt


def _validate_snapshot_catalog(
    entries: tuple[SnapshotSetCatalogEntryV1, ...],
    *,
    warmup_start_ts: datetime,
    end_ts: datetime,
) -> tuple[SnapshotSetCatalogEntryV1, ...]:
    if (
        type(entries) is not tuple
        or not 1 <= len(entries) <= DERIVATIVES_MAX_SNAPSHOT_CATALOG_ENTRIES
    ):
        raise DerivativesBacktestContractError("snapshot_catalog_invalid")
    if any(type(item) is not SnapshotSetCatalogEntryV1 for item in entries):
        raise DerivativesBacktestContractError("snapshot_catalog_invalid")
    rebuilt = tuple(
        SnapshotSetCatalogEntryV1.from_dict(item.to_dict()) for item in entries
    )
    if rebuilt[0].activation_ts != warmup_start_ts:
        raise DerivativesBacktestContractError("snapshot_catalog_opening_missing")
    refs_by_id: dict[str, ImmutableSnapshotRefV1] = {}
    refs_by_path: dict[str, ImmutableSnapshotRefV1] = {}
    for entry in rebuilt:
        for ref in (
            entry.refs.instrument,
            entry.refs.position_tier,
            entry.refs.execution_fee,
            entry.refs.funding_schedule,
        ):
            known_id = refs_by_id.setdefault(ref.snapshot_id, ref)
            if known_id != ref:
                raise DerivativesBacktestContractError(
                    "snapshot_catalog_identity_conflict"
                )
            known_path = refs_by_path.setdefault(ref.relative_path.casefold(), ref)
            if known_path != ref:
                raise DerivativesBacktestContractError(
                    "snapshot_catalog_path_collision"
                )
    previous = rebuilt[0]
    for current in rebuilt[1:]:
        if current.activation_ts <= previous.activation_ts or current.activation_ts >= end_ts:
            raise DerivativesBacktestContractError("snapshot_catalog_order_invalid")
        validate_snapshot_transition(
            previous.refs,
            current.refs,
            switch_ts=current.activation_ts,
        )
        previous = current
    previous.refs.validate_at(end_ts - timedelta(microseconds=1))
    return rebuilt


def _require_manifest_component_budget(
    *,
    snapshot_catalog: tuple[SnapshotSetCatalogEntryV1, ...],
    streams: tuple[DerivativesEventStreamRefV1, ...],
) -> None:
    """Bound construction before one combined manifest DAG is materialized."""

    observed_size = 0
    for entry in snapshot_catalog:
        observed_size += len(canonical_typed_json_bytes(entry.to_dict()))
        if observed_size > (
            DERIVATIVES_MANIFEST_MAX_BYTES
            - DERIVATIVES_MANIFEST_ENVELOPE_RESERVE_BYTES
        ):
            raise DerivativesBacktestContractError(
                "event_set_manifest_size_exceeded"
            )
    for stream in streams:
        observed_size += len(canonical_typed_json_bytes(stream.to_dict()))
        if observed_size > (
            DERIVATIVES_MANIFEST_MAX_BYTES
            - DERIVATIVES_MANIFEST_ENVELOPE_RESERVE_BYTES
        ):
            raise DerivativesBacktestContractError(
                "event_set_manifest_size_exceeded"
            )


def _scope_payload() -> dict[str, str]:
    return {
        "venue": DERIVATIVES_VENUE,
        "symbol": DERIVATIVES_BACKTEST_SYMBOL,
        "instrument_type": DERIVATIVES_INSTRUMENT_TYPE,
        "contract_type": DERIVATIVES_CONTRACT_TYPE,
        "settle_currency": DERIVATIVES_SETTLE_CURRENCY,
        "margin_mode": DERIVATIVES_MARGIN_MODE,
        "position_mode": DERIVATIVES_POSITION_MODE,
        "family": DERIVATIVES_BACKTEST_FAMILY,
        "timeframe": DERIVATIVES_BACKTEST_TIMEFRAME,
    }


def _manifest_semantic_identity(
    *,
    event_set_id: str,
    warmup_start_ts: datetime,
    evaluation_start_ts: datetime,
    end_ts: datetime,
    dataset_version: str,
    transform_policy_id: str,
    transform_policy_version: str,
    transform_policy_fingerprint: str,
    snapshot_catalog: tuple[SnapshotSetCatalogEntryV1, ...],
    streams: tuple[DerivativesEventStreamRefV1, ...],
) -> dict[str, Any]:
    return {
        "schema": DERIVATIVES_EVENT_SET_MANIFEST_SCHEMA,
        "event_set_id": event_set_id,
        "authority_status": DERIVATIVES_EVENT_SET_AUTHORITY_SYNTHETIC,
        "capital_promotion_eligible": False,
        "scope": _scope_payload(),
        "window": {
            "warmup_start": canonical_utc_timestamp(
                warmup_start_ts,
                "warmup_start_ts",
            ),
            "evaluation_start": canonical_utc_timestamp(
                evaluation_start_ts,
                "evaluation_start_ts",
            ),
            "end": canonical_utc_timestamp(end_ts, "end_ts"),
        },
        "dataset_version": dataset_version,
        "transform": {
            "policy_id": transform_policy_id,
            "policy_version": transform_policy_version,
            "policy_fingerprint": transform_policy_fingerprint,
        },
        "ordering_policy_id": DERIVATIVES_EVENT_ORDERING_POLICY_ID,
        "resource_policy_id": DERIVATIVES_RESOURCE_POLICY_ID,
        "snapshot_sets": [entry.semantic_identity_dict() for entry in snapshot_catalog],
        "streams": {
            stream.kind.value: stream.semantic_identity_dict() for stream in streams
        },
    }


@dataclass(frozen=True, slots=True)
class DerivativesEventSetManifestV1:
    event_set_id: str
    warmup_start_ts: datetime
    evaluation_start_ts: datetime
    end_ts: datetime
    dataset_version: str
    transform_policy_id: str
    transform_policy_version: str
    transform_policy_fingerprint: str
    snapshot_catalog: tuple[SnapshotSetCatalogEntryV1, ...]
    streams: tuple[DerivativesEventStreamRefV1, ...]
    raw_artifact_set_fingerprint: str
    semantic_event_set_fingerprint: str
    authority_status: str = DERIVATIVES_EVENT_SET_AUTHORITY_SYNTHETIC
    capital_promotion_eligible: bool = False

    def __post_init__(self) -> None:
        require_canonical_uuid(self.event_set_id, "event_set_id")
        warmup = require_utc_datetime(self.warmup_start_ts, "warmup_start_ts")
        evaluation = require_utc_datetime(
            self.evaluation_start_ts,
            "evaluation_start_ts",
        )
        end = require_utc_datetime(self.end_ts, "end_ts")
        if not warmup <= evaluation < end:
            raise DerivativesBacktestContractError("event_set_window_invalid")
        for boundary, name in (
            (warmup, "warmup_start_ts"),
            (evaluation, "evaluation_start_ts"),
            (end, "end_ts"),
        ):
            if boundary.minute % 15 or boundary.second or boundary.microsecond:
                raise DerivativesBacktestContractError(
                    "event_set_window_alignment_invalid",
                    field=name,
                )
        dataset = require_identifier(self.dataset_version, "dataset_version")
        policy_id = require_identifier(self.transform_policy_id, "transform_policy_id")
        policy_version = require_identifier(
            self.transform_policy_version,
            "transform_policy_version",
        )
        require_sha256(
            self.transform_policy_fingerprint,
            "transform_policy_fingerprint",
        )
        streams = _validate_stream_tuple(self.streams)
        catalog = _validate_snapshot_catalog(
            self.snapshot_catalog,
            warmup_start_ts=warmup,
            end_ts=end,
        )
        _require_manifest_component_budget(
            snapshot_catalog=catalog,
            streams=streams,
        )
        for stream in streams:
            if stream.coverage_start_ts != warmup or stream.coverage_end_ts != end:
                raise DerivativesBacktestContractError(
                    "event_stream_manifest_coverage_mismatch",
                    field=stream.kind.value,
                )
        contract_stream = next(
            item
            for item in streams
            if item.kind is DerivativeEventKindV1.CONTRACT_TIER_EFFECTIVE
        )
        expected_transitions = len(catalog) - 1
        if contract_stream.event_count != expected_transitions:
            raise DerivativesBacktestContractError(
                "snapshot_catalog_event_count_mismatch"
            )
        if expected_transitions and (
            contract_stream.first_key is None
            or contract_stream.last_key is None
            or contract_stream.first_key.ts != catalog[1].activation_ts
            or contract_stream.last_key.ts != catalog[-1].activation_ts
        ):
            raise DerivativesBacktestContractError(
                "snapshot_catalog_event_boundary_mismatch"
            )
        bar_stream = next(
            item
            for item in streams
            if item.kind is DerivativeEventKindV1.BAR_CLOSE
        )
        if (
            bar_stream.event_count == 0
            or bar_stream.last_key is None
            or bar_stream.last_key.ts < evaluation
        ):
            raise DerivativesBacktestContractError("event_set_bar_stream_empty")
        expected_raw = derive_raw_artifact_set_fingerprint(streams)
        require_sha256(
            self.raw_artifact_set_fingerprint,
            "raw_artifact_set_fingerprint",
        )
        if self.raw_artifact_set_fingerprint != expected_raw:
            raise DerivativesBacktestContractError(
                "raw_artifact_set_fingerprint_mismatch"
            )
        expected_semantic = typed_json_sha256(
            _manifest_semantic_identity(
                event_set_id=self.event_set_id,
                warmup_start_ts=warmup,
                evaluation_start_ts=evaluation,
                end_ts=end,
                dataset_version=dataset,
                transform_policy_id=policy_id,
                transform_policy_version=policy_version,
                transform_policy_fingerprint=self.transform_policy_fingerprint,
                snapshot_catalog=catalog,
                streams=streams,
            )
        )
        require_sha256(
            self.semantic_event_set_fingerprint,
            "semantic_event_set_fingerprint",
        )
        if self.semantic_event_set_fingerprint != expected_semantic:
            raise DerivativesBacktestContractError(
                "semantic_event_set_fingerprint_mismatch"
            )
        if self.authority_status != DERIVATIVES_EVENT_SET_AUTHORITY_SYNTHETIC:
            raise DerivativesBacktestContractError("event_set_authority_invalid")
        if self.capital_promotion_eligible is not False:
            raise DerivativesBacktestContractError(
                "synthetic_event_set_cannot_be_promotable"
            )
        object.__setattr__(self, "streams", streams)
        object.__setattr__(self, "snapshot_catalog", catalog)
        if len(canonical_typed_json_bytes(self.to_dict())) > (
            DERIVATIVES_MANIFEST_MAX_BYTES
        ):
            raise DerivativesBacktestContractError(
                "event_set_manifest_size_exceeded"
            )

    @classmethod
    def create(
        cls,
        *,
        event_set_id: str,
        warmup_start_ts: datetime,
        evaluation_start_ts: datetime,
        end_ts: datetime,
        dataset_version: str,
        transform_policy_id: str,
        transform_policy_version: str,
        transform_policy_fingerprint: str,
        snapshot_catalog: tuple[SnapshotSetCatalogEntryV1, ...],
        streams: tuple[DerivativesEventStreamRefV1, ...],
    ) -> DerivativesEventSetManifestV1:
        canonical_event_set_id = require_canonical_uuid(event_set_id, "event_set_id")
        canonical_warmup = require_utc_datetime(
            warmup_start_ts,
            "warmup_start_ts",
        )
        canonical_evaluation = require_utc_datetime(
            evaluation_start_ts,
            "evaluation_start_ts",
        )
        canonical_end = require_utc_datetime(end_ts, "end_ts")
        if not canonical_warmup <= canonical_evaluation < canonical_end:
            raise DerivativesBacktestContractError("event_set_window_invalid")
        for boundary, name in (
            (canonical_warmup, "warmup_start_ts"),
            (canonical_evaluation, "evaluation_start_ts"),
            (canonical_end, "end_ts"),
        ):
            if boundary.minute % 15 or boundary.second or boundary.microsecond:
                raise DerivativesBacktestContractError(
                    "event_set_window_alignment_invalid",
                    field=name,
                )
        canonical_dataset = require_identifier(dataset_version, "dataset_version")
        canonical_policy_id = require_identifier(
            transform_policy_id,
            "transform_policy_id",
        )
        canonical_policy_version = require_identifier(
            transform_policy_version,
            "transform_policy_version",
        )
        canonical_policy_fingerprint = require_sha256(
            transform_policy_fingerprint,
            "transform_policy_fingerprint",
        )
        canonical_streams = _validate_stream_tuple(streams)
        canonical_catalog = _validate_snapshot_catalog(
            snapshot_catalog,
            warmup_start_ts=canonical_warmup,
            end_ts=canonical_end,
        )
        _require_manifest_component_budget(
            snapshot_catalog=canonical_catalog,
            streams=canonical_streams,
        )
        raw_fingerprint = derive_raw_artifact_set_fingerprint(canonical_streams)
        semantic_fingerprint = typed_json_sha256(
            _manifest_semantic_identity(
                event_set_id=canonical_event_set_id,
                warmup_start_ts=canonical_warmup,
                evaluation_start_ts=canonical_evaluation,
                end_ts=canonical_end,
                dataset_version=canonical_dataset,
                transform_policy_id=canonical_policy_id,
                transform_policy_version=canonical_policy_version,
                transform_policy_fingerprint=canonical_policy_fingerprint,
                snapshot_catalog=canonical_catalog,
                streams=canonical_streams,
            )
        )
        return cls(
            event_set_id=canonical_event_set_id,
            warmup_start_ts=canonical_warmup,
            evaluation_start_ts=canonical_evaluation,
            end_ts=canonical_end,
            dataset_version=canonical_dataset,
            transform_policy_id=canonical_policy_id,
            transform_policy_version=canonical_policy_version,
            transform_policy_fingerprint=canonical_policy_fingerprint,
            snapshot_catalog=canonical_catalog,
            streams=canonical_streams,
            raw_artifact_set_fingerprint=raw_fingerprint,
            semantic_event_set_fingerprint=semantic_fingerprint,
        )

    def semantic_identity_dict(self) -> dict[str, Any]:
        return _manifest_semantic_identity(
            event_set_id=self.event_set_id,
            warmup_start_ts=self.warmup_start_ts,
            evaluation_start_ts=self.evaluation_start_ts,
            end_ts=self.end_ts,
            dataset_version=self.dataset_version,
            transform_policy_id=self.transform_policy_id,
            transform_policy_version=self.transform_policy_version,
            transform_policy_fingerprint=self.transform_policy_fingerprint,
            snapshot_catalog=self.snapshot_catalog,
            streams=self.streams,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.semantic_identity_dict()
        payload["snapshot_sets"] = [entry.to_dict() for entry in self.snapshot_catalog]
        payload["streams"] = {
            stream.kind.value: stream.to_dict() for stream in self.streams
        }
        payload["raw_artifact_set_fingerprint"] = self.raw_artifact_set_fingerprint
        payload["semantic_event_set_fingerprint"] = (
            self.semantic_event_set_fingerprint
        )
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DerivativesEventSetManifestV1:
        payload = require_exact_mapping_keys(
            value,
            {
                "schema",
                "event_set_id",
                "authority_status",
                "capital_promotion_eligible",
                "scope",
                "window",
                "dataset_version",
                "transform",
                "ordering_policy_id",
                "resource_policy_id",
                "snapshot_sets",
                "streams",
                "raw_artifact_set_fingerprint",
                "semantic_event_set_fingerprint",
            },
            "event_set_manifest_shape_invalid",
        )
        if payload["schema"] != DERIVATIVES_EVENT_SET_MANIFEST_SCHEMA:
            raise DerivativesBacktestContractError("event_set_manifest_schema_invalid")
        scope = require_exact_mapping_keys(
            payload["scope"],
            set(_scope_payload()),
            "event_set_scope_out_of_v1",
        )
        if scope != _scope_payload():
            raise DerivativesBacktestContractError("event_set_scope_out_of_v1")
        if payload["ordering_policy_id"] != DERIVATIVES_EVENT_ORDERING_POLICY_ID:
            raise DerivativesBacktestContractError("event_ordering_policy_mismatch")
        if payload["resource_policy_id"] != DERIVATIVES_RESOURCE_POLICY_ID:
            raise DerivativesBacktestContractError("event_resource_policy_mismatch")
        window = require_exact_mapping_keys(
            payload["window"],
            {"warmup_start", "evaluation_start", "end"},
            "event_set_window_invalid",
        )
        transform = require_exact_mapping_keys(
            payload["transform"],
            {"policy_id", "policy_version", "policy_fingerprint"},
            "event_set_transform_invalid",
        )
        if (
            type(payload["snapshot_sets"]) is not list
            or not 1
            <= len(payload["snapshot_sets"])
            <= DERIVATIVES_MAX_SNAPSHOT_CATALOG_ENTRIES
        ):
            raise DerivativesBacktestContractError("snapshot_catalog_invalid")
        raw_streams = require_exact_mapping_keys(
            payload["streams"],
            _STREAM_KIND_NAMES,
            "event_stream_set_invalid",
        )
        return cls(
            event_set_id=require_canonical_uuid(payload["event_set_id"], "event_set_id"),
            warmup_start_ts=require_canonical_utc_timestamp(
                window["warmup_start"],
                "warmup_start_ts",
            ),
            evaluation_start_ts=require_canonical_utc_timestamp(
                window["evaluation_start"],
                "evaluation_start_ts",
            ),
            end_ts=require_canonical_utc_timestamp(window["end"], "end_ts"),
            dataset_version=require_identifier(
                payload["dataset_version"],
                "dataset_version",
            ),
            transform_policy_id=require_identifier(
                transform["policy_id"],
                "transform_policy_id",
            ),
            transform_policy_version=require_identifier(
                transform["policy_version"],
                "transform_policy_version",
            ),
            transform_policy_fingerprint=require_sha256(
                transform["policy_fingerprint"],
                "transform_policy_fingerprint",
            ),
            snapshot_catalog=tuple(
                SnapshotSetCatalogEntryV1.from_dict(item)
                for item in payload["snapshot_sets"]
            ),
            streams=tuple(
                DerivativesEventStreamRefV1.from_dict(raw_streams[kind.value])
                for kind in _STREAM_KINDS
            ),
            raw_artifact_set_fingerprint=require_sha256(
                payload["raw_artifact_set_fingerprint"],
                "raw_artifact_set_fingerprint",
            ),
            semantic_event_set_fingerprint=require_sha256(
                payload["semantic_event_set_fingerprint"],
                "semantic_event_set_fingerprint",
            ),
            authority_status=payload["authority_status"],
            capital_promotion_eligible=payload["capital_promotion_eligible"],
        )


@dataclass(frozen=True, slots=True)
class DerivativesEventSetRefV1:
    event_set_id: str
    manifest_relative_path: str
    manifest_size_bytes: int
    manifest_raw_sha256: str
    raw_artifact_set_fingerprint: str
    semantic_event_set_fingerprint: str

    def __post_init__(self) -> None:
        require_canonical_uuid(self.event_set_id, "event_set_id")
        require_safe_relative_posix_path(
            self.manifest_relative_path,
            "manifest_relative_path",
        )
        require_exact_int(
            self.manifest_size_bytes,
            "manifest_size_bytes",
            minimum=1,
            maximum=DERIVATIVES_MANIFEST_MAX_BYTES,
        )
        require_sha256(self.manifest_raw_sha256, "manifest_raw_sha256")
        require_sha256(
            self.raw_artifact_set_fingerprint,
            "raw_artifact_set_fingerprint",
        )
        require_sha256(
            self.semantic_event_set_fingerprint,
            "semantic_event_set_fingerprint",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DERIVATIVES_EVENT_SET_REF_SCHEMA,
            "event_set_id": self.event_set_id,
            "manifest_relative_path": self.manifest_relative_path,
            "manifest_size_bytes": self.manifest_size_bytes,
            "manifest_raw_sha256": self.manifest_raw_sha256,
            "raw_artifact_set_fingerprint": self.raw_artifact_set_fingerprint,
            "semantic_event_set_fingerprint": self.semantic_event_set_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DerivativesEventSetRefV1:
        payload = require_exact_mapping_keys(
            value,
            {
                "schema",
                "event_set_id",
                "manifest_relative_path",
                "manifest_size_bytes",
                "manifest_raw_sha256",
                "raw_artifact_set_fingerprint",
                "semantic_event_set_fingerprint",
            },
            "event_set_ref_shape_invalid",
        )
        if payload["schema"] != DERIVATIVES_EVENT_SET_REF_SCHEMA:
            raise DerivativesBacktestContractError("event_set_ref_schema_invalid")
        return cls(
            event_set_id=require_canonical_uuid(payload["event_set_id"], "event_set_id"),
            manifest_relative_path=require_safe_relative_posix_path(
                payload["manifest_relative_path"],
                "manifest_relative_path",
            ),
            manifest_size_bytes=require_exact_int(
                payload["manifest_size_bytes"],
                "manifest_size_bytes",
                minimum=1,
                maximum=DERIVATIVES_MANIFEST_MAX_BYTES,
            ),
            manifest_raw_sha256=require_sha256(
                payload["manifest_raw_sha256"],
                "manifest_raw_sha256",
            ),
            raw_artifact_set_fingerprint=require_sha256(
                payload["raw_artifact_set_fingerprint"],
                "raw_artifact_set_fingerprint",
            ),
            semantic_event_set_fingerprint=require_sha256(
                payload["semantic_event_set_fingerprint"],
                "semantic_event_set_fingerprint",
            ),
        )

    def validate_manifest(
        self,
        manifest: DerivativesEventSetManifestV1,
        *,
        observed_relative_path: str,
        manifest_bytes: bytes,
    ) -> None:
        observed_path = require_safe_relative_posix_path(
            observed_relative_path,
            "observed_manifest_relative_path",
        )
        if observed_path != self.manifest_relative_path:
            raise DerivativesBacktestContractError("event_set_manifest_path_mismatch")
        if type(manifest_bytes) is not bytes:
            raise DerivativesBacktestContractError("event_set_manifest_bytes_invalid")
        if (
            len(manifest_bytes) != self.manifest_size_bytes
            or len(manifest_bytes) > DERIVATIVES_MANIFEST_MAX_BYTES
            or hashlib.sha256(manifest_bytes).hexdigest()
            != self.manifest_raw_sha256
        ):
            raise DerivativesBacktestContractError("event_set_manifest_raw_mismatch")
        if type(manifest) is not DerivativesEventSetManifestV1:
            raise DerivativesBacktestContractError("event_set_manifest_invalid")
        restored = DerivativesEventSetManifestV1.from_dict(manifest.to_dict())
        if restored != manifest:
            raise DerivativesBacktestContractError(
                "event_set_manifest_revalidation_mismatch"
            )
        if manifest_bytes != canonical_typed_json_bytes(restored.to_dict()):
            raise DerivativesBacktestContractError(
                "event_set_manifest_bytes_noncanonical"
            )
        if any(
            self.manifest_relative_path.casefold()
            == stream.relative_path.casefold()
            for stream in restored.streams
        ):
            raise DerivativesBacktestContractError(
                "event_set_manifest_stream_path_collision"
            )
        if (
            self.event_set_id != restored.event_set_id
            or self.raw_artifact_set_fingerprint
            != restored.raw_artifact_set_fingerprint
            or self.semantic_event_set_fingerprint
            != restored.semantic_event_set_fingerprint
        ):
            raise DerivativesBacktestContractError("event_set_ref_manifest_mismatch")


@dataclass(frozen=True, slots=True)
class DerivativesEventStreamCursorV1:
    stream_fingerprint: str
    next_byte_offset: int
    committed_event_count: int
    raw_prefix_sha256: str
    semantic_prefix_sha256: str
    last_committed_key: EventStreamBoundaryKeyV1 | None

    def __post_init__(self) -> None:
        require_sha256(self.stream_fingerprint, "stream_fingerprint")
        offset = require_exact_int(
            self.next_byte_offset,
            "next_byte_offset",
            minimum=0,
            maximum=DERIVATIVES_EVENT_STREAM_MAX_BYTES,
        )
        count = require_exact_int(
            self.committed_event_count,
            "committed_event_count",
            minimum=0,
            maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
        )
        require_sha256(self.raw_prefix_sha256, "raw_prefix_sha256")
        require_sha256(self.semantic_prefix_sha256, "semantic_prefix_sha256")
        if count == 0:
            if offset != 0 or self.raw_prefix_sha256 != _EMPTY_RAW_SHA256:
                raise DerivativesBacktestContractError("empty_stream_cursor_invalid")
            if self.last_committed_key is not None:
                raise DerivativesBacktestContractError("empty_stream_cursor_invalid")
            return
        if offset == 0 or type(self.last_committed_key) is not EventStreamBoundaryKeyV1:
            raise DerivativesBacktestContractError("stream_cursor_boundary_missing")
        object.__setattr__(
            self,
            "last_committed_key",
            EventStreamBoundaryKeyV1.from_dict(self.last_committed_key.to_dict()),
        )

    @classmethod
    def empty(
        cls,
        stream: DerivativesEventStreamRefV1,
    ) -> DerivativesEventStreamCursorV1:
        if type(stream) is not DerivativesEventStreamRefV1:
            raise DerivativesBacktestContractError("event_stream_ref_invalid")
        return cls(
            stream_fingerprint=stream.fingerprint,
            next_byte_offset=0,
            committed_event_count=0,
            raw_prefix_sha256=_EMPTY_RAW_SHA256,
            semantic_prefix_sha256=event_stream_semantic_seed(stream.kind),
            last_committed_key=None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DERIVATIVES_EVENT_STREAM_CURSOR_SCHEMA,
            "stream_fingerprint": self.stream_fingerprint,
            "next_byte_offset": self.next_byte_offset,
            "committed_event_count": self.committed_event_count,
            "raw_prefix_sha256": self.raw_prefix_sha256,
            "semantic_prefix_sha256": self.semantic_prefix_sha256,
            "last_committed_key": (
                None
                if self.last_committed_key is None
                else self.last_committed_key.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DerivativesEventStreamCursorV1:
        payload = require_exact_mapping_keys(
            value,
            {
                "schema",
                "stream_fingerprint",
                "next_byte_offset",
                "committed_event_count",
                "raw_prefix_sha256",
                "semantic_prefix_sha256",
                "last_committed_key",
            },
            "event_stream_cursor_shape_invalid",
        )
        if payload["schema"] != DERIVATIVES_EVENT_STREAM_CURSOR_SCHEMA:
            raise DerivativesBacktestContractError("event_stream_cursor_schema_invalid")
        return cls(
            stream_fingerprint=require_sha256(
                payload["stream_fingerprint"],
                "stream_fingerprint",
            ),
            next_byte_offset=require_exact_int(
                payload["next_byte_offset"],
                "next_byte_offset",
                minimum=0,
                maximum=DERIVATIVES_EVENT_STREAM_MAX_BYTES,
            ),
            committed_event_count=require_exact_int(
                payload["committed_event_count"],
                "committed_event_count",
                minimum=0,
                maximum=DERIVATIVES_EVENT_STREAM_MAX_EVENTS,
            ),
            raw_prefix_sha256=require_sha256(
                payload["raw_prefix_sha256"],
                "raw_prefix_sha256",
            ),
            semantic_prefix_sha256=require_sha256(
                payload["semantic_prefix_sha256"],
                "semantic_prefix_sha256",
            ),
            last_committed_key=(
                None
                if payload["last_committed_key"] is None
                else EventStreamBoundaryKeyV1.from_dict(
                    payload["last_committed_key"]
                )
            ),
        )

    def validate_against(self, stream: DerivativesEventStreamRefV1) -> None:
        if type(stream) is not DerivativesEventStreamRefV1:
            raise DerivativesBacktestContractError("event_stream_ref_invalid")
        if (
            self.stream_fingerprint != stream.fingerprint
            or self.next_byte_offset > stream.size_bytes
            or self.committed_event_count > stream.event_count
        ):
            raise DerivativesBacktestContractError("event_stream_cursor_mismatch")
        if self.committed_event_count == 0:
            if self.semantic_prefix_sha256 != event_stream_semantic_seed(stream.kind):
                raise DerivativesBacktestContractError(
                    "empty_stream_cursor_mismatch"
                )
            return
        if self.committed_event_count == stream.event_count:
            if (
                self.next_byte_offset != stream.size_bytes
                or self.raw_prefix_sha256 != stream.raw_sha256
                or self.semantic_prefix_sha256 != stream.semantic_event_digest
                or self.last_committed_key != stream.last_key
            ):
                raise DerivativesBacktestContractError(
                    "completed_event_stream_cursor_mismatch"
                )
            return
        if (
            self.next_byte_offset >= stream.size_bytes
            or self.last_committed_key is None
            or stream.first_key is None
            or stream.last_key is None
            or self.last_committed_key.order_key < stream.first_key.order_key
            or self.last_committed_key.order_key >= stream.last_key.order_key
        ):
            raise DerivativesBacktestContractError(
                "partial_event_stream_cursor_mismatch"
            )


__all__ = [
    "DERIVATIVES_EVENT_SET_AUTHORITY_SYNTHETIC",
    "DERIVATIVES_EVENT_SET_MANIFEST_SCHEMA",
    "DERIVATIVES_EVENT_SET_MAX_EVENTS",
    "DERIVATIVES_EVENT_SET_REF_SCHEMA",
    "DERIVATIVES_EVENT_STREAM_CURSOR_SCHEMA",
    "DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_FINGERPRINT",
    "DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_ID",
    "DERIVATIVES_EVENT_STREAM_INTEGRITY_POLICY_VERSION",
    "DERIVATIVES_EVENT_STREAM_INTEGRITY_SCHEMA",
    "DERIVATIVES_EVENT_STREAM_MAX_BYTES",
    "DERIVATIVES_EVENT_STREAM_MAX_EVENTS",
    "DERIVATIVES_EVENT_STREAM_REF_SCHEMA",
    "DERIVATIVES_JSONL_RECORD_MAX_BYTES",
    "DERIVATIVES_MANIFEST_MAX_BYTES",
    "DERIVATIVES_MANIFEST_ENVELOPE_RESERVE_BYTES",
    "DERIVATIVES_MAX_PARENT_RAW_PARTITIONS",
    "DERIVATIVES_MAX_SNAPSHOT_CATALOG_ENTRIES",
    "DERIVATIVES_MAX_SOURCE_REGISTRY_IDS",
    "DERIVATIVES_RESOURCE_POLICY_ID",
    "DerivativesEventSetManifestV1",
    "DerivativesEventSetRefV1",
    "DerivativesEventStreamCursorV1",
    "DerivativesEventStreamRefV1",
    "EventStreamIntegritySummaryV1",
    "EventStreamBoundaryKeyV1",
    "SnapshotSetCatalogEntryV1",
    "derive_raw_artifact_set_fingerprint",
    "event_stream_semantic_seed",
    "update_event_stream_semantic_digest",
]
