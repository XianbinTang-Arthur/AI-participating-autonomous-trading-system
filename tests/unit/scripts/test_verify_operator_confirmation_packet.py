from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "verify_operator_confirmation_packet.py"


def load_module():
    spec = importlib.util.spec_from_file_location("verify_operator_confirmation_packet", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_truth() -> dict:
    return {
        "generated_at": "2026-04-28T09:42:33Z",
        "ok": True,
        "blocking_findings": [],
        "git": {
            "deployed_matches_windows": True,
            "windows": {
                "dirty": False,
                "origin_divergence": {"ahead": 0, "behind": 0},
            },
        },
        "runtime": {
            "live_runtime_facts": {
                "gateway_health_ok": True,
                "required_app_containers_healthy": True,
            },
        },
        "claimed_submit_stuck_submission_truth": {
            "status": "blocked_external_operator_confirmation_required",
            "required_operator_confirmation": "resolve_claimed_submit_as_failed:cl_stuck",
            "latest_operator_action_for_order": None,
            "latest_order": {
                "client_order_id": "cl_stuck",
                "command_id": "cmd_stuck",
                "decision_id": "decision_stuck",
                "intent_id": "intent_stuck",
                "execution_order_state": "SUBMITTING",
                "command_state": "CLAIMED",
                "venue_order_id": None,
                "exchange_order_id": None,
                "execution_fill_count": 0,
                "fill_event_count": 0,
            },
            "latest_reconciliation": {
                "reconciliation_id": "recon_current",
                "severity": "HARD_MISMATCH",
                "halt_required": True,
            },
            "latest_baseline": {
                "baseline_kind": "operator_rebaseline",
                "safe_for_automatic_continuation": True,
                "requires_operator_review": False,
            },
            "operator_action_counts": {
                "resolve_stuck_submission_for_order": 0,
            },
        },
    }


def _packet(mod, *, evidence_updates: dict | None = None, hash_override: str | None = None) -> dict:
    evidence = {
        "generated_at": "2026-04-28T09:16:40Z",
        "client_order_id": "cl_stuck",
        "command_id": "cmd_stuck",
        "decision_id": "decision_stuck",
        "intent_id": "intent_stuck",
        "execution_order_state": "SUBMITTING",
        "command_state": "CLAIMED",
        "venue_order_id": None,
        "exchange_order_id": None,
        "execution_fill_count": 0,
        "fill_event_count": 0,
        "latest_reconciliation_id": "recon_old",
        "latest_reconciliation_severity": "HARD_MISMATCH",
        "latest_reconciliation_halt_required": True,
        "latest_baseline_kind": "operator_rebaseline",
        "latest_baseline_safe_for_automatic_continuation": True,
        "latest_baseline_requires_operator_review": False,
        "resolve_stuck_submission_for_order": 0,
        "required_operator_confirmation": "resolve_claimed_submit_as_failed:cl_stuck",
    }
    evidence.update(evidence_updates or {})
    return {
        "packet_type": "operator_confirmation_packet",
        "status": "awaiting_external_operator_confirmation",
        "evidence": evidence,
        "evidence_sha256": hash_override or mod.evidence_sha256(evidence),
        "exact_confirmation_required": "resolve_claimed_submit_as_failed:cl_stuck",
    }


def test_valid_packet_waits_for_external_confirmation() -> None:
    mod = load_module()

    result = mod.validate_confirmation_packet(_packet(mod), _runtime_truth())

    assert result["valid"] is True
    assert result["ready_for_protected_recovery"] is False
    assert result["status"] == "awaiting_external_operator_confirmation"
    assert result["warnings"] == ["runtime_reconciliation_id_changed_since_packet"]


def test_valid_packet_with_exact_confirmation_is_ready() -> None:
    mod = load_module()

    result = mod.validate_confirmation_packet(
        _packet(mod),
        _runtime_truth(),
        operator_confirmation="resolve_claimed_submit_as_failed:cl_stuck",
    )

    assert result["valid"] is True
    assert result["ready_for_protected_recovery"] is True
    assert result["status"] == "ready_for_protected_recovery"


def test_packet_blocks_when_runtime_order_has_fill() -> None:
    mod = load_module()
    runtime = _runtime_truth()
    runtime["claimed_submit_stuck_submission_truth"]["latest_order"]["execution_fill_count"] = 1

    result = mod.validate_confirmation_packet(_packet(mod), runtime)

    assert result["valid"] is False
    assert "runtime_evidence_mismatch:execution_fill_count" in result["failures"]


def test_packet_blocks_when_hash_is_tampered() -> None:
    mod = load_module()

    result = mod.validate_confirmation_packet(
        _packet(mod, hash_override="bad_hash"),
        _runtime_truth(),
    )

    assert result["valid"] is False
    assert "packet_evidence_hash_mismatch" in result["failures"]


def test_packet_blocks_when_operator_confirmation_mismatches() -> None:
    mod = load_module()

    result = mod.validate_confirmation_packet(
        _packet(mod),
        _runtime_truth(),
        operator_confirmation="wrong",
    )

    assert result["valid"] is False
    assert "operator_confirmation_mismatch" in result["failures"]


def test_handoff_waits_for_external_confirmation() -> None:
    mod = load_module()
    packet = _packet(mod)
    runtime_truth = _runtime_truth()
    result = mod.validate_confirmation_packet(packet, runtime_truth)

    handoff = mod.build_operator_handoff(
        validation=result,
        packet={
            **packet,
            "operator_must_verify_on_okx": ["No open order exists on OKX."],
            "protected_execution_path": {
                "api_method": "POST",
                "api_endpoint": "/orders/{client_order_id}/resolve-stuck-submission",
                "required_role": "admin",
                "request_body": {
                    "operator_confirmation": "resolve_claimed_submit_as_failed:cl_stuck",
                },
                "terminal_state_if_success": "FAILED",
            },
            "forbidden_actions": ["Do not hand-edit order state."],
            "acceptance_after_operator_confirmation": ["Post-action runtime truth generated."],
        },
        runtime_truth=runtime_truth,
        packet_path=Path("packet.json"),
        runtime_truth_path=Path("runtime.json"),
    )

    assert handoff["handoff_status"] == "awaiting_external_operator_confirmation"
    assert handoff["next_action"] == "operator_verify_okx_absence_then_rerun_verifier_with_exact_confirmation"
    assert handoff["order"]["exact_confirmation_required"] == "resolve_claimed_submit_as_failed:cl_stuck"
    assert handoff["protected_execution_path"]["required_role"] == "admin"
    assert handoff["forbidden_actions"] == ["Do not hand-edit order state."]


def test_handoff_ready_after_exact_confirmation() -> None:
    mod = load_module()
    packet = _packet(mod)
    runtime_truth = _runtime_truth()
    result = mod.validate_confirmation_packet(
        packet,
        runtime_truth,
        operator_confirmation="resolve_claimed_submit_as_failed:cl_stuck",
    )

    handoff = mod.build_operator_handoff(
        validation=result,
        packet=packet,
        runtime_truth=runtime_truth,
        packet_path=Path("packet.json"),
        runtime_truth_path=Path("runtime.json"),
    )

    assert handoff["handoff_status"] == "ready_for_protected_recovery"
    assert handoff["next_action"] == "run_protected_resolve_stuck_submission"
