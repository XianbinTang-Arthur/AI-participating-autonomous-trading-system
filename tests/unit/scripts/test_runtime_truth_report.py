from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "runtime_truth_report.py"


def load_module():
    spec = importlib.util.spec_from_file_location("runtime_truth_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_redact_secret_text_masks_urls_and_tokens() -> None:
    mod = load_module()
    raw = (
        "postgresql+psycopg://user:pass@host:5432/db "
        "redis://:secret@redis:6379/0 api_key=abc token:xyz password=hunter2"
    )

    redacted = mod.redact_secret_text(raw)

    assert "user:pass" not in redacted
    assert "secret@redis" not in redacted
    assert "abc" not in redacted
    assert "xyz" not in redacted
    assert "hunter2" not in redacted
    assert "<redacted" in redacted


def test_parse_git_divergence_and_status_header() -> None:
    mod = load_module()

    assert mod.parse_left_right_count("2\t3") == {"ahead": 2, "behind": 3}
    assert mod.parse_left_right_count("bad") == {"ahead": None, "behind": None}
    assert mod.parse_git_status_header("## main...origin/main [ahead 4, behind 1]") == {
        "branch": "main",
        "tracking": "origin/main",
        "ahead": 4,
        "behind": 1,
        "raw": "## main...origin/main [ahead 4, behind 1]",
    }


def test_container_health_requires_all_app_containers_healthy() -> None:
    mod = load_module()
    statuses = mod.parse_docker_ps(
        "\n".join(
            [
                "aats-gateway\tUp 1 minute (healthy)",
                "aats-market\tUp 1 minute (healthy)",
                "aats-decision\tUp 1 minute (healthy)",
                "aats-execution\tUp 1 minute (healthy)",
                "aats-rdp-daemon\tUp 1 minute (healthy)",
            ],
        ),
    )

    summary = mod.summarize_container_health(statuses)

    assert summary["all_required_app_containers_healthy"] is True
    assert summary["required"]["aats-gateway"]["healthy"] is True


def test_bash_cd_target_preserves_home_expansion() -> None:
    mod = load_module()

    assert mod.bash_cd_target("~/aats") == "$HOME/aats"
    assert mod.bash_cd_target("plain/path") == "plain/path"


def test_dashboard_auth_required_is_not_confused_with_runtime_mode() -> None:
    mod = load_module()
    payload = {
        "panels": {
            "mode": {"data": None, "error": "operator_auth_required"},
            "aiRuntime": {"data": None, "error": "operator_auth_required"},
        },
        "auth": {
            "access_state": "auth_required",
            "primary_error": "operator_auth_required",
            "blocked_panel_keys": ["mode", "aiRuntime"],
        },
    }

    summary = mod.summarize_dashboard_bundle(payload)

    assert summary["status"] == "auth_required"
    assert summary["effective_operating_mode"] == {
        "status": "unknown_auth_required",
        "value": None,
    }


def test_db_probe_command_does_not_embed_database_url() -> None:
    mod = load_module()

    command = " ".join(mod.db_probe_command("Ubuntu", "aats-gateway"))

    assert "postgresql://" not in command
    assert "postgresql+psycopg://" not in command
    assert "DATABASE_URL" not in command
    assert "AATS_DATABASE_URL" not in command


def test_parse_db_probe_returns_only_json_payload() -> None:
    mod = load_module()
    payload = {
        "ok": True,
        "portfolio_allocation_decisions": 32633,
        "execution_fills": 25,
        "latest_decision": {
            "decision_id": "decision_1",
            "symbol": "BTC-USDT-SWAP",
            "route_action": "advisory_only",
            "primary_family": "independent",
            "portfolio_requested_notional": "0",
            "portfolio_approved_notional": "0",
            "portfolio_budget_cut_notional": "0",
            "payload": {
                "reason_codes": [
                    "allocator_primary_family_independent",
                    "independent_long_book_signal_below_entry_threshold",
                ],
                "operator_summary": "no executable allocation",
                "execution_legs": [],
            },
        },
        "latest_decision_audit": {
            "execution_plan_ref": None,
            "execution_plan_refs": [],
            "order_intent_refs": [],
            "order_state_refs": [],
            "fill_event_refs": [],
            "strategy_sleeve_intent_refs": ["intent-1", "intent-2"],
            "risk_decision_ref": "risk-1",
            "decision_outcome_ref": "outcome-1",
        },
        "latest_decision_counts": {
            "execution_orders": 0,
            "order_states": 0,
            "execution_fills": 0,
            "legacy_fill_events": 0,
        },
    }

    parsed = mod.parse_db_probe(json.dumps(payload), "")

    latest = parsed["latest_decision"]
    assert "payload" not in latest
    assert latest["execution_chain"]["db_order_count"] == 0
    assert latest["execution_chain"]["strategy_sleeve_intent_ref_count"] == 2
    attribution = latest["no_trade_attribution"]
    assert attribution["classification"] == "no_order_fill_expected_for_latest_decision"
    assert attribution["primary_blocker"] == "strategy_signal_below_entry_threshold"
    assert attribution["is_current_no_trade"] is True
    assert attribution["final_blockers"] == [
        "strategy_signal_below_entry_threshold",
        "allocator_zero_notional_advisory",
        "no_execution_plan_emitted",
    ]
    assert attribution["contributing_factors"] == []
    assert attribution["reason_codes"] == [
        "allocator_primary_family_independent",
        "independent_long_book_signal_below_entry_threshold",
    ]
    assert attribution["operator_summary"] == "no executable allocation"
    assert attribution["execution_legs_count"] == 0
    assert attribution["sleeve_intent_summary"] == []


def test_latest_decision_no_trade_attribution_detects_inactive_hold_only() -> None:
    mod = load_module()
    latest = {
        "allocation_id": "alloc-1",
        "decision_id": "decision-1",
        "symbol": "BTC-USDT-SWAP",
        "route_action": "advisory_only",
        "primary_family": "independent",
        "portfolio_requested_notional": "0",
        "portfolio_approved_notional": "0",
        "portfolio_budget_cut_notional": "0",
        "payload": {
            "reason_codes": [
                "independent_family_candidate_inactive",
                "legacy_configured_strategy_family_independent_hold_only",
            ],
            "strategy_sleeve_intents": [
                {
                    "family": "independent",
                    "strategy_sleeve_id": "sleeve-1",
                    "route_action": "hold_current",
                    "delta_notional": "0",
                    "reason_codes": ["independent_family_candidate_inactive"],
                }
            ],
        },
    }

    summarized = mod.summarize_latest_decision(
        latest,
        {
            "execution_plan_ref": None,
            "execution_plan_refs": [],
            "order_intent_refs": [],
            "order_state_refs": [],
            "fill_event_refs": [],
        },
        {"execution_orders": 0, "order_states": 0, "execution_fills": 0, "legacy_fill_events": 0},
    )

    assert summarized is not None
    attribution = summarized["no_trade_attribution"]
    assert attribution["is_current_no_trade"] is True
    assert attribution["primary_blocker"] == "strategy_candidate_inactive"
    assert attribution["final_blockers"] == [
        "strategy_candidate_inactive",
        "strategy_hold_only",
        "allocator_zero_notional_advisory",
        "no_execution_plan_emitted",
    ]
    assert attribution["sleeve_intent_summary"][0]["family"] == "independent"
    assert attribution["sleeve_intent_summary"][0]["reason_codes"] == ["independent_family_candidate_inactive"]


def test_no_trade_attribution_separates_soft_contraction_from_final_blockers() -> None:
    mod = load_module()
    latest = {
        "allocation_id": "alloc-1",
        "decision_id": "decision-1",
        "symbol": "BTC-USDT-SWAP",
        "route_action": "advisory_only",
        "primary_family": "independent",
        "portfolio_requested_notional": "0",
        "portfolio_approved_notional": "0",
        "portfolio_budget_cut_notional": "0",
        "payload": {
            "reason_codes": [
                "approved_for_non_protective_execution",
                "reconciliation_contraction_active",
                "allocator_budget_assignment_active",
                "candidate_execution_incompatible",
                "composed_as_advisory_only",
                "no_budget_contraction",
            ],
            "strategy_sleeve_intents": [
                {
                    "family": "directional",
                    "strategy_sleeve_id": "sleeve-directional",
                    "route_action": "override_target",
                    "approved_for_execution": True,
                    "effective_scale": "0.5",
                    "target_notional": "780",
                    "reason_codes": ["directional_strategy_target", "reconciliation_contraction_active"],
                },
                {
                    "family": "independent",
                    "strategy_sleeve_id": "sleeve-independent",
                    "route_action": "advisory_only",
                    "approved_for_execution": False,
                    "execution_compatible": False,
                    "execution_prerequisites_supported": False,
                    "execution_behavior": "advisory_only",
                    "execution_control_mode": "permission_denied",
                    "execution_mode": "independent_books",
                    "permission_mode": "unsupported",
                    "automatic_enabled": False,
                    "selectable": False,
                    "state": "inactive",
                    "state_phase": "inactive",
                    "family_action": "hold_family",
                    "target_notional": "0",
                    "control_reason_codes": [
                        "candidate_execution_incompatible",
                        "reconciliation_contraction_active",
                        "composed_as_advisory_only",
                    ],
                    "control_summary": "current sleeve candidate is not execution compatible",
                    "control_trace": {
                        "permission": {
                            "approved_for_execution": False,
                            "candidate_enabled": True,
                            "candidate_execution_compatible": False,
                            "execution_prerequisites_supported": False,
                            "configured_auto_execution_enabled": True,
                            "permission_mode": "unsupported",
                            "runtime_supported": True,
                            "state_runtime_supported": True,
                            "reason_codes": ["candidate_execution_incompatible"],
                            "human_summary": "candidate is not execution compatible",
                        },
                        "composition": {
                            "approved_for_execution": False,
                            "route_action": "advisory_only",
                            "execution_behavior": "advisory_only",
                            "execution_control_mode": "permission_denied",
                            "requested_delta_position_qty": "0",
                            "composed_delta_position_qty": "0",
                            "reason_codes": ["composed_as_advisory_only"],
                        },
                        "budget": {
                            "base_scale": "1",
                            "effective_scale": "0.5",
                            "requested_delta_position_qty": "0",
                            "scaled_delta_position_qty": "0",
                            "budget_zero_suppressed": False,
                            "reason_codes": ["reconciliation_contraction_active"],
                        },
                    },
                    "metrics": {
                        "book_runtime_states": [
                            {
                                "leg": "long",
                                "state": "inactive",
                                "book_action": "inactive",
                                "book_state": "flat",
                                "score": 0.041,
                                "score_adjusted": 0.041,
                                "expected_signal_edge_bps": 0.5,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": -7.5,
                                "health_state": "ok",
                                "execution_health_state": "ok",
                                "transition_valid": True,
                                "reason_codes": ["independent_long_book_signal_below_entry_threshold"],
                                "threshold_snapshot": {"effective_entry_threshold": 0.25},
                            },
                            {
                                "leg": "short",
                                "state": "inactive",
                                "book_action": "inactive",
                                "book_state": "flat",
                                "score": 0.0,
                                "score_adjusted": 0.0,
                                "expected_signal_edge_bps": 0.0,
                                "expected_cost_bps": 6.0,
                                "expected_net_edge_bps": -8.0,
                                "health_state": "ok",
                                "execution_health_state": "ok",
                                "transition_valid": True,
                                "reason_codes": ["independent_short_book_signal_below_entry_threshold"],
                                "threshold_snapshot": {"effective_entry_threshold": 0.25},
                            },
                        ],
                    },
                    "reason_codes": [
                        "independent_long_book_signal_below_entry_threshold",
                        "independent_short_book_signal_below_entry_threshold",
                        "independent_family_candidate_inactive",
                    ],
                },
            ],
        },
    }

    summarized = mod.summarize_latest_decision(
        latest,
        {
            "execution_plan_ref": None,
            "execution_plan_refs": [],
            "order_intent_refs": [],
            "order_state_refs": [],
            "fill_event_refs": [],
        },
        {"execution_orders": 0, "order_states": 0, "execution_fills": 0, "legacy_fill_events": 0},
    )

    assert summarized is not None
    attribution = summarized["no_trade_attribution"]
    assert attribution["primary_blocker"] == "candidate_execution_incompatible"
    assert "reconciliation_contraction_active" in attribution["contributing_factors"]
    assert "reconciliation_contraction_active" != attribution["primary_blocker"]
    assert attribution["final_blockers"] == [
        "candidate_execution_incompatible",
        "strategy_candidate_inactive",
        "strategy_signal_below_entry_threshold",
        "composed_as_advisory_only",
        "allocator_zero_notional_advisory",
        "no_execution_plan_emitted",
    ]
    independent = attribution["sleeve_intent_summary"][1]
    assert independent["family"] == "independent"
    assert independent["approved_for_execution"] is False
    assert independent["route_action"] == "advisory_only"
    assert "independent_family_candidate_inactive" in independent["reason_codes"]
    drilldown = attribution["candidate_execution_drilldown"][0]
    assert drilldown["family"] == "independent"
    assert drilldown["execution"]["execution_compatible"] is False
    assert drilldown["execution"]["permission_mode"] == "unsupported"
    assert drilldown["permission"]["candidate_execution_compatible"] is False
    assert drilldown["permission"]["reason_codes"] == ["candidate_execution_incompatible"]
    assert drilldown["permission_root_cause"] == {
        "primary": "candidate_execution_incompatible",
        "classification": "permission_denied_by_candidate_execution_compatibility",
        "blocking_evidence": [
            "permission_mode=unsupported",
            "approved_for_execution=false",
            "candidate_execution_compatible=false",
            "execution_prerequisites_supported=false",
            "execution_compatible=false",
            "reason_code=candidate_execution_incompatible",
        ],
        "upstream_reason_codes": [
            "independent_long_book_signal_below_entry_threshold",
            "independent_short_book_signal_below_entry_threshold",
            "independent_family_candidate_inactive",
        ],
        "positive_context": [
            "candidate_enabled=true",
            "configured_auto_execution_enabled=true",
            "runtime_supported=true",
            "state_runtime_supported=true",
        ],
        "composition_effect": [
            "route_action=advisory_only",
            "execution_behavior=advisory_only",
            "execution_control_mode=permission_denied",
            "reason_code=composed_as_advisory_only",
        ],
        "summary": (
            "候选已启用且运行时支持，但执行兼容性或执行前置条件未满足；"
            "因此权限模式为 unsupported，组合层只能输出 advisory_only。"
        ),
    }
    assert drilldown["composition"]["reason_codes"] == ["composed_as_advisory_only"]
    assert drilldown["budget"]["effective_scale"] == "0.5"
    assert drilldown["budget"]["reason_codes"] == ["reconciliation_contraction_active"]
    assert drilldown["book_runtime_states"][0]["leg"] == "long"
    assert drilldown["book_runtime_states"][0]["effective_entry_threshold"] == "0.25"
    assert drilldown["book_runtime_states"][0]["activation_gap"] == {
        "score_gap_to_entry_threshold": "0.209",
        "score_minus_entry_threshold": "-0.209",
        "score_meets_entry_threshold": False,
        "signal_edge_minus_cost_bps": "-5.5",
        "signal_edge_gap_to_cost_bps": "5.5",
        "signal_edge_covers_cost": False,
    }
    assert drilldown["book_runtime_states"][0]["reason_codes"] == [
        "independent_long_book_signal_below_entry_threshold"
    ]


def test_permission_root_cause_missing_evidence_degrades_safely() -> None:
    mod = load_module()

    assert mod.summarize_permission_root_cause(
        permission={},
        execution={},
        composition={},
        candidate_reason_codes=[],
    ) == {
        "primary": None,
        "classification": "insufficient_evidence",
        "blocking_evidence": [],
        "upstream_reason_codes": [],
        "positive_context": [],
        "composition_effect": [],
        "summary": None,
    }


def test_blocking_findings_separate_report_generation_from_runtime_state() -> None:
    mod = load_module()
    report = {
        "git": {
            "windows": {
                "dirty": True,
                "origin_divergence": {"ahead": 0, "behind": 0},
            },
            "deployed_matches_windows": True,
        },
        "deployment_health": {
            "gateway_health": {"ok": True},
            "containers": {"all_required_app_containers_healthy": True},
        },
        "database_truth": {"ok": True},
    }

    assert mod.collect_blocking_findings(report) == ["windows_worktree_dirty"]


def test_artifact_last_known_is_non_authoritative_when_live_fact_differs(tmp_path: Path) -> None:
    mod = load_module()
    artifact_dir = tmp_path / "artifacts" / "automation"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "task_registry.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-26T06:30:00Z",
                "latest_runtime_facts": {
                    "latest_decision_id": "decision_old",
                    "portfolio_allocation_decisions": 10,
                    "execution_fills": 2,
                    "shadow_benchmark": "none_verified",
                    "ai_timeout_active_blocker": False,
                    "runtime_truth_generated_at": "2026-04-26T06:30:00Z",
                },
            },
        ),
        encoding="utf-8",
    )
    projection = mod.load_artifact_runtime_projection(tmp_path, "2026-04-26T07:01:00Z")
    live_facts = {
        "latest_decision_id": "decision_new",
        "portfolio_allocation_decisions": 11,
        "execution_fills": 2,
        "shadow_benchmark": "none_verified",
        "ai_timeout_active_blocker": False,
    }

    status = mod.summarize_artifact_runtime_status(
        artifact_projection=projection,
        live_facts=live_facts,
        report_generated_at="2026-04-26T07:01:00Z",
    )

    assert status["status"] == "stale_mismatch"
    assert status["may_override_live"] is False
    assert {item["fact"] for item in status["mismatched_facts"]} == {
        "latest_decision_id",
        "portfolio_allocation_decisions",
    }


