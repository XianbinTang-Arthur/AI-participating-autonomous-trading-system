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


def test_operator_read_auth_context_never_exposes_env_secret(monkeypatch) -> None:
    mod = load_module()
    monkeypatch.setenv("AATS_TEST_READ_KEY", "super-secret-read-key")

    context = mod.operator_read_auth_context("AATS_TEST_READ_KEY")
    report = mod.operator_read_auth_report(context)

    assert context["headers"] == {"X-AATS-API-Key": "super-secret-read-key"}
    assert report == {
        "status": "credential_present",
        "method": "api_key_env",
        "env_var": "AATS_TEST_READ_KEY",
        "credential_present": True,
        "header_injected": True,
        "raw_credential_exposed": False,
    }
    assert "super-secret-read-key" not in json.dumps(report)


def test_ai_runtime_endpoint_probe_uses_read_header_without_exposing_secret(monkeypatch) -> None:
    mod = load_module()

    def fake_fetch_url_text(
        url: str,
        *,
        timeout: int,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        assert url == "https://operator.local/ai/runtime"
        assert timeout == 10
        assert headers == {"X-AATS-API-Key": "super-secret-read-key"}
        return {
            "ok": True,
            "status": 200,
            "body": json.dumps(
                {
                    "configured_operating_mode": "ai_decision_maker",
                    "effective_operating_mode": "ai_decision_maker",
                    "manual_override_active": False,
                    "provider": "deepseek",
                    "configured": True,
                    "provider_ready": True,
                    "provider_degraded": False,
                    "shadow_mode_enabled": True,
                    "strategy_profile_auto_control_effective": True,
                }
            ),
        }

    monkeypatch.setattr(mod, "fetch_url_text", fake_fetch_url_text)
    auth_context = {
        "status": "credential_present",
        "method": "api_key_env",
        "env_var": "AATS_TEST_READ_KEY",
        "credential_present": True,
        "headers": {"X-AATS-API-Key": "super-secret-read-key"},
        "raw_credential_exposed": False,
    }

    result = mod.ai_runtime_endpoint_probe(
        "https://operator.local",
        headers=auth_context["headers"],
        auth_context=auth_context,
    )

    assert result["status"] == "verified"
    assert result["auth_attempt"]["credential_present"] is True
    assert result["auth_attempt"]["header_injected"] is True
    assert result["runtime"]["effective_operating_mode"] == {
        "status": "verified",
        "value": "ai_decision_maker",
    }
    assert "super-secret-read-key" not in json.dumps(result)


def test_ai_runtime_auth_failed_is_not_timeout_blocker() -> None:
    mod = load_module()
    truth = mod.summarize_ai_runtime_effective_mode_truth(
        dashboard_bundle={"status": "auth_required", "primary_error": "operator_auth_required"},
        ai_runtime_endpoint={
            "status": "auth_failed",
            "http_status": 401,
            "error": "operator_auth_required",
            "auth_attempt": {
                "status": "credential_present",
                "method": "api_key_env",
                "env_var": "AATS_TEST_READ_KEY",
                "credential_present": True,
                "header_injected": True,
                "raw_credential_exposed": False,
            },
            "runtime": {},
            "raw_payload_exposed": False,
        },
    )

    assert truth["status"] == "auth_failed_effective_ai_runtime_truth"
    assert truth["smallest_missing_field"] == "valid_operator_read_credential"
    assert truth["operator_read_auth"]["credential_present"] is True
    assert truth["provider"]["path_active"] is None
    assert truth["ai_timeout"]["active_blocker"] is False
    assert (
        truth["ai_timeout"]["classification"]
        == "not_active_blocker_auth_failed_provider_path_not_verified"
    )


def test_ai_runtime_effective_mode_truth_marks_auth_gate_not_timeout_blocker() -> None:
    mod = load_module()
    dashboard = {
        "status": "auth_required",
        "access_state": "auth_required",
        "primary_error": "operator_auth_required",
        "effective_operating_mode": {"status": "unknown_auth_required", "value": None},
        "profile_auto_control_effective": {"status": "unknown_auth_required", "value": None},
    }
    endpoint = {
        "status": "auth_required",
        "http_status": 401,
        "error": "operator_auth_required",
        "runtime": {},
        "raw_payload_exposed": False,
    }

    truth = mod.summarize_ai_runtime_effective_mode_truth(
        dashboard_bundle=dashboard,
        ai_runtime_endpoint=endpoint,
    )
    live_facts = mod.project_live_runtime_facts(
        {
            "scope": {"shadow_benchmark": "none_verified"},
            "runtime": {
                "dashboard_bundle": dashboard,
                "ai_runtime_endpoint": endpoint,
                "ai_timeout_active_blocker": truth["ai_timeout"]["active_blocker"],
            },
            "ai_runtime_effective_mode_truth": truth,
            "database_truth": {"ok": True, "latest_decision": {}},
            "git": {},
            "deployment_health": {},
        }
    )

    assert truth["status"] == "auth_gated_effective_ai_runtime_truth"
    assert truth["smallest_missing_field"] == "operator_authenticated_runtime_read_access"
    assert truth["configured_target"]["status"] == "unknown_auth_required"
    assert truth["effective_runtime_mode"]["status"] == "unknown_auth_required"
    assert truth["provider"]["path_evidence_present"] is False
    assert truth["provider"]["path_active"] is None
    assert truth["ai_timeout"]["active_blocker"] is False
    assert (
        truth["ai_timeout"]["classification"]
        == "not_active_blocker_auth_gated_provider_path_not_verified"
    )
    assert live_facts["ai_runtime_effective_truth_status"] == "auth_gated_effective_ai_runtime_truth"
    assert live_facts["ai_runtime_effective_auth_required"] is True
    assert live_facts["ai_runtime_endpoint_http_status"] == 401
    assert live_facts["ai_runtime_timeout_blocker_classification"] == (
        "not_active_blocker_auth_gated_provider_path_not_verified"
    )


def test_ai_runtime_effective_mode_truth_projects_verified_provider_path() -> None:
    mod = load_module()
    endpoint = {
        "status": "verified",
        "http_status": 200,
        "runtime": mod.summarize_ai_runtime_endpoint_payload(
            {
                "configured_operating_mode": "ai_decision_maker",
                "effective_operating_mode": "ai_decision_maker",
                "manual_override_active": False,
                "manual_override_mode": None,
                "provider": "deepseek",
                "configured": True,
                "provider_ready": True,
                "provider_degraded": False,
                "provider_state": "healthy",
                "recent_timeout_count": 0,
                "shadow_mode_enabled": True,
                "strategy_profile_auto_control_effective": True,
                "ai_runtime_source": "remote_decision",
                "queried_from_process_role": "gateway",
                "ai_service_loaded": True,
            }
        ),
    }
    dashboard = {
        "status": "verified",
        "effective_operating_mode": {"status": "verified", "value": "ai_decision_maker"},
        "profile_auto_control_effective": {"status": "verified", "value": True},
    }

    truth = mod.summarize_ai_runtime_effective_mode_truth(
        dashboard_bundle=dashboard,
        ai_runtime_endpoint=endpoint,
    )
    live_facts = mod.project_live_runtime_facts(
        {
            "scope": {"shadow_benchmark": "none_verified"},
            "runtime": {
                "dashboard_bundle": dashboard,
                "ai_runtime_endpoint": endpoint,
                "ai_timeout_active_blocker": truth["ai_timeout"]["active_blocker"],
            },
            "ai_runtime_effective_mode_truth": truth,
            "database_truth": {"ok": True, "latest_decision": {}},
            "git": {},
            "deployment_health": {},
        }
    )

    assert truth["status"] == "verified_effective_ai_runtime_truth"
    assert truth["provider"]["path_evidence_present"] is True
    assert truth["provider"]["path_active"] is True
    assert truth["ai_timeout"]["classification"] == "not_active_blocker_no_recent_timeout"
    assert truth["manual_override"]["status"] == "verified"
    assert truth["manual_override"]["active"] is False
    assert live_facts["ai_runtime_configured_operating_mode"] == "ai_decision_maker"
    assert live_facts["ai_runtime_effective_operating_mode"] == "ai_decision_maker"
    assert live_facts["ai_runtime_provider_configured"] is True
    assert live_facts["ai_runtime_provider_ready"] is True
    assert live_facts["ai_runtime_provider_path_active"] is True
    assert live_facts["ai_runtime_shadow_enabled"] is True
    assert live_facts["ai_runtime_profile_auto_control_effective"] is True


def test_static_truth_surface_checks_terminal_no_fill_ui_markers(monkeypatch) -> None:
    mod = load_module()

    bodies = {
        "/ui/modules/views/strategy-view.js": (
            "strategyPreOrderFeasibility preOrderFeasibilitySummary "
            "terminal_no_fill_explanation 无成交终局 这次为什么没有成交"
        ),
        "/ui/modules/views/overview-view.js": (
            "terminal_no_fill_explanation 无成交终局 终端无成交解释 "
            "claimedSubmitGate 恢复仍被 CLAIMED 提交阻断 已接受新基线不等于清除 CLAIMED 提交"
        ),
        "/ui/modules/no-trade-display.js": (
            "hasPreOrderFeasibility preOrderFeasibilitySummary 执行可行性 阻断维度"
        ),
    }

    def fake_fetch_url_text(url: str, timeout: int) -> dict[str, object]:
        assert timeout == 10
        for path, body in bodies.items():
            if url.endswith(path):
                return {"ok": True, "status": 200, "body": body, "error": None}
        return {"ok": False, "status": 404, "body": "", "error": "unexpected_url"}

    monkeypatch.setattr(mod, "fetch_url_text", fake_fetch_url_text)

    result = mod.static_truth_surface("https://operator.local")

    strategy_markers = result["/ui/modules/views/strategy-view.js"]["markers"]
    assert strategy_markers["terminal_no_fill_explanation"] is True
    assert strategy_markers["无成交终局"] is True
    assert strategy_markers["这次为什么没有成交"] is True
    overview_markers = result["/ui/modules/views/overview-view.js"]["markers"]
    assert overview_markers["terminal_no_fill_explanation"] is True
    assert overview_markers["无成交终局"] is True
    assert overview_markers["终端无成交解释"] is True
    assert overview_markers["claimedSubmitGate"] is True
    assert overview_markers["恢复仍被 CLAIMED 提交阻断"] is True
    assert overview_markers["已接受新基线不等于清除 CLAIMED 提交"] is True
    assert all(surface["ok"] for surface in result.values())


def test_db_probe_command_does_not_embed_database_url() -> None:
    mod = load_module()

    command = " ".join(mod.db_probe_command("Ubuntu", "aats-gateway"))

    assert "postgresql://" not in command
    assert "postgresql+psycopg://" not in command
    assert "DATABASE_URL" not in command
    assert "AATS_DATABASE_URL" not in command


def test_rdp_microstructure_probe_command_does_not_embed_database_url() -> None:
    mod = load_module()

    command = " ".join(mod.rdp_microstructure_probe_command("Ubuntu", "aats-gateway"))

    assert "postgresql://" not in command
    assert "postgresql+psycopg://" not in command
    assert "RDP_DATABASE_URL" not in command
    assert "AATS_ACTIVE_PARAMETER_DB_URL" not in command


def test_rdp_microstructure_payload_sequence_groups_by_unique_scope() -> None:
    mod = load_module()

    probe = mod.RDP_MICROSTRUCTURE_PROBE

    assert "group by collector_sequence_scope, ingest_run_id, channel" in probe
    assert "left(ingest_run_id, 8) as ingest_run_id_prefix" in probe
    assert "coalesce(channel, '') as channel" in probe


def test_gateway_health_probe_falls_back_to_wsl_localhost(monkeypatch) -> None:
    mod = load_module()

    monkeypatch.setattr(
        mod,
        "fetch_json_url",
        lambda url, timeout=10: {
            "ok": False,
            "status": None,
            "json": None,
            "error": "<urlopen error [SSL: WRONG_VERSION_NUMBER]>",
        },
    )
    monkeypatch.setattr(
        mod,
        "run_command",
        lambda args, timeout=30, stdin=None, cwd=None: {
            "ok": True,
            "returncode": 0,
            "stdout": '{"status":"ok","process_role":"gateway"}',
            "stderr": "",
        },
    )

    health = mod.gateway_health_probe("https://127.0.0.1:8011", "Ubuntu")

    assert health["ok"] is True
    assert health["status"] == 200
    assert health["json"] == {"status": "ok", "process_role": "gateway"}
    assert health["probe_source"] == "wsl_localhost_fallback"
    assert health["fallback_from_error"] == "<urlopen error [SSL: WRONG_VERSION_NUMBER]>"


def test_db_probe_executable_directional_query_excludes_hold_current_notional() -> None:
    mod = load_module()

    assert "route_action not in ('advisory_only', 'hold_current')" in mod.DB_PROBE
    assert "coalesce(portfolio_requested_notional, 0) <> 0" not in mod.DB_PROBE
    assert "coalesce(portfolio_approved_notional, 0) <> 0" not in mod.DB_PROBE
    assert mod.TARGET_CONVERGENCE_GUARD_FLAG in mod.DB_PROBE
    assert "target_convergence_guard" in mod.DB_PROBE
    for flag in mod.IMPULSE_CHASE_GUARD_FLAGS:
        assert flag in mod.DB_PROBE
    assert "directional_impulse_chase_guard" in mod.DB_PROBE
    assert mod.OKX_HEDGE_SCALE_IN_MISMATCH_REASON in mod.DB_PROBE
    assert "okx_hedge_scale_in_intent" in mod.DB_PROBE
    assert mod.CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE in mod.DB_PROBE
    assert "created_no_command_directional_order" in mod.DB_PROBE
    assert mod.CLAIMED_SUBMIT_STUCK_ROOT_CAUSE in mod.DB_PROBE
    assert "claimed_submit_stuck_submission" in mod.DB_PROBE
    assert "resolve_claimed_submit_as_failed:" in mod.DB_PROBE
    assert (
        "status not in ('FILLED', 'CANCELED', 'REJECTED', 'BLOCKED', 'DRY_RUN', 'FAILED', 'EXPIRED')"
        in mod.DB_PROBE
    )


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


def test_parse_db_probe_summarizes_latest_executable_directional_decision() -> None:
    mod = load_module()
    payload = {
        "ok": True,
        "portfolio_allocation_decisions": 120,
        "execution_fills": 7,
        "latest_decision": {
            "allocation_id": "alloc-hold",
            "decision_id": "decision_hold",
            "symbol": "BTC-USDT-SWAP",
            "route_action": "hold_current",
            "primary_family": "directional",
            "portfolio_requested_notional": "0",
            "portfolio_approved_notional": "0",
            "portfolio_budget_cut_notional": "0",
            "payload": {"execution_legs": []},
        },
        "latest_decision_audit": {
            "execution_plan_ref": None,
            "execution_plan_refs": [],
            "order_intent_refs": [],
            "order_state_refs": [],
            "fill_event_refs": [],
        },
        "latest_decision_counts": {
            "execution_orders": 0,
            "order_states": 0,
            "execution_fills": 0,
            "legacy_fill_events": 0,
        },
        "latest_executable_directional_decision": {
            "allocation_id": "alloc-exec",
            "decision_id": "decision_exec",
            "symbol": "BTC-USDT-SWAP",
            "created_at": "2026-04-26T15:00:00Z",
            "route_action": "override_target",
            "primary_family": "directional",
            "portfolio_requested_notional": "1000",
            "portfolio_approved_notional": "1000",
            "portfolio_budget_cut_notional": "0",
            "payload": {"execution_legs": [{"side": "buy"}]},
        },
        "latest_executable_directional_decision_audit": {
            "execution_plan_ref": "plan-exec",
            "execution_plan_refs": ["plan-exec"],
            "order_intent_refs": ["order-exec"],
            "order_state_refs": ["state-exec"],
            "fill_event_refs": ["fill-exec"],
            "strategy_sleeve_intent_refs": ["intent-directional"],
            "risk_decision_ref": "risk-exec",
            "decision_outcome_ref": "outcome-exec",
        },
        "latest_executable_directional_decision_counts": {
            "execution_orders": 1,
            "order_states": 1,
            "execution_fills": 1,
            "legacy_fill_events": 0,
        },
    }

    parsed = mod.parse_db_probe(json.dumps(payload), "")

    latest = parsed["latest_decision"]
    executable = parsed["latest_executable_directional_decision"]
    assert latest["decision_id"] == "decision_hold"
    assert executable["decision_id"] == "decision_exec"
    assert "payload" not in executable
    assert executable["execution_chain"]["db_order_count"] == 1
    assert executable["execution_chain"]["db_fill_count"] == 1
    assert executable["execution_truth_chain"]["status"] == "verified_execution_surface_present"
    assert executable["execution_truth_chain"]["order_expected"] is True
    assert executable["execution_truth_chain"]["fill_expected"] is True
    assert executable["execution_truth_chain"]["smallest_missing_field"] is None


def test_directional_executable_episode_truth_summarizes_terminal_no_fill_surface() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "portfolio_allocation_decisions": 120,
        "execution_fills": 7,
        "latest_decision": {},
        "latest_executable_directional_decision": {
            "allocation_id": "alloc-exec",
            "decision_id": "decision_exec",
            "created_at": "2026-04-26T15:00:00Z",
            "route_action": "override_target",
            "symbol": "BTC-USDT-SWAP",
            "primary_family": "directional",
            "portfolio_requested_notional": "1000",
            "portfolio_approved_notional": "1000",
            "portfolio_budget_cut_notional": "0",
            "expected_edge_bps": "12.5",
            "expected_cost_bps": "1.2",
            "execution_chain": {
                "execution_plan_ref_count": 1,
                "order_intent_ref_count": 1,
                "order_state_ref_count": 1,
                "fill_event_ref_count": 0,
                "db_order_count": 2,
                "db_order_state_count": 2,
                "db_fill_count": 0,
                "db_fill_via_order_count": 0,
                "db_execution_command_count": 1,
                "db_execution_submit_command_count": 1,
                "db_execution_order_terminal_no_fill_count": 2,
                "db_order_state_terminal_no_fill_count": 2,
                "execution_command_flow_enabled": True,
                "execution_command_flow_flag_present": True,
                "terminal_no_fill_order_state_drilldown": [
                    {
                        "decision_id": "decision_exec",
                        "allocation_id": "alloc-exec",
                        "order_id": "order_close",
                        "client_order_id": "cl_close",
                        "intent_id": "intent_close",
                        "symbol": "BTC-USDT-SWAP",
                        "side": "sell",
                        "pos_side": "long",
                        "position_intent": "close_long",
                        "execution_action": "close",
                        "order_type": "market",
                        "time_in_force": "IOC",
                        "source_system": "local_order_manager",
                        "execution_style": "taker",
                        "reduce_only": True,
                        "close_only": True,
                        "execution_order_state": "FAILED",
                        "execution_order_payload_status": "FAILED",
                        "nested_order_state_status": "FAILED",
                        "venue_order_id_present": False,
                        "raw_payload_venue_order_id_present": False,
                        "raw_payload_exchange_order_id_present": False,
                        "order_state_status": "FAILED",
                        "order_state_payload_status": "FAILED",
                        "order_state_exchange_order_id_present": False,
                        "order_state_payload_exchange_order_id_present": False,
                        "command_id": "cmd_close",
                        "command_type": "submit",
                        "command_state": "FAILED",
                        "attempt_count": 1,
                        "command_has_last_error": True,
                        "execution_fill_count": 0,
                        "legacy_fill_event_count": 0,
                    },
                    {
                        "decision_id": "decision_exec",
                        "allocation_id": "alloc-exec",
                        "order_id": "order_open",
                        "client_order_id": "cl_open",
                        "intent_id": "intent_open",
                        "symbol": "BTC-USDT-SWAP",
                        "side": "sell",
                        "pos_side": "short",
                        "position_intent": "open_short",
                        "execution_action": "open",
                        "order_type": "market",
                        "time_in_force": "IOC",
                        "source_system": "semantic_dup_snapshot_blocked",
                        "execution_style": "semantic_dup_snapshot_blocked",
                        "reduce_only": False,
                        "close_only": False,
                        "execution_order_state": "BLOCKED",
                        "execution_order_payload_status": "SUBMITTING",
                        "nested_order_state_status": "BLOCKED",
                        "venue_order_id_present": False,
                        "raw_payload_venue_order_id_present": False,
                        "raw_payload_exchange_order_id_present": False,
                        "order_state_status": "BLOCKED",
                        "order_state_payload_status": "BLOCKED",
                        "order_state_exchange_order_id_present": False,
                        "order_state_payload_exchange_order_id_present": False,
                        "command_id": None,
                        "command_type": None,
                        "command_state": None,
                        "attempt_count": None,
                        "command_has_last_error": False,
                        "execution_fill_count": 0,
                        "legacy_fill_event_count": 0,
                    },
                ],
            },
            "execution_truth_chain": {
                "status": "verified_terminal_order_no_fill_expected",
                "order_expected": True,
                "fill_expected": False,
                "position_lifecycle_status": "no_position_lifecycle_transition_expected",
                "smallest_missing_field": None,
                "terminal_no_fill_explanation": {
                    "classification": "terminal_order_surface_without_fill",
                    "reason": "terminal_order_blocked_before_fill",
                    "terminal_states": ["BLOCKED", "FAILED"],
                    "terminal_source_systems": ["local_order_manager"],
                    "terminal_execution_styles": ["semantic_dup_snapshot_blocked", "taker"],
                    "terminal_position_intents": ["close_long", "open_short"],
                    "execution_order_count": 2,
                    "order_state_count": 2,
                    "terminal_execution_order_count": 2,
                    "terminal_order_state_count": 2,
                    "operator_summary": "all_visible_order_surfaces_are_terminal_no_fill",
                },
            },
        },
    }

    summary = mod.summarize_directional_executable_episode_truth(db)
    live_facts = mod.project_live_runtime_facts(
        {
            "runtime": {"dashboard_bundle": {}},
            "database_truth": db,
            "directional_executable_episode_truth": summary,
            "git": {},
            "deployment_health": {},
        }
    )

    assert summary["status"] == "verified_executable_terminal_order_no_fill_truth"
    assert summary["smallest_missing_field"] is None
    assert summary["latest_executable_decision"]["decision_id"] == "decision_exec"
    assert summary["latest_executable_decision"]["order_expected"] is True
    assert summary["latest_executable_decision"]["fill_expected"] is False
    assert summary["terminal_no_fill"]["reason"] == "terminal_order_blocked_before_fill"
    assert summary["terminal_no_fill"]["terminal_position_intents"] == [
        "close_long",
        "open_short",
    ]
    assert summary["terminal_no_fill_drilldown"]["status"] == (
        "verified_terminal_no_fill_order_state_drilldown"
    )
    assert summary["terminal_no_fill_drilldown"]["coverage"] == {
        "expected_execution_order_count": 2,
        "expected_order_state_count": 2,
        "drilldown_order_count": 2,
        "command_present_count": 1,
        "command_absent_count": 1,
        "exchange_ack_present_count": 0,
        "exchange_ack_absent_count": 2,
        "fill_absent_count": 2,
        "terminal_state_verified_count": 2,
    }
    drilldown_order = summary["terminal_no_fill_drilldown"]["per_order"][0]
    assert drilldown_order["decision"]["decision_id"] == "decision_exec"
    assert drilldown_order["order_intent"]["intent_id"] == "intent_close"
    assert drilldown_order["execution_command"]["command_id"] == "cmd_close"
    assert drilldown_order["execution_order"]["state"] == "FAILED"
    assert drilldown_order["order_state"]["status"] == "FAILED"
    assert drilldown_order["exchange_ack"]["absent"] is True
    assert drilldown_order["fill_absence"]["verified"] is True
    assert drilldown_order["classification"] == "terminal_no_fill_order_state_link_verified"
    assert summary["provenance"]["db_order_count"] == 2
    assert summary["provenance"]["db_execution_command_count"] == 1
    assert summary["interpretation"]["terminal_no_fill_verified"] is True
    assert summary["interpretation"]["terminal_no_fill_order_state_drilldown_verified"] is True
    assert live_facts["directional_executable_episode_truth_status"] == (
        "verified_executable_terminal_order_no_fill_truth"
    )
    assert live_facts["directional_executable_episode_latest_decision_id"] == "decision_exec"
    assert live_facts["directional_executable_episode_fill_expected"] is False
    assert live_facts["directional_executable_episode_terminal_no_fill_states"] == [
        "BLOCKED",
        "FAILED",
    ]
    assert live_facts["directional_executable_episode_terminal_no_fill_drilldown_status"] == (
        "verified_terminal_no_fill_order_state_drilldown"
    )
    assert live_facts["directional_executable_episode_terminal_no_fill_drilldown_order_count"] == 2
    assert (
        live_facts[
            "directional_executable_episode_terminal_no_fill_drilldown_exchange_ack_absent_count"
        ]
        == 2
    )
    assert live_facts["directional_executable_episode_terminal_no_fill_drilldown_fill_absent_count"] == 2


def test_executable_terminal_no_fill_pretrade_microstructure_drilldown_links_context() -> None:
    mod = load_module()
    executable_episode = {
        "status": "verified_executable_terminal_order_no_fill_truth",
        "latest_executable_decision": {
            "decision_id": "decision_exec",
            "created_at": "2026-04-28T03:40:50+08:00",
            "symbol": "BTC-USDT-SWAP",
            "route_action": "override_target",
            "primary_family": "directional",
            "order_expected": True,
            "fill_expected": False,
            "expected_edge_bps": "14.1",
            "expected_cost_bps": "11.5",
        },
        "terminal_no_fill": {
            "terminal_states": ["BLOCKED", "FAILED"],
            "terminal_source_systems": ["local_order_manager", "semantic_dup_snapshot_blocked"],
            "terminal_execution_styles": ["taker", "semantic_dup_snapshot_blocked"],
        },
        "terminal_no_fill_drilldown": {
            "status": "verified_terminal_no_fill_order_state_drilldown",
            "coverage": {
                "drilldown_order_count": 2,
                "exchange_ack_absent_count": 2,
                "fill_absent_count": 2,
                "terminal_state_verified_count": 2,
            },
        },
    }
    rdp_microstructure = {
        "ok": True,
        "recent_silver_limit": 672,
        "recent_silver_orderbook": [
            {
                "ts": "2026-04-28T03:30:00+08:00",
                "bbo_samples_n": 820,
                "books5_samples_n": 1600,
                "mid_price_last": "76000.0",
                "spread_bps_mean": "0.0132",
                "spread_bps_max": "0.0133",
                "spread_bps_min": "0.0131",
                "quality_flags": [],
            }
        ],
        "recent_silver_trade_flow": [
            {
                "ts": "2026-04-28T03:30:00+08:00",
                "trade_count": 12400,
                "total_volume_ccy": "1000",
                "taker_buy_ratio": "0.52",
                "trade_flow_imbalance": "0.04",
                "vwap_minus_mid_bps": "1.25",
                "quality_flags": [],
            }
        ],
    }
    execution_science = {
        "status": "verified_orderbook_sequence_and_silver_bar_present",
        "payload_sequence": {
            "status": "sequence_continuous",
            "window_minutes": 30,
            "sequence_gap_count": 0,
            "latest_ts": "2026-04-30T09:00:00Z",
        },
    }
    orderbook_payload_depth = {
        "status": "verified_books5_payload_depth_evidence_present",
        "raw_payload_exposed": False,
        "sequence": {
            "books5_row_count": 120,
            "books5_sequence_gap_count": 0,
            "diff_payload_persisted_row_count": 400,
        },
    }
    depth_slippage_lifecycle = {
        "status": "forward_depth_ready_no_order_expected_regime",
        "interpretation": {
            "forward_depth_ready": True,
            "existing_fill_slippage_baseline_present": True,
        },
    }
    slippage_cost = {
        "status": "verified_slippage_cost_calibration_evidence_present",
        "fee": {"sample_count": 73},
        "slippage_proxy": {
            "sample_count": 17,
            "coverage_audit": {
                "classification": "missing_reference_price_coverage_is_no_submit_command_path",
                "covered_reference_fills_with_command_reference": 17,
                "missing_reference_fills": 56,
                "reference_policy": "pretrade_order_or_command_reference_only",
            },
        },
    }

    truth = mod.summarize_directional_executable_terminal_no_fill_pretrade_microstructure_truth(
        directional_executable_episode=executable_episode,
        rdp_microstructure=rdp_microstructure,
        execution_science=execution_science,
        orderbook_payload_depth=orderbook_payload_depth,
        depth_slippage_lifecycle=depth_slippage_lifecycle,
        slippage_cost=slippage_cost,
    )
    live_facts = mod.project_live_runtime_facts(
        {
            "runtime": {"dashboard_bundle": {}},
            "database_truth": {"ok": True},
            "directional_executable_episode_truth": executable_episode,
            "directional_executable_terminal_no_fill_pretrade_microstructure_truth": truth,
            "execution_science_truth": execution_science,
            "orderbook_payload_depth_truth": orderbook_payload_depth,
            "slippage_cost_calibration_truth": slippage_cost,
            "depth_slippage_lifecycle_truth": depth_slippage_lifecycle,
            "git": {},
            "deployment_health": {},
        }
    )

    assert truth["status"] == "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
    assert truth["smallest_missing_field"] is None
    assert truth["pretrade_microstructure"]["status"] == "verified_pretrade_microstructure_context_present"
    assert truth["pretrade_microstructure"]["decision_context"]["orderbook"]["books5_samples_n"] == 1600
    assert truth["snapshot_diff_sequence"]["status"] == "sequence_continuous"
    assert truth["orderbook_payload_depth"]["books5_sequence_gap_count"] == 0
    assert truth["local_fill_feasibility"]["status"] == "terminal_no_fill_before_exchange_ack"
    assert truth["local_fill_feasibility"]["market_fill_feasibility_observable"] is False
    assert truth["slippage_baseline"]["slippage_proxy_sample_count"] == 17
    assert live_facts[
        "directional_executable_terminal_no_fill_pretrade_microstructure_status"
    ] == "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
    assert live_facts[
        "directional_executable_terminal_no_fill_pretrade_microstructure_decision_id"
    ] == "decision_exec"
    assert live_facts[
        "directional_executable_terminal_no_fill_pretrade_microstructure_orderbook_books5_samples_n"
    ] == 1600
    assert live_facts[
        "directional_executable_terminal_no_fill_local_fill_feasibility_status"
    ] == "terminal_no_fill_before_exchange_ack"
    assert live_facts[
        "directional_executable_terminal_no_fill_market_fill_feasibility_observable"
    ] is False


def test_execution_science_truth_verifies_orderbook_sequence_and_silver_bar() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "symbol": "BTC-USDT-SWAP",
        "tables": {
            "bronze.market_orderbook_bbo": {
                "exists": True,
                "count": 10,
                "max_ts": "2026-04-26T20:29:10+00:00",
            },
            "bronze.market_orderbook_books5": {
                "exists": True,
                "count": 20,
                "max_ts": "2026-04-26T20:29:20+00:00",
            },
            "bronze.market_orderbook_payloads": {
                "exists": True,
                "count": 30,
                "max_ts": "2026-04-26T20:29:20+00:00",
            },
            "silver.market_orderbook_metrics_15m": {
                "exists": True,
                "count": 2,
                "max_ts": "2026-04-26T20:00:00+00:00",
            },
            "silver.market_trade_flow_15m": {
                "exists": True,
                "count": 2,
                "max_ts": "2026-04-26T20:00:00+00:00",
            },
        },
        "payload_sequence": {
            "exists": True,
            "window_minutes": 30,
            "scopes": [
                {
                    "collector_sequence_scope": "per_ingest_run_symbol_channel",
                    "ingest_run_id_prefix": "11111111",
                    "channel": "books5",
                    "n": 30,
                    "min_seq": 1,
                    "max_seq": 30,
                    "distinct_n": 30,
                    "sequence_gap_count": 0,
                },
            ],
            "capture_status_counts": [
                {
                    "capture_status": "diff_payload_persisted",
                    "n": 30,
                    "max_ts": "2026-04-26T20:29:20+00:00",
                },
            ],
        },
        "latest_silver_orderbook": {
            "ts": "2026-04-26T20:00:00+00:00",
            "bbo_samples_n": 700,
            "books5_samples_n": 1300,
            "spread_bps_mean": "0.0128",
            "spread_bps_max": "0.0130",
            "spread_bps_min": "0.0120",
            "mid_price_last": "78000.1",
            "quality_flags": [],
        },
        "workflow": {"exists": True, "active_count": 0, "status_counts": []},
    }

    summary = mod.summarize_execution_science_truth(
        raw,
        report_generated_at="2026-04-26T20:30:00Z",
    )

    assert summary["status"] == "verified_orderbook_sequence_and_silver_bar_present"
    assert summary["smallest_missing_field"] is None
    assert summary["payload_sequence"]["status"] == "sequence_continuous"
    assert summary["payload_sequence"]["scopes"][0]["ingest_run_id_prefix"] == "11111111"
    assert summary["payload_sequence"]["scopes"][0]["channel"] == "books5"
    assert summary["silver_orderbook"]["status"] == "verified_silver_orderbook_bar_present"
    assert summary["fill_feasibility_truth_status"] == "verified_preorder_orderbook_features_available"


