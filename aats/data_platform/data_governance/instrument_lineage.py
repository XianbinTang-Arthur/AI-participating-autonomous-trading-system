"""Contract-snapshot lineage checks for historical data bundles."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import text

from aats.data_platform.data_governance.contracts import (
    DataSourceRecord,
    canonical_json_bytes,
)
from aats.domain.instrument_contract import InstrumentContractError
from aats.domain.instrument_contract_snapshot import InstrumentContractSnapshot
from aats.domain.instrument_scope import (
    INSTRUMENT_SCOPE_UNSUPPORTED_REASON,
    classify_instrument_scope,
)


@dataclass(frozen=True)
class InstrumentContractBindingReport:
    policy_version: str
    required: bool
    eligible: bool
    snapshot_digest: str | None
    reason_codes: tuple[str, ...]
    evidence_fingerprint: str


def instrument_contract_snapshot_registry_identity(
    snapshot: InstrumentContractSnapshot,
) -> dict[str, Any]:
    """Return the stable identity used by the narrow registry evidence anchor."""

    serialized = snapshot.to_dict()
    return {
        "record_type": "instrument_contract_snapshot_identity_v1",
        "venue": snapshot.venue,
        "symbol": snapshot.contract.symbol,
        "observed_at": serialized["observed_at"],
        "effective_window": serialized["effective_window"],
        "evidence_kind": serialized["evidence"]["kind"],
        "source_locator": snapshot.source_locator,
        "source_schema": snapshot.source_schema,
    }


def instrument_contract_snapshot_source_key(
    snapshot: InstrumentContractSnapshot,
) -> str:
    """Hash identity and window, deliberately excluding the contract payload."""

    identity_digest = hashlib.sha256(
        canonical_json_bytes(instrument_contract_snapshot_registry_identity(snapshot))
    ).hexdigest()
    return (
        f"instrument-contract:{snapshot.venue}:"
        f"{snapshot.contract.symbol}:{identity_digest}"
    )


def load_verified_instrument_contract_snapshot(
    session,
    *,
    snapshot_source_id: str,
) -> InstrumentContractSnapshot:
    """Load and verify the registry row behind an embedded snapshot reference.

    JSON references have no database foreign key in the Legacy schema.  Every
    consumer therefore has to re-anchor the reference at use time.  This is an
    application-layer safeguard; database-native immutability still requires
    the separately governed Stage 20 migration.
    """

    try:
        normalized_source_id = str(uuid.UUID(str(snapshot_source_id)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("instrument_snapshot_source_reference_invalid") from exc
    row = session.execute(
        text(
            "SELECT source_key, source_kind, provider, source_locator, "
            "schema_version, truth_tier, source_metadata "
            "FROM meta.data_source_registry "
            "WHERE source_id = CAST(:source_id AS UUID)"
        ),
        {"source_id": normalized_source_id},
    ).mappings().one_or_none()
    if row is None:
        raise ValueError("instrument_snapshot_source_not_found")
    metadata = row["source_metadata"]
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError as exc:
            raise ValueError("instrument_snapshot_source_anchor_mismatch") from exc
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("record_type") != "instrument_contract_snapshot_v1"
        or not isinstance(metadata.get("identity"), Mapping)
        or not isinstance(metadata.get("snapshot"), Mapping)
    ):
        raise ValueError("instrument_snapshot_source_anchor_mismatch")
    snapshot = InstrumentContractSnapshot.from_dict(metadata["snapshot"])
    evidence_reason = instrument_snapshot_temporal_evidence_reason(snapshot)
    expected_kind = "okx_rest" if evidence_reason is None else "third_party"
    expected_tier = (
        "authoritative_external" if evidence_reason is None else "external_unverified"
    )
    if (
        str(row["source_key"]) != instrument_contract_snapshot_source_key(snapshot)
        or str(row["source_kind"]) != expected_kind
        or str(row["provider"]) != snapshot.venue
        or str(row["source_locator"]) != snapshot.source_locator
        or str(row["schema_version"]) != snapshot.source_schema
        or str(row["truth_tier"]) != expected_tier
        or dict(metadata["identity"])
        != instrument_contract_snapshot_registry_identity(snapshot)
    ):
        raise ValueError("instrument_snapshot_source_anchor_mismatch")
    return snapshot


def evaluate_instrument_contract_binding(
    source: DataSourceRecord,
    *,
    symbol: str,
    coverage_start: datetime,
    coverage_end: datetime,
) -> InstrumentContractBindingReport:
    """Evaluate monetary-contract eligibility independently from raw eligibility."""

    normalized_symbol = str(symbol or "").strip().upper()
    instrument_scope = classify_instrument_scope(normalized_symbol)
    required = instrument_scope == "swap"
    reasons: list[str] = []
    if instrument_scope == "unsupported":
        reasons.append(INSTRUMENT_SCOPE_UNSUPPORTED_REASON)
    snapshot = source.instrument_contract_snapshot
    digest: str | None = None
    if isinstance(snapshot, InstrumentContractSnapshot):
        digest = snapshot.digest
        scope_reason = instrument_snapshot_scope_reason(
            snapshot,
            symbol=normalized_symbol,
        )
        if scope_reason is not None:
            reasons.append(scope_reason)
        evidence_reason = instrument_snapshot_temporal_evidence_reason(snapshot)
        if evidence_reason is not None:
            reasons.append(evidence_reason)
        if scope_reason is None and evidence_reason is None:
            try:
                snapshot.validate_window(
                    symbol=normalized_symbol,
                    start=coverage_start,
                    end=coverage_end,
                )
            except InstrumentContractError as exc:
                reasons.append(str(exc))
    elif required:
        reasons.append("derivative_instrument_metadata_required")

    ordered = tuple(sorted(set(reasons)))
    material = {
        "policy_version": "instrument-contract-binding-v1",
        "required": required,
        "eligible": not ordered,
        "snapshot_digest": digest,
        "reason_codes": ordered,
    }
    return InstrumentContractBindingReport(
        **material,
        evidence_fingerprint=hashlib.sha256(
            canonical_json_bytes(material)
        ).hexdigest(),
    )


def instrument_snapshot_temporal_evidence_reason(
    snapshot: InstrumentContractSnapshot,
) -> str | None:
    """Return why a snapshot cannot yet authorize a historical window."""

    if snapshot.evidence_kind != "observed_forward":
        return "instrument_snapshot_authoritative_history_unverified"
    # Both single observations and aggregate windows are caller-provided DTOs.
    # Until a verifier anchors their raw REST captures/manifest, a locator and
    # payload digest alone cannot authorize any historical or future window.
    return "instrument_snapshot_observation_evidence_unverified"


def instrument_snapshot_scope_reason(
    snapshot: InstrumentContractSnapshot,
    *,
    symbol: str,
) -> str | None:
    """Return why snapshot contract semantics contradict the requested scope."""

    normalized_symbol = str(symbol or "").strip().upper()
    requested_scope = classify_instrument_scope(normalized_symbol)
    snapshot_scope = classify_instrument_scope(snapshot.contract.symbol)
    if requested_scope == "unsupported":
        return INSTRUMENT_SCOPE_UNSUPPORTED_REASON
    if snapshot.contract.symbol != normalized_symbol:
        return "instrument_snapshot_symbol_mismatch"
    if snapshot_scope != requested_scope:
        return "instrument_snapshot_scope_mismatch"
    if (
        requested_scope == "spot"
        and snapshot.contract.contract_type != "spot"
    ) or (
        requested_scope == "swap"
        and snapshot.contract.contract_type not in {"linear", "inverse"}
    ):
        return "instrument_snapshot_scope_mismatch"
    return None


def binding_report_payload(
    report: InstrumentContractBindingReport,
) -> dict[str, object]:
    return asdict(report)


__all__ = [
    "InstrumentContractBindingReport",
    "binding_report_payload",
    "evaluate_instrument_contract_binding",
    "instrument_contract_snapshot_registry_identity",
    "instrument_contract_snapshot_source_key",
    "instrument_snapshot_scope_reason",
    "instrument_snapshot_temporal_evidence_reason",
    "load_verified_instrument_contract_snapshot",
]
