"""Verified lineage contract for derivative research inputs.

This DTO does not perform database verification and its ``verified`` field is
only an upstream claim carried for audit compatibility.  Production consumers
must independently verify the immutable artifact index and instrument snapshot
registry reference.  Legacy Gold readers must not create authorization from a
formatted fingerprint or this DTO alone.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Mapping


CONTRACT_ARTIFACT_LINEAGE_SCHEMA = "research_contract_artifact_lineage_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ContractAwareArtifactLineage:
    """Claimed lineage material that still requires independent verification."""

    artifact_output_fingerprint: str
    instrument_snapshot_digest: str
    instrument_snapshot_source_ref: str
    verification_ref: str
    symbol: str
    timeframe: str
    coverage_start: str
    coverage_end: str
    verified: bool
    schema_version: str = CONTRACT_ARTIFACT_LINEAGE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != CONTRACT_ARTIFACT_LINEAGE_SCHEMA:
            raise ValueError("contract_artifact_lineage_schema_invalid")
        if not _SHA256.fullmatch(str(self.artifact_output_fingerprint or "")):
            raise ValueError("artifact_output_fingerprint_invalid")
        if not _SHA256.fullmatch(str(self.instrument_snapshot_digest or "")):
            raise ValueError("instrument_snapshot_digest_invalid")
        _require_text(
            self.instrument_snapshot_source_ref,
            "instrument_snapshot_source_ref",
        )
        _require_text(self.verification_ref, "contract_lineage_verification_ref")
        symbol = _require_text(self.symbol, "contract_lineage_symbol").upper()
        timeframe = _require_text(self.timeframe, "contract_lineage_timeframe").lower()
        coverage_start = _parse_timestamp(
            self.coverage_start,
            "contract_lineage_coverage_start",
        )
        coverage_end = _parse_timestamp(
            self.coverage_end,
            "contract_lineage_coverage_end",
        )
        if coverage_end <= coverage_start:
            raise ValueError("contract_lineage_coverage_window_invalid")
        if type(self.verified) is not bool:
            raise ValueError("contract_lineage_verified_must_be_bool")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "coverage_start", coverage_start.isoformat())
        object.__setattr__(self, "coverage_end", coverage_end.isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def covers(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> bool:
        requested_start = _require_aware_datetime(start, "requested_start")
        requested_end = _require_aware_datetime(end, "requested_end")
        if requested_end <= requested_start:
            return False
        return (
            self.symbol == str(symbol or "").strip().upper()
            and self.timeframe == str(timeframe or "").strip().lower()
            and _parse_timestamp(self.coverage_start, "contract_lineage_coverage_start")
            <= requested_start
            and _parse_timestamp(self.coverage_end, "contract_lineage_coverage_end")
            >= requested_end
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ContractAwareArtifactLineage:
        if not isinstance(payload, Mapping):
            raise ValueError("contract_artifact_lineage_must_be_mapping")
        if set(payload) != {
            "artifact_output_fingerprint",
            "instrument_snapshot_digest",
            "instrument_snapshot_source_ref",
            "verification_ref",
            "symbol",
            "timeframe",
            "coverage_start",
            "coverage_end",
            "verified",
            "schema_version",
        }:
            raise ValueError("contract_artifact_lineage_shape_invalid")
        return cls(
            artifact_output_fingerprint=str(
                payload.get("artifact_output_fingerprint") or ""
            ),
            instrument_snapshot_digest=str(
                payload.get("instrument_snapshot_digest") or ""
            ),
            instrument_snapshot_source_ref=str(
                payload.get("instrument_snapshot_source_ref") or ""
            ),
            verification_ref=str(payload.get("verification_ref") or ""),
            symbol=str(payload.get("symbol") or ""),
            timeframe=str(payload.get("timeframe") or ""),
            coverage_start=str(payload.get("coverage_start") or ""),
            coverage_end=str(payload.get("coverage_end") or ""),
            verified=payload.get("verified"),
            schema_version=str(payload.get("schema_version") or ""),
        )


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name}_required")
    return value.strip()


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    text_value = _require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name}_invalid") from exc
    return _require_aware_datetime(parsed, field_name).astimezone(UTC)


def _require_aware_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}_must_be_timezone_aware")
    return value