def test_execution_science_truth_reports_smallest_missing_orderbook_field() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "symbol": "BTC-USDT-SWAP",
        "tables": {
            "bronze.market_orderbook_bbo": {"exists": True, "count": 0},
            "bronze.market_orderbook_books5": {"exists": True, "count": 10, "max_ts": "2026-04-26T20:29:20Z"},
            "bronze.market_orderbook_payloads": {"exists": True, "count": 10, "max_ts": "2026-04-26T20:29:20Z"},
            "silver.market_orderbook_metrics_15m": {"exists": True, "count": 1, "max_ts": "2026-04-26T20:00:00Z"},
            "silver.market_trade_flow_15m": {"exists": True, "count": 1, "max_ts": "2026-04-26T20:00:00Z"},
        },
        "payload_sequence": {"exists": True, "scopes": [], "capture_status_counts": []},
        "latest_silver_orderbook": {"bbo_samples_n": 1, "books5_samples_n": 1, "quality_flags": []},
    }

    summary = mod.summarize_execution_science_truth(
        raw,
        report_generated_at="2026-04-26T20:30:00Z",
    )

    assert summary["status"] == "missing_execution_science_evidence"
    assert summary["smallest_missing_field"] == "bronze.market_orderbook_bbo"
    assert summary["fill_feasibility_truth_status"] == "blocked_missing_orderbook_truth"


def microstructure_runtime_growth_inputs() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    table = {
        "exists": True,
        "count": 10,
        "min_ts": "2026-04-30T15:00:00Z",
        "max_ts": "2026-04-30T15:34:55Z",
        "recent_window_minutes": 5,
        "recent_count": 3,
    }
    silver_table = {
        "exists": True,
        "count": 4,
        "min_ts": "2026-04-30T14:00:00Z",
        "max_ts": "2026-04-30T15:30:00Z",
        "recent_window_minutes": 5,
        "recent_count": 0,
    }
    collector = {
        "container": "aats-microstructure-collector",
        "status": "Up 2 minutes (healthy)",
        "running": True,
        "healthy": True,
        "daemon_script_detected": True,
        "heartbeat": {
            "exists": True,
            "fresh": True,
            "age_seconds": 2,
            "stale_after_seconds": 60,
            "mtime_utc": "2026-04-30T15:34:58Z",
        },
    }
    raw = {
        "ok": True,
        "symbol": "BTC-USDT-SWAP",
        "tables": {
            "bronze.market_trades": table,
            "bronze.market_orderbook_bbo": table,
            "bronze.market_orderbook_books5": table,
            "bronze.market_orderbook_payloads": table,
            "silver.market_liquidation_metrics_15m": silver_table,
            "silver.market_oi_funding_metrics_15m": silver_table,
            "silver.market_orderbook_metrics_15m": silver_table,
            "silver.market_trade_flow_15m": silver_table,
            "silver.market_volume_profile_15m": silver_table,
        },
        "workflow": {
            "exists": True,
            "active_count": 0,
            "latest_task": {
                "task_id": 42,
                "workflow": "microstructure_silver_15m",
                "status": "done",
                "exit_code": 0,
                "max_seen": "2026-04-30T15:34:00Z",
            },
        },
    }
    execution_science = {
        "payload_sequence": {
            "status": "sequence_continuous",
            "window_minutes": 30,
            "row_count": 120,
            "sequence_gap_count": 0,
            "latest_ts": "2026-04-30T15:34:55Z",
            "age_seconds": 5,
        },
    }
    return collector, raw, execution_science


def test_microstructure_runtime_growth_truth_verifies_collector_bronze_silver_growth() -> None:
    mod = load_module()
    collector, raw, execution_science = microstructure_runtime_growth_inputs()

    truth = mod.summarize_microstructure_runtime_growth_truth(
        collector,
        raw,
        execution_science,
        report_generated_at="2026-04-30T15:35:00Z",
    )

    assert truth["status"] == "verified_microstructure_runtime_growth"
    assert truth["smallest_missing_field"] is None
    assert truth["collector"]["heartbeat_fresh"] is True
    assert truth["bronze_growth"]["market_trades"]["recent_rows"] == 3
    assert truth["bronze_growth"]["market_orderbook_payloads"]["fresh"] is True
    assert truth["payload_sequence"]["sequence_gap_count"] == 0
    assert truth["silver_workflow"]["status"] == "latest_done_recent"
    assert truth["silver_update"]["market_trade_flow_15m"]["fresh"] is True
    assert truth["interpretation"]["raw_payload_exposed"] is False


def test_microstructure_runtime_growth_truth_blocks_stale_heartbeat_before_db_checks() -> None:
    mod = load_module()
    collector, raw, execution_science = microstructure_runtime_growth_inputs()
    collector["heartbeat"] = {
        "exists": True,
        "fresh": False,
        "age_seconds": 90,
        "stale_after_seconds": 60,
        "mtime_utc": "2026-04-30T15:33:30Z",
    }

    truth = mod.summarize_microstructure_runtime_growth_truth(
        collector,
        raw,
        execution_science,
        report_generated_at="2026-04-30T15:35:00Z",
    )

    assert truth["ok"] is False
    assert truth["status"] == "collector_not_fresh"
    assert truth["smallest_missing_field"] == "microstructure_ws_daemon.heartbeat"
    assert truth["collector"]["heartbeat_age_seconds"] == 90


def test_project_live_runtime_facts_exposes_microstructure_runtime_growth_truth() -> None:
    mod = load_module()
    collector, raw, execution_science = microstructure_runtime_growth_inputs()
    truth = mod.summarize_microstructure_runtime_growth_truth(
        collector,
        raw,
        execution_science,
        report_generated_at="2026-04-30T15:35:00Z",
    )

    live_facts = mod.project_live_runtime_facts(
        {
            "database_truth": {"ok": True, "latest_decision": {}},
            "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
            "scope": {"shadow_benchmark": "none_verified"},
            "microstructure_runtime_growth_truth": truth,
            "git": {},
            "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
        },
    )

    assert live_facts["microstructure_runtime_growth_status"] == (
        "verified_microstructure_runtime_growth"
    )
    assert live_facts["microstructure_runtime_growth_raw_payload_exposed"] is False
    assert live_facts["microstructure_collector_running"] is True
    assert live_facts["microstructure_heartbeat_fresh"] is True
    assert live_facts["microstructure_bronze_market_trades_recent_rows"] == 3
    assert live_facts["microstructure_payload_sequence_status"] == "sequence_continuous"
    assert live_facts["microstructure_silver_workflow_status"] == "latest_done_recent"
    assert live_facts["microstructure_silver_market_trade_flow_15m_latest_ts"] == (
        "2026-04-30T15:30:00Z"
    )


def test_orderbook_payload_depth_truth_verifies_books5_sidecar_evidence() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "symbol": "BTC-USDT-SWAP",
        "latest_orderbook_payloads": {
            "exists": True,
            "rows": [
                {
                    "channel": "bbo-tbt",
                    "storage_table": "bronze.market_orderbook_payloads",
                    "snapshot_table": "bronze.market_orderbook_bbo",
                    "ts": "2026-04-29T00:59:59Z",
                    "collector_sequence": 1001,
                    "collector_sequence_scope": "per_ingest_run_symbol_channel",
                    "ingest_run_id_prefix": "9a527fc4",
                    "row_checksum_present": True,
                    "checksum_version": "orderbook_row_v1",
                    "capture_status": "diff_payload_persisted",
                    "payload_hash_present": True,
                    "payload_schema_version": "orderbook_payload_v1",
                    "payload_kind": "okx_public_orderbook",
                    "exchange_sequence_id_present": True,
                    "previous_payload_hash_present": True,
                },
                {
                    "channel": "books5",
                    "storage_table": "bronze.market_orderbook_payloads",
                    "snapshot_table": "bronze.market_orderbook_books5",
                    "ts": "2026-04-29T01:00:01Z",
                    "collector_sequence": 2002,
                    "collector_sequence_scope": "per_ingest_run_symbol_channel",
                    "ingest_run_id_prefix": "9a527fc4",
                    "row_checksum_present": True,
                    "checksum_version": "orderbook_row_v1",
                    "capture_status": "diff_payload_persisted",
                    "payload_hash_present": True,
                    "payload_schema_version": "orderbook_payload_v1",
                    "payload_kind": "okx_public_orderbook",
                    "exchange_sequence_id_present": True,
                    "previous_payload_hash_present": True,
                },
            ],
        },
    }
    execution_science = {
        "payload_sequence": {
            "status": "sequence_continuous",
            "window_minutes": 30,
            "scopes": [
                {
                    "channel": "bbo-tbt",
                    "row_count": 300,
                    "sequence_gap_count": 0,
                },
                {
                    "channel": "books5",
                    "row_count": 600,
                    "sequence_gap_count": 0,
                },
            ],
            "capture_status_counts": [
                {
                    "capture_status": "diff_payload_persisted",
                    "row_count": 900,
                }
            ],
        },
        "silver_orderbook": {
            "status": "verified_silver_orderbook_bar_present",
            "latest_bar_ts": "2026-04-29T00:45:00Z",
            "books5_samples_n": 1528,
            "bbo_samples_n": 845,
            "spread_bps_mean": "0.0135",
        },
    }

    truth = mod.summarize_orderbook_payload_depth_truth(raw, execution_science)

    assert truth["status"] == "verified_books5_payload_depth_evidence_present"
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["books5_payload"]["payload_hash_present"] is True
    assert truth["books5_payload"]["row_checksum_present"] is True
    assert truth["books5_payload"]["exchange_sequence_id_present"] is True
    assert truth["sequence"]["books5_row_count"] == 600
    assert truth["sequence"]["books5_sequence_gap_count"] == 0
    assert truth["sequence"]["diff_payload_persisted_row_count"] == 900
    assert truth["silver_orderbook"]["books5_samples_n"] == 1528


def test_slippage_cost_calibration_truth_verifies_fee_and_slippage_context() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "slippage_cost_calibration": {
            "symbol": "BTC-USDT-SWAP",
            "fills_total": 12,
            "fills_24h": 4,
            "fills_with_order": 12,
            "fills_with_limit_price": 12,
            "fills_with_slippage_reference_price": 12,
            "fee_bps_samples": 12,
            "fee_bps_mean": "5.0",
            "fee_bps_p95": "5.0",
            "slippage_proxy_samples": 12,
            "slippage_proxy_mean": "0.2",
            "slippage_proxy_p95": "0.5",
            "latest_fill_ts": "2026-04-26T20:45:00Z",
            "liquidity_role_samples": 12,
            "taker_fills": 12,
            "by_liquidity_role": [{"liquidity_role": "taker", "n": 12}],
            "by_reference_source": [{"reference_source": "execution_orders.limit_price", "n": 12}],
        },
    }
    execution_science = {
        "silver_orderbook": {
            "status": "verified_silver_orderbook_bar_present",
            "spread_bps_mean": "0.015",
        },
        "silver_trade_flow": {
            "status": "verified_silver_trade_flow_bar_present",
            "vwap_minus_mid_bps": "1.2",
            "trade_count": 100,
        },
    }

    summary = mod.summarize_slippage_cost_calibration_truth(
        db,
        execution_science,
        report_generated_at="2026-04-26T20:50:00Z",
    )

    assert summary["status"] == "verified_slippage_cost_calibration_evidence_present"
    assert summary["smallest_missing_field"] is None
    assert summary["fee"]["sample_count"] == 12
    assert summary["slippage_proxy"]["sample_count"] == 12
    assert summary["slippage_proxy"]["reference"] == "coalesced_order_or_command_reference_price"
    assert summary["slippage_proxy"]["by_reference_source"][0]["reference_source"] == "execution_orders.limit_price"
    assert summary["market_context"]["silver_trade_flow_status"] == "verified_silver_trade_flow_bar_present"


def test_slippage_cost_calibration_truth_accepts_command_intent_reference_price() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "slippage_cost_calibration": {
            "symbol": "BTC-USDT-SWAP",
            "fills_total": 68,
            "fills_24h": 40,
            "fills_with_order": 68,
            "fills_with_limit_price": 0,
            "fills_with_command_intent_reference_price": 12,
            "fills_with_slippage_reference_price": 12,
            "fee_bps_samples": 68,
            "fee_bps_mean": "5.0",
            "fee_bps_p95": "5.0",
            "slippage_proxy_samples": 12,
            "slippage_proxy_mean": "0.42",
            "slippage_proxy_p95": "0.8",
            "latest_fill_ts": "2026-04-26T21:40:00Z",
            "liquidity_role_samples": 68,
            "taker_fills": 68,
            "by_reference_source": [
                {"reference_source": "execution_commands.command_payload.intent.reference_price", "n": 12},
                {"reference_source": "missing", "n": 56},
            ],
            "by_reference_coverage_path": [
                {
                    "coverage": "missing",
                    "source_system": "local_order_manager",
                    "order_type": "market",
                    "time_in_force": "IOC",
                    "execution_style": "local_order_manager",
                    "strategy_family": "directional",
                    "order_state": "FILLED",
                    "command_presence": "no_submit_command",
                    "command_reference_presence": "command_no_reference",
                    "submit_command_states": "none",
                    "n": 56,
                    "order_count": 54,
                    "first_fill_ingestion_ts": "2026-04-26T13:03:29Z",
                    "last_fill_ingestion_ts": "2026-04-26T15:21:51Z",
                },
                {
                    "coverage": "covered",
                    "source_system": "local_order_manager",
                    "order_type": "market",
                    "time_in_force": "IOC",
                    "execution_style": "taker",
                    "strategy_family": "directional",
                    "order_state": "FILLED",
                    "command_presence": "has_submit_command",
                    "command_reference_presence": "command_has_reference",
                    "submit_command_states": "ACKED",
                    "n": 12,
                    "order_count": 8,
                    "first_fill_ingestion_ts": "2026-04-26T20:35:12Z",
                    "last_fill_ingestion_ts": "2026-04-26T21:55:29Z",
                },
            ],
        },
    }
    execution_science = {
        "silver_orderbook": {"status": "verified_silver_orderbook_bar_present"},
        "silver_trade_flow": {"status": "verified_silver_trade_flow_bar_present"},
    }

    summary = mod.summarize_slippage_cost_calibration_truth(
        db,
        execution_science,
        report_generated_at="2026-04-26T21:45:00Z",
    )

    assert summary["status"] == "verified_slippage_cost_calibration_evidence_present"
    assert summary["smallest_missing_field"] is None
    assert summary["fills_with_command_intent_reference_price"] == 12
    assert summary["fills_with_slippage_reference_price"] == 12
    assert summary["slippage_proxy"]["sample_count"] == 12
    assert summary["slippage_proxy"]["by_reference_source"][0]["reference_source"] == (
        "execution_commands.command_payload.intent.reference_price"
    )
    coverage_audit = summary["slippage_proxy"]["coverage_audit"]
    assert coverage_audit["classification"] == "missing_reference_price_coverage_is_no_submit_command_path"
    assert coverage_audit["missing_reference_fills"] == 56
    assert coverage_audit["missing_reference_fills_with_submit_command"] == 0
    assert coverage_audit["missing_reference_fills_without_submit_command"] == 56
    assert coverage_audit["covered_reference_fills_with_command_reference"] == 12
    assert coverage_audit["deterministic_backfill_status"] == "blocked_no_persisted_pretrade_reference_price"
    assert coverage_audit["deterministic_backfill_fill_count"] == 56
    assert coverage_audit["deterministic_backfill_mutates_database"] is False
    assert coverage_audit["reference_policy"] == "pretrade_order_or_command_reference_only"
    assert "post-trade prices" in coverage_audit["deterministic_backfill_reason"]
    assert coverage_audit["by_order_path"][0]["source_system"] == "local_order_manager"
    assert coverage_audit["by_order_path"][0]["row_count"] == 56


def test_directional_command_flow_provenance_separates_current_and_legacy_paths() -> None:
    mod = load_module()
    slippage_cost = {
        "slippage_proxy": {
            "coverage_audit": {
                "classification": "missing_reference_price_coverage_is_no_submit_command_path",
                "reference_policy": "pretrade_order_or_command_reference_only",
                "by_order_path": [
                    {
                        "coverage": "missing",
                        "source_system": "local_order_manager",
                        "order_type": "market",
                        "time_in_force": "IOC",
                        "execution_style": "local_order_manager",
                        "strategy_family": "directional",
                        "order_state": "FILLED",
                        "command_presence": "no_submit_command",
                        "command_reference_presence": "command_no_reference",
                        "submit_command_states": "none",
                        "row_count": 31,
                        "order_count": 29,
                    },
                    {
                        "coverage": "covered",
                        "source_system": "local_order_manager",
                        "order_type": "market",
                        "time_in_force": "IOC",
                        "execution_style": "taker",
                        "strategy_family": "directional",
                        "order_state": "FILLED",
                        "command_presence": "has_submit_command",
                        "command_reference_presence": "command_has_reference",
                        "submit_command_states": "ACKED",
                        "row_count": 17,
                        "order_count": 12,
                    },
                    {
                        "coverage": "missing",
                        "source_system": "local_order_manager",
                        "order_type": "market",
                        "time_in_force": "IOC",
                        "execution_style": "null",
                        "strategy_family": "independent",
                        "order_state": "FILLED",
                        "command_presence": "no_submit_command",
                        "command_reference_presence": "command_no_reference",
                        "submit_command_states": "none",
                        "row_count": 25,
                        "order_count": 25,
                    },
                ],
            },
        },
    }

    summary = mod.summarize_directional_command_flow_provenance_truth(slippage_cost)

    assert summary["status"] == "verified_current_directional_command_flow_fill_provenance_present"
    assert summary["smallest_missing_field"] is None
    assert summary["current_command_path_reference_gap"] is False
    assert summary["coverage"]["directional_fill_count"] == 48
    assert summary["coverage"]["current_submit_command_fill_count"] == 17
    assert summary["coverage"]["current_submit_command_reference_covered_fill_count"] == 17
    assert summary["coverage"]["current_submit_command_reference_missing_fill_count"] == 0
    assert summary["coverage"]["historical_no_submit_command_fill_count"] == 31
    assert summary["coverage"]["historical_no_submit_command_reference_missing_fill_count"] == 31
    assert summary["coverage_classification"] == "missing_reference_price_coverage_is_no_submit_command_path"
    assert summary["reference_policy"] == "pretrade_order_or_command_reference_only"
    assert len(summary["by_order_path"]) == 2


def test_depth_slippage_lifecycle_truth_marks_forward_ready_without_recent_filled_episode() -> None:
    mod = load_module()

    truth = mod.summarize_depth_slippage_lifecycle_truth(
        orderbook_payload_depth={
            "status": "verified_books5_payload_depth_evidence_present",
            "raw_payload_exposed": False,
            "books5_payload": {
                "payload_hash_present": True,
                "row_checksum_present": True,
                "exchange_sequence_id_present": True,
            },
            "sequence": {
                "books5_row_count": 90,
                "books5_sequence_gap_count": 0,
                "diff_payload_persisted_row_count": 4486,
            },
            "silver_orderbook": {"books5_samples_n": 1527},
        },
        slippage_cost={
            "status": "verified_slippage_cost_calibration_evidence_present",
            "fills_total": 73,
            "fills_24h": 0,
            "fee": {"sample_count": 73},
            "slippage_proxy": {
                "sample_count": 17,
                "coverage_audit": {
                    "classification": "missing_reference_price_coverage_is_no_submit_command_path",
                    "missing_reference_fills": 56,
                    "covered_reference_fills_with_command_reference": 17,
                    "deterministic_backfill_status": "blocked_no_persisted_pretrade_reference_price",
                    "reference_policy": "pretrade_order_or_command_reference_only",
                },
            },
        },
        directional_command_flow={
            "status": "verified_current_directional_command_flow_fill_provenance_present",
            "coverage": {
                "current_submit_command_fill_count": 17,
                "current_submit_command_reference_covered_fill_count": 17,
                "current_submit_command_reference_missing_fill_count": 0,
                "historical_no_submit_command_reference_missing_fill_count": 31,
            },
        },
        directional_attribution={
            "status": "partial_directional_episode_decisions_without_fills",
            "coverage": {
                "recent_decision_count": 24,
                "decisions_with_fills": 0,
                "decisions_with_slippage_reference": 0,
                "filled_decisions_with_pretrade_microstructure": 0,
                "filled_decisions_with_resolved_pnl_lifecycle": 0,
            },
            "pnl_lifecycle": {
                "status": "no_recent_filled_directional_decisions",
                "smallest_missing_field": None,
            },
        },
    )

    assert truth["status"] == "forward_depth_ready_no_recent_directional_filled_episode"
    assert truth["smallest_missing_field"] == (
        "directional_episode_attribution.recent_directional_filled_decisions"
    )
    assert truth["raw_payload_exposed"] is False
    assert truth["depth_readiness"]["books5_sequence_gap_count"] == 0
    assert truth["slippage_baseline"]["slippage_proxy_sample_count"] == 17
    assert truth["directional_command_coverage"][
        "current_submit_command_reference_covered_fill_count"
    ] == 17
    assert truth["recent_directional_lifecycle_coverage"]["recent_filled_decision_count"] == 0
    assert truth["interpretation"]["forward_depth_ready"] is True
    assert truth["interpretation"]["existing_fill_slippage_baseline_present"] is True
    assert truth["interpretation"]["per_recent_directional_fill_depth_lifecycle_link_present"] is False
    assert truth["interpretation"]["does_not_claim_historical_fills_have_sidecar_payload_depth"] is True


def test_depth_slippage_lifecycle_truth_accepts_no_order_expected_regime() -> None:
    mod = load_module()

    truth = mod.summarize_depth_slippage_lifecycle_truth(
        orderbook_payload_depth={
            "status": "verified_books5_payload_depth_evidence_present",
            "raw_payload_exposed": False,
            "books5_payload": {
                "payload_hash_present": True,
                "row_checksum_present": True,
                "exchange_sequence_id_present": True,
            },
            "sequence": {
                "books5_row_count": 97,
                "books5_sequence_gap_count": 0,
                "diff_payload_persisted_row_count": 4625,
            },
            "silver_orderbook": {"books5_samples_n": 1563},
        },
        slippage_cost={
            "status": "verified_slippage_cost_calibration_evidence_present",
            "fills_total": 73,
            "fills_24h": 0,
            "fee": {"sample_count": 73},
            "slippage_proxy": {
                "sample_count": 17,
                "coverage_audit": {
                    "classification": "missing_reference_price_coverage_is_no_submit_command_path",
                    "missing_reference_fills": 56,
                    "covered_reference_fills_with_command_reference": 17,
                    "deterministic_backfill_status": "blocked_no_persisted_pretrade_reference_price",
                    "reference_policy": "pretrade_order_or_command_reference_only",
                },
            },
        },
        directional_command_flow={
            "status": "verified_current_directional_command_flow_fill_provenance_present",
            "coverage": {
                "current_submit_command_fill_count": 17,
                "current_submit_command_reference_covered_fill_count": 17,
                "current_submit_command_reference_missing_fill_count": 0,
                "historical_no_submit_command_reference_missing_fill_count": 31,
            },
        },
        directional_attribution={
            "status": "verified_directional_episode_no_order_expected",
            "coverage": {
                "recent_decision_count": 24,
                "decisions_with_fills": 0,
                "decisions_with_no_order_expected": 24,
                "decisions_missing_order_surface": 0,
                "all_recent_decisions_no_order_expected": True,
                "decisions_with_slippage_reference": 0,
                "filled_decisions_with_pretrade_microstructure": 0,
                "filled_decisions_with_resolved_pnl_lifecycle": 0,
            },
            "pnl_lifecycle": {
                "status": "no_position_lifecycle_transition_expected",
                "smallest_missing_field": None,
            },
        },
    )

    assert truth["status"] == "forward_depth_ready_no_order_expected_regime"
    assert truth["smallest_missing_field"] is None
    assert truth["recent_directional_lifecycle_coverage"]["recent_decision_count"] == 24
    assert truth["recent_directional_lifecycle_coverage"]["decisions_with_no_order_expected"] == 24
    assert truth["recent_directional_lifecycle_coverage"]["decisions_missing_order_surface"] == 0
    assert truth["recent_directional_lifecycle_coverage"]["all_recent_decisions_no_order_expected"] is True
    assert truth["recent_directional_lifecycle_coverage"]["no_order_expected_regime"] is True
    assert truth["interpretation"]["no_order_expected_regime"] is True
    assert truth["interpretation"]["waiting_for_executable_directional_episode"] is True
    assert truth["interpretation"]["per_recent_directional_fill_depth_lifecycle_link_present"] is False


def test_directional_command_flow_provenance_reports_current_reference_gap() -> None:
    mod = load_module()
    slippage_cost = {
        "slippage_proxy": {
            "coverage_audit": {
                "classification": "current_command_path_reference_gap_possible",
                "by_order_path": [
                    {
                        "coverage": "missing",
                        "strategy_family": "directional",
                        "command_presence": "has_submit_command",
                        "command_reference_presence": "command_no_reference",
                        "submit_command_states": "ACKED",
                        "row_count": 3,
                        "order_count": 2,
                    },
                ],
            },
        },
    }

    summary = mod.summarize_directional_command_flow_provenance_truth(slippage_cost)

    assert summary["status"] == "current_directional_command_flow_reference_gap"
    assert summary["smallest_missing_field"] == "current_directional_submit_command_reference_price"
    assert summary["current_command_path_reference_gap"] is True
    assert summary["coverage"]["current_submit_command_reference_missing_fill_count"] == 3


def test_slippage_cost_calibration_truth_reports_missing_slippage_reference() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "slippage_cost_calibration": {
            "symbol": "BTC-USDT-SWAP",
            "fills_total": 62,
            "fills_24h": 37,
            "fills_with_order": 62,
            "fills_with_limit_price": 0,
            "fills_with_slippage_reference_price": 0,
            "fee_bps_samples": 62,
            "fee_bps_mean": "5.0",
            "fee_bps_p95": "5.0",
            "slippage_proxy_samples": 0,
            "latest_fill_ts": "2026-04-26T20:45:00Z",
            "liquidity_role_samples": 62,
            "taker_fills": 62,
        },
    }
    execution_science = {
        "silver_orderbook": {"status": "verified_silver_orderbook_bar_present"},
        "silver_trade_flow": {"status": "verified_silver_trade_flow_bar_present"},
    }

    summary = mod.summarize_slippage_cost_calibration_truth(
        db,
        execution_science,
        report_generated_at="2026-04-26T20:50:00Z",
    )

    assert summary["status"] == "partial_fee_verified_slippage_proxy_missing"
    assert summary["smallest_missing_field"] == "order_or_command_reference_price_for_slippage_proxy"
    assert summary["fee"]["sample_count"] == 62
    assert summary["slippage_proxy"]["sample_count"] == 0


def test_directional_episode_attribution_truth_summarizes_edge_cost_fill_and_pnl() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "portfolio_allocation_decisions": 100,
        "execution_fills": 3,
        "directional_episode_attribution": {
            "symbol": "BTC-USDT-SWAP",
            "recent_decisions": [
                {
                    "allocation_id": "alloc_1",
                    "decision_id": "decision_loss_drilldown",
                    "symbol": "BTC-USDT-SWAP",
                    "created_at": "2026-04-27T06:00:00+08:00",
                    "route_action": "override_target",
                    "primary_family": "directional",
                    "portfolio_requested_notional": "200",
                    "portfolio_approved_notional": "100",
                    "portfolio_budget_cut_notional": "100",
                    "expected_edge_bps": "47.19",
                    "expected_cost_bps": "6.00",
                    "order_count": 1,
                    "created_or_submitting_no_venue_count": 0,
                    "terminal_no_fill_order_count": 0,
                    "blocked_order_count": 0,
                    "order_states": "FILLED",
                    "order_position_intents": "open_long",
                    "order_execution_actions": "open",
                    "order_strategy_bundle_ids": "bundle_1",
                    "fill_count": 2,
                    "filled_order_count": 1,
                    "fill_outcome_count": 2,
                    "turnover_usdt": "1000",
                    "fee_usdt": "0.50",
                    "realized_pnl_usdt": "0.12",
                    "fill_outcome_fee_delta_usdt": "-0.50",
                    "actual_fee_bps_sample_count": 2,
                    "actual_fee_bps_mean": "5.10",
                    "realized_slippage_sample_count": 2,
                    "realized_slippage_bps_mean": "1.40",
                    "slippage_reference_sample_count": 2,
                    "fill_sides": "buy",
                    "liquidity_roles": "taker",
                    "fill_position_intents": "open_long",
                    "filled_order_states": "FILLED",
                    "fill_strategy_bundle_ids": "bundle_1",
                    "latest_fill_id": "fill_2",
                    "latest_fill_side": "buy",
                    "latest_fill_qty": "0.001",
                    "latest_fill_price": "78813.2",
                    "latest_fill_fee_amount": "-0.25",
                    "latest_fill_ingestion_ts": "2026-04-27T06:00:10+08:00",
                    "latest_fill_slippage_bps": "1.5",
                    "latest_fill_slippage_reference_source": (
                        "execution_commands.command_payload.intent.reference_price"
                    ),
                    "latest_fill_realized_pnl_delta": "0.04",
                    "payload": {
                        "operator_summary": "directional executable allocation",
                        "reason_codes": ["baseline_impulse_override_long", "pnl_contraction_active"],
                        "strategy_sleeve_intents": [
                            {
                                "family": "directional",
                                "route_action": "override_target",
                                "position_intent": "open_long",
                                "effective_scale": "0.5",
                                "reason_codes": ["budget_scale_applied"],
                            }
                        ],
                    },
                }
            ],
        },
    }

    parsed = mod.parse_db_probe(json.dumps(raw))
    rows = parsed["directional_episode_attribution"]["recent_decisions"]
    rdp_microstructure = {
        "ok": True,
        "recent_silver_orderbook": [
            {
                "ts": "2026-04-27T05:45:00+08:00",
                "bbo_samples_n": 41,
                "books5_samples_n": 39,
                "spread_bps_mean": "0.021",
                "spread_bps_max": "0.090",
                "spread_bps_min": "0.010",
                "mid_price_last": "78800.1",
                "quality_flags": [],
            }
        ],
        "recent_silver_trade_flow": [
            {
                "ts": "2026-04-27T05:45:00+08:00",
                "trade_count": 120,
                "total_volume_ccy": "1000000",
                "taker_buy_ratio": "0.71",
                "trade_flow_imbalance": "0.42",
                "vwap": "78790.0",
                "mid_price_ref": "78800.1",
                "vwap_minus_mid_bps": "-1.28",
                "quality_flags": [],
            }
        ],
    }
    summary = mod.summarize_directional_episode_attribution_truth(parsed, rdp_microstructure)

    assert "payload" not in rows[0]
    assert summary["status"] == "verified_directional_episode_edge_cost_pnl_attribution_present"
    assert summary["smallest_missing_field"] is None
    assert summary["coverage"]["recent_decision_count"] == 1
    assert summary["coverage"]["decisions_with_edge_cost"] == 1
    assert summary["coverage"]["decisions_with_fills"] == 1
    assert summary["coverage"]["decisions_with_pnl_outcome"] == 1
    latest = summary["latest_filled_decision"]
    assert latest["decision_id"] == "decision_loss_drilldown"
    assert latest["expected_net_edge_bps"] == "41.19"
    assert latest["realized_cost_proxy_bps"] == "6.5"
    assert latest["edge_after_realized_cost_proxy_bps"] == "40.69"
    assert latest["classification"] == "filled_with_realized_pnl_outcome"
    assert latest["pnl_outcome"]["realized_pnl_usdt"] == "0.12"
    assert latest["pnl_lifecycle"]["status"] == "realized_pnl_outcome_complete"
    assert latest["pnl_lifecycle"]["smallest_missing_field"] is None
    assert summary["pnl_lifecycle"]["status"] == "verified_directional_episode_pnl_lifecycle_explained"
    assert latest["latest_fill"]["slippage_reference_source"] == (
        "execution_commands.command_payload.intent.reference_price"
    )
    assert summary["pretrade_microstructure"]["status"] == (
        "verified_filled_directional_episode_pretrade_microstructure_present"
    )
    assert summary["coverage"]["decisions_with_pretrade_microstructure"] == 1
    assert summary["coverage"]["filled_decisions_with_pretrade_microstructure"] == 1
    assert latest["pretrade_microstructure"]["status"] == "verified_pretrade_microstructure_context_present"
    assert latest["pretrade_microstructure"]["decision_context"]["orderbook"]["spread_bps_mean"] == "0.021"
    assert latest["pretrade_microstructure"]["latest_fill_context"]["trade_flow"]["taker_buy_ratio"] == "0.71"
    assert "baseline_impulse_override_long" in latest["guard_decision"]["reason_codes"]
    assert "budget_scale_applied" in latest["guard_decision"]["reason_codes"]