def test_runtime_fact_authority_points_to_live_runtime_facts() -> None:
    mod = load_module()
    report = {
        "scope": {"shadow_benchmark": "none_verified"},
        "runtime": {
            "ai_timeout_active_blocker": False,
            "dashboard_bundle": {
                "status": "auth_required",
                "effective_operating_mode": {
                    "status": "unknown_auth_required",
                    "value": None,
                },
                "profile_auto_control_effective": {
                    "status": "unknown_auth_required",
                    "value": None,
                },
            },
            "artifact_last_known": {
                "latest_decision_id": "decision_old",
                "portfolio_allocation_decisions": 10,
            },
        },
        "database_truth": {
            "ok": True,
            "portfolio_allocation_decisions": 11,
            "execution_fills": 2,
            "latest_decision": {
                "decision_id": "decision_new",
                "route_action": "advisory_only",
                "symbol": "BTC-USDT-SWAP",
                "primary_family": "independent",
            },
        },
        "git": {
            "deployed_matches_windows": True,
            "windows": {
                "dirty": False,
                "origin_divergence": {"ahead": 0, "behind": 0},
            },
        },
        "deployment_health": {
            "gateway_health": {"ok": True},
            "containers": {"all_required_app_containers_healthy": True},
        },
    }

    live_facts = mod.project_live_runtime_facts(report)
    authority = mod.summarize_runtime_fact_authority(
        live_facts=live_facts,
        artifact_status={"status": "stale_mismatch"},
    )

    assert live_facts["latest_decision_id"] == "decision_new"
    assert live_facts["portfolio_allocation_decisions"] == 11
    assert authority["authoritative_source"] == "runtime.live_runtime_facts"
    assert authority["artifact_may_override_live"] is False
