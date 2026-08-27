"""Versioned provenance contract for post-apply capital evidence.

Observation and rollback artifacts are persisted independently from the
mutable snapshots that produced them.  A timestamp on the wrapper alone does
not prove that the underlying signal was observed after the parameter release.
This module gives every risk-bearing item an explicit, versioned source
identity and validates it before the signal may authorize a rollback.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from aats.data_platform.governance._time_util import parse_iso_datetime_utc

POST_APPLY_EVIDENCE_CONTRACT_VERSION = "rdp-post-apply-evidence/v1"

_SOURCE_CONTRACTS = {
    "governance_snapshot": "governance-snapshot/v1",
    "active_decision": "active-decision/v1",
    "research_round": "research-round-snapshot/v1",
}
_EXPECTED_RISK_SOURCES: dict[str, dict[str, tuple[str, str | None]]] = {
    "observation": {
        "quality_monitor": ("governance_snapshot", None),
        "decision_status": ("active_decision", None),
        "attribution": ("research_round", "phase3"),
        "execution_realism": ("research_round", "phase4"),
    },
    "rollback_recommendation": {
        "attribution_regression": ("research_round", "phase3"),
        "execution_regression": ("research_round", "phase4"),
        "governance_regression": ("governance_snapshot", None),
    },
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def make_source_provenance(
    *,
    source_kind: str,
    source_id: str,
    source_timestamp: str | datetime,
    source_payload: Mapping[str, Any],
    source_phase: str | None = None,
    source_family: str | None = None,
    source_timeframe: str | None = None,
) -> dict[str, Any]:
    """Build one canonical source reference and a deterministic payload hash."""

    kind = _exact_nonempty_string(source_kind)
    identifier = _exact_nonempty_string(source_id)
    if kind not in _SOURCE_CONTRACTS or identifier is None:
        raise ValueError("post-apply source identity is invalid")
    instant = _canonical_instant(source_timestamp)
    if instant is None:
        raise ValueError("post-apply source timestamp is invalid")
    if not isinstance(source_payload, Mapping):
        raise ValueError("post-apply source payload must be a mapping")
    encoded = json.dumps(
        source_payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    provenance: dict[str, Any] = {
        "source_kind": kind,
        "source_contract_version": _SOURCE_CONTRACTS[kind],
        "source_id": identifier,
        "source_timestamp": instant.isoformat(),
        "source_fingerprint": hashlib.sha256(encoded).hexdigest(),
    }
    if source_phase is not None:
        phase = _exact_nonempty_string(source_phase)
        if phase is None:
            raise ValueError("post-apply source phase is invalid")
        provenance["source_phase"] = phase
    if kind in {"active_decision", "research_round"}:
        family = _normalized_scope_token(source_family)
        timeframe = _normalized_scope_token(source_timeframe)
        if family is None or timeframe is None:
            raise ValueError("post-apply scoped source identity is invalid")
        provenance["source_scope"] = {
            "family": family,
            "timeframe": timeframe,
            "combo_key": f"{family}_{timeframe}",
        }
    elif source_family is not None or source_timeframe is not None:
        raise ValueError("global source must not declare a combo scope")
    return provenance


def collect_source_provenance(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return a stable, de-duplicated provenance list from evidence items."""

    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        source = item.get("source_provenance")
        if not isinstance(source, dict):
            continue
        key = json.dumps(source, ensure_ascii=False, sort_keys=True, default=str)
        unique[key] = dict(source)
    return [unique[key] for key in sorted(unique)]


