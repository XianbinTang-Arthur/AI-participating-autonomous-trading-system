"""Persistent provenance and historical dataset-bundle registration."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import text

from aats.data_platform.data_governance.contracts import (
    DataSourceRecord,
    DatasetBundleContract,
    SourceKind,
    TruthTier,
    bundle_fingerprint,
    canonical_json_bytes,
)
from aats.data_platform.data_governance.eligibility import (
    HistoricalEligibilityReport,
    evaluate_historical_bundle,
)


_TRUTH_TIER = {
    SourceKind.AATS_WS_CAPTURE: TruthTier.LOCAL_OBSERVATION,
    SourceKind.OKX_REST: TruthTier.AUTHORITATIVE_EXTERNAL,
    SourceKind.OKX_BULK: TruthTier.AUTHORITATIVE_EXTERNAL,
    SourceKind.THIRD_PARTY: TruthTier.EXTERNAL_UNVERIFIED,
    SourceKind.DERIVED: TruthTier.DERIVED,
    SourceKind.PROXY: TruthTier.PROXY,
}
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,79}$")


def import_source_record(
    *,
    source_key: str,
    source_kind: str,
    provider: str,
    source_locator: str,
    coverage_start: datetime,
    coverage_end: datetime,
    timestamp_semantics: str,
    schema_version: str,
    dataset_version: str,
    transform_version: str | None,
    git_commit: str,
    raw_partition_sha256: Sequence[str],
    row_count: int,
    gaps: Sequence[dict[str, Any]],
    retrieved_at: datetime | None = None,
) -> DataSourceRecord:
    kind = SourceKind(source_kind)
    hashes = tuple(sorted(str(value) for value in raw_partition_sha256))
    if not hashes or any(
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in hashes
    ):
        raise ValueError("source_raw_partition_sha256_invalid")
    aggregate_hash = hashlib.sha256(canonical_json_bytes(hashes)).hexdigest()
    ordered_gaps = sorted(
        (dict(item) for item in gaps),
        key=canonical_json_bytes,
    )
    gap_fingerprint = hashlib.sha256(canonical_json_bytes(ordered_gaps)).hexdigest()
    return DataSourceRecord(
        source_key=source_key,
        source_kind=kind,
        provider=provider,
        source_locator=source_locator,
        retrieved_at=retrieved_at or datetime.now(timezone.utc),
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        timestamp_semantics=timestamp_semantics,
        schema_version=schema_version,
        dataset_version=dataset_version,
        transform_version=transform_version,
        git_commit=git_commit,
        raw_sha256=aggregate_hash,
        row_count=row_count,
        gap_manifest={
            "gap_count": len(gaps),
            "unclassified_gap_count": 0,
            "gap_fingerprint_sha256": gap_fingerprint,
            "raw_partition_count": len(hashes),
            "raw_partition_sha256": hashes,
        },
        license_usage_note=(
            "OKX public historical market data; preserve source terms and provenance"
        ),
        truth_tier=_TRUTH_TIER[kind],
    )


def persist_historical_bundle(
    session,
    *,
    source_id: str,
    source: DataSourceRecord,
    symbol: str,
    role: str,
    purpose: str,
    coverage_ratio: float,
    causal_time_check: bool,
) -> tuple[str, HistoricalEligibilityReport]:
    material = _bundle_material(
        source_id=source_id,
        source=source,
        symbol=symbol,
        role=role,
        purpose=purpose,
        coverage_ratio=coverage_ratio,
        causal_time_check=causal_time_check,
    )
    result = session.execute(
        text(
            """
            INSERT INTO meta.dataset_bundles (
                bundle_key, dataset_version, purpose, eligibility_mode,
                component_sources, fingerprint, coverage_start, coverage_end,
                status, eligibility_report
            ) VALUES (
                :bundle_key, :dataset_version, :purpose, 'historical_research',
                CAST(:components AS jsonb), :fingerprint, :coverage_start,
                :coverage_end, :status, CAST(:report AS jsonb)
            )
            ON CONFLICT (bundle_key) DO UPDATE SET
                bundle_key = EXCLUDED.bundle_key
            WHERE meta.dataset_bundles.fingerprint = EXCLUDED.fingerprint
              AND meta.dataset_bundles.dataset_version = EXCLUDED.dataset_version
              AND meta.dataset_bundles.purpose = EXCLUDED.purpose
              AND meta.dataset_bundles.eligibility_mode = EXCLUDED.eligibility_mode
              AND meta.dataset_bundles.coverage_start = EXCLUDED.coverage_start
              AND meta.dataset_bundles.coverage_end = EXCLUDED.coverage_end
              AND meta.dataset_bundles.status = EXCLUDED.status
            RETURNING bundle_id
            """
        ),
        {
            "bundle_key": material["bundle"].bundle_key,
            "dataset_version": source.dataset_version,
            "purpose": purpose,
            "components": json.dumps(material["components"], sort_keys=True),
            "fingerprint": bundle_fingerprint(material["bundle"]),
            "coverage_start": source.coverage_start,
            "coverage_end": source.coverage_end,
            "status": material["status"],
            "report": json.dumps(material["report_payload"], sort_keys=True, default=str),
        },
    )
    bundle_id = result.scalar_one_or_none()
    if bundle_id is None:
        raise RuntimeError("dataset_bundle_immutable_provenance_conflict")
    return str(bundle_id), material["report"]


def reserve_historical_bundle(
    session,
    *,
    source_id: str,
    source: DataSourceRecord,
    symbol: str,
    role: str,
    purpose: str,
) -> tuple[str, str | None]:
    """Reserve a transaction-local BUILDING bundle before bounded derivation.

    The reservation identity excludes derived gap evidence but binds the raw
    source, code/transform version and exact window. A committed final bundle
    may be returned for an exact retry; finalization still rechecks its full
    immutable fingerprint.
    """

    _validate_bundle_identity(
        source_id=source_id,
        symbol=symbol,
        role=role,
        purpose=purpose,
    )
    bundle_key = _historical_bundle_key(source, symbol=symbol, purpose=purpose)
    reservation_fingerprint = hashlib.sha256(
        canonical_json_bytes(
            {
                "contract": "historical-bundle-reservation-v1",
                "bundle_key": bundle_key,
                "source_id": source_id,
                "role": role,
            }
        )
    ).hexdigest()
    components = [
        {
            "source_id": source_id,
            "symbol": symbol,
            "role": role,
            "provenance": source.canonical_dict(),
            "reservation": True,
        }
    ]
    row = session.execute(
        text(
            """
            INSERT INTO meta.dataset_bundles (
                bundle_key, dataset_version, purpose, eligibility_mode,
                component_sources, fingerprint, coverage_start, coverage_end,
                status, eligibility_report
            ) VALUES (
                :bundle_key, :dataset_version, :purpose, 'historical_research',
                CAST(:components AS jsonb), :fingerprint, :coverage_start,
                :coverage_end, 'BUILDING', CAST(:report AS jsonb)
            )
            ON CONFLICT (bundle_key) DO UPDATE SET
                bundle_key = EXCLUDED.bundle_key
            WHERE (
                meta.dataset_bundles.status = 'BUILDING'
                AND meta.dataset_bundles.fingerprint = EXCLUDED.fingerprint
            ) OR meta.dataset_bundles.status IN ('ELIGIBLE','INELIGIBLE')
            RETURNING bundle_id, status, fingerprint
            """
        ),
        {
            "bundle_key": bundle_key,
            "dataset_version": source.dataset_version,
            "purpose": purpose,
            "components": json.dumps(components, sort_keys=True),
            "fingerprint": reservation_fingerprint,
            "coverage_start": source.coverage_start,
            "coverage_end": source.coverage_end,
            "report": json.dumps(
                {
                    "contract": "historical-bundle-reservation-v1",
                    "reservation_fingerprint": reservation_fingerprint,
                },
                sort_keys=True,
            ),
        },
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("dataset_bundle_reservation_conflict")
    reservation = (
        reservation_fingerprint if str(row["status"]) == "BUILDING" else None
    )
    return str(row["bundle_id"]), reservation


def finalize_historical_bundle(
    session,
    *,
    bundle_id: str,
    reservation_fingerprint: str,
    source_id: str,
    source: DataSourceRecord,
    symbol: str,
    role: str,
    purpose: str,
    coverage_ratio: float,
    causal_time_check: bool,
) -> tuple[str, HistoricalEligibilityReport]:
    """Finalize only the exact BUILDING reservation in the current transaction."""

    material = _bundle_material(
        source_id=source_id,
        source=source,
        symbol=symbol,
        role=role,
        purpose=purpose,
        coverage_ratio=coverage_ratio,
        causal_time_check=causal_time_check,
    )
    result = session.execute(
        text(
            """
            UPDATE meta.dataset_bundles SET
                dataset_version = :dataset_version,
                purpose = :purpose,
                component_sources = CAST(:components AS jsonb),
                fingerprint = :fingerprint,
                coverage_start = :coverage_start,
                coverage_end = :coverage_end,
                status = :status,
                eligibility_report = CAST(:report AS jsonb),
                updated_at = NOW()
            WHERE bundle_id = CAST(:bundle_id AS UUID)
              AND bundle_key = :bundle_key
              AND status = 'BUILDING'
              AND fingerprint = :reservation_fingerprint
            RETURNING bundle_id
            """
        ),
        {
            "bundle_id": bundle_id,
            "bundle_key": material["bundle"].bundle_key,
            "reservation_fingerprint": reservation_fingerprint,
            "dataset_version": source.dataset_version,
            "purpose": purpose,
            "components": json.dumps(material["components"], sort_keys=True),
            "fingerprint": bundle_fingerprint(material["bundle"]),
            "coverage_start": source.coverage_start,
            "coverage_end": source.coverage_end,
            "status": material["status"],
            "report": json.dumps(material["report_payload"], sort_keys=True, default=str),
        },
    ).scalar_one_or_none()
    if result is None:
        raise RuntimeError("dataset_bundle_finalize_conflict")
    return str(result), material["report"]


def _bundle_material(
    *,
    source_id: str,
    source: DataSourceRecord,
    symbol: str,
    role: str,
    purpose: str,
    coverage_ratio: float,
    causal_time_check: bool,
) -> dict[str, Any]:
    if not 0 <= coverage_ratio <= 1:
        raise ValueError("bundle_coverage_ratio_out_of_range")
    _validate_bundle_identity(
        source_id=source_id,
        symbol=symbol,
        role=role,
        purpose=purpose,
    )
    bundle = DatasetBundleContract(
        bundle_key=_historical_bundle_key(source, symbol=symbol, purpose=purpose),
        dataset_version=source.dataset_version,
        purpose=purpose,
        eligibility_mode="historical_research",
        coverage_start=source.coverage_start,
        coverage_end=source.coverage_end,
        components=(source,),
    )
    report = evaluate_historical_bundle(
        bundle,
        component_roles={source.source_key: role},
        coverage_ratios={source.source_key: coverage_ratio},
        causal_time_checks={source.source_key: causal_time_check},
    )
    components = [
        {
            "source_id": source_id,
            "symbol": symbol,
            "role": role,
            "provenance": source.canonical_dict(),
            "coverage_ratio": coverage_ratio,
            "causal_time_check": causal_time_check,
        }
    ]
    report_payload = asdict(report)
    report_payload["policy"] = asdict(report.policy)
    return {
        "bundle": bundle,
        "report": report,
        "components": components,
        "report_payload": report_payload,
        "status": "ELIGIBLE" if report.eligible else "INELIGIBLE",
    }


def _historical_bundle_key(
    source: DataSourceRecord,
    *,
    symbol: str,
    purpose: str,
) -> str:
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("bundle_symbol_invalid")
    if not str(purpose).strip():
        raise ValueError("bundle_purpose_must_be_nonempty")
    start_utc = source.coverage_start.astimezone(timezone.utc).isoformat()
    end_utc = source.coverage_end.astimezone(timezone.utc).isoformat()
    window_key = (
        f"{start_utc}_"
        f"{end_utc}"
    )
    generation_identity = {
        "source_key": source.source_key,
        "source_kind": source.source_kind.value,
        "raw_sha256": source.raw_sha256,
        "row_count": source.row_count,
        "schema_version": source.schema_version,
        "dataset_version": source.dataset_version,
        "transform_version": source.transform_version,
        "git_commit": source.git_commit,
        "coverage_start": start_utc,
        "coverage_end": end_utc,
    }
    generation = hashlib.sha256(
        canonical_json_bytes(generation_identity)
    ).hexdigest()[:16]
    return f"{purpose}:{symbol}:{source.source_key}:{window_key}:{generation}"


def _validate_bundle_identity(
    *,
    source_id: str,
    symbol: str,
    role: str,
    purpose: str,
) -> None:
    try:
        uuid.UUID(source_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("bundle_source_id_invalid") from exc
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("bundle_symbol_invalid")
    if not str(role).strip() or not str(purpose).strip():
        raise ValueError("bundle_role_and_purpose_must_be_nonempty")


__all__ = [
    "finalize_historical_bundle",
    "import_source_record",
    "persist_historical_bundle",
    "reserve_historical_bundle",
]