def test_db_probe_directional_episode_attribution_uses_compact_payload_projection() -> None:
    mod = load_module()

    assert "portfolio_budget_cut_notional, expected_edge_bps, expected_cost_bps, payload " not in mod.DB_PROBE
    assert "rd.expected_edge_bps, rd.expected_cost_bps, rd.payload," in mod.DB_PROBE
    assert "jsonb_strip_nulls(jsonb_build_object(" in mod.DB_PROBE
    assert "payload::jsonb -> 'strategy_sleeve_intents'" in mod.DB_PROBE
    assert "'expected_signal_edge_bps'," in mod.DB_PROBE


def test_database_truth_probe_uses_measured_runtime_timeout_budget(monkeypatch) -> None:
    mod = load_module()
    calls = []

    def fake_run_command(args, *, timeout=None, stdin=None, **_kwargs):
        calls.append({"args": args, "timeout": timeout, "stdin": stdin})
        return {
            "ok": True,
            "returncode": 0,
            "stdout": '{"ok": true, "portfolio_allocation_decisions": 0, "execution_fills": 0}',
            "stderr": "",
        }

    monkeypatch.setattr(mod, "run_command", fake_run_command)

    result = mod.database_truth_probe("Ubuntu", "aats-gateway")

    assert result["ok"] is True
    assert calls[0]["timeout"] == mod.DATABASE_TRUTH_PROBE_TIMEOUT_SECONDS
    assert mod.DATABASE_TRUTH_PROBE_TIMEOUT_SECONDS == 75
    assert calls[0]["stdin"] == mod.DB_PROBE


def test_directional_episode_attribution_uses_payload_expected_edge_cost_fallback() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "latest_decision": {
            "allocation_id": "alloc_payload",
            "decision_id": "decision_payload_edge_cost",
            "symbol": "BTC-USDT-SWAP",
            "created_at": "2026-04-30T09:12:13+08:00",
            "route_action": "advisory_only",
            "primary_family": "directional",
            "expected_edge_bps": None,
            "expected_cost_bps": None,
            "payload": {
                "expected_edge_bps": "12.50",
                "expected_cost_bps": "6.00",
                "reason_codes": ["approved_but_budget_zero_suppressed"],
            },
        },
        "latest_decision_counts": {},
        "directional_episode_attribution": {
            "symbol": "BTC-USDT-SWAP",
            "recent_decisions": [
                {
                    "allocation_id": "alloc_payload",
                    "decision_id": "decision_payload_edge_cost",
                    "symbol": "BTC-USDT-SWAP",
                    "created_at": "2026-04-30T09:12:13+08:00",
                    "route_action": "advisory_only",
                    "primary_family": "directional",
                    "expected_edge_bps": None,
                    "expected_cost_bps": None,
                    "order_count": 0,
                    "fill_count": 0,
                    "payload": {
                        "expected_edge_bps": "12.50",
                        "expected_cost_bps": "6.00",
                        "reason_codes": ["approved_but_budget_zero_suppressed"],
                    },
                }
            ],
        },
    }

    parsed = mod.parse_db_probe(json.dumps(raw))
    latest = parsed["latest_decision"]
    summary = mod.summarize_directional_episode_attribution_truth(parsed, {"ok": False})
    decision = summary["recent_decisions"][0]

    assert latest["expected_edge_bps"] == "12.50"
    assert latest["expected_cost_bps"] == "6.00"
    assert latest["expected_edge_bps_source"] == "portfolio_allocation_decisions.payload.expected_edge_bps"
    assert latest["expected_cost_bps_source"] == "portfolio_allocation_decisions.payload.expected_cost_bps"
    assert decision["expected_edge_bps"] == "12.50"
    assert decision["expected_cost_bps"] == "6.00"
    assert decision["expected_net_edge_bps"] == "6.5"
    assert decision["expected_edge_bps_source"] == "portfolio_allocation_decisions.payload.expected_edge_bps"
    assert decision["expected_cost_bps_source"] == "portfolio_allocation_decisions.payload.expected_cost_bps"
    assert summary["coverage"]["decisions_with_edge_cost"] == 1
    assert summary["status"] == "verified_directional_episode_no_order_expected"
    assert summary["smallest_missing_field"] is None
    assert summary["coverage"]["decisions_with_no_order_expected"] == 1
    assert summary["coverage"]["decisions_with_no_order_semantics"] == 1
    assert summary["coverage"]["decisions_with_stable_no_order_equivalence_class"] == 1
    assert summary["coverage"]["all_no_order_expected_decisions_have_no_order_semantics"] is True
    assert summary["coverage"]["all_no_order_expected_decisions_stable_equivalence_class"] is True
    assert summary["coverage"]["decisions_with_order_surface_or_no_order_expectation"] == 1
    assert summary["coverage"]["decisions_missing_order_surface"] == 0
    assert summary["coverage"]["all_recent_decisions_no_order_expected"] is True
    assert decision["order_expectation"]["classification"] == "no_order_expected_by_route_action"
    assert decision["order_expectation"]["smallest_missing_field"] is None
    assert decision["no_order_semantics"]["status"] == (
        "verified_directional_decision_no_order_expected_semantics"
    )
    assert decision["no_order_semantics"]["equivalence_class"] == (
        "verified_non_executable_no_order_expected"
    )
    assert decision["no_order_semantics"]["root_cause_is_material_without_order_or_fill_change"] is False
    assert decision["no_order_semantics"]["requires_order_or_fill_change_for_materiality"] is True
    assert summary["no_order_semantics"]["status"] == "verified_recent_no_order_semantics_present"
    assert summary["no_order_semantics"]["smallest_missing_field"] is None
    assert summary["no_order_semantics"]["equivalence_classes"] == [
        "verified_non_executable_no_order_expected"
    ]
    assert summary["no_order_semantics"]["root_cause_is_material_without_order_or_fill_change"] is False
    assert summary["no_order_semantics"]["requires_order_or_fill_change_for_materiality"] is True


def test_directional_episode_attribution_keeps_missing_order_for_executable_no_order() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "directional_episode_attribution": {
            "symbol": "BTC-USDT-SWAP",
            "recent_decisions": [
                {
                    "allocation_id": "alloc_missing_order",
                    "decision_id": "decision_missing_order",
                    "symbol": "BTC-USDT-SWAP",
                    "created_at": "2026-04-30T09:17:13+08:00",
                    "route_action": "override_target",
                    "primary_family": "directional",
                    "expected_edge_bps": "18.25",
                    "expected_cost_bps": "6.00",
                    "order_count": 0,
                    "fill_count": 0,
                    "payload": {"reason_codes": ["baseline_directional_entry"]},
                }
            ],
        },
    }

    parsed = mod.parse_db_probe(json.dumps(raw))
    summary = mod.summarize_directional_episode_attribution_truth(parsed, {"ok": False})
    decision = summary["recent_decisions"][0]

    assert summary["status"] == "missing_directional_episode_order_surface_or_no_order_expectation"
    assert summary["smallest_missing_field"] == (
        "directional_episode_attribution.order_surface_or_no_order_expectation"
    )
    assert summary["coverage"]["decisions_with_no_order_expected"] == 0
    assert summary["coverage"]["decisions_with_no_order_semantics"] == 0
    assert summary["coverage"]["decisions_with_stable_no_order_equivalence_class"] == 0
    assert summary["coverage"]["decisions_requiring_order_surface"] == 1
    assert summary["coverage"]["decisions_missing_order_surface"] == 1
    assert summary["coverage"]["decisions_with_order_surface_or_no_order_expectation"] == 0
    assert summary["coverage"]["all_recent_decisions_no_order_expected"] is False
    assert summary["no_order_semantics"]["status"] == (
        "not_applicable_no_recent_no_order_expected_decisions"
    )
    assert decision["order_expectation"]["classification"] == (
        "order_surface_missing_for_order_expected_decision"
    )
    assert decision["order_expectation"]["smallest_missing_field"] == (
        "execution_orders.directional_recent_decision"
    )
    assert decision["no_order_semantics"]["status"] == (
        "directional_decision_no_order_semantics_not_verified"
    )
    assert decision["no_order_semantics"]["root_cause_is_material_without_order_or_fill_change"] is True


def test_directional_episode_attribution_uses_sleeve_metric_expected_edge_cost_fallback() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "directional_episode_attribution": {
            "symbol": "BTC-USDT-SWAP",
            "recent_decisions": [
                {
                    "allocation_id": "alloc_metric_payload",
                    "decision_id": "decision_metric_payload_edge_cost",
                    "symbol": "BTC-USDT-SWAP",
                    "created_at": "2026-04-30T09:24:13+08:00",
                    "route_action": "advisory_only",
                    "primary_family": "directional",
                    "expected_edge_bps": None,
                    "expected_cost_bps": None,
                    "order_count": 0,
                    "fill_count": 0,
                    "payload": {
                        "expected_edge_bps": None,
                        "expected_cost_bps": None,
                        "sleeve_intents": [
                            {
                                "family": "directional",
                                "route_action": "advisory_only",
                                "metrics": {
                                    "expected_signal_edge_bps": "18.25",
                                    "expected_cost_bps": "6.00",
                                    "expected_net_edge_bps": "12.25",
                                },
                            }
                        ],
                    },
                }
            ],
        },
    }

    parsed = mod.parse_db_probe(json.dumps(raw))
    summary = mod.summarize_directional_episode_attribution_truth(parsed, {"ok": False})
    decision = summary["recent_decisions"][0]

    assert decision["expected_edge_bps"] == "18.25"
    assert decision["expected_cost_bps"] == "6.00"
    assert decision["expected_net_edge_bps"] == "12.25"
    assert decision["expected_edge_bps_source"] == (
        "portfolio_allocation_decisions.payload.sleeve_intents[].metrics.expected_signal_edge_bps"
    )
    assert decision["expected_cost_bps_source"] == (
        "portfolio_allocation_decisions.payload.sleeve_intents[].metrics.expected_cost_bps"
    )
    assert summary["coverage"]["decisions_with_edge_cost"] == 1


def test_directional_spike_reversion_truth_classifies_adverse_fill_and_reversion() -> None:
    mod = load_module()
    decision = {
        "decision_id": "decision_spike",
        "created_at": "2026-04-27T05:55:00+08:00",
        "route_action": "override_target",
        "fill": {"count": 1},
        "latest_fill": {
            "side": "buy",
            "fill_price": "101.5",
            "ingestion_ts": "2026-04-27T05:55:20+08:00",
            "slippage_bps": "12.0",
        },
        "pretrade_microstructure": {
            "status": "verified_pretrade_microstructure_context_present",
            "decision_context": {
                "orderbook": {
                    "mid_price_last": "100",
                    "spread_bps_mean": "0.2",
                },
                "trade_flow": {
                    "vwap_minus_mid_bps": "14.0",
                    "trade_flow_imbalance": "0.66",
                    "taker_buy_ratio": "0.83",
                },
            },
            "latest_fill_context": {
                "orderbook": {
                    "mid_price_last": "101.4",
                },
                "trade_flow": {},
            },
            "post_fill_context": {
                "orderbook": {
                    "mid_price_last": "99.0",
                },
                "trade_flow": {},
            },
        },
    }

    summary = mod.summarize_directional_spike_reversion_truth(
        {
            "status": "verified_directional_episode_edge_cost_pnl_attribution_present",
            "recent_decisions": [decision],
            "latest_filled_decision": decision,
        }
    )
    latest = summary["latest_filled_decision"]

    assert summary["status"] == "verified_directional_spike_reversion_execution_context_present"
    assert summary["smallest_missing_field"] is None
    assert summary["coverage"]["recent_filled_directional_decision_count"] == 1
    assert summary["coverage"]["filled_decisions_with_spike_reversion_context"] == 1
    assert summary["coverage"]["adverse_fill_vs_decision_mid_10bps_count"] == 1
    assert summary["coverage"]["post_fill_adverse_reversion_10bps_count"] == 1
    assert summary["coverage"]["decision_trade_flow_dislocation_10bps_count"] == 1
    assert latest["classification"] == "adverse_fill_and_post_fill_reversion_observed"
    assert latest["adverse_fill_vs_decision_mid_bps"] == "150"
    assert latest["post_fill_mid_move_bps"] == "-246.3054187192118226600985222"
    assert latest["decision_trade_flow_vwap_minus_mid_bps"] == "14.0"


def test_latest_decision_fill_feasibility_marks_no_order_not_applicable_with_context() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "latest_decision": {
            "decision_id": "decision_current",
            "created_at": "2026-04-29 00:15:39+08:00",
            "symbol": "BTC-USDT-SWAP",
            "route_action": "advisory_only",
            "primary_family": "directional",
            "execution_truth_chain": {
                "status": "verified_no_order_expected",
                "order_expected": False,
                "fill_expected": False,
            },
            "no_trade_attribution": {
                "classification": "no_order_fill_expected_for_latest_decision",
                "primary_blocker": "candidate_execution_incompatible",
                "final_blockers": ["candidate_execution_incompatible", "no_execution_plan_emitted"],
                "primary_family_candidate_truth": {
                    "status": "primary_family_candidate_truth_present",
                    "smallest_missing_field": "primary_candidate_order_expectation_classification",
                    "order_expected_from_primary_candidate": None,
                    "no_order_root_cause": None,
                },
            },
        },
    }
    directional_attribution = {
        "recent_decisions": [
            {
                "decision_id": "decision_current",
                "created_at": "2026-04-29 00:15:39+08:00",
                "symbol": "BTC-USDT-SWAP",
                "route_action": "advisory_only",
                "primary_family": "directional",
                "pretrade_microstructure": {
                    "source": "rdp_microstructure_silver_15m",
                    "status": "verified_pretrade_microstructure_context_present",
                    "smallest_missing_field": None,
                    "decision_context": {
                        "orderbook": {
                            "bar_ts": "2026-04-29 00:00:00+08:00",
                            "bar_age_seconds": 939,
                            "bbo_samples_n": 859,
                            "books5_samples_n": 1568,
                            "mid_price_last": "75988.2500000000",
                            "spread_bps_mean": "0.0132",
                            "quality_flags": [],
                        },
                        "trade_flow": {
                            "bar_ts": "2026-04-29 00:00:00+08:00",
                            "bar_age_seconds": 939,
                            "trade_count": 15454,
                            "taker_buy_ratio": "0.53400222",
                            "vwap_minus_mid_bps": "2.1660",
                            "quality_flags": [],
                        },
                    },
                },
            },
        ],
    }
    execution_science = {
        "status": "verified_orderbook_sequence_and_silver_bar_present",
        "fill_feasibility_truth_status": "verified_preorder_orderbook_features_available",
        "payload_sequence": {"status": "sequence_continuous"},
        "silver_orderbook": {"status": "verified_silver_orderbook_bar_present"},
        "silver_trade_flow": {"status": "verified_silver_trade_flow_bar_present"},
    }

    truth = mod.summarize_latest_decision_fill_feasibility_truth(
        db,
        directional_attribution,
        execution_science,
    )

    assert truth["status"] == "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context"
    assert truth["smallest_missing_field"] is None
    assert truth["fill_feasibility_applicable"] is False
    assert truth["order_expected"] is False
    assert truth["fill_expected"] is False
    assert truth["no_order"]["primary_candidate_no_order_semantics"]["status"] == (
        "missing_primary_candidate_no_order_root_semantics"
    )
    assert truth["pretrade_microstructure"]["status"] == "verified_pretrade_microstructure_context_present"
    assert truth["pretrade_microstructure"]["orderbook"]["books5_samples_n"] == 1568
    assert truth["pretrade_microstructure"]["trade_flow"]["trade_count"] == 15454
    assert truth["execution_science"]["orderbook_sequence_validation_status"] == "sequence_continuous"


def test_decision_lifecycle_provenance_continuity_verifies_current_no_order_and_terminal_no_fill() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "latest_decision": {
            "decision_id": "decision_latest",
            "created_at": "2026-04-30 20:15:00+08:00",
            "primary_family": "directional",
            "route_action": "advisory_only",
            "execution_truth_chain": {
                "status": "verified_no_order_expected",
                "smallest_missing_field": None,
                "order_expected": False,
                "fill_expected": False,
                "position_lifecycle_status": "no_position_lifecycle_transition_expected",
            },
            "execution_chain": {
                "execution_plan_ref_count": 0,
                "order_intent_ref_count": 0,
                "order_state_ref_count": 0,
                "fill_event_ref_count": 0,
                "db_order_count": 0,
                "db_order_state_count": 0,
                "db_fill_count": 0,
                "db_execution_command_count": 0,
            },
            "no_trade_attribution": {
                "classification": "no_order_fill_expected_for_latest_decision",
                "primary_blocker": "candidate_execution_incompatible",
            },
        },
    }
    directional_attribution = {
        "status": "verified_directional_episode_no_order_expected",
        "smallest_missing_field": None,
        "coverage": {
            "recent_decision_count": 24,
            "decisions_with_no_order_expected": 24,
            "decisions_with_orders": 0,
            "decisions_with_fills": 0,
            "decisions_missing_order_surface": 0,
            "all_recent_decisions_no_order_expected": True,
            "filled_decisions_with_resolved_pnl_lifecycle": 0,
        },
    }
    executable_episode = {
        "status": "verified_executable_terminal_order_no_fill_truth",
        "smallest_missing_field": None,
        "latest_executable_decision": {
            "decision_id": "decision_exec",
            "created_at": "2026-04-30 19:45:00+08:00",
            "execution_truth_status": "verified_terminal_order_no_fill_expected",
            "order_expected": True,
            "fill_expected": False,
            "position_lifecycle_status": "position_lifecycle_transition_evidence_missing",
        },
        "terminal_no_fill_drilldown": {
            "status": "verified_terminal_no_fill_order_state_drilldown",
        },
        "provenance": {
            "db_order_count": 2,
            "db_order_state_count": 2,
            "db_fill_count": 0,
            "db_execution_command_count": 1,
        },
    }
    latest_fill_feasibility = {
        "status": "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context",
        "smallest_missing_field": None,
        "fill_feasibility_applicable": False,
        "no_order": {
            "classification": "no_order_fill_expected_for_latest_decision",
        },
    }
    command_flow = {
        "status": "verified_current_directional_command_flow_fill_provenance_present",
        "smallest_missing_field": None,
        "current_command_path_reference_gap": False,
        "coverage": {
            "current_submit_command_fill_count": 17,
            "current_submit_command_reference_covered_fill_count": 17,
            "current_submit_command_reference_missing_fill_count": 0,
        },
    }
    depth_slippage_lifecycle = {
        "status": "forward_depth_ready_no_order_expected_regime",
        "smallest_missing_field": None,
        "recent_directional_lifecycle_coverage": {
            "recent_filled_decision_count": 0,
            "recent_filled_with_resolved_pnl_lifecycle": 0,
            "no_order_expected_regime": True,
        },
    }

    truth = mod.summarize_decision_lifecycle_provenance_continuity_truth(
        db=db,
        directional_attribution=directional_attribution,
        directional_executable_episode=executable_episode,
        latest_decision_fill_feasibility=latest_fill_feasibility,
        directional_command_flow=command_flow,
        depth_slippage_lifecycle=depth_slippage_lifecycle,
    )

    assert truth["ok"] is True
    assert truth["status"] == "verified_current_no_order_plus_executable_terminal_no_fill_continuity"
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["current_decision"]["decision_id"] == "decision_latest"
    assert truth["current_decision"]["order_expected"] is False
    assert truth["latest_executable_directional_episode"]["decision_id"] == "decision_exec"
    assert truth["latest_executable_directional_episode"]["terminal_no_fill_drilldown_status"] == (
        "verified_terminal_no_fill_order_state_drilldown"
    )
    assert truth["interpretation"]["current_decision_no_order_expected"] is True
    assert truth["interpretation"]["latest_executable_terminal_no_fill_verified"] is True
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True


def test_decision_lifecycle_provenance_continuity_reports_missing_latest_decision_chain() -> None:
    mod = load_module()

    truth = mod.summarize_decision_lifecycle_provenance_continuity_truth(
        db={
            "ok": True,
            "latest_decision": {
                "decision_id": "decision_latest",
                "execution_truth_chain": {},
            },
        },
        directional_attribution={},
        directional_executable_episode={},
        latest_decision_fill_feasibility={},
        directional_command_flow={},
        depth_slippage_lifecycle={},
    )

    assert truth["ok"] is False
    assert truth["status"] == "missing_latest_decision_lifecycle_provenance"
    assert truth["smallest_missing_field"] == "latest_decision.execution_truth_chain"
    assert truth["raw_payload_exposed"] is False


def test_decision_lifecycle_execution_science_continuity_verifies_no_order_terminal_no_fill() -> None:
    mod = load_module()

    lifecycle = {
        "ok": True,
        "status": "verified_current_no_order_plus_executable_terminal_no_fill_continuity",
        "smallest_missing_field": None,
        "current_decision": {
            "decision_id": "decision_latest",
            "order_expected": False,
            "fill_expected": False,
            "execution_truth_status": "verified_no_order_expected",
            "fill_feasibility_status": (
                "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context"
            ),
        },
        "latest_executable_directional_episode": {
            "decision_id": "decision_exec",
            "status": "verified_executable_terminal_order_no_fill_truth",
            "terminal_no_fill_drilldown_status": "verified_terminal_no_fill_order_state_drilldown",
        },
        "recent_directional_batch": {
            "recent_decision_count": 24,
            "decisions_with_fills": 0,
        },
        "command_flow": {
            "status": "verified_current_directional_command_flow_fill_provenance_present",
        },
    }
    terminal_pretrade = {
        "status": "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown",
        "smallest_missing_field": None,
        "decision": {"decision_id": "decision_exec"},
        "snapshot_diff_sequence": {
            "status": "sequence_continuous",
            "sequence_gap_count": 0,
        },
        "local_fill_feasibility": {
            "status": "terminal_no_fill_before_exchange_ack",
            "market_fill_feasibility_observable": False,
            "terminal_order_count": 2,
            "exchange_ack_absent_count": 2,
            "fill_absent_count": 2,
        },
        "slippage_baseline": {
            "status": "verified_slippage_cost_calibration_evidence_present",
        },
    }
    orderbook_depth = {
        "status": "verified_books5_payload_depth_evidence_present",
        "smallest_missing_field": None,
        "raw_payload_exposed": False,
        "books5_payload": {"payload_hash_present": True},
        "sequence": {
            "books5_row_count": 120,
            "books5_sequence_gap_count": 0,
            "diff_payload_persisted_row_count": 400,
        },
    }
    latest_fill_feasibility = {
        "status": "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context",
        "smallest_missing_field": None,
        "decision_id": "decision_latest",
        "order_expected": False,
        "fill_expected": False,
        "fill_feasibility_applicable": False,
    }
    depth_lifecycle = {
        "status": "forward_depth_ready_no_order_expected_regime",
        "smallest_missing_field": None,
        "interpretation": {
            "forward_depth_ready": True,
            "no_order_expected_regime": True,
        },
        "recent_directional_lifecycle_coverage": {
            "recent_filled_decision_count": 0,
        },
        "slippage_baseline": {
            "slippage_proxy_sample_count": 17,
            "fee_sample_count": 73,
        },
    }
    execution_science = {
        "status": "verified_orderbook_sequence_and_silver_bar_present",
        "payload_sequence": {
            "status": "sequence_continuous",
            "sequence_gap_count": 0,
        },
        "fill_feasibility_truth_status": "verified_fill_feasibility_surface_present",
    }
    slippage_cost = {
        "status": "verified_slippage_cost_calibration_evidence_present",
        "smallest_missing_field": None,
        "fee": {"sample_count": 73},
        "slippage_proxy": {
            "sample_count": 17,
            "coverage_audit": {
                "classification": "missing_reference_price_coverage_is_no_submit_command_path",
                "missing_reference_fills": 56,
                "covered_reference_fills_with_command_reference": 17,
            },
        },
    }

    truth = mod.summarize_decision_lifecycle_execution_science_continuity_truth(
        decision_lifecycle_provenance_continuity=lifecycle,
        executable_terminal_pretrade_microstructure=terminal_pretrade,
        orderbook_payload_depth=orderbook_depth,
        latest_decision_fill_feasibility=latest_fill_feasibility,
        depth_slippage_lifecycle=depth_lifecycle,
        execution_science=execution_science,
        slippage_cost=slippage_cost,
    )

    assert truth["ok"] is True
    assert truth["status"] == "verified_no_order_terminal_no_fill_execution_science_continuity"
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["lifecycle_provenance"]["current_decision_id"] == "decision_latest"
    assert truth["terminal_no_fill_execution_science"]["decision_id"] == "decision_exec"
    assert truth["terminal_no_fill_execution_science"]["local_fill_feasibility_status"] == (
        "terminal_no_fill_before_exchange_ack"
    )
    assert truth["orderbook_payload_depth"]["books5_sequence_gap_count"] == 0
    assert truth["slippage_cost_calibration"]["slippage_proxy_sample_count"] == 17
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True


def test_decision_lifecycle_execution_science_continuity_reports_missing_terminal_context() -> None:
    mod = load_module()

    truth = mod.summarize_decision_lifecycle_execution_science_continuity_truth(
        decision_lifecycle_provenance_continuity={
            "ok": True,
            "status": "verified_current_no_order_plus_executable_terminal_no_fill_continuity",
            "smallest_missing_field": None,
            "current_decision": {
                "order_expected": False,
                "fill_expected": False,
            },
        },
        executable_terminal_pretrade_microstructure={
            "status": "blocked_missing_snapshot_diff_sequence_validation",
            "smallest_missing_field": "execution_science.payload_sequence.sequence_continuous",
        },
        orderbook_payload_depth={
            "status": "verified_books5_payload_depth_evidence_present",
            "smallest_missing_field": None,
        },
        latest_decision_fill_feasibility={
            "status": "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context",
            "smallest_missing_field": None,
        },
        depth_slippage_lifecycle={
            "status": "forward_depth_ready_no_order_expected_regime",
            "smallest_missing_field": None,
        },
        execution_science={
            "status": "verified_orderbook_sequence_and_silver_bar_present",
            "payload_sequence": {"status": "sequence_continuous"},
        },
        slippage_cost={
            "status": "verified_slippage_cost_calibration_evidence_present",
            "smallest_missing_field": None,
        },
    )

    assert truth["ok"] is False
    assert truth["status"] == "missing_terminal_no_fill_execution_science_context"
    assert truth["smallest_missing_field"] == (
        "directional_executable_terminal_no_fill_pretrade_microstructure_truth"
    )
    assert truth["raw_payload_exposed"] is False


def test_recent_directional_decision_chain_density_verifies_no_order_regime() -> None:
    mod = load_module()

    directional_attribution = {
        "ok": True,
        "status": "verified_directional_episode_no_order_expected",
        "smallest_missing_field": None,
        "coverage": {
            "recent_decision_count": 24,
            "decisions_with_order_surface_or_no_order_expectation": 24,
            "decisions_missing_order_surface": 0,
            "decisions_with_no_order_expected": 24,
            "all_recent_decisions_no_order_expected": True,
            "decisions_with_no_order_semantics": 24,
            "decisions_with_stable_no_order_equivalence_class": 24,
            "all_no_order_expected_decisions_have_no_order_semantics": True,
            "all_no_order_expected_decisions_stable_equivalence_class": True,
            "decisions_with_fills": 0,
            "decisions_with_pnl_outcome": 0,
            "filled_decisions_with_pnl_lifecycle_classification": 0,
            "filled_decisions_with_resolved_pnl_lifecycle": 0,
            "decisions_with_pretrade_microstructure": 24,
            "filled_decisions_with_pretrade_microstructure": 0,
        },
        "no_order_semantics": {
            "coverage": {
                "all_no_order_expected_decisions_have_no_order_semantics": True,
                "all_no_order_expected_decisions_stable_equivalence_class": True,
            },
        },
        "recent_decisions": [
            {
                "decision_id": "decision_latest",
                "created_at": "2026-04-30T17:50:00Z",
                "route_action": "advisory_only",
                "order_expectation": {
                    "order_surface_present": False,
                    "no_order_expected": True,
                },
                "no_order_semantics": {
                    "status": "verified_directional_decision_no_order_expected_semantics",
                },
                "fill": {"count": 0},
                "pnl_lifecycle": {},
                "pretrade_microstructure": {
                    "status": "verified_pretrade_microstructure_context_present",
                },
            },
        ],
    }

    truth = mod.summarize_recent_directional_decision_chain_density_truth(
        directional_attribution=directional_attribution,
        decision_lifecycle_provenance_continuity={
            "status": "verified_current_no_order_plus_executable_terminal_no_fill_continuity",
            "smallest_missing_field": None,
        },
        decision_lifecycle_execution_science_continuity={
            "status": "verified_no_order_terminal_no_fill_execution_science_continuity",
            "smallest_missing_field": None,
        },
        directional_command_flow={
            "status": "verified_current_directional_command_flow_fill_provenance_present",
            "smallest_missing_field": None,
            "current_command_path_reference_gap": False,
        },
    )

    assert truth["ok"] is True
    assert truth["status"] == "verified_recent_directional_decision_chain_density_no_order_regime"
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["coverage"]["recent_decision_count"] == 24
    assert truth["coverage"]["all_recent_decisions_no_order_expected"] is True
    assert truth["interpretation"]["waiting_for_executable_directional_episode"] is True
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True
    assert truth["recent_decisions"][0]["decision_id"] == "decision_latest"
    assert truth["recent_decisions"][0]["no_order_expected"] is True


def test_recent_directional_decision_chain_density_reports_missing_order_surface() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_decision_chain_density_truth(
        directional_attribution={
            "ok": True,
            "status": "missing_directional_episode_order_surface_or_no_order_expectation",
            "smallest_missing_field": (
                "directional_episode_attribution.order_surface_or_no_order_expectation"
            ),
            "coverage": {
                "recent_decision_count": 1,
                "decisions_with_order_surface_or_no_order_expectation": 0,
                "decisions_missing_order_surface": 1,
                "decisions_with_no_order_expected": 0,
                "all_recent_decisions_no_order_expected": False,
                "decisions_with_no_order_semantics": 0,
                "decisions_with_stable_no_order_equivalence_class": 0,
                "decisions_with_fills": 0,
            },
        },
        decision_lifecycle_provenance_continuity={
            "status": "verified_decision_lifecycle_provenance_continuity",
            "smallest_missing_field": None,
        },
        decision_lifecycle_execution_science_continuity={
            "status": "verified_decision_lifecycle_execution_science_continuity",
            "smallest_missing_field": None,
        },
        directional_command_flow={
            "smallest_missing_field": None,
            "current_command_path_reference_gap": False,
        },
    )

    assert truth["ok"] is False
    assert truth["status"] == "missing_recent_directional_order_surface_or_no_order_expectation"
    assert truth["smallest_missing_field"] == (
        "directional_episode_attribution.order_surface_or_no_order_expectation"
    )
    assert truth["coverage"]["decisions_missing_order_surface"] == 1
    assert truth["raw_payload_exposed"] is False


def test_recent_directional_no_order_root_cause_density_verifies_roots() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_root_cause_density_truth(
        directional_attribution={
            "ok": True,
            "coverage": {
                "recent_decision_count": 2,
                "decisions_with_order_surface_or_no_order_expectation": 2,
                "decisions_missing_order_surface": 0,
                "decisions_with_no_order_expected": 2,
                "all_recent_decisions_no_order_expected": True,
                "decisions_with_no_order_semantics": 2,
                "decisions_with_stable_no_order_equivalence_class": 2,
                "all_no_order_expected_decisions_have_no_order_semantics": True,
                "all_no_order_expected_decisions_stable_equivalence_class": True,
            },
            "no_order_semantics": {
                "status": "verified_recent_no_order_semantics_present",
                "smallest_missing_field": None,
                "coverage": {
                    "decisions_with_no_order_expected": 2,
                    "decisions_with_no_order_semantics": 2,
                    "decisions_with_stable_no_order_equivalence_class": 2,
                    "decisions_with_root_cause": 2,
                    "decisions_with_root_materiality": 2,
                    "decisions_with_materiality_requirement": 2,
                    "all_no_order_expected_decisions_have_no_order_semantics": True,
                    "all_no_order_expected_decisions_stable_equivalence_class": True,
                    "all_root_causes_non_material_without_order_or_fill_change": True,
                    "all_root_causes_require_order_or_fill_change_for_materiality": True,
                },
                "distributions": {
                    "root_cause": [
                        {
                            "value": "decision_route_action_advisory_only_no_order_expected",
                            "count": 2,
                        }
                    ],
                    "equivalence_class": [
                        {
                            "value": "verified_non_executable_no_order_expected",
                            "count": 2,
                        }
                    ],
                    "route_action": [{"value": "advisory_only", "count": 2}],
                    "semantic_status": [
                        {
                            "value": (
                                "verified_directional_decision_no_order_expected_semantics"
                            ),
                            "count": 2,
                        }
                    ],
                },
            },
            "recent_decisions": [
                {
                    "decision_id": "decision_latest",
                    "created_at": "2026-04-30T17:50:00Z",
                    "route_action": "advisory_only",
                    "order_expectation": {
                        "classification": "no_order_expected_by_route_action",
                        "no_order_expected": True,
                    },
                    "no_order_semantics": {
                        "status": "verified_directional_decision_no_order_expected_semantics",
                        "equivalence_class": "verified_non_executable_no_order_expected",
                        "root_cause": "decision_route_action_advisory_only_no_order_expected",
                        "root_cause_is_material_without_order_or_fill_change": False,
                        "requires_order_or_fill_change_for_materiality": True,
                    },
                }
            ],
        }
    )

    assert truth["ok"] is True
    assert truth["status"] == "verified_recent_directional_no_order_root_cause_density"
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["coverage"]["no_order_expected_decision_count"] == 2
    assert truth["coverage"]["decisions_missing_root_cause"] == 0
    assert truth["top_root_cause"] == "decision_route_action_advisory_only_no_order_expected"
    assert truth["top_equivalence_class"] == "verified_non_executable_no_order_expected"
    assert truth["top_route_action"] == "advisory_only"
    assert truth["interpretation"][
        "all_roots_non_material_without_order_or_fill_change"
    ] is True
    assert truth["interpretation"][
        "root_cause_switch_without_order_or_fill_is_non_material"
    ] is True
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True


