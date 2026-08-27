"""Persistent provenance and historical dataset-bundle registration."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

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
from aats.data_platform.data_governance.instrument_lineage import (
    binding_report_payload,
    evaluate_instrument_contract_binding,
    instrument_contract_snapshot_registry_identity,
    instrument_contract_snapshot_source_key,
    instrument_snapshot_temporal_evidence_reason,
)
from aats.domain.instrument_contract_snapshot import InstrumentContractSnapshot


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
    instrument_contract_snapshot: InstrumentContractSnapshot | Mapping[str, Any] | None = None,
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
        instrument_contract_snapshot=instrument_contract_snapshot,
    )


def register_instrument_contract_snapshot_source(
    session,
    snapshot: InstrumentContractSnapshot,
) -> str:
    """Converge one immutable OKX contract observation in the source registry.

    This deliberately does not implement an Instrument Master or a latest-by-
    symbol lookup.  The source key binds the observation identity and window,
    while the conflict clause compares every persisted field so a same-
    identity/different-payload race fails closed at the application boundary.
    """

    if snapshot.venue != "OKX":
        raise ValueError("instrument_snapshot_venue_unsupported")
    serialized = snapshot.to_dict()
    identity = instrument_contract_snapshot_registry_identity(snapshot)
    source_key = instrument_contract_snapshot_source_key(snapshot)
    metadata = {
        "record_type": "instrument_contract_snapshot_v1",
        "identity": identity,
        "snapshot": serialized,
    }
    evidence_reason = instrument_snapshot_temporal_evidence_reason(snapshot)
    # The current registry constraint intentionally permits OKX REST rows only
    # as authoritative external evidence.  A caller-supplied aggregate window
    # has not yet been re-anchored to raw REST captures, so persist that claim
    # in the schema's unverified class instead of mislabelling it authoritative.
    source_kind = "okx_rest" if evidence_reason is None else "third_party"
    truth_tier = (
        "authoritative_external" if evidence_reason is None else "external_unverified"
    )
    value = session.execute(
        text(
            """
            INSERT INTO meta.data_source_registry (
                source_key, source_kind, provider, source_locator,
                schema_version, timestamp_semantics, truth_tier,
                license_usage_note, source_metadata
            ) VALUES (
                :source_key, :source_kind, :provider, :source_locator,
                :schema_version, 'half-open effective instrument-contract window',
                :truth_tier,
                'OKX public instrument definition; retain source terms and provenance',
                CAST(:source_metadata AS jsonb)
            ) ON CONFLICT (source_key) DO UPDATE SET
                source_key = EXCLUDED.source_key
            WHERE meta.data_source_registry.source_kind = EXCLUDED.source_kind
              AND meta.data_source_registry.provider = EXCLUDED.provider
              AND meta.data_source_registry.source_locator = EXCLUDED.source_locator
              AND meta.data_source_registry.schema_version = EXCLUDED.schema_version
              AND meta.data_source_registry.timestamp_semantics = EXCLUDED.timestamp_semantics
              AND meta.data_source_registry.truth_tier = EXCLUDED.truth_tier
              AND meta.data_source_registry.license_usage_note = EXCLUDED.license_usage_note
              AND meta.data_source_registry.source_metadata = EXCLUDED.source_metadata
            RETURNING source_id
            """
        ),
        {
            "source_key": source_key,
            "source_kind": source_kind,
            "provider": snapshot.venue,
            "source_locator": snapshot.source_locator,
            "schema_version": snapshot.source_schema,
            "truth_tier": truth_tier,
            "source_metadata": json.dumps(metadata, sort_keys=True),
        },
    ).scalar_one_or_none()
    if value is None:
        existing = session.execute(
            text(
                "SELECT source_metadata FROM meta.data_source_registry "
                "WHERE source_key = :source_key"
            ),
            {"source_key": source_key},
        ).mappings().one_or_none()
        existing_metadata = (
            existing.get("source_metadata")
            if isinstance(existing, Mapping)
            else None
        )
        existing_snapshot = (
            existing_metadata.get("snapshot")
            if isinstance(existing_metadata, Mapping)
            else None
        )
        if (
            isinstance(existing_snapshot, Mapping)
            and existing_snapshot.get("snapshot_digest") == snapshot.digest
            and dict(existing_snapshot) != serialized
        ):
            raise RuntimeError("instrument_snapshot_digest_collision")
        raise RuntimeError("instrument_snapshot_source_identity_conflict")
    return str(value)


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
    _validate_bundle_identity(
        source_id=source_id,
        symbol=symbol,
        role=role,
        purpose=purpose,
    )
    snapshot_source_id = _register_snapshot_source_if_present(session, source)
    material = _bundle_material(
        source_id=source_id,
        snapshot_source_id=snapshot_source_id,
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
              AND (
                    meta.dataset_bundles.component_sources
                    #- '{0,provenance,retrieved_at}'
                  ) = (
                    EXCLUDED.component_sources
                    #- '{0,provenance,retrieved_at}'
                  )
              AND meta.dataset_bundles.eligibility_report = EXCLUDED.eligibility_report
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
    snapshot_source_id = _register_snapshot_source_if_present(session, source)
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
    component = {
        "source_id": source_id,
        "symbol": symbol,
        "role": role,
        "provenance": source.canonical_dict(),
        "reservation": True,
    }
    _bind_snapshot_reference(
        component,
        source=source,
        snapshot_source_id=snapshot_source_id,
    )
    components = [component]
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

    _validate_bundle_identity(
        source_id=source_id,
        symbol=symbol,
        role=role,
        purpose=purpose,
    )
    snapshot_source_id = _register_snapshot_source_if_present(session, source)
    material = _bundle_material(
        source_id=source_id,
        snapshot_source_id=snapshot_source_id,
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
    snapshot_source_id: str | None,
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
    binding_report = evaluate_instrument_contract_binding(
        source,
        symbol=symbol,
        coverage_start=source.coverage_start,
        coverage_end=source.coverage_end,
    )
    if not binding_report.eligible:
        combined_reasons = tuple(
            sorted({*report.reason_codes, *binding_report.reason_codes})
        )
        evidence_material = {
            "policy": asdict(report.policy),
            "purpose": report.purpose,
            "bundle_fingerprint": report.bundle_fingerprint,
            "eligible": False,
            "reason_codes": combined_reasons,
            "component_fingerprints": report.component_fingerprints,
            "instrument_contract_binding": binding_report_payload(binding_report),
        }
        report = HistoricalEligibilityReport(
            policy=report.policy,
            purpose=report.purpose,
            bundle_fingerprint=report.bundle_fingerprint,
            eligible=False,
            reason_codes=combined_reasons,
            component_fingerprints=report.component_fingerprints,
            evidence_fingerprint=hashlib.sha256(
                canonical_json_bytes(evidence_material)
            ).hexdigest(),
        )
    component = {
        "source_id": source_id,
        "symbol": symbol,
        "role": role,
        "provenance": source.canonical_dict(),
        "coverage_ratio": coverage_ratio,
        "causal_time_check": causal_time_check,
    }
    _bind_snapshot_reference(
        component,
        source=source,
        snapshot_source_id=snapshot_source_id,
    )
    components = [component]
    report_payload = asdict(report)
    report_payload["policy"] = asdict(report.policy)
    report_payload["instrument_contract_binding"] = binding_report_payload(
        binding_report
    )
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
    snapshot = source.instrument_contract_snapshot
    if isinstance(snapshot, InstrumentContractSnapshot):
        generation_identity["instrument_snapshot_digest"] = snapshot.digest
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
        parsed_source_id = uuid.UUID(source_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("bundle_source_id_invalid") from exc
    if source_id != str(parsed_source_id):
        raise ValueError("bundle_source_id_invalid")
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("bundle_symbol_invalid")
    if not str(role).strip() or not str(purpose).strip():
        raise ValueError("bundle_role_and_purpose_must_be_nonempty")


def _register_snapshot_source_if_present(session, source: DataSourceRecord) -> str | None:
    snapshot = source.instrument_contract_snapshot
    if snapshot is None:
        return None
    if not isinstance(snapshot, InstrumentContractSnapshot):  # pragma: no cover
        raise ValueError("instrument_snapshot_shape_invalid")
    return register_instrument_contract_snapshot_source(session, snapshot)


def _bind_snapshot_reference(
    component: dict[str, Any],
    *,
    source: DataSourceRecord,
    snapshot_source_id: str | None,
) -> None:
    snapshot = source.instrument_contract_snapshot
    if snapshot is None:
        if snapshot_source_id is not None:  # pragma: no cover
            raise ValueError("instrument_snapshot_source_reference_unexpected")
        return
    if not isinstance(snapshot, InstrumentContractSnapshot):  # pragma: no cover
        raise ValueError("instrument_snapshot_shape_invalid")
    if not snapshot_source_id:
        raise ValueError("instrument_snapshot_source_reference_required")
    component["instrument_snapshot_digest"] = snapshot.digest
    component["instrument_snapshot_source_id"] = snapshot_source_id


__all__ = [
    "finalize_historical_bundle",
    "import_source_record",
    "persist_historical_bundle",
    "register_instrument_contract_snapshot_source",
    "reserve_historical_bundle",
]