def risk_evidence_provenance_error(
    release: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    evidence_kind: str,
) -> str | None:
    """Validate versioned source lineage for an artifact that asserts risk.

    Non-risk artifacts do not authorize capital movement and remain readable
    for backwards-compatible audit/display.  A risk artifact, however, must
    use the current contract and bind every firing item to a canonical source
    timestamp at or after ``release.applied_at``.
    """

    risk_items = _risk_items(evidence, evidence_kind=evidence_kind)
    if risk_items is None:
        return None
    if not risk_items:
        return "risk evidence contains no valid firing source item"
    shape_error = _risk_shape_error(
        evidence,
        evidence_kind=evidence_kind,
        risk_items=risk_items,
    )
    if shape_error is not None:
        return shape_error
    if evidence.get("evidence_contract_version") != POST_APPLY_EVIDENCE_CONTRACT_VERSION:
        return "risk evidence contract version is missing or unsupported"
    top_sources = evidence.get("source_provenance")
    if not isinstance(top_sources, list) or not top_sources:
        return "risk evidence source provenance is missing"
    if not all(isinstance(source, dict) for source in top_sources):
        return "risk evidence source provenance is malformed"

    applied_at = _canonical_instant(release.get("applied_at"))
    evaluated_at = _canonical_instant(evidence.get("evaluated_at"))
    now = datetime.now(timezone.utc)
    if applied_at is None or evaluated_at is None:
        return "release/evidence timestamp is invalid for source provenance"
    expected_sources = _EXPECTED_RISK_SOURCES.get(evidence_kind)
    if expected_sources is None:
        return "risk evidence kind is unsupported"

    for item_name, item in risk_items:
        expected = expected_sources.get(item_name)
        if expected is None:
            return f"risk evidence item is unsupported: {item_name}"
        source = item.get("source_provenance")
        if not isinstance(source, dict) or source not in top_sources:
            return f"risk evidence item lacks top-level provenance: {item_name}"
        expected_kind, expected_phase = expected
        if source.get("source_kind") != expected_kind:
            return f"risk evidence source kind is invalid: {item_name}"
        if source.get("source_contract_version") != _SOURCE_CONTRACTS[expected_kind]:
            return f"risk evidence source contract is invalid: {item_name}"
        if _exact_nonempty_string(source.get("source_id")) is None:
            return f"risk evidence source id is invalid: {item_name}"
        fingerprint = source.get("source_fingerprint")
        if not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
            return f"risk evidence source fingerprint is invalid: {item_name}"
        if expected_phase is not None and source.get("source_phase") != expected_phase:
            return f"risk evidence source phase is invalid: {item_name}"
        if expected_phase is None and source.get("source_phase") not in {None, ""}:
            return f"risk evidence source phase is unexpected: {item_name}"
        canonical_family = _normalized_scope_token(release.get("family"))
        canonical_timeframe = _normalized_scope_token(release.get("timeframe"))
        if expected_kind in {"active_decision", "research_round"}:
            expected_scope = {
                "family": canonical_family,
                "timeframe": canonical_timeframe,
                "combo_key": f"{canonical_family}_{canonical_timeframe}",
            }
            if (
                canonical_family is None
                or canonical_timeframe is None
                or source.get("source_scope") != expected_scope
            ):
                return f"risk evidence source scope is invalid: {item_name}"
        elif source.get("source_scope") is not None:
            return f"global risk evidence source scope is invalid: {item_name}"
        source_time = _canonical_instant(source.get("source_timestamp"))
        if (
            source_time is None
            or source_time < applied_at
            or source_time > evaluated_at
            or source_time > now
        ):
            return f"risk evidence source timestamp is invalid: {item_name}"
    return None


def _risk_items(
    evidence: Mapping[str, Any],
    *,
    evidence_kind: str,
) -> list[tuple[str, Mapping[str, Any]]] | None:
    if evidence_kind == "observation":
        is_risk = (
            evidence.get("status") == "rollback_recommended"
            and evidence.get("recommendation") == "rollback_recommended"
        )
        if not is_risk:
            return None
        items = evidence.get("checklist")
        name_key = "name"
        selected = (
            [item for item in items if isinstance(item, Mapping) and item.get("status") == "regression"]
            if isinstance(items, list)
            else []
        )
    elif evidence_kind == "rollback_recommendation":
        if evidence.get("rollback_recommended") is not True:
            return None
        items = evidence.get("triggers")
        name_key = "trigger"
        selected = (
            [
                item
                for item in items
                if isinstance(item, Mapping)
                and item.get("fired") is True
                and item.get("evidence_status") == "valid"
            ]
            if isinstance(items, list)
            else []
        )
    else:
        return []
    if not selected:
        return []
    output: list[tuple[str, Mapping[str, Any]]] = []
    for item in selected:
        name = _exact_nonempty_string(item.get(name_key))
        if name is None:
            return [("<invalid>", item)]
        output.append((name, item))
    return output


def _risk_shape_error(
    evidence: Mapping[str, Any],
    *,
    evidence_kind: str,
    risk_items: list[tuple[str, Mapping[str, Any]]],
) -> str | None:
    if any(item.get("severity") not in {"medium", "high"} for _, item in risk_items):
        return "risk evidence firing item severity is invalid"
    if evidence_kind == "observation":
        if evidence.get("regression_count") != len(risk_items):
            return "risk observation regression count is inconsistent"
        return None

    triggers = evidence.get("triggers")
    if not isinstance(triggers, list) or any(
        isinstance(item, Mapping)
        and item.get("fired") is True
        and item.get("evidence_status") != "valid"
        for item in triggers
    ):
        return "rollback evidence contains a fired non-valid trigger"
    high_count = sum(
        1 for _, item in risk_items if item.get("severity") == "high"
    )
    medium_count = sum(
        1 for _, item in risk_items if item.get("severity") == "medium"
    )
    expected_severity = "high" if high_count > 0 or medium_count >= 2 else "medium"
    if evidence.get("severity") != expected_severity:
        return "rollback evidence severity is inconsistent with firing triggers"
    if evidence.get("fired_trigger_count") != len(risk_items):
        return "rollback evidence firing trigger count is inconsistent"
    return None


def _canonical_instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
    elif not isinstance(value, str):
        return None
    else:
        token = value.strip()
        if not token or not (token.endswith("Z") or token.endswith("+00:00")):
            return None
    try:
        return parse_iso_datetime_utc(value, context="post_apply_evidence.source_time")
    except (TypeError, ValueError):
        return None


def _exact_nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    return value


def _normalized_scope_token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    return token or None