def test_recent_directional_no_order_root_cause_density_reports_missing_semantics() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_root_cause_density_truth(
        directional_attribution={
            "ok": True,
            "coverage": {
                "recent_decision_count": 2,
                "decisions_with_order_surface_or_no_order_expectation": 2,
                "decisions_missing_order_surface": 0,
                "decisions_with_no_order_expected": 2,
                "all_recent_decisions_no_order_expected": True,
                "decisions_with_no_order_semantics": 1,
                "decisions_with_stable_no_order_equivalence_class": 1,
                "all_no_order_expected_decisions_have_no_order_semantics": False,
                "all_no_order_expected_decisions_stable_equivalence_class": False,
            },
            "no_order_semantics": {
                "coverage": {
                    "decisions_with_no_order_expected": 2,
                    "decisions_with_no_order_semantics": 1,
                    "decisions_with_stable_no_order_equivalence_class": 1,
                    "all_no_order_expected_decisions_have_no_order_semantics": False,
                    "all_no_order_expected_decisions_stable_equivalence_class": False,
                }
            },
        }
    )

    assert truth["ok"] is False
    assert truth["status"] == "missing_recent_directional_no_order_root_semantics"
    assert truth["smallest_missing_field"] == (
        "directional_episode_attribution.no_order_semantics"
    )
    assert truth["coverage"]["decisions_missing_no_order_semantics"] == 1
    assert truth["coverage"]["decisions_missing_stable_no_order_equivalence_class"] == 1
    assert truth["raw_payload_exposed"] is False


def test_latest_directional_no_order_primary_candidate_bridge_verifies_distinct_roots() -> None:
    mod = load_module()

    truth = mod.summarize_latest_directional_no_order_primary_candidate_bridge_truth(
        db={
            "ok": True,
            "latest_decision": {
                "decision_id": "decision-current",
                "created_at": "2026-04-30T20:54:08Z",
                "symbol": "BTC-USDT-SWAP",
                "primary_family": "directional",
                "route_action": "advisory_only",
                "execution_truth_chain": {
                    "order_expected": False,
                    "fill_expected": False,
                },
                "no_trade_attribution": {
                    "classification": "no_order_fill_expected_for_latest_decision",
                    "is_current_no_trade": True,
                    "primary_blocker": "candidate_execution_incompatible",
                    "primary_family_candidate_truth": {
                        "status": "verified_primary_candidate_hold_current_zero_delta_no_order_expected",
                        "smallest_missing_field": None,
                        "primary_family": "directional",
                        "candidate_route_action": "hold_current",
                        "candidate_execution_behavior": "hold_current",
                        "order_expected_from_primary_candidate": False,
                        "no_order_root_cause": "primary_candidate_hold_current_zero_delta",
                        "candidate_execution_compatible": True,
                        "candidate_approved_for_execution": True,
                        "candidate_permission_mode": "approved",
                        "composed_delta_position_qty": "0",
                        "target_notional": "0",
                        "global_primary_blocker": "candidate_execution_incompatible",
                        "global_primary_blocker_applies_to_candidate": False,
                        "global_blocker_scope": "other_candidate_or_portfolio_level",
                    },
                },
            },
        }
    )

    assert truth["ok"] is True
    assert truth["status"] == "verified_latest_directional_no_order_primary_candidate_bridge"
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["latest_decision"]["route_action"] == "advisory_only"
    assert truth["latest_decision"]["order_expected"] is False
    assert truth["latest_decision"]["portfolio_route_no_order_root_cause"] == (
        "decision_route_action_advisory_only_no_order_expected"
    )
    assert truth["primary_candidate"]["route_action"] == "hold_current"
    assert truth["primary_candidate"]["order_expected"] is False
    assert truth["primary_candidate"]["no_order_root_cause"] == (
        "primary_candidate_hold_current_zero_delta"
    )
    assert truth["primary_candidate"]["no_order_semantic_status"] == (
        "verified_primary_candidate_no_order_expected_semantics"
    )
    assert truth["bridge"]["latest_route_action_differs_from_primary_candidate_route_action"] is True
    assert truth["bridge"]["route_root_and_primary_candidate_root_distinct"] is True
    assert truth["bridge"]["global_blocker_scope"] == "other_candidate_or_portfolio_level"
    assert truth["interpretation"]["portfolio_route_action_is_not_primary_candidate_root"] is True
    assert (
        truth["interpretation"][
            "hold_current_zero_delta_explains_primary_directional_no_order"
        ]
        is True
    )
    assert (
        truth["interpretation"][
            "advisory_only_route_action_explains_portfolio_route_no_order"
        ]
        is True
    )
    assert (
        truth["interpretation"][
            "global_blocker_is_other_candidate_or_portfolio_level"
        ]
        is True
    )
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True


def test_latest_directional_no_order_primary_candidate_bridge_reports_missing_candidate() -> None:
    mod = load_module()

    truth = mod.summarize_latest_directional_no_order_primary_candidate_bridge_truth(
        db={
            "ok": True,
            "latest_decision": {
                "decision_id": "decision-current",
                "symbol": "BTC-USDT-SWAP",
                "primary_family": "directional",
                "route_action": "advisory_only",
                "execution_truth_chain": {
                    "order_expected": False,
                    "fill_expected": False,
                },
                "no_trade_attribution": {
                    "classification": "no_order_fill_expected_for_latest_decision",
                    "is_current_no_trade": True,
                    "primary_blocker": "candidate_execution_incompatible",
                },
            },
        }
    )

    assert truth["ok"] is False
    assert truth["status"] == "missing_latest_directional_primary_candidate_truth"
    assert truth["smallest_missing_field"] == (
        "database_truth.latest_decision.no_trade_attribution."
        "primary_family_candidate_truth"
    )
    assert truth["raw_payload_exposed"] is False


def test_recent_directional_no_order_primary_candidate_bridge_density_verifies_latest_scope() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_primary_candidate_bridge_density_truth(
        directional_attribution={
            "ok": True,
            "coverage": {"recent_decision_count": 2},
            "recent_decisions": [
                {"decision_id": "decision-current"},
                {"decision_id": "decision-previous"},
            ],
        },
        recent_no_order_root_density={
            "status": "verified_recent_directional_no_order_root_cause_density",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 2,
                "no_order_expected_decision_count": 2,
                "decisions_with_no_order_semantics": 2,
                "decisions_with_root_cause": 2,
                "decisions_with_root_materiality": 2,
            },
            "top_root_cause": "decision_route_action_advisory_only_no_order_expected",
            "top_equivalence_class": "verified_non_executable_no_order_expected",
            "top_route_action": "advisory_only",
            "distributions": {
                "root_cause": [
                    {
                        "value": "decision_route_action_advisory_only_no_order_expected",
                        "count": 2,
                    }
                ],
                "route_action": [{"value": "advisory_only", "count": 2}],
            },
            "interpretation": {
                "all_roots_non_material_without_order_or_fill_change": True,
                "all_roots_require_order_or_fill_change_for_materiality": True,
            },
        },
        latest_bridge={
            "status": "verified_latest_directional_no_order_primary_candidate_bridge",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "latest_decision": {"decision_id": "decision-current"},
            "bridge": {
                "latest_route_action": "advisory_only",
                "primary_candidate_route_action": "hold_current",
                "latest_route_action_differs_from_primary_candidate_route_action": True,
                "portfolio_route_no_order_root_cause": (
                    "decision_route_action_advisory_only_no_order_expected"
                ),
                "primary_candidate_no_order_root_cause": (
                    "primary_candidate_hold_current_zero_delta"
                ),
                "route_root_and_primary_candidate_root_distinct": True,
                "latest_decision_order_expected": False,
                "primary_candidate_order_expected": False,
            },
            "interpretation": {
                "portfolio_route_action_is_not_primary_candidate_root": True,
                "hold_current_zero_delta_explains_primary_directional_no_order": True,
                "not_alpha_or_profitability_evidence": True,
            },
        },
    )

    assert truth["ok"] is True
    assert (
        truth["status"]
        == "verified_recent_directional_no_order_primary_candidate_bridge_density"
    )
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["coverage"]["recent_decision_count"] == 2
    assert truth["coverage"]["latest_primary_candidate_bridge_verified"] is True
    assert truth["coverage"]["latest_bridge_decision_id"] == "decision-current"
    assert (
        truth["coverage"]["latest_bridge_decision_present_in_recent_decisions"]
        is True
    )
    assert truth["coverage"]["historical_primary_candidate_bridge_scope"] == (
        "latest_decision_only"
    )
    assert (
        truth["coverage"][
            "historical_primary_candidate_bridge_available_for_recent_decisions"
        ]
        is False
    )
    assert (
        truth["coverage"]["historical_primary_candidate_bridge_not_claimed"] is True
    )
    assert truth["recent_portfolio_route_roots"]["top_route_action"] == "advisory_only"
    assert truth["latest_bridge"]["primary_candidate_route_action"] == "hold_current"
    assert truth["latest_bridge"]["route_root_and_primary_candidate_root_distinct"] is True
    assert (
        truth["interpretation"][
            "latest_primary_candidate_root_distinct_from_portfolio_route_root"
        ]
        is True
    )
    assert truth["interpretation"]["historical_primary_candidate_bridge_not_claimed"] is True
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True


def test_recent_directional_no_order_primary_candidate_bridge_density_reports_missing_latest_bridge() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_primary_candidate_bridge_density_truth(
        directional_attribution={
            "ok": True,
            "coverage": {"recent_decision_count": 2},
            "recent_decisions": [{"decision_id": "decision-current"}],
        },
        recent_no_order_root_density={
            "status": "verified_recent_directional_no_order_root_cause_density",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 2,
                "no_order_expected_decision_count": 2,
            },
        },
        latest_bridge={
            "status": "missing_latest_directional_primary_candidate_truth",
            "smallest_missing_field": (
                "database_truth.latest_decision.no_trade_attribution."
                "primary_family_candidate_truth"
            ),
            "raw_payload_exposed": False,
        },
    )

    assert truth["ok"] is False
    assert (
        truth["status"]
        == "missing_latest_directional_no_order_primary_candidate_bridge"
    )
    assert truth["smallest_missing_field"] == (
        "database_truth.latest_decision.no_trade_attribution."
        "primary_family_candidate_truth"
    )
    assert truth["raw_payload_exposed"] is False


def test_recent_directional_no_order_bridge_decision_context_verifies_current_bridge() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_bridge_decision_context_truth(
        db={
            "ok": True,
            "latest_decision": {
                "decision_id": "decision-current",
                "created_at": "2026-04-30T23:12:13Z",
                "symbol": "BTC-USDT-SWAP",
                "primary_family": "directional",
                "route_action": "advisory_only",
                "execution_truth_chain": {
                    "status": "verified_no_order_expected",
                    "order_expected": False,
                    "fill_expected": False,
                },
                "no_trade_attribution": {
                    "classification": "no_order_fill_expected_for_latest_decision",
                    "primary_blocker": "candidate_execution_incompatible",
                },
            },
        },
        recent_decision_chain_density={
            "status": "verified_recent_directional_decision_chain_density_no_order_regime",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "decisions_with_no_order_expected": 24,
                "all_recent_decisions_no_order_expected": True,
                "decisions_with_fills": 0,
            },
            "recent_decisions": [{"decision_id": "decision-current"}],
            "latest_filled_decision": {
                "decision_id": None,
                "fill_count": 0,
                "pnl_lifecycle_status": None,
            },
            "interpretation": {
                "waiting_for_executable_directional_episode": True,
            },
        },
        recent_no_order_bridge_density={
            "status": (
                "verified_recent_directional_no_order_primary_candidate_bridge_density"
            ),
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "no_order_expected_decision_count": 24,
                "decisions_with_fills": 0,
                "latest_bridge_decision_id": "decision-current",
                "latest_bridge_decision_present_in_recent_decisions": True,
                "historical_primary_candidate_bridge_scope": "latest_decision_only",
                "historical_primary_candidate_bridge_not_claimed": True,
            },
            "latest_bridge": {
                "decision_id": "decision-current",
                "latest_route_action": "advisory_only",
                "primary_candidate_route_action": "hold_current",
                "portfolio_route_no_order_root_cause": (
                    "decision_route_action_advisory_only_no_order_expected"
                ),
                "primary_candidate_no_order_root_cause": (
                    "primary_candidate_hold_current_zero_delta"
                ),
                "route_root_and_primary_candidate_root_distinct": True,
            },
            "interpretation": {
                "latest_primary_candidate_root_distinct_from_portfolio_route_root": True,
            },
        },
        latest_bridge={
            "status": "verified_latest_directional_no_order_primary_candidate_bridge",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "latest_decision": {"decision_id": "decision-current"},
            "bridge": {
                "latest_route_action": "advisory_only",
                "primary_candidate_route_action": "hold_current",
                "route_root_and_primary_candidate_root_distinct": True,
            },
        },
        decision_lifecycle_provenance_continuity={
            "status": "verified_current_no_order_plus_executable_terminal_no_fill_continuity",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "current_decision": {
                "execution_truth_status": "verified_no_order_expected",
                "fill_feasibility_status": (
                    "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context"
                ),
                "position_lifecycle_status": "no_position_lifecycle_transition_expected",
            },
            "latest_executable_directional_episode": {
                "decision_id": "decision-executable",
                "status": "verified_executable_terminal_order_no_fill_truth",
                "terminal_no_fill_drilldown_status": (
                    "verified_terminal_no_fill_order_state_drilldown"
                ),
            },
            "recent_directional_batch": {
                "status": "verified_directional_episode_no_order_expected",
                "decisions_with_fills": 0,
            },
        },
        decision_lifecycle_execution_science_continuity={
            "status": "verified_no_order_terminal_no_fill_execution_science_continuity",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "latest_decision_fill_feasibility": {
                "status": (
                    "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context"
                )
            },
            "terminal_no_fill_execution_science": {
                "status": (
                    "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
                ),
                "market_fill_feasibility_observable": False,
            },
            "execution_science": {"payload_sequence_status": "sequence_continuous"},
        },
    )

    assert truth["ok"] is True
    assert (
        truth["status"]
        == "verified_recent_directional_no_order_bridge_decision_context"
    )
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["coverage"]["recent_decision_count"] == 24
    assert truth["coverage"]["decisions_with_fills"] == 0
    assert truth["coverage"]["latest_decision_matches_bridge"] is True
    assert truth["current_decision_context"]["route_action"] == "advisory_only"
    assert (
        truth["current_decision_context"]["primary_candidate_route_action"]
        == "hold_current"
    )
    assert (
        truth["chain_context"]["latest_executable_decision_id"]
        == "decision-executable"
    )
    assert truth["interpretation"]["recent_window_no_order_regime"] is True
    assert truth["interpretation"]["no_recent_fills_in_context_window"] is True
    assert truth["interpretation"]["historical_primary_candidate_bridge_not_claimed"] is True
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True


def test_recent_directional_no_order_bridge_decision_context_reports_missing_bridge_density() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_bridge_decision_context_truth(
        db={
            "ok": True,
            "latest_decision": {"decision_id": "decision-current"},
        },
        recent_decision_chain_density={
            "status": "verified_recent_directional_decision_chain_density_no_order_regime",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {"all_recent_decisions_no_order_expected": True},
        },
        recent_no_order_bridge_density={
            "status": "missing_latest_directional_no_order_primary_candidate_bridge",
            "smallest_missing_field": (
                "latest_directional_no_order_primary_candidate_bridge_truth"
            ),
            "raw_payload_exposed": False,
        },
        latest_bridge={
            "status": "verified_latest_directional_no_order_primary_candidate_bridge",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
        },
        decision_lifecycle_provenance_continuity={
            "status": "verified_current_no_order_plus_executable_terminal_no_fill_continuity",
            "smallest_missing_field": None,
        },
        decision_lifecycle_execution_science_continuity={
            "status": "verified_no_order_terminal_no_fill_execution_science_continuity",
            "smallest_missing_field": None,
        },
    )

    assert truth["ok"] is False
    assert (
        truth["status"]
        == "missing_recent_directional_no_order_primary_candidate_bridge_density"
    )
    assert (
        truth["smallest_missing_field"]
        == "latest_directional_no_order_primary_candidate_bridge_truth"
    )
    assert truth["raw_payload_exposed"] is False


def test_latest_directional_no_order_candidate_drilldown_verifies_context() -> None:
    mod = load_module()

    truth = mod.summarize_latest_directional_no_order_candidate_drilldown_truth(
        db={
            "ok": True,
            "latest_decision": {
                "decision_id": "decision-current",
                "created_at": "2026-05-01T01:35:45Z",
                "symbol": "BTC-USDT-SWAP",
                "primary_family": "directional",
                "route_action": "advisory_only",
                "execution_truth_chain": {
                    "order_expected": False,
                    "fill_expected": False,
                },
                "no_trade_attribution": {
                    "classification": "no_order_fill_expected_for_latest_decision",
                    "primary_blocker": "candidate_execution_incompatible",
                    "final_blockers": [
                        "candidate_execution_incompatible",
                        "composed_as_advisory_only",
                    ],
                    "contributing_factors": ["candidate_signal_below_threshold"],
                    "candidate_execution_drilldown": [
                        {
                            "family": "directional",
                            "route_action": "hold_current",
                            "target_notional": "0",
                            "reason_codes": ["hold_current_zero_delta"],
                            "execution": {
                                "approved_for_execution": True,
                                "execution_compatible": True,
                                "execution_behavior": "hold_current",
                                "permission_mode": "approved",
                                "legs_count": 0,
                            },
                            "permission": {
                                "approved_for_execution": True,
                                "candidate_execution_compatible": True,
                                "permission_mode": "approved",
                            },
                            "composition": {
                                "route_action": "hold_current",
                                "execution_behavior": "hold_current",
                                "requested_delta_position_qty": "0",
                                "composed_delta_position_qty": "0",
                            },
                            "budget": {"effective_scale": "1.0"},
                            "permission_root_cause": {
                                "primary": "primary_candidate_hold_current_zero_delta"
                            },
                        }
                    ],
                    "primary_family_candidate_truth": {
                        "status": (
                            "verified_primary_candidate_hold_current_zero_delta_no_order_expected"
                        ),
                        "smallest_missing_field": None,
                        "primary_family": "directional",
                        "candidate_route_action": "hold_current",
                        "candidate_execution_behavior": "hold_current",
                        "candidate_approved_for_execution": True,
                        "candidate_execution_compatible": True,
                        "candidate_permission_mode": "approved",
                        "order_expected_from_primary_candidate": False,
                        "no_order_root_cause": "primary_candidate_hold_current_zero_delta",
                        "global_blocker_scope": "other_candidate_or_portfolio_level",
                        "global_primary_blocker_applies_to_candidate": False,
                    },
                },
            },
        },
        latest_bridge={
            "status": "verified_latest_directional_no_order_primary_candidate_bridge",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "bridge": {
                "primary_candidate_route_action": "hold_current",
            },
            "primary_candidate": {"family": "directional"},
        },
    )

    assert truth["ok"] is True
    assert (
        truth["status"]
        == "verified_latest_directional_no_order_candidate_drilldown_context"
    )
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["coverage"]["final_blocker_count"] == 2
    assert truth["coverage"]["candidate_drilldown_count"] == 1
    assert truth["coverage"]["primary_candidate_drilldown_present"] is True
    assert truth["primary_candidate_drilldown"]["approved_for_execution"] is True
    assert truth["primary_candidate_drilldown"]["execution_compatible"] is True
    assert truth["primary_candidate_drilldown"]["zero_delta"] is True
    assert truth["primary_candidate_drilldown"]["legs_count"] == 0
    assert truth["primary_candidate_truth"]["no_order_root_cause"] == (
        "primary_candidate_hold_current_zero_delta"
    )
    assert (
        truth["interpretation"][
            "final_blockers_include_candidate_execution_incompatible"
        ]
        is True
    )
    assert truth["interpretation"]["primary_drilldown_zero_delta_no_legs"] is True
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True


def test_latest_directional_no_order_candidate_drilldown_reports_missing_drilldown() -> None:
    mod = load_module()

    truth = mod.summarize_latest_directional_no_order_candidate_drilldown_truth(
        db={
            "ok": True,
            "latest_decision": {
                "decision_id": "decision-current",
                "primary_family": "directional",
                "execution_truth_chain": {"order_expected": False},
                "no_trade_attribution": {
                    "classification": "no_order_fill_expected_for_latest_decision",
                    "final_blockers": ["candidate_execution_incompatible"],
                    "candidate_execution_drilldown": [],
                    "primary_family_candidate_truth": {
                        "primary_family": "directional",
                        "smallest_missing_field": None,
                        "order_expected_from_primary_candidate": False,
                        "no_order_root_cause": "primary_candidate_hold_current_zero_delta",
                    },
                },
            },
        },
        latest_bridge={
            "status": "verified_latest_directional_no_order_primary_candidate_bridge",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
        },
    )

    assert truth["ok"] is False
    assert truth["status"] == "missing_latest_no_order_candidate_execution_drilldown"
    assert truth["smallest_missing_field"] == (
        "database_truth.latest_decision.no_trade_attribution."
        "candidate_execution_drilldown"
    )
    assert truth["raw_payload_exposed"] is False


def test_recent_no_order_candidate_drilldown_context_verifies_current_context() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_candidate_drilldown_context_truth(
        recent_bridge_context={
            "status": "verified_recent_directional_no_order_bridge_decision_context",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "no_order_expected_decision_count": 24,
                "decisions_with_fills": 0,
                "latest_decision_id": "decision-current",
                "historical_primary_candidate_bridge_scope": "latest_decision_only",
                "historical_primary_candidate_bridge_not_claimed": True,
            },
            "current_decision_context": {
                "decision_id": "decision-current",
                "latest_bridge_route_action": "advisory_only",
                "primary_candidate_route_action": "hold_current",
                "primary_candidate_no_order_root_cause": (
                    "primary_candidate_hold_current_zero_delta"
                ),
                "route_root_and_primary_candidate_root_distinct": True,
            },
            "chain_context": {
                "latest_executable_decision_id": "decision-executable",
                "terminal_pretrade_status": (
                    "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
                ),
                "payload_sequence_status": "sequence_continuous",
            },
            "interpretation": {
                "recent_window_no_order_regime": True,
                "not_alpha_or_profitability_evidence": True,
            },
        },
        latest_candidate_drilldown={
            "status": "verified_latest_directional_no_order_candidate_drilldown_context",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "latest_decision_id": "decision-current",
                "candidate_drilldown_count": 1,
                "primary_candidate_drilldown_present": True,
                "final_blocker_count": 4,
            },
            "latest_decision": {
                "route_action": "advisory_only",
                "no_trade_primary_blocker": "candidate_execution_incompatible",
                "final_blockers": ["candidate_execution_incompatible"],
            },
            "primary_candidate_drilldown": {
                "family": "directional",
                "route_action": "hold_current",
                "execution_behavior": "hold_current",
                "legs_count": 0,
                "zero_delta": True,
            },
            "primary_candidate_truth": {
                "no_order_root_cause": "primary_candidate_hold_current_zero_delta",
                "no_order_semantic_status": (
                    "verified_primary_candidate_no_order_expected_semantics"
                ),
            },
            "interpretation": {
                "primary_drilldown_zero_delta_no_legs": True,
                "primary_drilldown_approved_and_compatible": True,
                "final_blocker_may_be_global_or_portfolio_level": True,
                "not_alpha_or_profitability_evidence": True,
            },
        },
    )

    assert truth["ok"] is True
    assert (
        truth["status"]
        == "verified_recent_directional_no_order_candidate_drilldown_context"
    )
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["coverage"]["recent_decision_count"] == 24
    assert truth["coverage"]["decisions_with_fills"] == 0
    assert truth["coverage"]["latest_decision_matches_candidate_drilldown"] is True
    assert truth["coverage"]["candidate_drilldown_count"] == 1
    assert truth["coverage"]["historical_candidate_drilldown_scope"] == (
        "latest_decision_only"
    )
    assert truth["coverage"]["historical_candidate_drilldown_not_claimed"] is True
    assert truth["current_decision_context"]["primary_candidate_route_action"] == (
        "hold_current"
    )
    assert truth["current_decision_context"]["primary_drilldown_zero_delta_no_legs"] is True
    assert truth["chain_context"]["payload_sequence_status"] == "sequence_continuous"
    assert truth["interpretation"]["latest_drilldown_scope_only"] is True
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True


def test_recent_no_order_candidate_drilldown_context_reports_missing_drilldown() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_candidate_drilldown_context_truth(
        recent_bridge_context={
            "status": "verified_recent_directional_no_order_bridge_decision_context",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "latest_decision_id": "decision-current",
                "decisions_with_fills": 0,
            },
        },
        latest_candidate_drilldown={
            "status": "missing_latest_no_order_candidate_execution_drilldown",
            "smallest_missing_field": (
                "database_truth.latest_decision.no_trade_attribution."
                "candidate_execution_drilldown"
            ),
            "raw_payload_exposed": False,
        },
    )

    assert truth["ok"] is False
    assert (
        truth["status"]
        == "missing_latest_directional_no_order_candidate_drilldown_context"
    )
    assert truth["smallest_missing_field"] == (
        "database_truth.latest_decision.no_trade_attribution."
        "candidate_execution_drilldown"
    )
    assert truth["raw_payload_exposed"] is False


def test_recent_no_order_provenance_density_gate_verifies_current_context() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_provenance_density_gate_truth(
        recent_candidate_drilldown_context={
            "status": "verified_recent_directional_no_order_candidate_drilldown_context",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "no_order_expected_decision_count": 24,
                "decisions_with_fills": 0,
                "candidate_drilldown_count": 1,
                "latest_decision_id": "decision-current",
                "historical_candidate_drilldown_scope": "latest_decision_only",
                "historical_candidate_drilldown_not_claimed": True,
            },
            "current_decision_context": {
                "latest_route_action": "advisory_only",
                "primary_candidate_route_action": "hold_current",
                "primary_candidate_no_order_root_cause": (
                    "primary_candidate_hold_current_zero_delta"
                ),
                "primary_candidate_semantic_status": (
                    "verified_primary_candidate_no_order_expected_semantics"
                ),
                "primary_drilldown_zero_delta_no_legs": True,
            },
            "chain_context": {
                "terminal_pretrade_status": (
                    "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
                ),
                "payload_sequence_status": "sequence_continuous",
            },
            "interpretation": {
                "recent_window_no_order_regime": True,
                "no_recent_fills_in_context_window": True,
                "latest_drilldown_scope_only": True,
            },
        },
        decision_lifecycle_provenance_continuity={
            "status": "verified_current_no_order_plus_executable_terminal_no_fill_continuity",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "current_decision": {
                "decision_id": "decision-current",
                "order_expected": False,
                "fill_expected": False,
                "execution_truth_status": "verified_no_order_expected",
            },
            "latest_executable_directional_episode": {
                "decision_id": "decision-executable",
                "status": "verified_executable_terminal_order_no_fill_truth",
            },
            "recent_directional_batch": {
                "all_recent_decisions_no_order_expected": True,
            },
        },
        decision_lifecycle_execution_science_continuity={
            "status": "verified_no_order_terminal_no_fill_execution_science_continuity",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "lifecycle_provenance": {
                "current_decision_id": "decision-current",
                "current_order_expected": False,
                "current_fill_expected": False,
                "executable_decision_id": "decision-executable",
            },
            "terminal_no_fill_execution_science": {
                "status": (
                    "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
                ),
                "snapshot_diff_sequence_status": "sequence_continuous",
                "local_fill_feasibility_status": "terminal_no_fill_before_exchange_ack",
                "market_fill_feasibility_observable": False,
            },
            "orderbook_payload_depth": {
                "status": "verified_books5_payload_depth_evidence_present",
            },
            "execution_science": {
                "payload_sequence_status": "sequence_continuous",
            },
            "slippage_cost_calibration": {
                "status": "verified_slippage_cost_calibration_evidence_present",
            },
        },
    )

    assert truth["ok"] is True
    assert (
        truth["status"]
        == "verified_recent_directional_no_order_provenance_density_gate"
    )
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["coverage"]["recent_decision_count"] == 24
    assert truth["coverage"]["no_order_expected_decision_count"] == 24
    assert truth["coverage"]["decisions_with_fills"] == 0
    assert truth["coverage"]["latest_decision_ids_consistent"] is True
    assert truth["coverage"]["all_recent_decisions_no_order_expected"] is True
    assert truth["current_decision"]["order_expected"] is False
    assert truth["current_decision"]["primary_candidate_route_action"] == "hold_current"
    assert truth["executable_episode"]["decision_id"] == "decision-executable"
    assert truth["executable_episode"]["payload_sequence_status"] == "sequence_continuous"
    assert truth["interpretation"]["gate_verified"] is True
    assert truth["interpretation"]["latest_drilldown_scope_only"] is True
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True


def test_recent_no_order_freshness_truth_verifies_fresh_current_decision() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_freshness_truth(
        db={
            "latest_decision": {
                "decision_id": "decision-current",
                "created_at": "2026-05-01T14:00:00+00:00",
                "route_action": "advisory_only",
                "execution_truth_chain": {
                    "status": "verified_no_order_expected",
                    "order_expected": False,
                    "fill_expected": False,
                },
            },
        },
        recent_no_order_provenance_density_gate={
            "status": "verified_recent_directional_no_order_provenance_density_gate",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "latest_decision_id": "decision-current",
                "recent_decision_count": 24,
                "no_order_expected_decision_count": 24,
                "decisions_with_fills": 0,
                "no_recent_fills": True,
            },
            "current_decision": {
                "execution_truth_status": "verified_no_order_expected",
            },
            "executable_episode": {
                "payload_sequence_status": "sequence_continuous",
            },
            "interpretation": {"gate_verified": True},
        },
        microstructure_runtime_growth={
            "status": "verified_microstructure_runtime_growth",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "collector": {
                "running": True,
                "healthy": True,
                "heartbeat_fresh": True,
                "heartbeat_age_seconds": 4,
            },
            "payload_sequence": {
                "status": "sequence_continuous",
                "sequence_gap_count": 0,
            },
            "silver_workflow": {
                "status": "latest_done_recent",
                "latest_age_seconds": 300,
            },
        },
        report_generated_at="2026-05-01T14:05:00Z",
    )

    assert truth["ok"] is True
    assert truth["status"] == "verified_recent_directional_no_order_freshness_truth"
    assert truth["smallest_missing_field"] is None
    assert truth["raw_payload_exposed"] is False
    assert truth["freshness"]["latest_decision_age_seconds"] == 300
    assert truth["freshness"]["latest_decision_recent"] is True
    assert truth["provenance_gate"]["latest_decision_ids_consistent"] is True
    assert truth["microstructure"]["verified"] is True
    assert truth["interpretation"]["not_alpha_or_profitability_evidence"] is True


def test_recent_no_order_freshness_truth_reports_stale_latest_decision() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_freshness_truth(
        db={
            "latest_decision": {
                "decision_id": "decision-current",
                "created_at": "2026-05-01T13:00:00+00:00",
                "route_action": "advisory_only",
                "execution_truth_chain": {"status": "verified_no_order_expected"},
            },
        },
        recent_no_order_provenance_density_gate={
            "status": "verified_recent_directional_no_order_provenance_density_gate",
            "smallest_missing_field": None,
            "coverage": {
                "latest_decision_id": "decision-current",
                "decisions_with_fills": 0,
                "no_recent_fills": True,
            },
            "interpretation": {"gate_verified": True},
        },
        microstructure_runtime_growth={
            "status": "verified_microstructure_runtime_growth",
            "smallest_missing_field": None,
            "collector": {"heartbeat_fresh": True},
        },
        report_generated_at="2026-05-01T14:05:00Z",
    )

    assert truth["ok"] is False
    assert truth["status"] == "latest_decision_stale_for_recent_no_order_freshness"
    assert truth["smallest_missing_field"] == "database_truth.latest_decision.created_at"
    assert truth["freshness"]["latest_decision_age_seconds"] == 3900
    assert truth["freshness"]["latest_decision_recent"] is False


