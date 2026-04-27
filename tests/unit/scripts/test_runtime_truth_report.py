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
    assert coverage_audit["by_order_path"][0]["source_system"] == "local_order_manager"
    assert coverage_audit["by_order_path"][0]["row_count"] == 56


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
            "db_execution_command_count": 0,
            "db_execution_submit_command_count": 0,
            "db_order_state_count": 1,
            "db_order_state_created_or_submitting_count": 0,
            "db_order_state_submitted_or_later_count": 1,
            "db_order_state_terminal_no_fill_count": 1,
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
            "runtime_config": {
                "execution_command_flow_enabled": True,
                "execution_command_flow_flag_present": True,
            },
            "latest_decision": {
                "decision_id": "decision_new",
                "route_action": "advisory_only",
                "symbol": "BTC-USDT-SWAP",
                "primary_family": "independent",
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
                },
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
    assert live_facts["latest_executable_directional_decision_id"] == "decision_exec"
    assert live_facts["latest_executable_directional_execution_truth_status"] == "verified_execution_surface_present"
    assert live_facts["latest_executable_directional_order_expected"] is True
    assert live_facts["latest_executable_directional_fill_expected"] is True
    assert live_facts["latest_executable_directional_truth_chain_smallest_missing_field"] is None
    assert live_facts["latest_executable_directional_submission_gap_root_cause"] is None
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
