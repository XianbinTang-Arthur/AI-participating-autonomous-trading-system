#!/usr/bin/env python
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def evidence_sha256(evidence: dict[str, Any]) -> str:
    payload = json.dumps(evidence, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _runtime_claimed_submit_snapshot(runtime_truth: dict[str, Any]) -> dict[str, Any]:
    claimed = as_dict(runtime_truth.get("claimed_submit_stuck_submission_truth"))
    latest_order = as_dict(claimed.get("latest_order"))
    latest_reconciliation = as_dict(claimed.get("latest_reconciliation"))
    latest_baseline = as_dict(claimed.get("latest_baseline"))
    operator_action_counts = as_dict(claimed.get("operator_action_counts"))
    return {
        "client_order_id": latest_order.get("client_order_id"),
        "command_id": latest_order.get("command_id"),
        "decision_id": latest_order.get("decision_id"),
        "intent_id": latest_order.get("intent_id"),
        "execution_order_state": latest_order.get("execution_order_state"),
        "command_state": latest_order.get("command_state"),
        "venue_order_id": latest_order.get("venue_order_id"),
        "exchange_order_id": latest_order.get("exchange_order_id"),
        "execution_fill_count": latest_order.get("execution_fill_count"),
        "fill_event_count": latest_order.get("fill_event_count"),
        "latest_reconciliation_severity": latest_reconciliation.get("severity"),
        "latest_reconciliation_halt_required": latest_reconciliation.get("halt_required"),
        "latest_baseline_kind": latest_baseline.get("baseline_kind"),
        "latest_baseline_safe_for_automatic_continuation": latest_baseline.get(
            "safe_for_automatic_continuation",
        ),
        "latest_baseline_requires_operator_review": latest_baseline.get("requires_operator_review"),
        "resolve_stuck_submission_for_order": operator_action_counts.get(
            "resolve_stuck_submission_for_order",
        ),
        "required_operator_confirmation": claimed.get("required_operator_confirmation"),
        "claimed_submit_status": claimed.get("status"),
        "latest_operator_action_for_order": claimed.get("latest_operator_action_for_order"),
    }


def _runtime_health_snapshot(runtime_truth: dict[str, Any]) -> dict[str, Any]:
    git = as_dict(runtime_truth.get("git"))
    runtime = as_dict(runtime_truth.get("runtime"))
    live_facts = as_dict(runtime.get("live_runtime_facts"))
    return {
        "runtime_truth_ok": runtime_truth.get("ok") is True,
        "blocking_findings": runtime_truth.get("blocking_findings") or [],
        "windows_dirty": as_dict(git.get("windows")).get("dirty"),
        "origin_divergence": as_dict(as_dict(git.get("windows")).get("origin_divergence")),
        "deployed_matches_windows": git.get("deployed_matches_windows"),
        "gateway_health_ok": live_facts.get("gateway_health_ok"),
        "required_app_containers_healthy": live_facts.get("required_app_containers_healthy"),
    }


def validate_confirmation_packet(
    packet: dict[str, Any],
    runtime_truth: dict[str, Any],
    *,
    operator_confirmation: str | None = None,
) -> dict[str, Any]:
    failures: list[str] = []
    warnings: list[str] = []
    evidence = as_dict(packet.get("evidence"))
    current = _runtime_claimed_submit_snapshot(runtime_truth)
    health = _runtime_health_snapshot(runtime_truth)

    if packet.get("packet_type") != "operator_confirmation_packet":
        failures.append("packet_type_mismatch")
    if not evidence:
        failures.append("packet_evidence_missing")

    stored_hash = str(packet.get("evidence_sha256") or "")
    computed_hash = evidence_sha256(evidence) if evidence else ""
    if not stored_hash or computed_hash != stored_hash:
        failures.append("packet_evidence_hash_mismatch")

    exact_confirmation = str(packet.get("exact_confirmation_required") or "")
    evidence_confirmation = str(evidence.get("required_operator_confirmation") or "")
    current_confirmation = str(current.get("required_operator_confirmation") or "")
    if not exact_confirmation:
        failures.append("packet_exact_confirmation_missing")
    if exact_confirmation and evidence_confirmation != exact_confirmation:
        failures.append("packet_confirmation_does_not_match_evidence")
    if exact_confirmation and current_confirmation != exact_confirmation:
        failures.append("runtime_confirmation_does_not_match_packet")

    critical_fields = (
        "client_order_id",
        "command_id",
        "decision_id",
        "intent_id",
        "execution_order_state",
        "command_state",
        "venue_order_id",
        "exchange_order_id",
        "execution_fill_count",
        "fill_event_count",
        "latest_baseline_kind",
        "latest_baseline_safe_for_automatic_continuation",
        "latest_baseline_requires_operator_review",
        "resolve_stuck_submission_for_order",
    )
    for field in critical_fields:
        if evidence.get(field) != current.get(field):
            failures.append(f"runtime_evidence_mismatch:{field}")

    if current.get("claimed_submit_status") != "blocked_external_operator_confirmation_required":
        failures.append("runtime_claimed_submit_status_not_confirmation_blocked")
    if current.get("latest_reconciliation_severity") != "HARD_MISMATCH":
        failures.append("runtime_reconciliation_not_hard_mismatch")
    if current.get("latest_reconciliation_halt_required") is not True:
        failures.append("runtime_reconciliation_not_halt_required")
    if current.get("latest_operator_action_for_order") is not None:
        failures.append("runtime_operator_action_already_present_for_order")

    if evidence.get("latest_reconciliation_id") != as_dict(
        runtime_truth.get("claimed_submit_stuck_submission_truth", {}),
    ).get("latest_reconciliation", {}).get("reconciliation_id"):
        warnings.append("runtime_reconciliation_id_changed_since_packet")

    if health["runtime_truth_ok"] is not True:
        failures.append("runtime_truth_not_ok")
    if health["blocking_findings"]:
        failures.append("runtime_truth_has_blocking_findings")
    if health["windows_dirty"] is not False:
        failures.append("windows_worktree_dirty")
    divergence = health["origin_divergence"]
    if divergence.get("ahead") != 0 or divergence.get("behind") != 0:
        failures.append("origin_divergence_nonzero")
    if health["deployed_matches_windows"] is not True:
        failures.append("deployed_head_mismatch")
    if health["gateway_health_ok"] is not True:
        failures.append("gateway_health_not_ok")
    if health["required_app_containers_healthy"] is not True:
        failures.append("required_app_containers_not_healthy")

    confirmation_text = str(operator_confirmation or "").strip()
    confirmation_matched = bool(exact_confirmation and confirmation_text == exact_confirmation)
    confirmation_required = bool(exact_confirmation)
    if operator_confirmation is not None and not confirmation_matched:
        failures.append("operator_confirmation_mismatch")

    valid = not failures
    ready = valid and confirmation_required and confirmation_matched
    status = "ready_for_protected_recovery" if ready else "awaiting_external_operator_confirmation"
    if not valid:
        status = "blocked_packet_or_runtime_mismatch"

    return {
        "valid": valid,
        "ready_for_protected_recovery": ready,
        "status": status,
        "failures": failures,
        "warnings": warnings,
        "exact_confirmation_required": exact_confirmation,
        "operator_confirmation_matched": confirmation_matched,
        "evidence_sha256": stored_hash,
        "computed_evidence_sha256": computed_hash,
        "client_order_id": evidence.get("client_order_id"),
        "command_id": evidence.get("command_id"),
        "runtime_generated_at": runtime_truth.get("generated_at"),
    }


def build_operator_handoff(
    *,
    validation: dict[str, Any],
    packet: dict[str, Any],
    runtime_truth: dict[str, Any],
    packet_path: Path,
    runtime_truth_path: Path,
) -> dict[str, Any]:
    protected_path = as_dict(packet.get("protected_execution_path"))
    current_runtime_truth = as_dict(packet.get("current_runtime_truth"))
    if validation.get("ready_for_protected_recovery") is True:
        handoff_status = "ready_for_protected_recovery"
        next_action = "run_protected_resolve_stuck_submission"
    elif validation.get("valid") is True:
        handoff_status = "awaiting_external_operator_confirmation"
        next_action = "operator_verify_okx_absence_then_rerun_verifier_with_exact_confirmation"
    else:
        handoff_status = "blocked_packet_or_runtime_mismatch"
        next_action = "regenerate_runtime_truth_and_confirmation_packet_before_operator_action"

    return {
        "artifact_type": "claimed_submit_operator_handoff",
        "generated_from_runtime_truth_at": runtime_truth.get("generated_at"),
        "handoff_status": handoff_status,
        "next_action": next_action,
        "source_artifacts": {
            "packet": str(packet_path),
            "runtime_truth": str(runtime_truth_path),
        },
        "validation": {
            "valid": validation.get("valid"),
            "ready_for_protected_recovery": validation.get("ready_for_protected_recovery"),
            "status": validation.get("status"),
            "failures": validation.get("failures", []),
            "warnings": validation.get("warnings", []),
            "operator_confirmation_matched": validation.get("operator_confirmation_matched"),
        },
        "order": {
            "client_order_id": validation.get("client_order_id"),
            "command_id": validation.get("command_id"),
            "evidence_sha256": validation.get("evidence_sha256"),
            "exact_confirmation_required": validation.get("exact_confirmation_required"),
        },
        "operator_must_verify_on_okx": packet.get("operator_must_verify_on_okx", []),
        "protected_execution_path": {
            "api_method": protected_path.get("api_method"),
            "api_endpoint": protected_path.get("api_endpoint"),
            "required_role": protected_path.get("required_role"),
            "request_body": protected_path.get("request_body"),
            "terminal_state_if_success": protected_path.get("terminal_state_if_success"),
        },
        "forbidden_actions": packet.get("forbidden_actions", []),
        "acceptance_after_operator_confirmation": packet.get(
            "acceptance_after_operator_confirmation",
            [],
        ),
        "runtime_guardrails": {
            "runtime_truth_ok": current_runtime_truth.get("ok"),
            "deployed_matches_windows": current_runtime_truth.get("deployed_matches_windows"),
            "gateway_health_ok": current_runtime_truth.get("gateway_health_ok"),
            "required_app_containers_healthy": current_runtime_truth.get(
                "required_app_containers_healthy",
            ),
            "ai_timeout_active_blocker": current_runtime_truth.get("ai_timeout_active_blocker"),
            "shadow_benchmark": current_runtime_truth.get("shadow_benchmark"),
        },
        "rollback": packet.get("rollback"),
    }


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"json_root_not_object:{path}")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a no-secret operator confirmation packet against runtime truth.",
    )
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--runtime-truth", required=True, type=Path)
    parser.add_argument("--operator-confirmation")
    parser.add_argument(
        "--handoff-output",
        type=Path,
        help="Write a no-secret operator handoff JSON artifact for the current validation result.",
    )
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    packet = load_json(args.packet)
    runtime_truth = load_json(args.runtime_truth)
    result = validate_confirmation_packet(
        packet,
        runtime_truth,
        operator_confirmation=args.operator_confirmation,
    )
    if args.handoff_output is not None:
        handoff = build_operator_handoff(
            validation=result,
            packet=packet,
            runtime_truth=runtime_truth,
            packet_path=args.packet,
            runtime_truth_path=args.runtime_truth,
        )
        args.handoff_output.parent.mkdir(parents=True, exist_ok=True)
        args.handoff_output.write_text(
            json.dumps(handoff, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