def test_recent_no_order_provenance_density_gate_reports_decision_mismatch() -> None:
    mod = load_module()

    truth = mod.summarize_recent_directional_no_order_provenance_density_gate_truth(
        recent_candidate_drilldown_context={
            "status": "verified_recent_directional_no_order_candidate_drilldown_context",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "no_order_expected_decision_count": 24,
                "decisions_with_fills": 0,
                "latest_decision_id": "decision-current",
            },
            "chain_context": {
                "terminal_pretrade_status": (
                    "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
                ),
                "payload_sequence_status": "sequence_continuous",
            },
            "interpretation": {
                "recent_window_no_order_regime": True,
                "no_recent_fills_in_context_window": True,
            },
        },
        decision_lifecycle_provenance_continuity={
            "status": "verified_current_no_order_plus_executable_terminal_no_fill_continuity",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "current_decision": {
                "decision_id": "decision-other",
                "order_expected": False,
                "fill_expected": False,
            },
        },
        decision_lifecycle_execution_science_continuity={
            "status": "verified_no_order_terminal_no_fill_execution_science_continuity",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "lifecycle_provenance": {
                "current_decision_id": "decision-current",
                "current_order_expected": False,
                "current_fill_expected": False,
            },
        },
    )

    assert truth["ok"] is False
    assert truth["status"] == (
        "recent_no_order_provenance_latest_decision_identity_mismatch"
    )
    assert truth["smallest_missing_field"] == (
        "recent_directional_no_order_candidate_drilldown_context_truth."
        "coverage.latest_decision_id/decision_lifecycle_current_decision_id"
    )
    assert truth["raw_payload_exposed"] is False


def test_project_live_runtime_facts_exposes_decision_lifecycle_execution_science_continuity() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "decision_lifecycle_execution_science_continuity_truth": {
            "status": "verified_no_order_terminal_no_fill_execution_science_continuity",
            "smallest_missing_field": None,
            "lifecycle_provenance": {
                "current_decision_id": "decision_latest",
                "current_order_expected": False,
                "current_fill_expected": False,
                "executable_decision_id": "decision_exec",
            },
            "latest_decision_fill_feasibility": {
                "status": "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context",
            },
            "terminal_no_fill_execution_science": {
                "status": "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown",
                "snapshot_diff_sequence_status": "sequence_continuous",
                "local_fill_feasibility_status": "terminal_no_fill_before_exchange_ack",
                "market_fill_feasibility_observable": False,
            },
            "orderbook_payload_depth": {
                "status": "verified_books5_payload_depth_evidence_present",
                "books5_sequence_gap_count": 0,
            },
            "depth_slippage_lifecycle": {
                "status": "forward_depth_ready_no_order_expected_regime",
            },
            "execution_science": {
                "payload_sequence_status": "sequence_continuous",
            },
            "slippage_cost_calibration": {
                "status": "verified_slippage_cost_calibration_evidence_present",
                "slippage_proxy_sample_count": 17,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["decision_lifecycle_execution_science_continuity_status"] == (
        "verified_no_order_terminal_no_fill_execution_science_continuity"
    )
    assert live_facts["decision_lifecycle_execution_science_continuity_smallest_missing_field"] is None
    assert live_facts["decision_lifecycle_execution_science_continuity_current_decision_id"] == (
        "decision_latest"
    )
    assert live_facts["decision_lifecycle_execution_science_continuity_current_order_expected"] is False
    assert live_facts["decision_lifecycle_execution_science_continuity_executable_decision_id"] == (
        "decision_exec"
    )
    assert live_facts["decision_lifecycle_execution_science_continuity_terminal_pretrade_status"] == (
        "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
    )
    assert live_facts["decision_lifecycle_execution_science_continuity_snapshot_sequence_status"] == (
        "sequence_continuous"
    )
    assert live_facts["decision_lifecycle_execution_science_continuity_local_fill_feasibility_status"] == (
        "terminal_no_fill_before_exchange_ack"
    )
    assert live_facts["decision_lifecycle_execution_science_continuity_market_fill_observable"] is False
    assert live_facts["decision_lifecycle_execution_science_continuity_orderbook_depth_status"] == (
        "verified_books5_payload_depth_evidence_present"
    )
    assert live_facts["decision_lifecycle_execution_science_continuity_books5_sequence_gap_count"] == 0
    assert live_facts["decision_lifecycle_execution_science_continuity_latest_fill_feasibility_status"] == (
        "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context"
    )
    assert live_facts["decision_lifecycle_execution_science_continuity_execution_sequence_status"] == (
        "sequence_continuous"
    )
    assert live_facts["decision_lifecycle_execution_science_continuity_slippage_cost_status"] == (
        "verified_slippage_cost_calibration_evidence_present"
    )
    assert live_facts["decision_lifecycle_execution_science_continuity_slippage_proxy_sample_count"] == 17


def test_project_live_runtime_facts_exposes_recent_directional_decision_chain_density() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "recent_directional_decision_chain_density_truth": {
            "status": "verified_recent_directional_decision_chain_density_no_order_regime",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "decisions_with_order_surface_or_no_order_expectation": 24,
                "decisions_missing_order_surface": 0,
                "all_recent_decisions_no_order_expected": True,
                "decisions_with_no_order_semantics": 24,
                "decisions_with_fills": 0,
                "decisions_with_pnl_outcome": 0,
                "filled_decisions_with_resolved_pnl_lifecycle": 0,
                "filled_decisions_with_pretrade_microstructure": 0,
            },
            "interpretation": {
                "waiting_for_executable_directional_episode": True,
                "not_alpha_or_profitability_evidence": True,
            },
            "latest_filled_decision": {
                "decision_id": None,
                "pnl_lifecycle_status": None,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["recent_directional_decision_chain_density_truth_status"] == (
        "verified_recent_directional_decision_chain_density_no_order_regime"
    )
    assert live_facts["recent_directional_decision_chain_density_smallest_missing_field"] is None
    assert live_facts["recent_directional_decision_chain_density_raw_payload_exposed"] is False
    assert live_facts["recent_directional_chain_recent_decision_count"] == 24
    assert live_facts["recent_directional_chain_order_surface_or_no_order_count"] == 24
    assert live_facts["recent_directional_chain_decisions_missing_order_surface"] == 0
    assert live_facts["recent_directional_chain_all_recent_decisions_no_order_expected"] is True
    assert live_facts["recent_directional_chain_decisions_with_no_order_semantics"] == 24
    assert live_facts["recent_directional_chain_decisions_with_fills"] == 0
    assert live_facts["recent_directional_chain_waiting_for_executable_directional_episode"] is True
    assert live_facts["recent_directional_chain_not_alpha_or_profitability_evidence"] is True


def test_project_live_runtime_facts_exposes_recent_no_order_root_density() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "recent_directional_no_order_root_cause_density_truth": {
            "status": "verified_recent_directional_no_order_root_cause_density",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "no_order_expected_decision_count": 24,
                "decisions_missing_no_order_semantics": 0,
                "decisions_missing_root_cause": 0,
                "decisions_missing_root_materiality": 0,
            },
            "top_root_cause": "decision_route_action_advisory_only_no_order_expected",
            "top_equivalence_class": "verified_non_executable_no_order_expected",
            "top_route_action": "advisory_only",
            "distributions": {
                "root_cause": [
                    {
                        "value": "decision_route_action_advisory_only_no_order_expected",
                        "count": 24,
                    }
                ]
            },
            "interpretation": {
                "all_roots_non_material_without_order_or_fill_change": True,
                "all_roots_require_order_or_fill_change_for_materiality": True,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["recent_directional_no_order_root_cause_density_truth_status"] == (
        "verified_recent_directional_no_order_root_cause_density"
    )
    assert (
        live_facts[
            "recent_directional_no_order_root_cause_density_smallest_missing_field"
        ]
        is None
    )
    assert (
        live_facts["recent_directional_no_order_root_cause_density_raw_payload_exposed"]
        is False
    )
    assert live_facts["recent_directional_no_order_root_recent_decision_count"] == 24
    assert (
        live_facts[
            "recent_directional_no_order_root_no_order_expected_decision_count"
        ]
        == 24
    )
    assert (
        live_facts["recent_directional_no_order_root_decisions_missing_root_cause"]
        == 0
    )
    assert live_facts["recent_directional_no_order_root_top_root_cause"] == (
        "decision_route_action_advisory_only_no_order_expected"
    )
    assert live_facts["recent_directional_no_order_root_top_route_action"] == (
        "advisory_only"
    )
    assert (
        live_facts[
            "recent_directional_no_order_root_all_roots_non_material_without_order_or_fill_change"
        ]
        is True
    )
    assert (
        live_facts[
            "recent_directional_no_order_root_requires_order_or_fill_change_for_materiality"
        ]
        is True
    )
    assert live_facts["recent_directional_no_order_root_distribution_root_cause"] == [
        {
            "value": "decision_route_action_advisory_only_no_order_expected",
            "count": 24,
        }
    ]


def test_project_live_runtime_facts_exposes_latest_directional_no_order_primary_candidate_bridge() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "latest_directional_no_order_primary_candidate_bridge_truth": {
            "status": "verified_latest_directional_no_order_primary_candidate_bridge",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "latest_decision": {
                "decision_id": "decision-current",
                "route_action": "advisory_only",
                "order_expected": False,
                "portfolio_route_no_order_root_cause": (
                    "decision_route_action_advisory_only_no_order_expected"
                ),
            },
            "primary_candidate": {
                "route_action": "hold_current",
                "order_expected": False,
                "no_order_root_cause": "primary_candidate_hold_current_zero_delta",
                "no_order_semantic_status": (
                    "verified_primary_candidate_no_order_expected_semantics"
                ),
                "global_blocker_scope": "other_candidate_or_portfolio_level",
            },
            "bridge": {
                "latest_route_action": "advisory_only",
                "primary_candidate_route_action": "hold_current",
                "latest_route_action_differs_from_primary_candidate_route_action": True,
                "portfolio_route_no_order_root_cause": (
                    "decision_route_action_advisory_only_no_order_expected"
                ),
                "primary_candidate_no_order_root_cause": (
                    "primary_candidate_hold_current_zero_delta"
                ),
                "route_root_and_primary_candidate_root_distinct": True,
                "latest_decision_order_expected": False,
                "primary_candidate_order_expected": False,
            },
            "interpretation": {
                "portfolio_route_action_is_not_primary_candidate_root": True,
                "hold_current_zero_delta_explains_primary_directional_no_order": True,
                "advisory_only_route_action_explains_portfolio_route_no_order": True,
                "global_blocker_is_other_candidate_or_portfolio_level": True,
                "not_alpha_or_profitability_evidence": True,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts[
        "latest_directional_no_order_primary_candidate_bridge_truth_status"
    ] == "verified_latest_directional_no_order_primary_candidate_bridge"
    assert (
        live_facts[
            "latest_directional_no_order_primary_candidate_bridge_raw_payload_exposed"
        ]
        is False
    )
    assert live_facts["latest_directional_no_order_bridge_decision_id"] == (
        "decision-current"
    )
    assert live_facts["latest_directional_no_order_bridge_latest_route_action"] == (
        "advisory_only"
    )
    assert live_facts[
        "latest_directional_no_order_bridge_primary_candidate_route_action"
    ] == "hold_current"
    assert live_facts["latest_directional_no_order_bridge_route_action_differs"] is True
    assert live_facts[
        "latest_directional_no_order_bridge_route_root_and_primary_candidate_root_distinct"
    ] is True
    assert live_facts[
        "latest_directional_no_order_bridge_primary_candidate_no_order_root_cause"
    ] == "primary_candidate_hold_current_zero_delta"
    assert live_facts[
        "latest_directional_no_order_bridge_portfolio_route_action_is_not_primary_candidate_root"
    ] is True
    assert live_facts[
        "latest_directional_no_order_bridge_hold_current_zero_delta_explains_primary_directional_no_order"
    ] is True
    assert live_facts[
        "latest_directional_no_order_bridge_advisory_only_route_action_explains_portfolio_route_no_order"
    ] is True
    assert live_facts[
        "latest_directional_no_order_bridge_not_alpha_or_profitability_evidence"
    ] is True


def test_project_live_runtime_facts_exposes_latest_no_order_candidate_drilldown() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "latest_directional_no_order_candidate_drilldown_truth": {
            "status": "verified_latest_directional_no_order_candidate_drilldown_context",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "latest_decision_id": "decision-current",
                "final_blocker_count": 4,
                "candidate_drilldown_count": 1,
                "primary_candidate_drilldown_present": True,
                "latest_order_expected": False,
                "primary_candidate_order_expected": False,
            },
            "latest_decision": {
                "route_action": "advisory_only",
                "no_trade_primary_blocker": "candidate_execution_incompatible",
                "final_blockers": [
                    "candidate_execution_incompatible",
                    "composed_as_advisory_only",
                ],
            },
            "primary_candidate_drilldown": {
                "family": "directional",
                "route_action": "hold_current",
                "execution_behavior": "hold_current",
                "approved_for_execution": True,
                "execution_compatible": True,
                "legs_count": 0,
                "zero_delta": True,
            },
            "primary_candidate_truth": {
                "no_order_root_cause": "primary_candidate_hold_current_zero_delta",
                "no_order_semantic_status": (
                    "verified_primary_candidate_no_order_expected_semantics"
                ),
                "global_blocker_scope": "other_candidate_or_portfolio_level",
            },
            "interpretation": {
                "primary_drilldown_zero_delta_no_legs": True,
                "final_blocker_may_be_global_or_portfolio_level": True,
                "not_alpha_or_profitability_evidence": True,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts[
        "latest_directional_no_order_candidate_drilldown_truth_status"
    ] == "verified_latest_directional_no_order_candidate_drilldown_context"
    assert (
        live_facts["latest_directional_no_order_candidate_drilldown_raw_payload_exposed"]
        is False
    )
    assert (
        live_facts[
            "latest_directional_no_order_candidate_drilldown_final_blocker_count"
        ]
        == 4
    )
    assert live_facts["latest_directional_no_order_candidate_drilldown_candidate_count"] == 1
    assert live_facts[
        "latest_directional_no_order_candidate_drilldown_primary_present"
    ] is True
    assert live_facts[
        "latest_directional_no_order_candidate_drilldown_primary_route_action"
    ] == "hold_current"
    assert live_facts[
        "latest_directional_no_order_candidate_drilldown_primary_execution_behavior"
    ] == "hold_current"
    assert live_facts[
        "latest_directional_no_order_candidate_drilldown_primary_approved_for_execution"
    ] is True
    assert live_facts[
        "latest_directional_no_order_candidate_drilldown_primary_execution_compatible"
    ] is True
    assert (
        live_facts[
            "latest_directional_no_order_candidate_drilldown_primary_no_order_root_cause"
        ]
        == "primary_candidate_hold_current_zero_delta"
    )
    assert live_facts[
        "latest_directional_no_order_candidate_drilldown_zero_delta_no_legs"
    ] is True
    assert live_facts[
        "latest_directional_no_order_candidate_drilldown_final_blocker_global_or_portfolio"
    ] is True
    assert live_facts[
        "latest_directional_no_order_candidate_drilldown_not_alpha_or_profitability_evidence"
    ] is True


def test_project_live_runtime_facts_exposes_recent_no_order_primary_candidate_bridge_density() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "recent_directional_no_order_primary_candidate_bridge_density_truth": {
            "status": (
                "verified_recent_directional_no_order_primary_candidate_bridge_density"
            ),
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "no_order_expected_decision_count": 24,
                "decisions_with_no_order_semantics": 24,
                "recent_portfolio_route_root_density_status": (
                    "verified_recent_directional_no_order_root_cause_density"
                ),
                "latest_primary_candidate_bridge_status": (
                    "verified_latest_directional_no_order_primary_candidate_bridge"
                ),
                "latest_primary_candidate_bridge_verified": True,
                "latest_bridge_decision_id": "decision-current",
                "latest_bridge_decision_present_in_recent_decisions": True,
                "historical_primary_candidate_bridge_scope": "latest_decision_only",
                "historical_primary_candidate_bridge_available_for_recent_decisions": False,
                "historical_primary_candidate_bridge_not_claimed": True,
            },
            "recent_portfolio_route_roots": {
                "top_root_cause": "decision_route_action_advisory_only_no_order_expected",
                "top_route_action": "advisory_only",
                "root_cause_distribution": [
                    {
                        "value": "decision_route_action_advisory_only_no_order_expected",
                        "count": 24,
                    }
                ],
            },
            "latest_bridge": {
                "latest_route_action": "advisory_only",
                "primary_candidate_route_action": "hold_current",
                "route_action_differs": True,
                "portfolio_route_no_order_root_cause": (
                    "decision_route_action_advisory_only_no_order_expected"
                ),
                "primary_candidate_no_order_root_cause": (
                    "primary_candidate_hold_current_zero_delta"
                ),
                "route_root_and_primary_candidate_root_distinct": True,
            },
            "interpretation": {
                "recent_portfolio_route_roots_non_material_without_order_or_fill_change": True,
                "recent_portfolio_route_roots_require_order_or_fill_change_for_materiality": True,
                "latest_primary_candidate_root_distinct_from_portfolio_route_root": True,
                "not_alpha_or_profitability_evidence": True,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts[
        "recent_directional_no_order_primary_candidate_bridge_density_truth_status"
    ] == "verified_recent_directional_no_order_primary_candidate_bridge_density"
    assert (
        live_facts[
            "recent_directional_no_order_primary_candidate_bridge_density_raw_payload_exposed"
        ]
        is False
    )
    assert (
        live_facts["recent_directional_no_order_bridge_density_recent_decision_count"]
        == 24
    )
    assert (
        live_facts[
            "recent_directional_no_order_bridge_density_latest_bridge_verified"
        ]
        is True
    )
    assert live_facts[
        "recent_directional_no_order_bridge_density_latest_bridge_decision_id"
    ] == "decision-current"
    assert (
        live_facts[
            "recent_directional_no_order_bridge_density_latest_bridge_decision_present_in_recent"
        ]
        is True
    )
    assert live_facts[
        "recent_directional_no_order_bridge_density_historical_primary_candidate_bridge_scope"
    ] == "latest_decision_only"
    assert (
        live_facts[
            "recent_directional_no_order_bridge_density_historical_primary_candidate_bridge_not_claimed"
        ]
        is True
    )
    assert live_facts[
        "recent_directional_no_order_bridge_density_top_portfolio_route_action"
    ] == "advisory_only"
    assert live_facts[
        "recent_directional_no_order_bridge_density_primary_candidate_route_action"
    ] == "hold_current"
    assert (
        live_facts["recent_directional_no_order_bridge_density_route_roots_distinct"]
        is True
    )
    assert (
        live_facts[
            "recent_directional_no_order_bridge_density_not_alpha_or_profitability_evidence"
        ]
        is True
    )


def test_project_live_runtime_facts_exposes_recent_no_order_bridge_decision_context() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "recent_directional_no_order_bridge_decision_context_truth": {
            "status": "verified_recent_directional_no_order_bridge_decision_context",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "no_order_expected_decision_count": 24,
                "decisions_with_fills": 0,
                "recent_decision_chain_density_status": (
                    "verified_recent_directional_decision_chain_density_no_order_regime"
                ),
                "recent_bridge_density_status": (
                    "verified_recent_directional_no_order_primary_candidate_bridge_density"
                ),
                "decision_lifecycle_provenance_status": (
                    "verified_current_no_order_plus_executable_terminal_no_fill_continuity"
                ),
                "decision_lifecycle_execution_science_status": (
                    "verified_no_order_terminal_no_fill_execution_science_continuity"
                ),
                "latest_decision_id": "decision-current",
                "latest_decision_matches_bridge": True,
                "historical_primary_candidate_bridge_scope": "latest_decision_only",
                "historical_primary_candidate_bridge_not_claimed": True,
            },
            "current_decision_context": {
                "latest_bridge_route_action": "advisory_only",
                "primary_candidate_route_action": "hold_current",
                "route_root_and_primary_candidate_root_distinct": True,
            },
            "chain_context": {
                "latest_executable_decision_id": "decision-executable",
                "terminal_pretrade_status": (
                    "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
                ),
                "payload_sequence_status": "sequence_continuous",
            },
            "interpretation": {
                "waiting_for_executable_directional_episode": True,
                "no_recent_fills_in_context_window": True,
                "not_alpha_or_profitability_evidence": True,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts[
        "recent_directional_no_order_bridge_decision_context_truth_status"
    ] == "verified_recent_directional_no_order_bridge_decision_context"
    assert (
        live_facts[
            "recent_directional_no_order_bridge_decision_context_raw_payload_exposed"
        ]
        is False
    )
    assert (
        live_facts["recent_directional_no_order_bridge_context_recent_decision_count"]
        == 24
    )
    assert (
        live_facts["recent_directional_no_order_bridge_context_decisions_with_fills"]
        == 0
    )
    assert (
        live_facts[
            "recent_directional_no_order_bridge_context_latest_decision_matches_bridge"
        ]
        is True
    )
    assert live_facts[
        "recent_directional_no_order_bridge_context_latest_route_action"
    ] == "advisory_only"
    assert live_facts[
        "recent_directional_no_order_bridge_context_primary_candidate_route_action"
    ] == "hold_current"
    assert (
        live_facts[
            "recent_directional_no_order_bridge_context_route_roots_distinct"
        ]
        is True
    )
    assert live_facts[
        "recent_directional_no_order_bridge_context_latest_executable_decision_id"
    ] == "decision-executable"
    assert live_facts[
        "recent_directional_no_order_bridge_context_payload_sequence_status"
    ] == "sequence_continuous"
    assert (
        live_facts[
            "recent_directional_no_order_bridge_context_historical_primary_candidate_bridge_scope"
        ]
        == "latest_decision_only"
    )
    assert (
        live_facts[
            "recent_directional_no_order_bridge_context_waiting_for_executable_directional_episode"
        ]
        is True
    )
    assert (
        live_facts[
            "recent_directional_no_order_bridge_context_not_alpha_or_profitability_evidence"
        ]
        is True
    )


def test_project_live_runtime_facts_exposes_recent_no_order_candidate_drilldown_context() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "recent_directional_no_order_candidate_drilldown_context_truth": {
            "status": "verified_recent_directional_no_order_candidate_drilldown_context",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "no_order_expected_decision_count": 24,
                "decisions_with_fills": 0,
                "recent_bridge_context_status": (
                    "verified_recent_directional_no_order_bridge_decision_context"
                ),
                "latest_candidate_drilldown_status": (
                    "verified_latest_directional_no_order_candidate_drilldown_context"
                ),
                "latest_decision_id": "decision-current",
                "latest_candidate_drilldown_decision_id": "decision-current",
                "latest_decision_matches_candidate_drilldown": True,
                "candidate_drilldown_count": 1,
                "primary_candidate_drilldown_present": True,
                "final_blocker_count": 4,
                "historical_candidate_drilldown_scope": "latest_decision_only",
                "historical_candidate_drilldown_not_claimed": True,
            },
            "current_decision_context": {
                "latest_route_action": "advisory_only",
                "primary_candidate_route_action": "hold_current",
                "primary_candidate_no_order_root_cause": (
                    "primary_candidate_hold_current_zero_delta"
                ),
                "primary_candidate_semantic_status": (
                    "verified_primary_candidate_no_order_expected_semantics"
                ),
                "primary_candidate_zero_delta": True,
                "primary_candidate_legs_count": 0,
                "primary_drilldown_zero_delta_no_legs": True,
            },
            "chain_context": {
                "terminal_pretrade_status": (
                    "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
                ),
                "payload_sequence_status": "sequence_continuous",
            },
            "interpretation": {
                "latest_drilldown_scope_only": True,
                "no_recent_fills_in_context_window": True,
                "not_alpha_or_profitability_evidence": True,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts[
        "recent_directional_no_order_candidate_drilldown_context_truth_status"
    ] == "verified_recent_directional_no_order_candidate_drilldown_context"
    assert (
        live_facts[
            "recent_directional_no_order_candidate_drilldown_context_raw_payload_exposed"
        ]
        is False
    )
    assert (
        live_facts[
            "recent_directional_no_order_candidate_drilldown_context_recent_decision_count"
        ]
        == 24
    )
    assert (
        live_facts[
            "recent_directional_no_order_candidate_drilldown_context_decisions_with_fills"
        ]
        == 0
    )
    assert live_facts[
        "recent_directional_no_order_candidate_drilldown_context_latest_decision_matches_drilldown"
    ] is True
    assert (
        live_facts[
            "recent_directional_no_order_candidate_drilldown_context_candidate_count"
        ]
        == 1
    )
    assert live_facts[
        "recent_directional_no_order_candidate_drilldown_context_historical_drilldown_scope"
    ] == "latest_decision_only"
    assert live_facts[
        "recent_directional_no_order_candidate_drilldown_context_historical_drilldown_not_claimed"
    ] is True
    assert live_facts[
        "recent_directional_no_order_candidate_drilldown_context_primary_candidate_route_action"
    ] == "hold_current"
    assert live_facts[
        "recent_directional_no_order_candidate_drilldown_context_primary_zero_delta_no_legs"
    ] is True
    assert live_facts[
        "recent_directional_no_order_candidate_drilldown_context_latest_scope_only"
    ] is True
    assert live_facts[
        "recent_directional_no_order_candidate_drilldown_context_payload_sequence_status"
    ] == "sequence_continuous"
    assert live_facts[
        "recent_directional_no_order_candidate_drilldown_context_not_alpha_or_profitability_evidence"
    ] is True


def test_project_live_runtime_facts_exposes_recent_no_order_provenance_density_gate() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {
                "decision_id": "decision-current",
                "created_at": "2026-05-01T14:00:00+00:00",
            },
            "latest_executable_directional_decision": {},
        },
        "recent_directional_no_order_provenance_density_gate_truth": {
            "status": "verified_recent_directional_no_order_provenance_density_gate",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "coverage": {
                "recent_decision_count": 24,
                "no_order_expected_decision_count": 24,
                "decisions_with_fills": 0,
                "candidate_drilldown_count": 1,
                "latest_decision_id": "decision-current",
                "lifecycle_decision_id": "decision-current",
                "execution_science_decision_id": "decision-current",
                "latest_decision_ids_consistent": True,
                "all_recent_decisions_no_order_expected": True,
                "no_recent_fills": True,
                "recent_candidate_drilldown_context_status": (
                    "verified_recent_directional_no_order_candidate_drilldown_context"
                ),
                "decision_lifecycle_provenance_status": (
                    "verified_current_no_order_plus_executable_terminal_no_fill_continuity"
                ),
                "decision_lifecycle_execution_science_status": (
                    "verified_no_order_terminal_no_fill_execution_science_continuity"
                ),
                "historical_candidate_drilldown_scope": "latest_decision_only",
                "historical_candidate_drilldown_not_claimed": True,
            },
            "current_decision": {
                "order_expected": False,
                "fill_expected": False,
                "execution_truth_status": "verified_no_order_expected",
                "primary_candidate_route_action": "hold_current",
                "primary_drilldown_zero_delta_no_legs": True,
            },
            "executable_episode": {
                "decision_id": "decision-executable",
                "terminal_pretrade_status": (
                    "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
                ),
                "payload_sequence_status": "sequence_continuous",
                "local_fill_feasibility_status": "terminal_no_fill_before_exchange_ack",
                "market_fill_feasibility_observable": False,
            },
            "interpretation": {
                "gate_verified": True,
                "latest_drilldown_scope_only": True,
                "payload_sequence_continuous": True,
                "not_alpha_or_profitability_evidence": True,
            },
        },
        "recent_directional_no_order_freshness_truth": {
            "status": "verified_recent_directional_no_order_freshness_truth",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "freshness": {
                "latest_decision_id": "decision-current",
                "latest_decision_created_at": "2026-05-01T14:00:00+00:00",
                "latest_decision_age_seconds": 300,
                "latest_decision_stale_after_seconds": 1800,
                "latest_decision_recent": True,
                "latest_route_action": "advisory_only",
                "latest_execution_truth_status": "verified_no_order_expected",
            },
            "provenance_gate": {
                "status": "verified_recent_directional_no_order_provenance_density_gate",
                "verified": True,
                "recent_decision_count": 24,
                "decisions_with_fills": 0,
                "latest_decision_ids_consistent": True,
            },
            "microstructure": {
                "status": "verified_microstructure_runtime_growth",
                "verified": True,
                "heartbeat_fresh": True,
                "payload_sequence_status": "sequence_continuous",
                "silver_workflow_status": "latest_done_recent",
            },
            "interpretation": {
                "latest_decision_fresh": True,
                "provenance_density_gate_verified": True,
                "microstructure_runtime_fresh": True,
                "not_alpha_or_profitability_evidence": True,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["latest_decision_created_at"] == "2026-05-01T14:00:00+00:00"
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_truth_status"
    ] == "verified_recent_directional_no_order_provenance_density_gate"
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_raw_payload_exposed"
    ] is False
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_recent_decision_count"
    ] == 24
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_decisions_with_fills"
    ] == 0
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_latest_decision_ids_consistent"
    ] is True
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_context_status"
    ] == "verified_recent_directional_no_order_candidate_drilldown_context"
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_lifecycle_status"
    ] == "verified_current_no_order_plus_executable_terminal_no_fill_continuity"
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_execution_science_status"
    ] == "verified_no_order_terminal_no_fill_execution_science_continuity"
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_current_order_expected"
    ] is False
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_primary_candidate_route_action"
    ] == "hold_current"
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_executable_decision_id"
    ] == "decision-executable"
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_terminal_pretrade_status"
    ] == "verified_executable_terminal_no_fill_pretrade_microstructure_drilldown"
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_payload_sequence_status"
    ] == "sequence_continuous"
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_market_fill_observable"
    ] is False
    assert live_facts["recent_directional_no_order_provenance_density_gate_verified"] is True
    assert live_facts[
        "recent_directional_no_order_provenance_density_gate_not_alpha_or_profitability_evidence"
    ] is True
    assert (
        live_facts["recent_directional_no_order_freshness_truth_status"]
        == "verified_recent_directional_no_order_freshness_truth"
    )
    assert live_facts[
        "recent_directional_no_order_freshness_latest_decision_created_at"
    ] == "2026-05-01T14:00:00+00:00"
    assert live_facts[
        "recent_directional_no_order_freshness_latest_decision_age_seconds"
    ] == 300
    assert live_facts[
        "recent_directional_no_order_freshness_latest_decision_ids_consistent"
    ] is True
    assert live_facts["recent_directional_no_order_freshness_verified"] is True
    assert live_facts[
        "recent_directional_no_order_freshness_microstructure_payload_sequence_status"
    ] == "sequence_continuous"


def test_directional_episode_pnl_lifecycle_classifies_open_unrealized_position() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "directional_episode_attribution": {
            "symbol": "BTC-USDT-SWAP",
            "recent_decisions": [
                {
                    "decision_id": "decision_open_position",
                    "symbol": "BTC-USDT-SWAP",
                    "created_at": "2026-04-27T06:34:36+08:00",
                    "route_action": "override_target",
                    "primary_family": "directional",
                    "expected_edge_bps": "-3.27",
                    "expected_cost_bps": "0.94",
                    "order_count": 1,
                    "order_states": "FILLED",
                    "order_position_intents": "open_long",
                    "order_execution_actions": "open",
                    "fill_count": 1,
                    "filled_order_count": 1,
                    "fill_outcome_count": 0,
                    "turnover_usdt": "100",
                    "fee_usdt": "0.05",
                    "actual_fee_bps_sample_count": 1,
                    "actual_fee_bps_mean": "5.0",
                    "realized_slippage_sample_count": 1,
                    "realized_slippage_bps_mean": "-0.79",
                    "slippage_reference_sample_count": 1,
                    "fill_position_intents": "open_long",
                    "filled_order_states": "FILLED",
                    "source_lot_count": 1,
                    "open_source_lot_count": 1,
                    "closed_source_lot_count": 0,
                    "open_source_lot_qty": "0.001",
                    "source_lot_statuses": "OPEN",
                    "source_lot_exposure_sides": "long",
                    "lot_event_count": 1,
                    "lot_open_event_count": 1,
                    "lot_close_event_count": 0,
                    "lot_event_types": "open",
                    "latest_fill_id": "fill_open",
                    "latest_fill_side": "buy",
                    "latest_fill_qty": "0.001",
                    "latest_fill_price": "78813.2",
                    "latest_fill_ingestion_ts": "2026-04-27T06:34:40+08:00",
                    "latest_fill_source_lot_status": "OPEN",
                    "latest_fill_source_lot_open_qty": "0.001",
                    "latest_fill_source_lot_exposure_side": "long",
                    "latest_fill_lot_event_types": "open",
                    "latest_fill_lot_open_event_count": 1,
                    "latest_fill_lot_close_event_count": 0,
                    "payload": {"reason_codes": ["baseline_directional_entry"]},
                }
            ],
        },
    }

    parsed = mod.parse_db_probe(json.dumps(raw))
    summary = mod.summarize_directional_episode_attribution_truth(parsed, {"ok": False})
    latest = summary["latest_filled_decision"]

    assert latest["pnl_outcome"]["realized_pnl_usdt"] is None
    assert latest["pnl_lifecycle"]["status"] == "open_position_not_yet_realized"
    assert latest["pnl_lifecycle"]["smallest_missing_field"] is None
    assert latest["pnl_lifecycle"]["lifecycle_evidence"]["open_source_lot_count"] == 1
    assert summary["pnl_lifecycle"]["status"] == "latest_filled_directional_episode_open_unrealized"
    assert summary["pnl_lifecycle"]["smallest_missing_field"] is None
    assert summary["coverage"]["filled_decisions_with_resolved_pnl_lifecycle"] == 1


