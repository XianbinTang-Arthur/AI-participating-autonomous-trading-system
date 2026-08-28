"""Exact immutable snapshot references for derivatives replay v1.

References identify bytes and semantic content independently.  They do not
grant historical authority by themselves; the loader must still verify the
referenced source seal and effective window before an engine may consume the
resolved economic schedules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from aats.data_platform.governance.typed_json_identity import typed_json_sha256

from .contracts import DerivativesBacktestContractError
from .wire import (
    canonical_utc_timestamp,
    require_canonical_utc_timestamp,
    require_canonical_uuid,
    require_exact_int,
    require_exact_mapping_keys,
    require_safe_relative_posix_path,
    require_sha256,
    require_utc_datetime,
)


DERIVATIVES_SNAPSHOT_REF_SCHEMA = "derivatives-snapshot-ref/v1"
DERIVATIVES_SNAPSHOT_SET_SCHEMA = "derivatives-snapshot-set/v1"
DERIVATIVES_SNAPSHOT_MAX_BYTES = 4 * 1024 * 1024


class SnapshotKindV1(StrEnum):
    INSTRUMENT = "instrument"
    POSITION_TIER = "position_tier"
    EXECUTION_FEE = "execution_fee"
    FUNDING_SCHEDULE = "funding_schedule"


def _require_schema_text(value: Any, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or value != value.strip()
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise DerivativesBacktestContractError(
            "snapshot_source_schema_invalid",
            field=field_name,
        )
    return value


@dataclass(frozen=True, slots=True)
class ImmutableSnapshotRefV1:
    """One exact reference to a small canonical snapshot artifact."""

    kind: SnapshotKindV1
    snapshot_id: str
    relative_path: str
    raw_sha256: str
    size_bytes: int
    semantic_sha256: str
    source_registry_id: str
    source_seal_fingerprint: str
    source_schema: str
    effective_from: datetime
    effective_to: datetime | None

    def __post_init__(self) -> None:
        if type(self.kind) is not SnapshotKindV1:
            raise DerivativesBacktestContractError("snapshot_kind_invalid")
        require_canonical_uuid(self.snapshot_id, "snapshot_id")
        require_safe_relative_posix_path(self.relative_path, "relative_path")
        require_sha256(self.raw_sha256, "raw_sha256")
        require_exact_int(
            self.size_bytes,
            "size_bytes",
            minimum=1,
            maximum=DERIVATIVES_SNAPSHOT_MAX_BYTES,
        )
        require_sha256(self.semantic_sha256, "semantic_sha256")
        require_canonical_uuid(self.source_registry_id, "source_registry_id")
        require_sha256(
            self.source_seal_fingerprint,
            "source_seal_fingerprint",
        )
        _require_schema_text(self.source_schema, "source_schema")
        effective_from = require_utc_datetime(
            self.effective_from,
            "effective_from",
        )
        effective_to = (
            None
            if self.effective_to is None
            else require_utc_datetime(self.effective_to, "effective_to")
        )
        if effective_to is not None and effective_to <= effective_from:
            raise DerivativesBacktestContractError(
                "snapshot_effective_window_invalid"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DERIVATIVES_SNAPSHOT_REF_SCHEMA,
            "kind": self.kind.value,
            "snapshot_id": self.snapshot_id,
            "relative_path": self.relative_path,
            "raw_sha256": self.raw_sha256,
            "size_bytes": self.size_bytes,
            "semantic_sha256": self.semantic_sha256,
            "source_registry_id": self.source_registry_id,
            "source_seal_fingerprint": self.source_seal_fingerprint,
            "source_schema": self.source_schema,
            "effective_window": {
                "start": canonical_utc_timestamp(
                    self.effective_from,
                    "effective_from",
                ),
                "end": (
                    None
                    if self.effective_to is None
                    else canonical_utc_timestamp(self.effective_to, "effective_to")
                ),
            },
        }

    def semantic_identity_dict(self) -> dict[str, Any]:
        """Return path-free content/source identity for economic fingerprints."""

        payload = self.to_dict()
        payload.pop("relative_path")
        return payload

    @property
    def fingerprint(self) -> str:
        return typed_json_sha256(self.semantic_identity_dict())

    def validate_window(self, *, start: datetime, end: datetime) -> None:
        window_start = require_utc_datetime(start, "start_ts")
        window_end = require_utc_datetime(end, "end_ts")
        if window_end <= window_start:
            raise DerivativesBacktestContractError("replay_window_invalid")
        if self.effective_from > window_start or (
            self.effective_to is not None and window_end > self.effective_to
        ):
            raise DerivativesBacktestContractError(
                "snapshot_effective_window_unproven",
                field=self.kind.value,
            )

    def validate_at(self, ts: datetime) -> None:
        point = require_utc_datetime(ts, "snapshot_use_ts")
        if self.effective_from > point or (
            self.effective_to is not None and point >= self.effective_to
        ):
            raise DerivativesBacktestContractError(
                "snapshot_not_effective_at_timestamp",
                field=self.kind.value,
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ImmutableSnapshotRefV1:
        payload = require_exact_mapping_keys(
            value,
            {
                "schema",
                "kind",
                "snapshot_id",
                "relative_path",
                "raw_sha256",
                "size_bytes",
                "semantic_sha256",
                "source_registry_id",
                "source_seal_fingerprint",
                "source_schema",
                "effective_window",
            },
            "snapshot_ref_shape_invalid",
        )
        if payload["schema"] != DERIVATIVES_SNAPSHOT_REF_SCHEMA:
            raise DerivativesBacktestContractError("snapshot_ref_schema_invalid")
        window = require_exact_mapping_keys(
            payload["effective_window"],
            {"start", "end"},
            "snapshot_effective_window_invalid",
        )
        try:
            kind = SnapshotKindV1(payload["kind"])
        except (TypeError, ValueError) as exc:
            raise DerivativesBacktestContractError("snapshot_kind_invalid") from exc
        if type(payload["kind"]) is not str:
            raise DerivativesBacktestContractError("snapshot_kind_invalid")
        return cls(
            kind=kind,
            snapshot_id=require_canonical_uuid(
                payload["snapshot_id"],
                "snapshot_id",
            ),
            relative_path=require_safe_relative_posix_path(
                payload["relative_path"],
                "relative_path",
            ),
            raw_sha256=require_sha256(payload["raw_sha256"], "raw_sha256"),
            size_bytes=require_exact_int(
                payload["size_bytes"],
                "size_bytes",
                minimum=1,
                maximum=DERIVATIVES_SNAPSHOT_MAX_BYTES,
            ),
            semantic_sha256=require_sha256(
                payload["semantic_sha256"],
                "semantic_sha256",
            ),
            source_registry_id=require_canonical_uuid(
                payload["source_registry_id"],
                "source_registry_id",
            ),
            source_seal_fingerprint=require_sha256(
                payload["source_seal_fingerprint"],
                "source_seal_fingerprint",
            ),
            source_schema=_require_schema_text(
                payload["source_schema"],
                "source_schema",
            ),
            effective_from=require_canonical_utc_timestamp(
                window["start"],
                "effective_from",
            ),
            effective_to=(
                None
                if window["end"] is None
                else require_canonical_utc_timestamp(window["end"], "effective_to")
            ),
        )


@dataclass(frozen=True, slots=True)
class DerivativesSnapshotRefsV1:
    """The four economic snapshots that must move as one versioned set."""

    instrument: ImmutableSnapshotRefV1
    position_tier: ImmutableSnapshotRefV1
    execution_fee: ImmutableSnapshotRefV1
    funding_schedule: ImmutableSnapshotRefV1

    def __post_init__(self) -> None:
        expected = (
            (self.instrument, SnapshotKindV1.INSTRUMENT),
            (self.position_tier, SnapshotKindV1.POSITION_TIER),
            (self.execution_fee, SnapshotKindV1.EXECUTION_FEE),
            (self.funding_schedule, SnapshotKindV1.FUNDING_SCHEDULE),
        )
        for ref, kind in expected:
            if type(ref) is not ImmutableSnapshotRefV1 or ref.kind is not kind:
                raise DerivativesBacktestContractError("snapshot_set_kind_mismatch")
        refs = tuple(ref for ref, _kind in expected)
        if (
            len({ref.snapshot_id for ref in refs}) != len(refs)
            or len({ref.relative_path.casefold() for ref in refs}) != len(refs)
        ):
            raise DerivativesBacktestContractError("snapshot_set_identity_duplicate")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DERIVATIVES_SNAPSHOT_SET_SCHEMA,
            "instrument": self.instrument.to_dict(),
            "position_tier": self.position_tier.to_dict(),
            "execution_fee": self.execution_fee.to_dict(),
            "funding_schedule": self.funding_schedule.to_dict(),
        }

    def semantic_identity_dict(self) -> dict[str, Any]:
        return {
            "schema": DERIVATIVES_SNAPSHOT_SET_SCHEMA,
            "instrument": self.instrument.semantic_identity_dict(),
            "position_tier": self.position_tier.semantic_identity_dict(),
            "execution_fee": self.execution_fee.semantic_identity_dict(),
            "funding_schedule": self.funding_schedule.semantic_identity_dict(),
        }

    @property
    def fingerprint(self) -> str:
        return typed_json_sha256(self.semantic_identity_dict())

    def validate_window(self, *, start: datetime, end: datetime) -> None:
        for ref in (
            self.instrument,
            self.position_tier,
            self.execution_fee,
            self.funding_schedule,
        ):
            ref.validate_window(start=start, end=end)

    def validate_at(self, ts: datetime) -> None:
        for ref in (
            self.instrument,
            self.position_tier,
            self.execution_fee,
            self.funding_schedule,
        ):
            ref.validate_at(ts)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DerivativesSnapshotRefsV1:
        payload = require_exact_mapping_keys(
            value,
            {
                "schema",
                "instrument",
                "position_tier",
                "execution_fee",
                "funding_schedule",
            },
            "snapshot_set_shape_invalid",
        )
        if payload["schema"] != DERIVATIVES_SNAPSHOT_SET_SCHEMA:
            raise DerivativesBacktestContractError("snapshot_set_schema_invalid")
        return cls(
            instrument=ImmutableSnapshotRefV1.from_dict(payload["instrument"]),
            position_tier=ImmutableSnapshotRefV1.from_dict(
                payload["position_tier"]
            ),
            execution_fee=ImmutableSnapshotRefV1.from_dict(
                payload["execution_fee"]
            ),
            funding_schedule=ImmutableSnapshotRefV1.from_dict(
                payload["funding_schedule"]
            ),
        )


__all__ = [
    "DERIVATIVES_SNAPSHOT_MAX_BYTES",
    "DERIVATIVES_SNAPSHOT_REF_SCHEMA",
    "DERIVATIVES_SNAPSHOT_SET_SCHEMA",
    "DerivativesSnapshotRefsV1",
    "ImmutableSnapshotRefV1",
    "SnapshotKindV1",
]
