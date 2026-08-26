"""Canonical provenance and dataset-bundle contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Sequence


class SourceKind(StrEnum):
    AATS_WS_CAPTURE = "aats_ws_capture"
    OKX_REST = "okx_rest"
    OKX_BULK = "okx_bulk"
    THIRD_PARTY = "third_party"
    DERIVED = "derived"
    PROXY = "proxy"


class TruthTier(StrEnum):
    AUTHORITATIVE_EXTERNAL = "authoritative_external"
    LOCAL_OBSERVATION = "local_observation"
    DERIVED = "derived"
    PROXY = "proxy"
    EXTERNAL_UNVERIFIED = "external_unverified"


_TRUTH_TIER_BY_SOURCE = {
    SourceKind.AATS_WS_CAPTURE: TruthTier.LOCAL_OBSERVATION,
    SourceKind.OKX_REST: TruthTier.AUTHORITATIVE_EXTERNAL,
    SourceKind.OKX_BULK: TruthTier.AUTHORITATIVE_EXTERNAL,
    SourceKind.THIRD_PARTY: TruthTier.EXTERNAL_UNVERIFIED,
    SourceKind.DERIVED: TruthTier.DERIVED,
    SourceKind.PROXY: TruthTier.PROXY,
}
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True)
class DataSourceRecord:
    source_key: str
    source_kind: SourceKind
    provider: str
    source_locator: str
    retrieved_at: datetime
    coverage_start: datetime
    coverage_end: datetime
    timestamp_semantics: str
    schema_version: str
    dataset_version: str
    transform_version: str | None
    git_commit: str
    raw_sha256: str
    row_count: int
    gap_manifest: Mapping[str, Any]
    license_usage_note: str
    truth_tier: TruthTier

    def __post_init__(self) -> None:
        for field_name in (
            "source_key",
            "provider",
            "source_locator",
            "timestamp_semantics",
            "schema_version",
            "dataset_version",
            "git_commit",
            "license_usage_note",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name}_must_be_nonempty")
        for field_name in ("retrieved_at", "coverage_start", "coverage_end"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name}_must_be_timezone_aware")
        if self.coverage_end <= self.coverage_start:
            raise ValueError("coverage_end_must_be_after_start")
        if self.row_count < 0:
            raise ValueError("row_count_must_be_non_negative")
        if not _GIT_COMMIT.fullmatch(self.git_commit):
            raise ValueError("git_commit_must_be_full_lowercase_hex")
        if self.truth_tier != _TRUTH_TIER_BY_SOURCE[self.source_kind]:
            raise ValueError("source_truth_tier_incompatible")
        if len(self.raw_sha256) != 64 or self.raw_sha256 != self.raw_sha256.lower():
            raise ValueError("raw_sha256_must_be_64_hex_characters")
        try:
            bytes.fromhex(self.raw_sha256)
        except ValueError as exc:
            raise ValueError("raw_sha256_must_be_hex") from exc
        try:
            raw_partition_hashes = tuple(
                str(value) for value in self.gap_manifest["raw_partition_sha256"]
            )
            raw_partition_count = int(self.gap_manifest["raw_partition_count"])
            gap_count = int(self.gap_manifest["gap_count"])
            unclassified_gap_count = int(
                self.gap_manifest["unclassified_gap_count"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("source_gap_manifest_shape_invalid") from exc
        if (
            not raw_partition_hashes
            or raw_partition_count != len(raw_partition_hashes)
            or any(
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in raw_partition_hashes
            )
        ):
            raise ValueError("source_gap_manifest_raw_partitions_invalid")
        if gap_count < 0 or not 0 <= unclassified_gap_count <= gap_count:
            raise ValueError("source_gap_manifest_gap_counts_invalid")
        expected_aggregate = hashlib.sha256(
            canonical_json_bytes(tuple(sorted(raw_partition_hashes)))
        ).hexdigest()
        if self.raw_sha256 != expected_aggregate:
            raise ValueError("source_raw_sha256_aggregate_mismatch")

    def canonical_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source_kind"] = self.source_kind.value
        payload["truth_tier"] = self.truth_tier.value
        for key in ("retrieved_at", "coverage_start", "coverage_end"):
            payload[key] = _utc_iso(getattr(self, key))
        return payload


@dataclass(frozen=True)
class DatasetBundleContract:
    bundle_key: str
    dataset_version: str
    purpose: str
    eligibility_mode: str
    coverage_start: datetime
    coverage_end: datetime
    components: Sequence[DataSourceRecord]

    def __post_init__(self) -> None:
        for field_name in ("bundle_key", "dataset_version", "purpose"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name}_must_be_nonempty")
        if self.eligibility_mode not in {"historical_research", "live_capture"}:
            raise ValueError("unsupported_eligibility_mode")
        for field_name in ("coverage_start", "coverage_end"):
            value = getattr(self, field_name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name}_must_be_timezone_aware")
        if self.coverage_end <= self.coverage_start:
            raise ValueError("coverage_end_must_be_after_start")
        if not self.components:
            raise ValueError("dataset_bundle_requires_components")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def source_fingerprint(source: DataSourceRecord) -> str:
    return hashlib.sha256(canonical_json_bytes(source_identity_dict(source))).hexdigest()


def source_identity_dict(source: DataSourceRecord) -> dict[str, Any]:
    """Content identity excludes retrieval time so exact retries are idempotent."""

    payload = source.canonical_dict()
    payload.pop("retrieved_at", None)
    return payload


def bundle_fingerprint(bundle: DatasetBundleContract) -> str:
    payload = {
        "bundle_key": bundle.bundle_key,
        "dataset_version": bundle.dataset_version,
        "purpose": bundle.purpose,
        "eligibility_mode": bundle.eligibility_mode,
        "coverage_start": _utc_iso(bundle.coverage_start),
        "coverage_end": _utc_iso(bundle.coverage_end),
        "component_fingerprints": sorted(
            source_fingerprint(component) for component in bundle.components
        ),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