def test_directional_episode_pnl_lifecycle_reports_closed_missing_outcome_link() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "directional_episode_attribution": {
            "symbol": "BTC-USDT-SWAP",
            "recent_decisions": [
                {
                    "decision_id": "decision_missing_closed_outcome",
                    "symbol": "BTC-USDT-SWAP",
                    "created_at": "2026-04-27T06:10:00+08:00",
                    "route_action": "override_target",
                    "primary_family": "directional",
                    "expected_edge_bps": "2.0",
                    "expected_cost_bps": "8.0",
                    "order_count": 1,
                    "order_states": "FILLED",
                    "order_position_intents": "close_long",
                    "order_execution_actions": "close",
                    "fill_count": 1,
                    "filled_order_count": 1,
                    "fill_outcome_count": 0,
                    "actual_fee_bps_sample_count": 1,
                    "actual_fee_bps_mean": "5.0",
                    "realized_slippage_sample_count": 1,
                    "realized_slippage_bps_mean": "1.0",
                    "slippage_reference_sample_count": 1,
                    "fill_position_intents": "close_long",
                    "source_lot_count": 0,
                    "lot_event_count": 1,
                    "lot_open_event_count": 0,
                    "lot_close_event_count": 1,
                    "lot_realized_pnl_usdt": "-0.42",
                    "lot_event_types": "close",
                    "latest_fill_id": "fill_close_missing_outcome",
                    "latest_fill_side": "sell",
                    "latest_fill_qty": "0.001",
                    "latest_fill_price": "78700.0",
                    "latest_fill_ingestion_ts": "2026-04-27T06:10:03+08:00",
                    "latest_fill_lot_event_types": "close",
                    "latest_fill_lot_open_event_count": 0,
                    "latest_fill_lot_close_event_count": 1,
                    "latest_fill_lot_realized_pnl_delta": "-0.42",
                    "payload": {"reason_codes": ["reduce_after_loss"]},
                }
            ],
        },
    }

    parsed = mod.parse_db_probe(json.dumps(raw))
    summary = mod.summarize_directional_episode_attribution_truth(parsed, {"ok": False})
    latest = summary["latest_filled_decision"]

    assert latest["pnl_lifecycle"]["status"] == "closed_lifecycle_missing_fill_outcome"
    assert latest["pnl_lifecycle"]["smallest_missing_field"] == "fill_outcomes.realized_pnl_delta"
    assert latest["pnl_lifecycle"]["lifecycle_evidence"]["lot_close_event_count"] == 1
    assert summary["pnl_lifecycle"]["status"] == "missing_directional_episode_pnl_lifecycle_evidence"
    assert summary["pnl_lifecycle"]["smallest_missing_field"] == "fill_outcomes.realized_pnl_delta"
    assert summary["coverage"]["filled_decisions_with_resolved_pnl_lifecycle"] == 0


def test_directional_episode_pnl_lifecycle_does_not_treat_close_intent_as_lot_close() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "directional_episode_attribution": {
            "symbol": "BTC-USDT-SWAP",
            "recent_decisions": [
                {
                    "decision_id": "decision_close_intent_without_projection",
                    "symbol": "BTC-USDT-SWAP",
                    "created_at": "2026-04-27T06:55:00+08:00",
                    "route_action": "override_target",
                    "primary_family": "directional",
                    "expected_edge_bps": "2.0",
                    "expected_cost_bps": "8.0",
                    "order_count": 1,
                    "order_states": "FILLED",
                    "order_position_intents": "close_long",
                    "order_execution_actions": "close",
                    "fill_count": 1,
                    "filled_order_count": 1,
                    "fill_outcome_count": 0,
                    "actual_fee_bps_sample_count": 1,
                    "actual_fee_bps_mean": "5.0",
                    "realized_slippage_sample_count": 1,
                    "realized_slippage_bps_mean": "1.0",
                    "slippage_reference_sample_count": 1,
                    "fill_position_intents": "close_long",
                    "source_lot_count": 0,
                    "lot_event_count": 0,
                    "lot_open_event_count": 0,
                    "lot_close_event_count": 0,
                    "latest_fill_id": "fill_close_intent_without_projection",
                    "latest_fill_side": "sell",
                    "latest_fill_qty": "0.001",
                    "latest_fill_price": "78700.0",
                    "latest_fill_ingestion_ts": "2026-04-27T06:55:03+08:00",
                    "latest_fill_lot_open_event_count": 0,
                    "latest_fill_lot_close_event_count": 0,
                    "payload": {"reason_codes": ["reduce_after_loss"]},
                }
            ],
        },
    }

    parsed = mod.parse_db_probe(json.dumps(raw))
    summary = mod.summarize_directional_episode_attribution_truth(parsed, {"ok": False})
    latest = summary["latest_filled_decision"]

    assert latest["pnl_lifecycle"]["status"] == "close_intent_missing_portfolio_projection"
    assert latest["pnl_lifecycle"]["smallest_missing_field"] == "lot_events.fill_id"
    assert latest["pnl_lifecycle"]["lifecycle_evidence"]["lot_close_event_count"] == 0
    assert summary["pnl_lifecycle"]["status"] == "missing_directional_episode_pnl_lifecycle_evidence"
    assert summary["pnl_lifecycle"]["smallest_missing_field"] == "lot_events.fill_id"
    assert summary["coverage"]["filled_decisions_with_resolved_pnl_lifecycle"] == 0


def test_directional_episode_attribution_truth_reports_missing_microstructure_context() -> None:
    mod = load_module()
    raw = {
        "ok": True,
        "directional_episode_attribution": {
            "symbol": "BTC-USDT-SWAP",
            "recent_decisions": [
                {
                    "allocation_id": "alloc_1",
                    "decision_id": "decision_without_rdp_bar",
                    "symbol": "BTC-USDT-SWAP",
                    "created_at": "2026-04-27T06:00:00+08:00",
                    "route_action": "override_target",
                    "primary_family": "directional",
                    "expected_edge_bps": "12.0",
                    "expected_cost_bps": "8.0",
                    "order_count": 1,
                    "fill_count": 1,
                    "fill_outcome_count": 0,
                    "actual_fee_bps_sample_count": 1,
                    "actual_fee_bps_mean": "5.0",
                    "slippage_reference_sample_count": 1,
                    "realized_slippage_sample_count": 1,
                    "realized_slippage_bps_mean": "2.0",
                    "latest_fill_ingestion_ts": "2026-04-27T06:00:03+08:00",
                    "payload": {},
                }
            ],
        },
    }

    parsed = mod.parse_db_probe(json.dumps(raw))
    summary = mod.summarize_directional_episode_attribution_truth(
        parsed,
        {
            "ok": True,
            "recent_silver_orderbook": [],
            "recent_silver_trade_flow": [],
        },
    )

    assert summary["pretrade_microstructure"]["status"] == "missing_pretrade_microstructure_context"
    assert (
        summary["pretrade_microstructure"]["smallest_missing_field"]
        == "rdp.silver.market_orderbook_metrics_15m.decision_bar"
    )
    assert summary["latest_filled_decision"]["pretrade_microstructure"]["status"] == (
        "missing_pretrade_microstructure_context"
    )


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


