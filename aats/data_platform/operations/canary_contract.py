"""Static validation for the non-deployable future derivatives canary contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class CanaryContractValidation:
    valid: bool
    deployable: bool
    reason_codes: tuple[str, ...]
    contract_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "deployable": self.deployable,
            "reason_codes": list(self.reason_codes),
            "contract_fingerprint": self.contract_fingerprint,
            "authorization_boundary": (
                "static contract validation only; no live-deployment authorization"
            ),
        }


def validate_canary_contract(payload: Mapping[str, Any]) -> CanaryContractValidation:
    """Require the v1 contract to remain least-privilege and hard NO-GO."""

    reasons: set[str] = set()
    deployment = payload.get("deployment")
    venue = payload.get("venue")
    credential = payload.get("credential_policy")
    risk = payload.get("risk_limits")
    governance = payload.get("governance")
    for name, value in (
        ("deployment", deployment),
        ("venue", venue),
        ("credential_policy", credential),
        ("risk_limits", risk),
        ("governance", governance),
    ):
        if not isinstance(value, Mapping):
            reasons.add(f"section_missing:{name}")
    if payload.get("format_version") != 1:
        reasons.add("unsupported_format_version")
    if payload.get("status") != "design_only_no_go":
        reasons.add("status_must_be_design_only_no_go")

    if isinstance(deployment, Mapping):
        false_fields = (
            "deployable",
            "registered_deploy_profile",
            "live_profile_unlock",
            "override_supported",
        )
        for field in false_fields:
            if deployment.get(field) is not False:
                reasons.add(f"deployment_must_be_false:{field}")
        if deployment.get("requires_separate_go_decision") is not True:
            reasons.add("separate_go_decision_required")

    if isinstance(venue, Mapping):
        if venue.get("exchange") != "okx" or venue.get("product_type") != "swap":
            reasons.add("venue_scope_invalid")
        if venue.get("margin_mode") != "isolated":
            reasons.add("isolated_margin_required")
        symbols = venue.get("symbols")
        if symbols != ["BTC-USDT-SWAP"]:
            reasons.add("single_symbol_scope_required")

    if isinstance(credential, Mapping):
        forbidden = set(credential.get("forbidden_permissions") or ())
        if not {"withdraw", "transfer"}.issubset(forbidden):
            reasons.add("withdraw_and_transfer_must_be_forbidden")
        required = set(credential.get("required_permissions") or ())
        if required != {"read", "trade"}:
            reasons.add("credential_permissions_not_minimal")
        if credential.get("dedicated_subaccount_required") is not True:
            reasons.add("dedicated_subaccount_required")
        if credential.get("ip_allowlist_required") is not True:
            reasons.add("ip_allowlist_required")

    if isinstance(risk, Mapping):
        numeric_limits = {
            "leverage": 1,
            "max_order_notional_usdt": 25,
            "max_gross_notional_usdt": 50,
            "max_daily_loss_usdt": 5,
            "max_open_positions": 1,
            "max_orders_per_hour": 4,
        }
        for field, maximum in numeric_limits.items():
            value = risk.get(field)
            if not isinstance(value, int | float) or isinstance(value, bool):
                reasons.add(f"risk_limit_missing:{field}")
            elif value <= 0 or value > maximum:
                reasons.add(f"risk_limit_exceeds_contract:{field}")
        if risk.get("reduce_only_exit_required") is not True:
            reasons.add("reduce_only_exit_required")
        if risk.get("manual_resume_required") is not True:
            reasons.add("manual_resume_required")

    if isinstance(governance, Mapping):
        if governance.get("minimum_distinct_operators", 0) < 2:
            reasons.add("dual_operator_required")
        if governance.get("approver_must_differ_from_executor") is not True:
            reasons.add("approver_executor_separation_required")
        if governance.get("automatic_cap_increase_allowed") is not False:
            reasons.add("automatic_cap_increase_forbidden")
        if governance.get("automatic_resume_allowed") is not False:
            reasons.add("automatic_resume_forbidden")
        required_evidence = set(governance.get("required_evidence") or ())
        minimum_evidence = {
            "trading_readiness_v2_or_later",
            "complete_fault_matrix",
            "fresh_exchange_reconciliation",
            "parameter_runtime_readback",
            "kill_switch_execution_ack",
            "credential_permission_attestation",
        }
        missing = sorted(minimum_evidence - required_evidence)
        reasons.update(f"required_evidence_missing:{name}" for name in missing)

    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    ordered = tuple(sorted(reasons))
    return CanaryContractValidation(
        valid=not ordered,
        deployable=False,
        reason_codes=ordered,
        contract_fingerprint=fingerprint,
    )
