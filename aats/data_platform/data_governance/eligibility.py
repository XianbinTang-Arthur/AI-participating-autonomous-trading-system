"""Source-aware historical research eligibility and compatibility matrix."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Mapping

from aats.data_platform.data_governance.contracts import (
    DataSourceRecord,
    DatasetBundleContract,
    SourceKind,
    bundle_fingerprint,
    source_identity_dict,
)


SOURCE_COMPATIBILITY: Mapping[str, frozenset[SourceKind]] = {
    "ohlcv_research": frozenset({SourceKind.OKX_REST, SourceKind.OKX_BULK, SourceKind.DERIVED}),
    "microstructure_research": frozenset({SourceKind.AATS_WS_CAPTURE, SourceKind.OKX_BULK, SourceKind.DERIVED}),
    "l2_replay": frozenset({SourceKind.AATS_WS_CAPTURE, SourceKind.OKX_BULK}),
    "live_calibration": frozenset({SourceKind.AATS_WS_CAPTURE, SourceKind.DERIVED}),
    "capital_eligibility": frozenset({SourceKind.AATS_WS_CAPTURE, SourceKind.OKX_REST, SourceKind.DERIVED}),
    "trade_flow_research": frozenset(
        {SourceKind.AATS_WS_CAPTURE, SourceKind.OKX_REST, SourceKind.OKX_BULK, SourceKind.DERIVED}
    ),
    "mark_price_research": frozenset(
        {SourceKind.OKX_REST, SourceKind.DERIVED, SourceKind.PROXY}
    ),
    "funding_research": frozenset(
        {SourceKind.OKX_REST, SourceKind.OKX_BULK, SourceKind.DERIVED}
    ),
}

_REQUIRED_ROLES: Mapping[str, frozenset[str]] = {
    "ohlcv_research": frozenset({"candles"}),
    "microstructure_research": frozenset({"trades", "orderbook"}),
    "l2_replay": frozenset({"l2_event_history"}),
    "live_calibration": frozenset({"live_capture"}),
    "capital_eligibility": frozenset({"capital_fact"}),
    "trade_flow_research": frozenset({"trades"}),
    "mark_price_research": frozenset({"mark_price_bar"}),
    "funding_research": frozenset({"funding"}),
}


@dataclass(frozen=True)
class HistoricalEligibilityPolicy:
    policy_version: str = "historical-research-v2"
    minimum_coverage_ratio: float = 0.995
    allow_proxy_roles: tuple[str, ...] = ("mark_price_bar",)
    allow_third_party: bool = False


@dataclass(frozen=True)
class HistoricalEligibilityReport:
    policy: HistoricalEligibilityPolicy
    purpose: str
    bundle_fingerprint: str
    eligible: bool
    reason_codes: tuple[str, ...]
    component_fingerprints: tuple[str, ...]
    evidence_fingerprint: str


def evaluate_historical_bundle(
    bundle: DatasetBundleContract,
    *,
    component_roles: Mapping[str, str],
    coverage_ratios: Mapping[str, float],
    causal_time_checks: Mapping[str, bool],
    policy: HistoricalEligibilityPolicy | None = None,
) -> HistoricalEligibilityReport:
    selected = policy or HistoricalEligibilityPolicy()
    reasons: set[str] = set()
    if bundle.eligibility_mode != "historical_research":
        reasons.add("bundle_mode_not_historical_research")
    allowed = SOURCE_COMPATIBILITY.get(bundle.purpose)
    if allowed is None:
        reasons.add("unsupported_research_purpose")

    present_roles = {
        role
        for component in bundle.components
        if (role := component_roles.get(component.source_key)) is not None
    }
    for required_role in _REQUIRED_ROLES.get(bundle.purpose, frozenset()):
        if required_role not in present_roles:
            reasons.add(f"required_component_role_missing:{required_role}")

    fingerprints: list[str] = []
    for component in bundle.components:
        key = component.source_key
        role = component_roles.get(key)
        if role is None:
            reasons.add(f"component_role_missing:{key}")
        if component.coverage_start > bundle.coverage_start or component.coverage_end < bundle.coverage_end:
            reasons.add(f"component_coverage_outside_bundle:{key}")
        ratio = coverage_ratios.get(key)
        if ratio is None:
            reasons.add(f"coverage_ratio_missing:{key}")
        elif ratio < selected.minimum_coverage_ratio:
            reasons.add(f"coverage_ratio_below_minimum:{key}")
        if causal_time_checks.get(key) is not True:
            reasons.add(f"causal_time_check_failed:{key}")
        if component.gap_manifest.get("unclassified_gap_count", 0):
            reasons.add(f"unclassified_gaps:{key}")
        if component.source_kind == SourceKind.THIRD_PARTY and not selected.allow_third_party:
            reasons.add(f"third_party_source_disallowed:{key}")
        if component.source_kind == SourceKind.PROXY and role not in selected.allow_proxy_roles:
            reasons.add(f"proxy_role_disallowed:{key}:{role}")
        if allowed is not None and component.source_kind not in allowed:
            if not (component.source_kind == SourceKind.PROXY and role in selected.allow_proxy_roles):
                reasons.add(f"source_incompatible:{key}:{component.source_kind.value}")
        fingerprints.append(_source_evidence_fingerprint(component))

    ordered = tuple(sorted(reasons))
    bundle_hash = bundle_fingerprint(bundle)
    payload = {
        "policy": asdict(selected),
        "purpose": bundle.purpose,
        "bundle_fingerprint": bundle_hash,
        "eligible": not ordered,
        "reason_codes": ordered,
        "component_fingerprints": sorted(fingerprints),
    }
    evidence = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return HistoricalEligibilityReport(
        policy=selected,
        purpose=bundle.purpose,
        bundle_fingerprint=bundle_hash,
        eligible=not ordered,
        reason_codes=ordered,
        component_fingerprints=tuple(sorted(fingerprints)),
        evidence_fingerprint=evidence,
    )


def _source_evidence_fingerprint(source: DataSourceRecord) -> str:
    payload = source_identity_dict(source)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