def test_directional_hold_current_zero_delta_has_verified_no_order_expectation() -> None:
    mod = load_module()
    latest = {
        "allocation_id": "alloc-directional",
        "decision_id": "decision-directional",
        "symbol": "BTC-USDT-SWAP",
        "route_action": "hold_current",
        "primary_family": "directional",
        "portfolio_requested_notional": "54.532170000000000000",
        "portfolio_approved_notional": "54.532170000000000000",
        "portfolio_budget_cut_notional": "0",
        "payload": {
            "operator_summary": "当前 allocator v2 识别到活跃 sleeve，但本轮没有新的可执行 delta。",
            "reason_codes": [
                "approved_for_non_protective_execution",
                "no_budget_contraction",
                "composed_as_hold_current",
                "allocator_budget_assignment_active",
            ],
            "execution_legs": [],
            "strategy_sleeve_intents": [
                {
                    "family": "directional",
                    "strategy_sleeve_id": "sleeve-directional",
                    "route_action": "hold_current",
                    "approved_for_execution": True,
                    "execution_behavior": "hold_current",
                    "execution_control_mode": "approved",
                    "permission_mode": "approved",
                    "automatic_enabled": True,
                    "selectable": True,
                    "target_notional": "54.532170000000",
                    "reason_codes": ["directional_strategy_target"],
                    "control_trace": {
                        "permission": {
                            "approved_for_execution": True,
                            "candidate_enabled": True,
                            "candidate_execution_compatible": True,
                            "execution_prerequisites_supported": True,
                            "configured_auto_execution_enabled": True,
                            "permission_mode": "approved",
                            "runtime_supported": True,
                            "state_runtime_supported": True,
                            "reason_codes": ["approved_for_non_protective_execution"],
                        },
                        "composition": {
                            "approved_for_execution": True,
                            "route_action": "hold_current",
                            "execution_behavior": "hold_current",
                            "execution_control_mode": "approved",
                            "requested_delta_position_qty": "0E-12",
                            "composed_delta_position_qty": "0",
                            "reason_codes": ["composed_as_hold_current"],
                        },
                        "budget": {
                            "base_scale": "1",
                            "effective_scale": "1",
                            "requested_delta_position_qty": "0",
                            "scaled_delta_position_qty": "0",
                            "budget_zero_suppressed": False,
                            "reason_codes": ["no_budget_contraction"],
                        },
                    },
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
            "strategy_sleeve_intent_refs": ["intent-directional"],
            "portfolio_allocation_decision_ref": "alloc-directional",
            "risk_decision_ref": "risk-directional",
            "decision_outcome_ref": "outcome-directional",
        },
        {"execution_orders": 0, "order_states": 0, "execution_fills": 0, "legacy_fill_events": 0},
    )

    assert summarized is not None
    truth_chain = summarized["execution_truth_chain"]
    assert truth_chain["status"] == "verified_no_order_expected_hold_current_zero_delta"
    assert truth_chain["order_expected"] is False
    assert truth_chain["fill_expected"] is False
    assert truth_chain["position_lifecycle_transition_expected"] is False
    assert truth_chain["position_lifecycle_status"] == "no_position_lifecycle_transition_expected"
    assert truth_chain["smallest_missing_field"] is None
    assert truth_chain["missing_fields"] == []
    assert "route_action=hold_current" in truth_chain["evidence"]
    assert "execution_behavior=hold_current" in truth_chain["evidence"]
    assert "execution_legs_count=0" in truth_chain["evidence"]
    assert summarized["no_trade_attribution"]["classification"] == (
        "execution_activity_or_positive_allocation_present"
    )
    primary_candidate_truth = summarized["no_trade_attribution"]["primary_family_candidate_truth"]
    assert primary_candidate_truth["status"] == "verified_primary_candidate_hold_current_zero_delta_no_order_expected"
    assert primary_candidate_truth["primary_family"] == "directional"
    assert primary_candidate_truth["candidate_execution_compatible"] is True
    assert primary_candidate_truth["order_expected_from_primary_candidate"] is False
    assert primary_candidate_truth["no_order_root_cause"] == "primary_candidate_hold_current_zero_delta"


def test_advisory_only_suppressed_after_approval_has_verified_no_order_expectation() -> None:
    mod = load_module()
    latest = {
        "allocation_id": "alloc-directional",
        "decision_id": "decision-directional",
        "symbol": "BTC-USDT-SWAP",
        "route_action": "advisory_only",
        "primary_family": "directional",
        "portfolio_requested_notional": "835.641748373740",
        "portfolio_approved_notional": "0",
        "portfolio_budget_cut_notional": "835.641748373740",
        "payload": {
            "reason_codes": [
                "candidate_execution_incompatible",
                "approved_for_non_protective_execution",
                "reconciliation_contraction_active",
                "pnl_contraction_active",
                "directional_loss_blocks_risk_increase",
                "budget_contracted_to_zero",
                "approved_but_budget_zero_suppressed",
                "composed_as_advisory_only",
            ],
            "execution_legs": [],
            "strategy_sleeve_intents": [
                {
                    "family": "directional",
                    "strategy_sleeve_id": "sleeve-directional",
                    "route_action": "advisory_only",
                    "approved_for_execution": True,
                    "execution_behavior": "suppressed_after_approval",
                    "execution_control_mode": "approved",
                    "permission_mode": "approved",
                    "automatic_enabled": True,
                    "selectable": True,
                    "target_notional": "835.641748373740",
                    "reason_codes": ["directional_strategy_target"],
                    "control_trace": {
                        "permission": {
                            "approved_for_execution": True,
                            "candidate_enabled": True,
                            "candidate_execution_compatible": True,
                            "execution_prerequisites_supported": True,
                            "configured_auto_execution_enabled": True,
                            "permission_mode": "approved",
                            "runtime_supported": True,
                            "state_runtime_supported": True,
                            "reason_codes": ["approved_for_non_protective_execution"],
                        },
                        "composition": {
                            "approved_for_execution": True,
                            "route_action": "advisory_only",
                            "execution_behavior": "suppressed_after_approval",
                            "execution_control_mode": "approved",
                            "requested_delta_position_qty": "0.01093143793518829278521917663",
                            "composed_delta_position_qty": "0",
                            "reason_codes": ["composed_as_advisory_only"],
                        },
                        "budget": {
                            "base_scale": "1",
                            "effective_scale": "0",
                            "requested_delta_position_qty": "0.01093143793518829278521917663",
                            "scaled_delta_position_qty": "0",
                            "budget_zero_suppressed": True,
                            "reason_codes": [
                                "reconciliation_contraction_active",
                                "pnl_contraction_active",
                                "directional_loss_blocks_risk_increase",
                                "budget_contracted_to_zero",
                                "approved_but_budget_zero_suppressed",
                            ],
                        },
                    },
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
            "strategy_sleeve_intent_refs": ["intent-directional"],
            "portfolio_allocation_decision_ref": "alloc-directional",
            "risk_decision_ref": "risk-directional",
            "decision_outcome_ref": "outcome-directional",
        },
        {"execution_orders": 0, "order_states": 0, "execution_fills": 0, "legacy_fill_events": 0},
    )

    assert summarized is not None
    truth_chain = summarized["execution_truth_chain"]
    assert truth_chain["status"] == "verified_no_order_expected"
    assert truth_chain["order_expected"] is False
    assert truth_chain["fill_expected"] is False
    assert truth_chain["smallest_missing_field"] is None
    primary_candidate_truth = summarized["no_trade_attribution"]["primary_family_candidate_truth"]
    assert primary_candidate_truth["status"] == (
        "verified_primary_candidate_advisory_suppressed_after_approval_no_order_expected"
    )
    assert primary_candidate_truth["primary_family"] == "directional"
    assert primary_candidate_truth["candidate_route_action"] == "advisory_only"
    assert primary_candidate_truth["candidate_execution_behavior"] == "suppressed_after_approval"
    assert primary_candidate_truth["candidate_execution_compatible"] is True
    assert primary_candidate_truth["candidate_approved_for_execution"] is True
    assert primary_candidate_truth["order_expected_from_primary_candidate"] is False
    assert primary_candidate_truth["no_order_root_cause"] == (
        "primary_candidate_advisory_only_suppressed_after_approval"
    )
    assert primary_candidate_truth["smallest_missing_field"] is None
    assert primary_candidate_truth["global_primary_blocker"] == "candidate_execution_incompatible"
    assert primary_candidate_truth["global_primary_blocker_applies_to_candidate"] is False
    assert primary_candidate_truth["global_primary_blocker_scope"] == "other_candidate_or_portfolio_level"


def test_primary_family_candidate_truth_separates_directional_hold_from_global_blocker() -> None:
    mod = load_module()
    latest = {
        "allocation_id": "alloc-1",
        "decision_id": "decision-1",
        "symbol": "BTC-USDT-SWAP",
        "route_action": "advisory_only",
        "primary_family": "directional",
        "portfolio_requested_notional": "0",
        "portfolio_approved_notional": "0",
        "portfolio_budget_cut_notional": "0",
        "payload": {
            "reason_codes": [
                "candidate_execution_incompatible",
                "composed_as_advisory_only",
                "allocator_zero_notional_advisory",
            ],
            "execution_legs": [],
            "strategy_sleeve_intents": [
                {
                    "family": "directional",
                    "strategy_sleeve_id": "sleeve-directional",
                    "route_action": "hold_current",
                    "target_notional": "0",
                    "reason_codes": ["directional_strategy_target"],
                    "control_trace": {
                        "permission": {
                            "approved_for_execution": True,
                            "candidate_enabled": True,
                            "candidate_execution_compatible": True,
                            "execution_prerequisites_supported": True,
                            "configured_auto_execution_enabled": True,
                            "permission_mode": "approved",
                            "runtime_supported": True,
                            "state_runtime_supported": True,
                            "reason_codes": ["approved_for_non_protective_execution"],
                        },
                        "composition": {
                            "approved_for_execution": True,
                            "route_action": "hold_current",
                            "execution_behavior": "hold_current",
                            "execution_control_mode": "approved",
                            "requested_delta_position_qty": "0E-17",
                            "composed_delta_position_qty": "0",
                            "reason_codes": ["composed_as_hold_current"],
                        },
                        "budget": {
                            "base_scale": "1",
                            "effective_scale": "1",
                            "requested_delta_position_qty": "0",
                            "scaled_delta_position_qty": "0",
                            "budget_zero_suppressed": False,
                            "reason_codes": ["no_budget_contraction"],
                        },
                    },
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
    assert attribution["primary_blocker"] == "candidate_execution_incompatible"
    truth = attribution["primary_family_candidate_truth"]
    assert truth["status"] == "verified_primary_candidate_hold_current_zero_delta_no_order_expected"
    assert truth["primary_family"] == "directional"
    assert truth["candidate_execution_compatible"] is True
    assert truth["candidate_route_action"] == "hold_current"
    assert truth["candidate_execution_behavior"] == "hold_current"
    assert truth["order_expected_from_primary_candidate"] is False
    assert truth["no_order_root_cause"] == "primary_candidate_hold_current_zero_delta"
    assert truth["global_primary_blocker"] == "candidate_execution_incompatible"
    assert truth["global_primary_blocker_applies_to_candidate"] is False
    assert truth["global_primary_blocker_scope"] == "other_candidate_or_portfolio_level"


def test_expected_execution_surface_reports_smallest_missing_field() -> None:
    mod = load_module()

    truth_chain = mod.summarize_execution_truth_chain(
        latest_decision={"route_action": "override_target", "primary_family": "directional"},
        execution_chain={
            "execution_plan_ref_count": 0,
            "order_intent_ref_count": 0,
            "order_state_ref_count": 0,
            "fill_event_ref_count": 0,
            "db_order_count": 0,
            "db_order_state_count": 0,
            "db_fill_count": 0,
            "legacy_fill_event_count": 0,
        },
        execution_legs_count=1,
        candidate_drilldown=[
            {
                "family": "directional",
                "composition": {
                    "route_action": "override_target",
                    "execution_behavior": "submit_order",
                    "requested_delta_position_qty": "0.01",
                    "composed_delta_position_qty": "0.01",
                },
                "budget": {},
                "execution": {"execution_behavior": "submit_order"},
            },
        ],
    )

    assert truth_chain["status"] == "expected_execution_surface_missing"
    assert truth_chain["order_expected"] is True
    assert truth_chain["fill_expected"] is True
    assert truth_chain["position_lifecycle_status"] == "position_lifecycle_transition_evidence_missing"
    assert truth_chain["smallest_missing_field"] == "execution_plan_refs"
    assert truth_chain["missing_fields"] == [
        "execution_plan_refs",
        "order_intent_refs_or_execution_orders",
        "fill_event_refs_or_execution_fills",
    ]


def test_created_order_without_submit_command_reports_submission_gap() -> None:
    mod = load_module()

    truth_chain = mod.summarize_execution_truth_chain(
        latest_decision={"route_action": "override_target", "primary_family": "directional"},
        execution_chain={
            "execution_plan_ref_count": 1,
            "order_intent_ref_count": 1,
            "order_state_ref_count": 1,
            "fill_event_ref_count": 0,
            "db_order_count": 1,
            "execution_command_flow_enabled": True,
            "db_execution_order_created_or_submitting_count": 1,
            "db_execution_order_submitted_or_later_count": 0,
            "db_execution_command_count": 0,
            "db_execution_submit_command_count": 0,
            "db_order_state_count": 1,
            "db_order_state_created_or_submitting_count": 1,
            "db_order_state_submitted_or_later_count": 0,
            "db_fill_count": 0,
            "db_fill_via_order_count": 0,
            "legacy_fill_event_count": 0,
            "legacy_fill_event_via_order_count": 0,
        },
        execution_legs_count=1,
        candidate_drilldown=[
            {
                "family": "directional",
                "composition": {
                    "route_action": "override_target",
                    "execution_behavior": "submit_order",
                    "requested_delta_position_qty": "0.0002",
                    "composed_delta_position_qty": "0.0002",
                },
                "budget": {},
                "execution": {"execution_behavior": "submit_order"},
            },
        ],
    )

    assert truth_chain["status"] == "expected_order_submission_missing"
    assert truth_chain["order_expected"] is True
    assert truth_chain["fill_expected"] is False
    assert truth_chain["position_lifecycle_status"] == "position_lifecycle_transition_evidence_missing"
    assert truth_chain["smallest_missing_field"] == "execution_command_or_submitted_order_state"
    assert truth_chain["missing_fields"] == ["execution_command_or_submitted_order_state"]
    assert truth_chain["submission_gap_root_cause"] == "execution_command_missing_for_created_order"


def test_claimed_submit_command_without_terminal_order_ack_reports_submission_gap() -> None:
    mod = load_module()

    truth_chain = mod.summarize_execution_truth_chain(
        latest_decision={"route_action": "override_target", "primary_family": "directional"},
        execution_chain={
            "execution_plan_ref_count": 1,
            "order_intent_ref_count": 2,
            "order_state_ref_count": 2,
            "fill_event_ref_count": 0,
            "db_order_count": 2,
            "execution_command_flow_enabled": True,
            "db_execution_order_created_or_submitting_count": 1,
            "db_execution_order_submitted_or_later_count": 1,
            "db_execution_order_terminal_no_fill_count": 1,
            "db_execution_order_terminal_no_fill_states": "BLOCKED",
            "db_execution_command_count": 1,
            "db_execution_submit_command_count": 1,
            "db_execution_submit_command_claimed_count": 1,
            "db_order_state_count": 2,
            "db_order_state_created_or_submitting_count": 1,
            "db_order_state_submitted_or_later_count": 1,
            "db_order_state_terminal_no_fill_count": 1,
            "db_order_state_terminal_no_fill_statuses": "BLOCKED",
            "db_fill_count": 0,
            "db_fill_via_order_count": 0,
            "legacy_fill_event_count": 0,
            "legacy_fill_event_via_order_count": 0,
        },
        execution_legs_count=2,
        candidate_drilldown=[
            {
                "family": "directional",
                "composition": {
                    "route_action": "override_target",
                    "execution_behavior": "submit_order",
                    "requested_delta_position_qty": "-0.0078",
                    "composed_delta_position_qty": "-0.0078",
                },
                "budget": {},
                "execution": {"execution_behavior": "submit_order"},
            },
        ],
    )

    assert truth_chain["status"] == "expected_order_submission_missing"
    assert truth_chain["order_expected"] is True
    assert truth_chain["fill_expected"] is False
    assert truth_chain["position_lifecycle_transition_expected"] is False
    assert truth_chain["position_lifecycle_status"] == "no_position_lifecycle_transition_expected"
    assert truth_chain["smallest_missing_field"] == (
        "execution_command_terminal_ack_or_exchange_order_id"
    )
    assert truth_chain["missing_fields"] == [
        "execution_command_terminal_ack_or_exchange_order_id"
    ]
    assert truth_chain["submission_gap_root_cause"] == (
        "execution_submit_command_claimed_without_terminal_order_ack"
    )
    assert "db_submit_claimed_count=1" in truth_chain["evidence"]


def test_order_intent_without_order_surface_reports_projection_gap() -> None:
    mod = load_module()

    truth_chain = mod.summarize_execution_truth_chain(
        latest_decision={"route_action": "override_target", "primary_family": "directional"},
        execution_chain={
            "execution_plan_ref_count": 2,
            "order_intent_ref_count": 2,
            "order_state_ref_count": 0,
            "fill_event_ref_count": 0,
            "db_order_count": 0,
            "execution_command_flow_enabled": True,
            "db_execution_order_created_or_submitting_count": 0,
            "db_execution_order_submitted_or_later_count": 0,
            "db_execution_command_count": 0,
            "db_execution_submit_command_count": 0,
            "db_order_state_count": 0,
            "db_order_state_created_or_submitting_count": 0,
            "db_order_state_submitted_or_later_count": 0,
            "db_fill_count": 0,
            "db_fill_via_order_count": 0,
            "legacy_fill_event_count": 0,
            "legacy_fill_event_via_order_count": 0,
        },
        execution_legs_count=2,
        candidate_drilldown=[
            {
                "family": "directional",
                "composition": {
                    "route_action": "override_target",
                    "execution_behavior": "execute_target",
                    "requested_delta_position_qty": "-0.01064996606678376941684205709",
                    "composed_delta_position_qty": "-0.005324983033",
                },
                "budget": {},
                "execution": {"execution_behavior": "execute_target"},
            },
        ],
    )

    assert truth_chain["status"] == "expected_order_submission_missing"
    assert truth_chain["order_expected"] is True
    assert truth_chain["fill_expected"] is False
    assert truth_chain["position_lifecycle_status"] == "position_lifecycle_transition_evidence_missing"
    assert truth_chain["smallest_missing_field"] == "execution_order_or_order_state_from_order_intent_refs"
    assert truth_chain["missing_fields"] == ["execution_order_or_order_state_from_order_intent_refs"]
    assert truth_chain["submission_gap_root_cause"] == "execution_order_missing_for_order_intent"


def test_direct_submit_created_order_reports_command_flow_disabled_root_cause() -> None:
    mod = load_module()

    truth_chain = mod.summarize_execution_truth_chain(
        latest_decision={"route_action": "override_target", "primary_family": "directional"},
        execution_chain={
            "execution_plan_ref_count": 1,
            "order_intent_ref_count": 1,
            "order_state_ref_count": 1,
            "fill_event_ref_count": 0,
            "db_order_count": 1,
            "execution_command_flow_enabled": False,
            "db_execution_order_created_or_submitting_count": 1,
            "db_execution_order_submitted_or_later_count": 0,
            "db_execution_command_count": 0,
            "db_execution_submit_command_count": 0,
            "db_order_state_count": 1,
            "db_order_state_created_or_submitting_count": 1,
            "db_order_state_submitted_or_later_count": 0,
            "db_fill_count": 0,
            "db_fill_via_order_count": 0,
            "legacy_fill_event_count": 0,
            "legacy_fill_event_via_order_count": 0,
        },
        execution_legs_count=1,
        candidate_drilldown=[
            {
                "family": "directional",
                "composition": {
                    "route_action": "override_target",
                    "execution_behavior": "submit_order",
                    "requested_delta_position_qty": "0.0002",
                    "composed_delta_position_qty": "0.0002",
                },
                "budget": {},
                "execution": {"execution_behavior": "submit_order"},
            },
        ],
    )

    assert truth_chain["status"] == "expected_order_submission_missing"
    assert truth_chain["order_expected"] is True
    assert truth_chain["fill_expected"] is False
    assert truth_chain["smallest_missing_field"] == "enable_execution_command_flow_or_recover_created_order"
    assert truth_chain["missing_fields"] == ["enable_execution_command_flow_or_recover_created_order"]
    assert truth_chain["submission_gap_root_cause"] == (
        "execution_command_flow_disabled_direct_submit_interruption_window"
    )


def test_fill_joined_through_execution_order_satisfies_fill_surface() -> None:
    mod = load_module()

    truth_chain = mod.summarize_execution_truth_chain(
        latest_decision={"route_action": "override_target", "primary_family": "directional"},
        execution_chain={
            "execution_plan_ref_count": 1,
            "order_intent_ref_count": 1,
            "order_state_ref_count": 1,
            "fill_event_ref_count": 0,
            "db_order_count": 1,
            "execution_command_flow_enabled": True,
            "db_execution_order_created_or_submitting_count": 0,
            "db_execution_order_submitted_or_later_count": 1,
            "db_execution_command_count": 1,
            "db_execution_submit_command_count": 1,
            "db_order_state_count": 1,
            "db_order_state_created_or_submitting_count": 0,
            "db_order_state_submitted_or_later_count": 1,
            "db_fill_count": 0,
            "db_fill_via_order_count": 1,
            "legacy_fill_event_count": 0,
            "legacy_fill_event_via_order_count": 0,
        },
        execution_legs_count=1,
        candidate_drilldown=[
            {
                "family": "directional",
                "composition": {
                    "route_action": "override_target",
                    "execution_behavior": "submit_order",
                    "requested_delta_position_qty": "0.0002",
                    "composed_delta_position_qty": "0.0002",
                },
                "budget": {},
                "execution": {"execution_behavior": "submit_order"},
            },
        ],
    )

    assert truth_chain["status"] == "verified_execution_surface_present"
    assert truth_chain["fill_expected"] is True
    assert truth_chain["smallest_missing_field"] is None
    assert truth_chain["missing_fields"] == []


def test_terminal_failed_order_without_fill_does_not_expect_fill_surface() -> None:
    mod = load_module()

    truth_chain = mod.summarize_execution_truth_chain(
        latest_decision={"route_action": "override_target", "primary_family": "directional"},
        execution_chain={
            "execution_plan_ref_count": 1,
            "order_intent_ref_count": 1,
            "order_state_ref_count": 2,
            "fill_event_ref_count": 0,
            "db_order_count": 1,
            "execution_command_flow_enabled": True,
            "db_execution_order_created_or_submitting_count": 0,
            "db_execution_order_submitted_or_later_count": 1,
            "db_execution_order_terminal_no_fill_count": 1,
            "db_execution_order_terminal_no_fill_states": "BLOCKED",
            "db_execution_order_terminal_no_fill_source_systems": "semantic_dup_snapshot_blocked",
            "db_execution_order_terminal_no_fill_execution_styles": "semantic_duplicate_snapshot_blocked",
            "db_execution_order_terminal_no_fill_position_intents": "scale_in_long",
            "db_execution_command_count": 0,
            "db_execution_submit_command_count": 0,
            "db_order_state_count": 1,
            "db_order_state_created_or_submitting_count": 0,
            "db_order_state_submitted_or_later_count": 1,
            "db_order_state_terminal_no_fill_count": 1,
            "db_order_state_terminal_no_fill_statuses": "BLOCKED",
            "db_order_state_terminal_no_fill_position_intents": "scale_in_long",
            "db_fill_count": 0,
            "db_fill_via_order_count": 0,
            "legacy_fill_event_count": 0,
            "legacy_fill_event_via_order_count": 0,
        },
        execution_legs_count=1,
        candidate_drilldown=[
            {
                "family": "directional",
                "composition": {
                    "route_action": "override_target",
                    "execution_behavior": "submit_order",
                    "requested_delta_position_qty": "0.0002",
                    "composed_delta_position_qty": "0.0002",
                },
                "budget": {},
                "execution": {"execution_behavior": "submit_order"},
            },
        ],
    )

    assert truth_chain["status"] == "verified_terminal_order_no_fill_expected"
    assert truth_chain["order_expected"] is True
    assert truth_chain["fill_expected"] is False
    assert truth_chain["position_lifecycle_transition_expected"] is False
    assert truth_chain["position_lifecycle_status"] == "no_position_lifecycle_transition_expected"
    assert truth_chain["smallest_missing_field"] is None
    assert truth_chain["missing_fields"] == []
    explanation = truth_chain["terminal_no_fill_explanation"]
    assert explanation["classification"] == "terminal_order_surface_without_fill"
    assert explanation["reason"] == "terminal_order_blocked_before_fill"
    assert explanation["terminal_states"] == ["BLOCKED"]
    assert explanation["terminal_source_systems"] == ["semantic_dup_snapshot_blocked"]
    assert explanation["terminal_execution_styles"] == ["semantic_duplicate_snapshot_blocked"]
    assert explanation["terminal_position_intents"] == ["scale_in_long"]
    assert explanation["execution_order_count"] == 1
    assert explanation["order_state_count"] == 1
    assert explanation["fill_surface_present"] is False


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


def test_target_convergence_guard_truth_reports_exact_no_trigger_reason() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "target_convergence_guard": {
            "symbol": "BTC-USDT-SWAP",
            "guard_flag": mod.TARGET_CONVERGENCE_GUARD_FLAG,
            "coverage": {
                "directional_decisions_total": 31,
                "directional_decisions_24h": 4,
                "directional_decisions_1h": 0,
                "guard_hits_total": 0,
                "guard_hits_24h": 0,
                "guard_hits_1h": 0,
            },
            "current_open_orders": {
                "execution_orders": {
                    "open_order_count": 0,
                    "directional_open_order_count": 0,
                    "states": None,
                },
                "legacy_order_states": {
                    "open_order_count": 0,
                    "directional_open_order_count": 0,
                    "states": None,
                },
            },
            "latest_guard_hit": None,
        },
    }

    summary = mod.summarize_target_convergence_guard_truth(
        db,
        {"deployed_matches_windows": True},
        report_generated_at="2026-04-27T07:46:34Z",
    )

    assert summary["status"] == "deployed_no_trigger_no_recent_decisions_no_open_orders"
    assert summary["smallest_missing_field"] is None
    assert summary["coverage"]["directional_decisions_1h"] == 0
    assert summary["coverage"]["guard_hits_1h"] == 0
    assert summary["current_open_orders"]["total_open_order_count"] == 0
    assert "no current open order condition" in summary["interpretation"]


def test_target_convergence_guard_truth_verifies_guard_hit() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "target_convergence_guard": {
            "symbol": "BTC-USDT-SWAP",
            "guard_flag": mod.TARGET_CONVERGENCE_GUARD_FLAG,
            "coverage": {
                "directional_decisions_total": 32,
                "directional_decisions_24h": 5,
                "directional_decisions_1h": 2,
                "guard_hits_total": 1,
                "guard_hits_24h": 1,
                "guard_hits_1h": 1,
            },
            "current_open_orders": {
                "execution_orders": {
                    "open_order_count": 1,
                    "directional_open_order_count": 1,
                    "states": "SUBMITTING",
                },
                "legacy_order_states": {
                    "open_order_count": 0,
                    "directional_open_order_count": 0,
                    "states": None,
                },
            },
            "latest_guard_hit": {
                "decision_id": "decision_guard",
                "created_at": "2026-04-27T07:44:00Z",
                "route_action": "hold_current",
            },
        },
    }

    summary = mod.summarize_target_convergence_guard_truth(
        db,
        {"deployed_matches_windows": True},
        report_generated_at="2026-04-27T07:46:34Z",
    )

    assert summary["status"] == "verified_guard_triggered"
    assert summary["smallest_missing_field"] is None
    assert summary["coverage"]["guard_hits_total"] == 1
    assert summary["current_open_orders"]["total_open_order_count"] == 1
    assert summary["latest_guard_hit"]["decision_id"] == "decision_guard"


def test_directional_impulse_chase_guard_truth_reports_deployed_no_trigger() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "directional_impulse_chase_guard": {
            "symbol": "BTC-USDT-SWAP",
            "guard_flags": list(mod.IMPULSE_CHASE_GUARD_FLAGS),
            "coverage": {
                "directional_decisions_total": 44,
                "directional_decisions_24h": 3,
                "directional_decisions_1h": 0,
                "guard_hits_total": 0,
                "guard_hits_24h": 0,
                "guard_hits_1h": 0,
                "blocked_live_entry_hits_total": 0,
                "blocked_live_entry_hits_24h": 0,
                "blocked_live_entry_hits_1h": 0,
            },
            "flag_hits_total": {
                flag: 0 for flag in mod.IMPULSE_CHASE_GUARD_FLAGS
            },
            "latest_guard_hit": None,
        },
    }
    code_markers = {
        "source_file_present": True,
        "all_required_markers_present": True,
        "missing_markers": [],
    }

    summary = mod.summarize_directional_impulse_chase_guard_truth(
        db,
        {"deployed_matches_windows": True},
        code_markers,
        report_generated_at="2026-04-27T11:20:00Z",
    )

    assert summary["status"] == "deployed_no_trigger_no_recent_directional_decisions"
    assert summary["smallest_missing_field"] is None
    assert summary["code"]["all_required_markers_present"] is True
    assert summary["coverage"]["guard_hits_total"] == 0
    assert summary["coverage"]["blocked_live_entry_hits_total"] == 0


def test_directional_impulse_chase_guard_truth_verifies_blocked_entry() -> None:
    mod = load_module()
    matched_flag = "long_impulse_entry_extreme_chase_unconfirmed"
    db = {
        "ok": True,
        "directional_impulse_chase_guard": {
            "symbol": "BTC-USDT-SWAP",
            "guard_flags": list(mod.IMPULSE_CHASE_GUARD_FLAGS),
            "coverage": {
                "directional_decisions_total": 45,
                "directional_decisions_24h": 4,
                "directional_decisions_1h": 2,
                "guard_hits_total": 1,
                "guard_hits_24h": 1,
                "guard_hits_1h": 1,
                "blocked_live_entry_hits_total": 1,
                "blocked_live_entry_hits_24h": 1,
                "blocked_live_entry_hits_1h": 1,
            },
            "flag_hits_total": {
                matched_flag: 1,
            },
            "latest_guard_hit": {
                "decision_id": "decision_impulse_guard",
                "created_at": "2026-04-27T11:19:00Z",
                "route_action": "hold_current",
                "matched_guard_flags": [matched_flag],
            },
        },
    }
    code_markers = {
        "source_file_present": True,
        "all_required_markers_present": True,
        "missing_markers": [],
    }

    summary = mod.summarize_directional_impulse_chase_guard_truth(
        db,
        {"deployed_matches_windows": True},
        code_markers,
        report_generated_at="2026-04-27T11:20:00Z",
    )

    assert summary["status"] == "verified_guard_blocked_live_directional_entry"
    assert summary["coverage"]["guard_hits_1h"] == 1
    assert summary["coverage"]["blocked_live_entry_hits_total"] == 1
    assert summary["latest_guard_hit"]["decision_id"] == "decision_impulse_guard"
    assert summary["latest_guard_hit"]["matched_guard_flags"] == [matched_flag]


def test_okx_hedge_scale_in_intent_truth_reports_historical_only_mismatch() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "okx_hedge_scale_in_intent": {
            "mismatch_reason": mod.OKX_HEDGE_SCALE_IN_MISMATCH_REASON,
            "history_reason_counts": {"total": 0, "last_24h": 0, "last_1h": 0},
            "execution_payload_reason_counts": {
                "total": 151,
                "last_24h": 150,
                "last_1h": 0,
                "latest_created_at": "2026-04-27 06:32:46.981909+08:00",
            },
            "order_state_payload_reason_counts": {
                "total": 151,
                "last_24h": 150,
                "last_1h": 0,
                "latest_created_at": "2026-04-27 06:32:48.913749+08:00",
            },
            "open_scale_in_leg_counts": {
                "total": 151,
                "last_24h": 150,
                "last_1h": 0,
                "latest_created_at": "2026-04-27 06:32:46.981909+08:00",
            },
            "latest_mismatches": [
                {
                    "created_at": "2026-04-27 06:32:46.981909+08:00",
                    "order_id": "cl_scale_in",
                    "position_intent": "scale_in_long",
                    "side": "buy",
                    "pos_side": "long",
                    "leg_action": "open",
                    "state": "BLOCKED",
                },
            ],
        },
    }
    code_markers = {"all_required_markers_present": True}

    summary = mod.summarize_okx_hedge_scale_in_intent_truth(
        db,
        {"deployed_matches_windows": True},
        code_markers,
        report_generated_at="2026-04-27T11:56:02Z",
    )

    assert summary["status"] == "historical_scale_in_intent_mismatch_no_recent_hits"
    assert summary["smallest_missing_field"] is None
    assert summary["coverage"]["mismatch_24h"] == 150
    assert summary["coverage"]["mismatch_1h"] == 0
    assert summary["coverage"]["open_scale_in_leg_total"] == 151
    assert summary["latest_mismatch_created_at"] == "2026-04-27 06:32:46.981909+08:00"


def test_okx_hedge_scale_in_intent_truth_reports_active_mismatch() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "okx_hedge_scale_in_intent": {
            "mismatch_reason": mod.OKX_HEDGE_SCALE_IN_MISMATCH_REASON,
            "history_reason_counts": {"total": 0, "last_24h": 0, "last_1h": 0},
            "execution_payload_reason_counts": {"total": 2, "last_24h": 2, "last_1h": 1},
            "order_state_payload_reason_counts": {"total": 2, "last_24h": 2, "last_1h": 1},
            "open_scale_in_leg_counts": {"total": 4, "last_24h": 4, "last_1h": 1},
            "latest_mismatches": [],
        },
    }
    code_markers = {"all_required_markers_present": True}

    summary = mod.summarize_okx_hedge_scale_in_intent_truth(
        db,
        {"deployed_matches_windows": True},
        code_markers,
        report_generated_at="2026-04-27T11:56:02Z",
    )

    assert summary["status"] == "active_scale_in_intent_mismatch_after_alignment"
    assert summary["smallest_missing_field"] == "recent_okx_hedge_scale_in_mismatch_payload"
    assert summary["coverage"]["mismatch_1h"] == 1


def test_project_live_runtime_facts_exposes_okx_hedge_scale_in_truth() -> None:
    mod = load_module()
    report = {
        "database_truth": {"ok": True, "latest_decision": {}, "latest_executable_directional_decision": {}},
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
        "okx_hedge_scale_in_intent_truth": {
            "status": "historical_scale_in_intent_mismatch_no_recent_hits",
            "smallest_missing_field": None,
            "deployed_matches_windows": True,
            "code": {"all_required_markers_present": True},
            "coverage": {
                "mismatch_total": 151,
                "mismatch_24h": 150,
                "mismatch_1h": 0,
                "open_scale_in_leg_total": 151,
                "open_scale_in_leg_24h": 150,
                "open_scale_in_leg_1h": 0,
            },
            "latest_mismatch_created_at": "2026-04-27 06:32:46.981909+08:00",
        },
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["okx_hedge_scale_in_intent_truth_status"] == (
        "historical_scale_in_intent_mismatch_no_recent_hits"
    )
    assert live_facts["okx_hedge_scale_in_intent_code_present"] is True
    assert live_facts["okx_hedge_scale_in_intent_mismatch_24h"] == 150
    assert live_facts["okx_hedge_scale_in_intent_mismatch_1h"] == 0
    assert live_facts["okx_hedge_scale_in_open_leg_total"] == 151


def test_created_no_command_directional_order_truth_reports_verified_absence() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "created_no_command_directional_order": {
            "root_cause": mod.CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE,
            "execution_order_missing_submit_command_counts": {
                "total": 0,
                "last_24h": 0,
                "last_1h": 0,
                "latest_created_at": None,
            },
            "order_state_missing_submit_command_counts": {
                "total": 0,
                "last_24h": 0,
                "last_1h": 0,
                "latest_created_at": None,
            },
            "latest_execution_order_rows": [],
            "latest_order_state_rows": [],
        },
    }

    summary = mod.summarize_created_no_command_directional_order_truth(
        db,
        {"deployed_matches_windows": True},
        report_generated_at="2026-04-27T13:00:32Z",
    )

    assert summary["status"] == "verified_no_created_no_command_directional_orders"
    assert summary["smallest_missing_field"] is None
    assert summary["root_cause"] == mod.CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE
    assert summary["coverage"]["missing_total"] == 0
    assert summary["coverage"]["missing_1h"] == 0


def test_created_no_command_directional_order_truth_reports_active_gap() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "created_no_command_directional_order": {
            "root_cause": mod.CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE,
            "execution_order_missing_submit_command_counts": {
                "total": 1,
                "last_24h": 1,
                "last_1h": 1,
                "latest_created_at": "2026-04-27 20:58:00+08:00",
            },
            "order_state_missing_submit_command_counts": {
                "total": 1,
                "last_24h": 1,
                "last_1h": 1,
                "latest_created_at": "2026-04-27 20:58:00+08:00",
            },
            "latest_execution_order_rows": [{"order_id": "order-1"}],
            "latest_order_state_rows": [{"client_order_id": "client-1"}],
        },
    }

    summary = mod.summarize_created_no_command_directional_order_truth(
        db,
        {"deployed_matches_windows": True},
        report_generated_at="2026-04-27T13:00:32Z",
    )

    assert summary["status"] == "active_created_no_command_directional_order"
    assert summary["smallest_missing_field"] == mod.CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE
    assert summary["coverage"]["missing_total"] == 1
    assert summary["coverage"]["missing_1h"] == 1
    assert summary["latest_execution_order_rows"] == [{"order_id": "order-1"}]


def test_project_live_runtime_facts_exposes_created_no_command_directional_order_truth() -> None:
    mod = load_module()
    report = {
        "database_truth": {"ok": True, "latest_decision": {}, "latest_executable_directional_decision": {}},
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
        "created_no_command_directional_order_truth": {
            "status": "verified_no_created_no_command_directional_orders",
            "smallest_missing_field": None,
            "root_cause": mod.CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE,
            "deployed_matches_windows": True,
            "coverage": {
                "missing_total": 0,
                "missing_24h": 0,
                "missing_1h": 0,
                "latest_created_at": None,
            },
        },
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["created_no_command_directional_order_truth_status"] == (
        "verified_no_created_no_command_directional_orders"
    )
    assert live_facts["created_no_command_directional_order_root_cause"] == (
        mod.CREATED_NO_COMMAND_DIRECTIONAL_ROOT_CAUSE
    )
    assert live_facts["created_no_command_directional_order_missing_total"] == 0
    assert live_facts["created_no_command_directional_order_missing_1h"] == 0


def test_project_live_runtime_facts_exposes_primary_candidate_truth() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {
                "decision_id": "decision-current",
                "route_action": "advisory_only",
                "symbol": "BTC-USDT-SWAP",
                "primary_family": "directional",
                "no_trade_attribution": {
                    "primary_blocker": "candidate_execution_incompatible",
                    "candidate_execution_drilldown": [{}],
                    "primary_family_candidate_truth": {
                        "status": "verified_primary_candidate_hold_current_zero_delta_no_order_expected",
                        "smallest_missing_field": None,
                        "primary_family": "directional",
                        "candidate_route_action": "hold_current",
                        "candidate_execution_behavior": "hold_current",
                        "order_expected_from_primary_candidate": False,
                        "no_order_root_cause": "primary_candidate_hold_current_zero_delta",
                        "candidate_execution_compatible": True,
                        "candidate_approved_for_execution": True,
                        "candidate_permission_mode": "approved",
                        "composed_delta_position_qty": "0",
                        "target_notional": "0",
                        "global_primary_blocker": "candidate_execution_incompatible",
                        "global_primary_blocker_applies_to_candidate": False,
                        "global_primary_blocker_scope": "other_candidate_or_portfolio_level",
                    },
                },
                "execution_truth_chain": {},
            },
            "latest_executable_directional_decision": {},
        },
        "latest_decision_fill_feasibility_truth": {
            "status": "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context",
            "smallest_missing_field": None,
            "order_expected": False,
            "fill_expected": False,
            "fill_feasibility_applicable": False,
            "no_order": {
                "classification": "no_order_fill_expected_for_latest_decision",
                "primary_blocker": "candidate_execution_incompatible",
            },
            "pretrade_microstructure": {
                "status": "verified_pretrade_microstructure_context_present",
                "smallest_missing_field": None,
                "orderbook": {
                    "bar_age_seconds": 939,
                    "bbo_samples_n": 859,
                    "books5_samples_n": 1568,
                    "spread_bps_mean": "0.0132",
                },
                "trade_flow": {
                    "bar_age_seconds": 939,
                    "trade_count": 15454,
                    "taker_buy_ratio": "0.53400222",
                    "vwap_minus_mid_bps": "2.1660",
                },
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["latest_decision_primary_candidate_truth_status"] == (
        "verified_primary_candidate_hold_current_zero_delta_no_order_expected"
    )
    assert live_facts["latest_decision_primary_candidate_family"] == "directional"
    assert live_facts["latest_decision_primary_candidate_route_action"] == "hold_current"
    assert live_facts["latest_decision_primary_candidate_execution_behavior"] == "hold_current"
    assert live_facts["latest_decision_primary_candidate_order_expected"] is False
    assert live_facts["latest_decision_primary_candidate_no_order_root_cause"] == (
        "primary_candidate_hold_current_zero_delta"
    )
    assert live_facts["latest_decision_primary_candidate_no_order_semantic_status"] == (
        "verified_primary_candidate_no_order_expected_semantics"
    )
    assert live_facts["latest_decision_primary_candidate_no_order_equivalence_class"] == (
        "verified_non_executable_no_order_expected"
    )
    assert (
        live_facts[
            "latest_decision_primary_candidate_no_order_root_material_without_order_or_fill_change"
        ]
        is False
    )
    assert (
        live_facts[
            "latest_decision_primary_candidate_no_order_requires_order_or_fill_change_for_materiality"
        ]
        is True
    )
    assert live_facts["latest_decision_primary_candidate_execution_compatible"] is True
    assert live_facts["latest_decision_primary_candidate_global_primary_blocker"] == (
        "candidate_execution_incompatible"
    )
    assert live_facts["latest_decision_primary_candidate_global_blocker_applies"] is False
    assert live_facts["latest_decision_primary_candidate_global_blocker_scope"] == (
        "other_candidate_or_portfolio_level"
    )
    assert live_facts["latest_decision_fill_feasibility_truth_status"] == (
        "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context"
    )
    assert live_facts["latest_decision_fill_feasibility_applicable"] is False
    assert live_facts["latest_decision_fill_feasibility_order_expected"] is False
    assert live_facts["latest_decision_fill_feasibility_pretrade_status"] == (
        "verified_pretrade_microstructure_context_present"
    )
    assert live_facts["latest_decision_fill_feasibility_no_order_primary_blocker"] == (
        "candidate_execution_incompatible"
    )
    assert live_facts["latest_decision_fill_feasibility_orderbook_books5_samples_n"] == 1568
    assert live_facts["latest_decision_fill_feasibility_orderbook_spread_bps_mean"] == "0.0132"
    assert live_facts["latest_decision_fill_feasibility_trade_count"] == 15454
    assert live_facts["latest_decision_fill_feasibility_vwap_minus_mid_bps"] == "2.1660"


def test_project_live_runtime_facts_exposes_decision_lifecycle_provenance_continuity() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "decision_lifecycle_provenance_continuity_truth": {
            "status": "verified_current_no_order_plus_executable_terminal_no_fill_continuity",
            "smallest_missing_field": None,
            "current_decision": {
                "decision_id": "decision_latest",
                "order_expected": False,
                "fill_expected": False,
                "execution_truth_status": "verified_no_order_expected",
                "fill_feasibility_status": (
                    "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context"
                ),
            },
            "latest_executable_directional_episode": {
                "decision_id": "decision_exec",
                "status": "verified_executable_terminal_order_no_fill_truth",
                "terminal_no_fill_drilldown_status": (
                    "verified_terminal_no_fill_order_state_drilldown"
                ),
            },
            "recent_directional_batch": {
                "recent_decision_count": 24,
                "decisions_with_fills": 0,
            },
            "command_flow": {
                "status": "verified_current_directional_command_flow_fill_provenance_present",
            },
            "depth_slippage_lifecycle": {
                "status": "forward_depth_ready_no_order_expected_regime",
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["decision_lifecycle_provenance_continuity_status"] == (
        "verified_current_no_order_plus_executable_terminal_no_fill_continuity"
    )
    assert live_facts["decision_lifecycle_provenance_continuity_smallest_missing_field"] is None
    assert live_facts["decision_lifecycle_provenance_continuity_current_decision_id"] == (
        "decision_latest"
    )
    assert live_facts["decision_lifecycle_provenance_continuity_current_order_expected"] is False
    assert live_facts["decision_lifecycle_provenance_continuity_current_fill_expected"] is False
    assert live_facts["decision_lifecycle_provenance_continuity_current_truth_status"] == (
        "verified_no_order_expected"
    )
    assert live_facts["decision_lifecycle_provenance_continuity_current_fill_feasibility_status"] == (
        "verified_no_order_fill_feasibility_not_applicable_with_pretrade_context"
    )
    assert live_facts["decision_lifecycle_provenance_continuity_executable_decision_id"] == (
        "decision_exec"
    )
    assert live_facts["decision_lifecycle_provenance_continuity_executable_status"] == (
        "verified_executable_terminal_order_no_fill_truth"
    )
    assert (
        live_facts[
            "decision_lifecycle_provenance_continuity_executable_terminal_no_fill_drilldown_status"
        ]
        == "verified_terminal_no_fill_order_state_drilldown"
    )
    assert live_facts["decision_lifecycle_provenance_continuity_recent_decision_count"] == 24
    assert live_facts["decision_lifecycle_provenance_continuity_recent_filled_decisions"] == 0
    assert live_facts["decision_lifecycle_provenance_continuity_command_flow_status"] == (
        "verified_current_directional_command_flow_fill_provenance_present"
    )
    assert live_facts["decision_lifecycle_provenance_continuity_depth_slippage_status"] == (
        "forward_depth_ready_no_order_expected_regime"
    )


def test_primary_candidate_no_order_semantics_groups_verified_non_executable_roots() -> None:
    mod = load_module()

    hold_current = mod.classify_primary_candidate_no_order_semantics(
        {
            "order_expected_from_primary_candidate": False,
            "no_order_root_cause": "primary_candidate_hold_current_zero_delta",
            "smallest_missing_field": None,
        }
    )
    advisory_suppressed = mod.classify_primary_candidate_no_order_semantics(
        {
            "order_expected_from_primary_candidate": False,
            "no_order_root_cause": "primary_candidate_advisory_only_suppressed_after_approval",
            "smallest_missing_field": None,
        }
    )

    assert hold_current["status"] == "verified_primary_candidate_no_order_expected_semantics"
    assert advisory_suppressed["status"] == "verified_primary_candidate_no_order_expected_semantics"
    assert hold_current["equivalence_class"] == advisory_suppressed["equivalence_class"]
    assert hold_current["equivalence_class"] == "verified_non_executable_no_order_expected"
    assert hold_current["root_cause_is_material_without_order_or_fill_change"] is False
    assert advisory_suppressed["root_cause_is_material_without_order_or_fill_change"] is False
    assert hold_current["requires_order_or_fill_change_for_materiality"] is True
    assert advisory_suppressed["requires_order_or_fill_change_for_materiality"] is True


def test_project_live_runtime_facts_exposes_directional_no_order_semantics() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "directional_episode_attribution_truth": {
            "status": "verified_directional_episode_no_order_expected",
            "smallest_missing_field": None,
            "coverage": {
                "recent_decision_count": 24,
                "decisions_with_no_order_expected": 24,
                "decisions_with_no_order_semantics": 24,
                "decisions_with_stable_no_order_equivalence_class": 24,
                "all_no_order_expected_decisions_have_no_order_semantics": True,
                "all_no_order_expected_decisions_stable_equivalence_class": True,
            },
            "no_order_semantics": {
                "status": "verified_recent_no_order_semantics_present",
                "smallest_missing_field": None,
                "coverage": {
                    "decisions_with_no_order_expected": 24,
                    "decisions_with_no_order_semantics": 24,
                    "decisions_with_stable_no_order_equivalence_class": 24,
                    "all_no_order_expected_decisions_have_no_order_semantics": True,
                    "all_no_order_expected_decisions_stable_equivalence_class": True,
                },
                "equivalence_classes": ["verified_non_executable_no_order_expected"],
                "root_cause_is_material_without_order_or_fill_change": False,
                "requires_order_or_fill_change_for_materiality": True,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["directional_episode_no_order_semantics_status"] == (
        "verified_recent_no_order_semantics_present"
    )
    assert live_facts["directional_episode_no_order_semantics_smallest_missing_field"] is None
    assert live_facts["directional_episode_decisions_with_no_order_semantics"] == 24
    assert live_facts["directional_episode_decisions_with_stable_no_order_equivalence_class"] == 24
    assert (
        live_facts["directional_episode_all_no_order_expected_decisions_have_no_order_semantics"]
        is True
    )
    assert live_facts["directional_episode_all_no_order_expected_decisions_stable_equivalence_class"] is True
    assert live_facts["directional_episode_no_order_equivalence_classes"] == [
        "verified_non_executable_no_order_expected"
    ]
    assert live_facts["directional_episode_no_order_root_material_without_order_or_fill_change"] is False
    assert live_facts["directional_episode_no_order_requires_order_or_fill_change_for_materiality"] is True


def test_project_live_runtime_facts_exposes_orderbook_payload_depth_truth() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "orderbook_payload_depth_truth": {
            "status": "verified_books5_payload_depth_evidence_present",
            "smallest_missing_field": None,
            "raw_payload_exposed": False,
            "books5_payload": {
                "payload_hash_present": True,
                "row_checksum_present": True,
                "exchange_sequence_id_present": True,
                "capture_status": "diff_payload_persisted",
                "collector_sequence": 2002,
            },
            "bbo_payload": {
                "payload_hash_present": True,
            },
            "sequence": {
                "books5_row_count": 600,
                "books5_sequence_gap_count": 0,
                "bbo_row_count": 300,
                "bbo_sequence_gap_count": 0,
                "diff_payload_persisted_row_count": 900,
            },
            "silver_orderbook": {
                "books5_samples_n": 1528,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["orderbook_payload_depth_truth_status"] == (
        "verified_books5_payload_depth_evidence_present"
    )
    assert live_facts["orderbook_payload_depth_smallest_missing_field"] is None
    assert live_facts["orderbook_payload_depth_raw_payload_exposed"] is False
    assert live_facts["orderbook_payload_depth_books5_payload_hash_present"] is True
    assert live_facts["orderbook_payload_depth_books5_row_checksum_present"] is True
    assert live_facts["orderbook_payload_depth_books5_exchange_sequence_id_present"] is True
    assert live_facts["orderbook_payload_depth_books5_capture_status"] == "diff_payload_persisted"
    assert live_facts["orderbook_payload_depth_books5_collector_sequence"] == 2002
    assert live_facts["orderbook_payload_depth_books5_row_count"] == 600
    assert live_facts["orderbook_payload_depth_books5_sequence_gap_count"] == 0
    assert live_facts["orderbook_payload_depth_bbo_payload_hash_present"] is True
    assert live_facts["orderbook_payload_depth_bbo_row_count"] == 300
    assert live_facts["orderbook_payload_depth_diff_payload_persisted_row_count"] == 900
    assert live_facts["orderbook_payload_depth_silver_books5_samples_n"] == 1528


def test_project_live_runtime_facts_exposes_depth_slippage_lifecycle_truth() -> None:
    mod = load_module()
    report = {
        "database_truth": {
            "ok": True,
            "latest_decision": {},
            "latest_executable_directional_decision": {},
        },
        "depth_slippage_lifecycle_truth": {
            "status": "forward_depth_ready_no_recent_directional_filled_episode",
            "smallest_missing_field": (
                "directional_episode_attribution.recent_directional_filled_decisions"
            ),
            "raw_payload_exposed": False,
            "depth_readiness": {
                "books5_row_count": 90,
                "books5_sequence_gap_count": 0,
            },
            "slippage_baseline": {
                "fee_sample_count": 73,
                "slippage_proxy_sample_count": 17,
            },
            "directional_command_coverage": {
                "current_submit_command_reference_covered_fill_count": 17,
                "current_submit_command_reference_missing_fill_count": 0,
            },
            "recent_directional_lifecycle_coverage": {
                "recent_filled_decision_count": 0,
                "recent_filled_with_pretrade_microstructure": 0,
                "recent_filled_with_resolved_pnl_lifecycle": 0,
            },
            "interpretation": {
                "forward_depth_ready": True,
                "existing_fill_slippage_baseline_present": True,
                "per_recent_directional_fill_depth_lifecycle_link_present": False,
                "no_order_expected_regime": True,
                "waiting_for_executable_directional_episode": True,
            },
        },
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["depth_slippage_lifecycle_truth_status"] == (
        "forward_depth_ready_no_recent_directional_filled_episode"
    )
    assert live_facts["depth_slippage_lifecycle_smallest_missing_field"] == (
        "directional_episode_attribution.recent_directional_filled_decisions"
    )
    assert live_facts["depth_slippage_lifecycle_raw_payload_exposed"] is False
    assert live_facts["depth_slippage_lifecycle_forward_depth_ready"] is True
    assert live_facts["depth_slippage_lifecycle_existing_fill_slippage_baseline_present"] is True
    assert live_facts["depth_slippage_lifecycle_per_recent_directional_fill_link_present"] is False
    assert live_facts["depth_slippage_lifecycle_no_order_expected_regime"] is True
    assert live_facts["depth_slippage_lifecycle_waiting_for_executable_directional_episode"] is True
    assert live_facts["depth_slippage_lifecycle_depth_books5_row_count"] == 90
    assert live_facts["depth_slippage_lifecycle_depth_books5_sequence_gap_count"] == 0
    assert live_facts["depth_slippage_lifecycle_slippage_proxy_sample_count"] == 17
    assert live_facts["depth_slippage_lifecycle_fee_sample_count"] == 73
    assert live_facts["depth_slippage_lifecycle_current_submit_reference_covered_fill_count"] == 17
    assert live_facts["depth_slippage_lifecycle_current_submit_reference_missing_fill_count"] == 0
    assert live_facts["depth_slippage_lifecycle_recent_filled_decision_count"] == 0
    assert live_facts["depth_slippage_lifecycle_recent_filled_with_pretrade_microstructure"] == 0
    assert live_facts["depth_slippage_lifecycle_recent_filled_with_resolved_pnl_lifecycle"] == 0


def test_execution_order_payload_status_residual_truth_classifies_non_authoritative_status() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "execution_order_payload_status_residual": {
            "symbol": "BTC-USDT-SWAP",
            "authority": {
                "order_status_source": "execution_orders.state",
                "order_state_status_source": "order_states.status",
                "raw_payload_top_level_status_authoritative": False,
                "notes": ["column state is authoritative"],
            },
            "coverage": {
                "top_level_status_mismatch_count": 319,
                "nested_status_mismatch_count": 417,
                "terminal_column_nonterminal_top_level_count": 1,
                "open_column_terminal_top_level_count": 0,
                "open_by_column_count": 0,
                "open_by_top_level_raw_payload_count": 319,
                "terminal_column_nonterminal_nested_count": 417,
                "open_column_terminal_nested_count": 0,
            },
            "target_order": {
                "client_order_id": "cl9d7875bd332bf6fb8a5e2bd248ba21",
                "state": "FAILED",
                "raw_payload_status": "SUBMITTING",
                "nested_order_state_status": "FAILED",
            },
            "latest_mismatch_rows": [],
            "top_level_status_mismatch_groups": [],
            "nested_status_mismatch_groups": [],
        },
    }

    summary = mod.summarize_execution_order_payload_status_residual_truth(
        db,
        report_generated_at="2026-04-28T14:41:34Z",
    )

    assert summary["status"] == "classified_non_authoritative_top_level_payload_status_residual"
    assert summary["smallest_missing_field"] is None
    assert summary["authority"]["order_status_source"] == "execution_orders.state"
    assert summary["authority"]["raw_payload_top_level_status_authoritative"] is False
    assert summary["coverage"]["open_by_column_count"] == 0
    assert summary["coverage"]["open_by_top_level_raw_payload_count"] == 319
    assert summary["coverage"]["raw_payload_status_would_misclassify_open_orders"] is True
    assert summary["target_order"]["top_level_status_mismatch"] is True
    assert summary["target_order"]["nested_status_matches_column"] is True
    assert "top-level raw_payload.status" in summary["consumer_audit"][2]


def test_project_live_runtime_facts_exposes_execution_order_payload_status_residual_truth() -> None:
    mod = load_module()
    report = {
        "database_truth": {"ok": True, "latest_decision": {}, "latest_executable_directional_decision": {}},
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
        "execution_order_payload_status_residual_truth": {
            "status": "classified_non_authoritative_top_level_payload_status_residual",
            "smallest_missing_field": None,
            "authority": {
                "order_status_source": "execution_orders.state",
                "raw_payload_top_level_status_authoritative": False,
            },
            "coverage": {
                "top_level_status_mismatch_count": 319,
                "nested_status_mismatch_count": 417,
                "terminal_column_nonterminal_top_level_count": 1,
                "open_column_terminal_top_level_count": 0,
                "open_by_column_count": 0,
                "open_by_top_level_raw_payload_count": 319,
                "raw_payload_status_would_misclassify_open_orders": True,
                "terminal_column_nonterminal_nested_count": 417,
                "open_column_terminal_nested_count": 0,
            },
            "target_order": {
                "client_order_id": "cl9d7875bd332bf6fb8a5e2bd248ba21",
                "state": "FAILED",
                "raw_payload_status": "SUBMITTING",
                "nested_order_state_status": "FAILED",
                "top_level_status_mismatch": True,
                "nested_status_matches_column": True,
            },
        },
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["execution_order_payload_status_residual_truth_status"] == (
        "classified_non_authoritative_top_level_payload_status_residual"
    )
    assert live_facts["execution_order_payload_status_authoritative_source"] == "execution_orders.state"
    assert live_facts["execution_order_payload_status_top_level_authoritative"] is False
    assert live_facts["execution_order_payload_status_open_by_column_count"] == 0
    assert live_facts["execution_order_payload_status_open_by_top_level_raw_payload_count"] == 319
    assert live_facts["execution_order_payload_status_raw_payload_status_would_misclassify_open_orders"] is True
    assert live_facts["execution_order_payload_status_target_state"] == "FAILED"
    assert live_facts["execution_order_payload_status_target_raw_payload_status"] == "SUBMITTING"
    assert live_facts["execution_order_payload_status_target_nested_matches_column"] is True


def test_claimed_submit_stuck_submission_truth_requires_operator_confirmation() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "claimed_submit_stuck_submission": {
            "symbol": "BTC-USDT-SWAP",
            "root_cause": mod.CLAIMED_SUBMIT_STUCK_ROOT_CAUSE,
            "coverage": {
                "total": 1,
                "last_24h": 1,
                "last_1h": 0,
                "oldest_created_at": "2026-04-28T03:40:50+08:00",
                "latest_updated_at": "2026-04-28T03:40:57+08:00",
            },
            "latest_order": {
                "client_order_id": "cl_stuck",
                "command_id": "cmd_stuck",
                "execution_order_state": "SUBMITTING",
                "command_state": "CLAIMED",
                "position_intent": "close_long",
                "reduce_only": True,
                "close_only": True,
                "execution_fill_count": 0,
                "fill_event_count": 0,
            },
            "latest_reconciliation": {
                "reconciliation_id": "recon_stuck",
                "severity": "HARD_MISMATCH",
                "halt_required": True,
            },
            "latest_reconciliation_finding_counts": {
                "total": 28,
                "blocking": 3,
                "mentions_stuck_order": 2,
            },
            "latest_reconciliation_findings_for_order": [
                {"reason_code": "order_state_unknown_on_exchange", "blocks_resume": True},
            ],
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

    summary = mod.summarize_claimed_submit_stuck_submission_truth(
        db,
        report_generated_at="2026-04-28T08:12:27Z",
    )

    assert summary["status"] == "blocked_external_operator_confirmation_required"
    assert summary["smallest_missing_field"] == "operator_confirmation"
    assert summary["root_cause"] == mod.CLAIMED_SUBMIT_STUCK_ROOT_CAUSE
    assert summary["current_blocker"] == (
        "external_operator_confirmation_required_before_resolve_stuck_submission"
    )
    assert summary["required_operator_confirmation"] == "resolve_claimed_submit_as_failed:cl_stuck"
    assert summary["coverage"]["claimed_submit_stuck_submission_count"] == 1
    assert summary["latest_order"]["command_state"] == "CLAIMED"


def test_claimed_submit_stuck_submission_truth_reports_verified_absence() -> None:
    mod = load_module()
    db = {
        "ok": True,
        "claimed_submit_stuck_submission": {
            "symbol": "BTC-USDT-SWAP",
            "root_cause": mod.CLAIMED_SUBMIT_STUCK_ROOT_CAUSE,
            "coverage": {
                "total": 0,
                "last_24h": 0,
                "last_1h": 0,
                "oldest_created_at": None,
                "latest_updated_at": None,
            },
        },
    }

    summary = mod.summarize_claimed_submit_stuck_submission_truth(
        db,
        report_generated_at="2026-04-28T08:12:27Z",
    )

    assert summary["status"] == "verified_no_claimed_submit_stuck_submission"
    assert summary["smallest_missing_field"] is None
    assert summary["required_operator_confirmation"] is None
    assert summary["coverage"]["claimed_submit_stuck_submission_count"] == 0


def test_project_live_runtime_facts_exposes_claimed_submit_stuck_submission_truth() -> None:
    mod = load_module()
    report = {
        "database_truth": {"ok": True, "latest_decision": {}, "latest_executable_directional_decision": {}},
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
        "claimed_submit_stuck_submission_truth": {
            "status": "blocked_external_operator_confirmation_required",
            "smallest_missing_field": "operator_confirmation",
            "root_cause": mod.CLAIMED_SUBMIT_STUCK_ROOT_CAUSE,
            "current_blocker": "external_operator_confirmation_required_before_resolve_stuck_submission",
            "required_operator_confirmation": "resolve_claimed_submit_as_failed:cl_stuck",
            "coverage": {
                "claimed_submit_stuck_submission_count": 1,
                "claimed_submit_stuck_submission_24h": 1,
                "claimed_submit_stuck_submission_1h": 0,
            },
            "latest_order": {
                "client_order_id": "cl_stuck",
                "command_id": "cmd_stuck",
                "execution_order_state": "SUBMITTING",
                "command_state": "CLAIMED",
                "position_intent": "close_long",
                "reduce_only": True,
                "close_only": True,
                "execution_fill_count": 0,
                "fill_event_count": 0,
            },
            "latest_reconciliation": {
                "reconciliation_id": "recon_stuck",
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

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["claimed_submit_stuck_submission_truth_status"] == (
        "blocked_external_operator_confirmation_required"
    )
    assert live_facts["claimed_submit_stuck_submission_client_order_id"] == "cl_stuck"
    assert live_facts["claimed_submit_stuck_submission_command_id"] == "cmd_stuck"
    assert live_facts["claimed_submit_stuck_submission_required_operator_confirmation"] == (
        "resolve_claimed_submit_as_failed:cl_stuck"
    )
    assert live_facts["claimed_submit_stuck_submission_latest_reconciliation_severity"] == (
        "HARD_MISMATCH"
    )
    assert live_facts["claimed_submit_stuck_submission_latest_baseline_kind"] == (
        "operator_rebaseline"
    )
    assert live_facts["claimed_submit_stuck_submission_resolve_action_count_for_order"] == 0


def test_claimed_submit_operator_handoff_truth_uses_latest_matching_artifact(tmp_path: Path) -> None:
    mod = load_module()
    artifact_dir = tmp_path / "artifacts" / "automation"
    artifact_dir.mkdir(parents=True)
    claimed_submit_truth = {
        "coverage": {"claimed_submit_stuck_submission_count": 1},
        "latest_order": {"client_order_id": "cl_stuck"},
        "required_operator_confirmation": "resolve_claimed_submit_as_failed:cl_stuck",
    }
    old_handoff = {
        "artifact_type": "claimed_submit_operator_handoff",
        "generated_from_runtime_truth_at": "2026-04-28T08:00:00Z",
        "handoff_status": "awaiting_external_operator_confirmation",
        "next_action": "operator_verify_okx_absence_then_rerun_verifier_with_exact_confirmation",
        "order": {
            "client_order_id": "cl_old",
            "command_id": "cmd_old",
            "exact_confirmation_required": "resolve_claimed_submit_as_failed:cl_old",
        },
        "source_artifacts": {"runtime_truth": "old_runtime.json", "packet": "old_packet.json"},
        "validation": {
            "valid": True,
            "ready_for_protected_recovery": False,
            "operator_confirmation_matched": False,
            "status": "awaiting_external_operator_confirmation",
            "warnings": [],
            "failures": [],
        },
    }
    latest_handoff = {
        "artifact_type": "claimed_submit_operator_handoff",
        "generated_from_runtime_truth_at": "2026-04-28T10:50:30Z",
        "handoff_status": "awaiting_external_operator_confirmation",
        "next_action": "operator_verify_okx_absence_then_rerun_verifier_with_exact_confirmation",
        "order": {
            "client_order_id": "cl_stuck",
            "command_id": "cmd_stuck",
            "exact_confirmation_required": "resolve_claimed_submit_as_failed:cl_stuck",
        },
        "source_artifacts": {"runtime_truth": "latest_runtime.json", "packet": "packet.json"},
        "validation": {
            "valid": True,
            "ready_for_protected_recovery": False,
            "operator_confirmation_matched": False,
            "status": "awaiting_external_operator_confirmation",
            "warnings": ["runtime_reconciliation_id_changed_since_packet"],
            "failures": [],
        },
    }
    (artifact_dir / "claimed_submit_operator_handoff_2026_04_28T08_00_00Z.json").write_text(
        json.dumps(old_handoff),
        encoding="utf-8",
    )
    latest_path = artifact_dir / "claimed_submit_operator_handoff_2026_04_28T10_50_30Z.json"
    latest_path.write_text(json.dumps(latest_handoff), encoding="utf-8")

    summary = mod.summarize_claimed_submit_operator_handoff_truth(
        tmp_path,
        claimed_submit_truth,
        report_generated_at="2026-04-28T11:13:33Z",
    )

    assert summary["status"] == "awaiting_external_operator_confirmation"
    assert summary["smallest_missing_field"] == "operator_confirmation"
    assert summary["artifact_path"] == "artifacts/automation/claimed_submit_operator_handoff_2026_04_28T10_50_30Z.json"
    assert summary["valid"] is True
    assert summary["ready_for_protected_recovery"] is False
    assert summary["matches_current_order"] is True
    assert summary["source_artifacts"]["runtime_truth"] == "latest_runtime.json"
    assert summary["warnings"] == ["runtime_reconciliation_id_changed_since_packet"]


def test_claimed_submit_operator_handoff_truth_rejects_mismatched_order(tmp_path: Path) -> None:
    mod = load_module()
    artifact_dir = tmp_path / "artifacts" / "automation"
    artifact_dir.mkdir(parents=True)
    claimed_submit_truth = {
        "coverage": {"claimed_submit_stuck_submission_count": 1},
        "latest_order": {"client_order_id": "cl_current"},
        "required_operator_confirmation": "resolve_claimed_submit_as_failed:cl_current",
    }
    handoff = {
        "artifact_type": "claimed_submit_operator_handoff",
        "generated_from_runtime_truth_at": "2026-04-28T10:50:30Z",
        "handoff_status": "ready_for_protected_recovery",
        "next_action": "run_protected_resolve_stuck_submission",
        "order": {
            "client_order_id": "cl_old",
            "command_id": "cmd_old",
            "exact_confirmation_required": "resolve_claimed_submit_as_failed:cl_old",
        },
        "validation": {
            "valid": True,
            "ready_for_protected_recovery": True,
            "operator_confirmation_matched": True,
            "status": "ready_for_protected_recovery",
            "warnings": [],
            "failures": [],
        },
    }
    (artifact_dir / "claimed_submit_operator_handoff_2026_04_28T10_50_30Z.json").write_text(
        json.dumps(handoff),
        encoding="utf-8",
    )

    summary = mod.summarize_claimed_submit_operator_handoff_truth(
        tmp_path,
        claimed_submit_truth,
        report_generated_at="2026-04-28T11:13:33Z",
    )

    assert summary["status"] == "stale_or_mismatched_operator_handoff"
    assert summary["smallest_missing_field"] == "operator_handoff.order.client_order_id"
    assert summary["ready_for_protected_recovery"] is False
    assert summary["matches_current_order"] is False


def test_project_live_runtime_facts_exposes_claimed_submit_operator_handoff_truth() -> None:
    mod = load_module()
    report = {
        "database_truth": {"ok": True, "latest_decision": {}, "latest_executable_directional_decision": {}},
        "runtime": {"dashboard_bundle": {}, "ai_timeout_active_blocker": False},
        "scope": {"shadow_benchmark": "none_verified"},
        "git": {"deployed_matches_windows": True, "windows": {"dirty": False}},
        "deployment_health": {"gateway_health": {"ok": True}, "containers": {}},
        "claimed_submit_operator_handoff_truth": {
            "status": "awaiting_external_operator_confirmation",
            "smallest_missing_field": "operator_confirmation",
            "current_blocker": "external_operator_confirmation_required_before_resolve_stuck_submission",
            "artifact_path": "artifacts/automation/claimed_submit_operator_handoff_current.json",
            "generated_from_runtime_truth_at": "2026-04-28T10:50:30Z",
            "handoff_status": "awaiting_external_operator_confirmation",
            "validation_status": "awaiting_external_operator_confirmation",
            "next_action": "operator_verify_okx_absence_then_rerun_verifier_with_exact_confirmation",
            "valid": True,
            "ready_for_protected_recovery": False,
            "operator_confirmation_matched": False,
            "matches_current_order": True,
            "matches_required_confirmation": True,
            "source_artifacts": {"runtime_truth": "runtime.json", "packet": "packet.json"},
        },
    }

    live_facts = mod.project_live_runtime_facts(report)

    assert live_facts["claimed_submit_operator_handoff_truth_status"] == (
        "awaiting_external_operator_confirmation"
    )
    assert live_facts["claimed_submit_operator_handoff_artifact"] == (
        "artifacts/automation/claimed_submit_operator_handoff_current.json"
    )
    assert live_facts["claimed_submit_operator_handoff_ready_for_protected_recovery"] is False
    assert live_facts["claimed_submit_operator_handoff_matches_current_order"] is True
    assert live_facts["claimed_submit_operator_handoff_source_packet"] == "packet.json"


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
            "runtime_config": {
                "execution_command_flow_enabled": True,
                "execution_command_flow_flag_present": True,
            },
            "latest_decision": {
                "decision_id": "decision_new",
                "route_action": "advisory_only",
                "symbol": "BTC-USDT-SWAP",
                "primary_family": "independent",
                "execution_truth_chain": {
                    "status": "verified_terminal_order_no_fill_expected",
                    "order_expected": True,
                    "fill_expected": False,
                    "position_lifecycle_status": "no_position_lifecycle_transition_expected",
                    "smallest_missing_field": None,
                    "terminal_no_fill_explanation": {
                        "classification": "terminal_order_surface_without_fill",
                        "reason": "terminal_order_blocked_before_fill",
                        "terminal_states": ["BLOCKED"],
                        "terminal_source_systems": ["semantic_dup_snapshot_blocked"],
                        "terminal_execution_styles": ["semantic_duplicate_snapshot_blocked"],
                        "terminal_position_intents": ["scale_in_long"],
                        "execution_order_count": 1,
                        "order_state_count": 1,
                    },
                },
            },
            "latest_executable_directional_decision": {
                "decision_id": "decision_exec",
                "created_at": "2026-04-26T15:00:00Z",
                "route_action": "override_target",
                "symbol": "BTC-USDT-SWAP",
                "primary_family": "directional",
                "execution_truth_chain": {
                    "status": "verified_execution_surface_present",
                    "order_expected": True,
                    "fill_expected": True,
                    "position_lifecycle_status": "position_lifecycle_transition_evidence_present",
                    "smallest_missing_field": None,
                    "submission_gap_root_cause": None,
                    "terminal_no_fill_explanation": {
                        "classification": "terminal_order_surface_without_fill",
                        "reason": "terminal_order_blocked_before_fill",
                        "terminal_states": ["BLOCKED"],
                        "terminal_source_systems": ["semantic_dup_snapshot_blocked"],
                        "terminal_execution_styles": ["semantic_duplicate_snapshot_blocked"],
                        "terminal_position_intents": ["scale_in_short"],
                        "execution_order_count": 1,
                        "order_state_count": 1,
                    },
                },
            },
        },
        "execution_science_truth": {
            "status": "verified_orderbook_sequence_and_silver_bar_present",
            "smallest_missing_field": None,
            "payload_sequence": {"status": "sequence_continuous"},
            "silver_orderbook": {"status": "verified_silver_orderbook_bar_present"},
            "silver_trade_flow": {"status": "verified_silver_trade_flow_bar_present"},
            "fill_feasibility_truth_status": "verified_preorder_orderbook_features_available",
        },
        "slippage_cost_calibration_truth": {
            "status": "partial_fee_verified_slippage_proxy_missing",
            "smallest_missing_field": "order_or_command_reference_price_for_slippage_proxy",
            "fee": {"sample_count": 62},
            "slippage_proxy": {
                "sample_count": 0,
                "coverage_audit": {
                    "classification": "missing_reference_price_coverage_is_no_submit_command_path",
                    "missing_reference_fills": 62,
                    "missing_reference_fills_with_submit_command": 0,
                    "missing_reference_fills_without_submit_command": 62,
                    "covered_reference_fills_with_command_reference": 0,
                    "deterministic_backfill_status": "blocked_no_persisted_pretrade_reference_price",
                    "deterministic_backfill_reason": (
                        "historical no-submit-command fills have no persisted pre-trade reference price"
                    ),
                    "deterministic_backfill_fill_count": 62,
                    "deterministic_backfill_mutates_database": False,
                    "reference_policy": "pretrade_order_or_command_reference_only",
                },
            },
        },
        "directional_command_flow_provenance_truth": {
            "status": "verified_current_directional_command_flow_fill_provenance_present",
            "smallest_missing_field": None,
            "current_command_path_reference_gap": False,
            "coverage": {
                "current_submit_command_fill_count": 17,
                "current_submit_command_reference_covered_fill_count": 17,
                "current_submit_command_reference_missing_fill_count": 0,
                "historical_no_submit_command_fill_count": 31,
                "historical_no_submit_command_reference_missing_fill_count": 31,
            },
        },
        "directional_episode_attribution_truth": {
            "status": "verified_directional_episode_edge_cost_pnl_attribution_present",
            "smallest_missing_field": None,
            "coverage": {
                "recent_decision_count": 12,
                "decisions_with_edge_cost": 12,
                "decisions_with_fills": 5,
                "decisions_with_pnl_outcome": 4,
                "decisions_with_pretrade_microstructure": 9,
                "filled_decisions_with_pretrade_microstructure": 5,
                "filled_decisions_with_pnl_lifecycle_classification": 5,
                "filled_decisions_with_resolved_pnl_lifecycle": 4,
            },
            "pnl_lifecycle": {
                "status": "missing_directional_episode_pnl_lifecycle_evidence",
                "smallest_missing_field": "fill_outcomes.realized_pnl_delta",
                "coverage": {
                    "filled_decisions_with_pnl_lifecycle_classification": 5,
                    "filled_decisions_with_resolved_pnl_lifecycle": 4,
                },
                "latest_filled_decision_status": "closed_lifecycle_missing_fill_outcome",
                "latest_filled_decision_smallest_missing_field": "fill_outcomes.realized_pnl_delta",
            },
            "pretrade_microstructure": {
                "status": "verified_filled_directional_episode_pretrade_microstructure_present",
                "smallest_missing_field": None,
                "coverage": {
                    "decisions_with_pretrade_microstructure": 9,
                    "filled_decisions_with_pretrade_microstructure": 5,
                },
            },
            "latest_filled_decision": {
                "decision_id": "decision_episode",
                "expected_net_edge_bps": "41.19",
                "realized_cost_proxy_bps": "6.5",
                "fill": {"count": 2},
                "pnl_outcome": {"realized_pnl_usdt": "-0.42"},
                "pnl_lifecycle": {
                    "status": "closed_lifecycle_missing_fill_outcome",
                    "smallest_missing_field": "fill_outcomes.realized_pnl_delta",
                },
                "pretrade_microstructure": {
                    "status": "verified_pretrade_microstructure_context_present",
                    "smallest_missing_field": None,
                },
            },
        },
        "directional_spike_reversion_truth": {
            "status": "verified_directional_spike_reversion_execution_context_present",
            "smallest_missing_field": None,
            "coverage": {
                "recent_filled_directional_decision_count": 5,
                "filled_decisions_with_spike_reversion_context": 5,
                "adverse_fill_vs_decision_mid_10bps_count": 2,
                "post_fill_adverse_reversion_10bps_count": 1,
                "decision_trade_flow_dislocation_10bps_count": 3,
            },
            "latest_filled_decision": {
                "classification": "adverse_fill_vs_decision_mid_observed",
                "adverse_fill_vs_decision_mid_bps": "27.66",
                "post_fill_mid_move_bps": "-4.2",
                "decision_trade_flow_vwap_minus_mid_bps": "-11.78",
            },
        },
        "target_convergence_guard_truth": {
            "status": "deployed_no_trigger_no_current_open_orders",
            "smallest_missing_field": None,
            "guard_flag": "target_convergence_open_orders_block_exposure_increase",
            "coverage": {
                "directional_decisions_total": 31,
                "directional_decisions_24h": 7,
                "directional_decisions_1h": 2,
                "guard_hits_total": 0,
                "guard_hits_24h": 0,
                "guard_hits_1h": 0,
            },
            "current_open_orders": {
                "total_open_order_count": 0,
                "execution_orders_open_order_count": 0,
                "legacy_order_states_open_order_count": 0,
            },
            "latest_guard_hit": None,
        },
        "directional_impulse_chase_guard_truth": {
            "status": "verified_guard_blocked_live_directional_entry",
            "smallest_missing_field": None,
            "deployed_matches_windows": True,
            "code": {
                "all_required_markers_present": True,
            },
            "coverage": {
                "directional_decisions_total": 34,
                "directional_decisions_24h": 4,
                "directional_decisions_1h": 1,
                "guard_hits_total": 1,
                "guard_hits_24h": 1,
                "guard_hits_1h": 1,
                "blocked_live_entry_hits_total": 1,
                "blocked_live_entry_hits_24h": 1,
                "blocked_live_entry_hits_1h": 1,
            },
            "latest_guard_hit": {
                "decision_id": "decision_impulse_guard",
                "created_at": "2026-04-27T11:19:00Z",
                "matched_guard_flags": [
                    "long_impulse_entry_extreme_chase_unconfirmed",
                ],
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
    assert live_facts["latest_decision_terminal_no_fill_classification"] == (
        "terminal_order_surface_without_fill"
    )
    assert live_facts["latest_decision_terminal_no_fill_reason"] == "terminal_order_blocked_before_fill"
    assert live_facts["latest_decision_terminal_no_fill_states"] == ["BLOCKED"]
    assert live_facts["latest_decision_terminal_no_fill_source_systems"] == [
        "semantic_dup_snapshot_blocked"
    ]
    assert live_facts["latest_decision_terminal_no_fill_execution_styles"] == [
        "semantic_duplicate_snapshot_blocked"
    ]
    assert live_facts["latest_decision_terminal_no_fill_position_intents"] == ["scale_in_long"]
    assert live_facts["latest_decision_terminal_no_fill_order_count"] == 1
    assert live_facts["latest_decision_terminal_no_fill_order_state_count"] == 1
    assert live_facts["latest_executable_directional_decision_id"] == "decision_exec"
    assert live_facts["latest_executable_directional_execution_truth_status"] == "verified_execution_surface_present"
    assert live_facts["latest_executable_directional_order_expected"] is True
    assert live_facts["latest_executable_directional_fill_expected"] is True
    assert live_facts["latest_executable_directional_truth_chain_smallest_missing_field"] is None
    assert live_facts["latest_executable_directional_submission_gap_root_cause"] is None
    assert live_facts["latest_executable_directional_terminal_no_fill_classification"] == (
        "terminal_order_surface_without_fill"
    )
    assert (
        live_facts["latest_executable_directional_terminal_no_fill_reason"]
        == "terminal_order_blocked_before_fill"
    )
    assert live_facts["latest_executable_directional_terminal_no_fill_states"] == ["BLOCKED"]
    assert live_facts["latest_executable_directional_terminal_no_fill_source_systems"] == [
        "semantic_dup_snapshot_blocked"
    ]
    assert live_facts["latest_executable_directional_terminal_no_fill_execution_styles"] == [
        "semantic_duplicate_snapshot_blocked"
    ]
    assert live_facts["latest_executable_directional_terminal_no_fill_position_intents"] == [
        "scale_in_short"
    ]
    assert live_facts["latest_executable_directional_terminal_no_fill_order_count"] == 1
    assert live_facts["latest_executable_directional_terminal_no_fill_order_state_count"] == 1
    assert live_facts["portfolio_allocation_decisions"] == 11
    assert live_facts["active_live_carrier"] == "independent"
    assert live_facts["execution_command_flow_enabled"] is True
    assert live_facts["execution_command_flow_flag_present"] is True
    assert live_facts["execution_science_truth_status"] == "verified_orderbook_sequence_and_silver_bar_present"
    assert live_facts["orderbook_sequence_validation_status"] == "sequence_continuous"
    assert live_facts["fill_feasibility_truth_status"] == "verified_preorder_orderbook_features_available"
    assert live_facts["silver_trade_flow_truth_status"] == "verified_silver_trade_flow_bar_present"
    assert live_facts["slippage_cost_calibration_truth_status"] == "partial_fee_verified_slippage_proxy_missing"
    assert (
        live_facts["slippage_cost_calibration_smallest_missing_field"]
        == "order_or_command_reference_price_for_slippage_proxy"
    )
    assert live_facts["slippage_cost_fee_sample_count"] == 62
    assert live_facts["slippage_cost_slippage_proxy_sample_count"] == 0
    assert (
        live_facts["slippage_reference_coverage_classification"]
        == "missing_reference_price_coverage_is_no_submit_command_path"
    )
    assert live_facts["slippage_missing_reference_fills"] == 62
    assert live_facts["slippage_missing_reference_fills_with_submit_command"] == 0
    assert live_facts["slippage_missing_reference_fills_without_submit_command"] == 62
    assert live_facts["slippage_covered_reference_fills_with_command_reference"] == 0
    assert live_facts["slippage_reference_deterministic_backfill_status"] == (
        "blocked_no_persisted_pretrade_reference_price"
    )
    assert live_facts["slippage_reference_deterministic_backfill_fill_count"] == 62
    assert live_facts["slippage_reference_deterministic_backfill_mutates_database"] is False
    assert live_facts["slippage_reference_policy"] == "pretrade_order_or_command_reference_only"
    assert (
        live_facts["directional_command_flow_provenance_truth_status"]
        == "verified_current_directional_command_flow_fill_provenance_present"
    )
    assert live_facts["directional_command_flow_provenance_smallest_missing_field"] is None
    assert live_facts["directional_command_flow_current_reference_gap"] is False
    assert live_facts["directional_command_flow_current_submit_fill_count"] == 17
    assert live_facts["directional_command_flow_current_reference_covered_fill_count"] == 17
    assert live_facts["directional_command_flow_current_reference_missing_fill_count"] == 0
    assert live_facts["directional_command_flow_historical_no_submit_fill_count"] == 31
    assert live_facts["directional_command_flow_historical_no_submit_reference_missing_fill_count"] == 31
    assert (
        live_facts["directional_episode_attribution_truth_status"]
        == "verified_directional_episode_edge_cost_pnl_attribution_present"
    )
    assert live_facts["directional_episode_attribution_smallest_missing_field"] is None
    assert live_facts["directional_episode_recent_decision_count"] == 12
    assert live_facts["directional_episode_decisions_with_edge_cost"] == 12
    assert live_facts["directional_episode_decisions_with_fills"] == 5
    assert live_facts["directional_episode_decisions_with_pnl_outcome"] == 4
    assert live_facts["directional_episode_pnl_lifecycle_status"] == (
        "missing_directional_episode_pnl_lifecycle_evidence"
    )
    assert live_facts["directional_episode_pnl_lifecycle_smallest_missing_field"] == (
        "fill_outcomes.realized_pnl_delta"
    )
    assert live_facts["directional_episode_filled_decisions_with_pnl_lifecycle_classification"] == 5
    assert live_facts["directional_episode_filled_decisions_with_resolved_pnl_lifecycle"] == 4
    assert live_facts["directional_episode_pretrade_microstructure_status"] == (
        "verified_filled_directional_episode_pretrade_microstructure_present"
    )
    assert live_facts["directional_episode_pretrade_microstructure_smallest_missing_field"] is None
    assert live_facts["directional_episode_decisions_with_pretrade_microstructure"] == 9
    assert live_facts["directional_episode_filled_decisions_with_pretrade_microstructure"] == 5
    assert live_facts["latest_directional_episode_decision_id"] == "decision_episode"
    assert live_facts["latest_directional_episode_expected_net_edge_bps"] == "41.19"
    assert live_facts["latest_directional_episode_realized_cost_proxy_bps"] == "6.5"
    assert live_facts["latest_directional_episode_fill_count"] == 2
    assert live_facts["latest_directional_episode_realized_pnl_usdt"] == "-0.42"
    assert live_facts["latest_directional_episode_pnl_lifecycle_status"] == (
        "closed_lifecycle_missing_fill_outcome"
    )
    assert live_facts["latest_directional_episode_pnl_lifecycle_smallest_missing_field"] == (
        "fill_outcomes.realized_pnl_delta"
    )
    assert live_facts["latest_directional_episode_pretrade_microstructure_status"] == (
        "verified_pretrade_microstructure_context_present"
    )
    assert live_facts["latest_directional_episode_pretrade_microstructure_smallest_missing_field"] is None
    assert (
        live_facts["directional_spike_reversion_truth_status"]
        == "verified_directional_spike_reversion_execution_context_present"
    )
    assert live_facts["directional_spike_reversion_smallest_missing_field"] is None
    assert live_facts["directional_spike_reversion_filled_decision_count"] == 5
    assert live_facts["directional_spike_reversion_context_count"] == 5
    assert live_facts["directional_spike_reversion_adverse_fill_10bps_count"] == 2
    assert live_facts["directional_spike_reversion_post_fill_adverse_reversion_10bps_count"] == 1
    assert live_facts["directional_spike_reversion_trade_flow_dislocation_10bps_count"] == 3
    assert live_facts["latest_directional_spike_reversion_classification"] == (
        "adverse_fill_vs_decision_mid_observed"
    )
    assert live_facts["latest_directional_spike_reversion_adverse_fill_vs_decision_mid_bps"] == "27.66"
    assert live_facts["latest_directional_spike_reversion_post_fill_mid_move_bps"] == "-4.2"
    assert live_facts["latest_directional_spike_reversion_decision_trade_flow_vwap_minus_mid_bps"] == "-11.78"
    assert live_facts["target_convergence_guard_truth_status"] == "deployed_no_trigger_no_current_open_orders"
    assert live_facts["target_convergence_guard_smallest_missing_field"] is None
    assert (
        live_facts["target_convergence_guard_flag"]
        == "target_convergence_open_orders_block_exposure_increase"
    )
    assert live_facts["target_convergence_guard_directional_decisions_1h"] == 2
    assert live_facts["target_convergence_guard_hits_24h"] == 0
    assert live_facts["target_convergence_guard_hits_1h"] == 0
    assert live_facts["target_convergence_guard_current_open_order_count"] == 0
    assert live_facts["target_convergence_guard_latest_hit_decision_id"] is None
    assert live_facts["directional_impulse_chase_guard_truth_status"] == (
        "verified_guard_blocked_live_directional_entry"
    )
    assert live_facts["directional_impulse_chase_guard_smallest_missing_field"] is None
    assert live_facts["directional_impulse_chase_guard_code_present"] is True
    assert live_facts["directional_impulse_chase_guard_deployed_matches_windows"] is True
    assert live_facts["directional_impulse_chase_guard_directional_decisions_1h"] == 1
    assert live_facts["directional_impulse_chase_guard_hits_24h"] == 1
    assert live_facts["directional_impulse_chase_guard_hits_1h"] == 1
    assert live_facts["directional_impulse_chase_guard_blocked_live_entry_hits_total"] == 1
    assert live_facts["directional_impulse_chase_guard_blocked_live_entry_hits_24h"] == 1
    assert live_facts["directional_impulse_chase_guard_blocked_live_entry_hits_1h"] == 1
    assert live_facts["directional_impulse_chase_guard_latest_hit_decision_id"] == (
        "decision_impulse_guard"
    )
    assert live_facts["directional_impulse_chase_guard_latest_hit_created_at"] == (
        "2026-04-27T11:19:00Z"
    )
    assert live_facts["directional_impulse_chase_guard_latest_hit_matched_flags"] == [
        "long_impulse_entry_extreme_chase_unconfirmed",
    ]
    assert authority["authoritative_source"] == "runtime.live_runtime_facts"
    assert authority["artifact_may_override_live"] is False


def test_runtime_truth_scope_uses_live_carrier_from_database_truth() -> None:
    mod = load_module()
    report = {
        "scope": {
            "venue": "OKX",
            "symbol": "BTC-USDT-SWAP",
            "live_carrier": "unknown_pending_database_truth",
            "shadow_benchmark": "none_verified",
        },
        "runtime": {
            "ai_timeout_active_blocker": False,
            "dashboard_bundle": {},
        },
        "database_truth": {
            "ok": True,
            "portfolio_allocation_decisions": 1,
            "execution_fills": 0,
            "runtime_config": {
                "execution_command_flow_enabled": True,
                "execution_command_flow_flag_present": True,
            },
            "latest_decision": {
                "decision_id": "decision_directional",
                "route_action": "advisory_only",
                "symbol": "BTC-USDT-SWAP",
                "primary_family": "directional",
            },
        },
        "git": {},
        "deployment_health": {},
    }

    live_facts = mod.project_live_runtime_facts(report)
    mod.apply_live_runtime_scope(report, live_facts)

    assert live_facts["active_live_carrier"] == "directional"
    assert live_facts["execution_command_flow_enabled"] is True
    assert live_facts["execution_command_flow_flag_present"] is True
    assert report["scope"]["live_carrier"] == "directional"
