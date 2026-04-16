from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aats.data_platform.decision_system.active_parameter_apply import (
    apply_approved_recommendation,
    rollback_active_parameter_set,
)
from aats.data_platform.production_workflow.gate_rules import (
    check_current_alerts,
    check_live_db_health,
)
from aats.data_platform.production_workflow.pre_apply_gate import (
    build_gate_context,
    run_pre_apply_gate,
)
from aats.data_platform.production_workflow.release_registry import (
    create_parameter_release,
)
from aats.services.operator.rdp_queries import query_rdp_health


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _seed_rdp_health_artifacts(root: Path) -> None:
    generated_at = datetime.now(timezone.utc).isoformat()
    _write_json(root / "artifacts/governance/artifact_index.json", {"generated_at": generated_at})
    _write_json(root / "artifacts/governance/active_round_index.json", {"generated_at": generated_at})
    _write_json(
        root / "artifacts/governance/current_parameter_registry.json",
        {"generated_at": generated_at, "parameter_sets": []},
    )
    _write_json(
        root / "artifacts/governance/quality_monitor_summary.json",
        {"generated_at": generated_at, "summary": {"health": "healthy", "critical_failures": 0}},
    )
    _write_json(root / "artifacts/decision_system/recommendation_registry.json", {"generated_at": generated_at})
    _write_json(root / "artifacts/decision_system/active_decision_registry.json", {"generated_at": generated_at})
    _write_json(root / "artifacts/decision_system/evidence_bundle_index.json", {"generated_at": generated_at})
    _write_json(
        root / "artifacts/operations/alerts/current_alerts.json",
        {
            "generated_at": generated_at,
            "overall_status": "healthy",
            "critical_alerts": 0,
            "warning_alerts": 0,
            "alerts": [],
        },
    )

    finished_at = datetime.now(timezone.utc) - timedelta(hours=1)
    for workflow in ("data_maintenance", "governance_cycle", "decision_cycle"):
        _write_json(
            root / "artifacts/operations/workflow_runs" / f"{workflow}.json",
            {
                "run_id": f"run_{workflow}",
                "workflow": workflow,
                "overall_status": "success",
                "started_at": (finished_at - timedelta(minutes=10)).isoformat(),
                "finished_at": finished_at.isoformat(),
            },
        )


def test_prod_direct_apply_requires_release_context() -> None:
    recommendation = {
        "recommendation_id": "rec_prod_1",
        "status": "approved",
        "approved_by": "reviewer",
        "approved_at": "2026-04-14T10:00:00+00:00",
        "target_parameter_set_id": "ps_prod_1",
    }
    registry = {
        "parameter_sets": [
            {
                "parameter_set_id": "ps_prod_1",
                "family": "independent",
                "timeframe": "15m",
                "values": {"entry_threshold": 0.41},
            },
        ],
    }
    with (
        patch.dict(
            os.environ,
            {"RDP_ENV": "prod", "RDP_PRODUCTION_APPLY_ENABLED": "true"},
            clear=False,
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=registry,
        ),
    ):
        result = apply_approved_recommendation(
            Path("."),
            recommendation_id="rec_prod_1",
            actor="operator",
            gate_result={"allow_apply": True, "blocking_reasons": []},
        )

    assert result["ok"] is False
    assert "direct apply" in result["message"]


def test_create_parameter_release_rejects_prod_skip_gate_and_short_window() -> None:
    with patch.dict(
        os.environ,
        {"RDP_ENV": "prod", "RDP_PRODUCTION_APPLY_ENABLED": "true"},
        clear=False,
    ):
        skip_gate = create_parameter_release(
            Path("."),
            recommendation_id="rec_any",
            run_gate=False,
            run_apply=True,
        )
        short_window = create_parameter_release(
            Path("."),
            recommendation_id="rec_any",
            run_gate=True,
            run_apply=True,
            observation_window_hours=24,
        )

    assert skip_gate["ok"] is False
    assert "requires gate pass" in skip_gate["message"]
    assert short_window["ok"] is False
    assert "observation_window_hours" in short_window["message"]


def test_create_parameter_release_passes_release_context_to_apply() -> None:
    recommendation = {
        "recommendation_id": "rec_dev_1",
        "status": "approved",
        "approved_by": "reviewer",
        "approved_at": "2026-04-14T10:00:00+00:00",
        "target_parameter_set_id": "ps_dev_1",
    }
    registry = {
        "parameter_sets": [
            {
                "parameter_set_id": "ps_dev_1",
                "family": "independent",
                "timeframe": "15m",
                "values": {"entry_threshold": 0.35},
            },
        ],
    }
    gate_result = {
        "allow_apply": True,
        "gate_status": "pass",
        "blocking_reasons": [],
        "warnings": [],
        "gate_run_id": "gate_1",
    }
    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": [recommendation]},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=recommendation,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value=registry,
        ),
        patch(
            "aats.bootstrap.active_parameters.load_active_parameter_registry",
            return_value={"active_sets": {}},
        ),
        patch(
            "aats.data_platform.production_workflow.pre_apply_gate.run_pre_apply_gate",
            return_value=gate_result,
        ),
        patch(
            "aats.data_platform.decision_system.active_parameter_apply.apply_approved_recommendation",
            return_value={"ok": True, "message": "applied"},
        ) as apply_mock,
    ):
        result = create_parameter_release(
            Path("."),
            recommendation_id="rec_dev_1",
            run_gate=True,
            run_apply=True,
        )

    assert result["ok"] is True
    kwargs = apply_mock.call_args.kwargs
    assert kwargs["gate_result"] == gate_result
    assert kwargs["release_id"].startswith("rel_")


def test_rollback_marks_latest_successful_release_as_rolled_back(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "artifacts/governance/current_parameter_registry.json",
        {
            "generated_at": "2026-04-16T10:00:00Z",
            "parameter_sets": [
                {
                    "parameter_set_id": "ps_live_0",
                    "family": "independent",
                    "timeframe": "15m",
                    "status": "frozen",
                    "source_round_id": "round_prev",
                    "values": {"entry_threshold": 0.4},
                },
            ],
        },
    )
    _write_json(
        tmp_path / "artifacts/production_workflow/parameter_release_history.json",
        {
            "generated_at": "2026-04-16T10:05:00Z",
            "releases": [
                {
                    "release_id": "rel_active_1",
                    "created_at": "2026-04-16T10:01:00Z",
                    "family": "independent",
                    "timeframe": "15m",
                    "combo_key": "independent_15m",
                    "recommendation_id": "rec_active_1",
                    "parameter_set_id": "ps_live_1",
                    "previous_parameter_set_id": "ps_live_0",
                    "actor": "operator",
                    "apply_result": "success",
                    "observation_status": "observing",
                },
            ],
        },
    )

    class _Result:
        def fetchone(self) -> SimpleNamespace:
            return SimpleNamespace(parameter_set_id="ps_live_1")

    class _Session:
        def execute(self, *_args, **_kwargs) -> _Result:
            return _Result()

        def __enter__(self) -> "_Session":
            return self

        def __exit__(self, _exc_type, _exc, _tb) -> bool:
            return False

    rollback_guard = SimpleNamespace(allowed=True, reason="")

    with (
        patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False),
        patch(
            "aats.data_platform.operations.environment_guard.get_current_environment",
            return_value="dev",
        ),
        patch(
            "aats.data_platform.operations.environment_guard.guard_parameter_rollback",
            return_value=rollback_guard,
        ),
        patch(
            "aats.data_platform.db.get_session",
            side_effect=lambda: _Session(),
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_upsert_active_set",
        ),
        patch(
            "aats.data_platform.governance.active_params_db.db_append_history",
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.try_governance_db",
            return_value=(None, False),
        ),
        patch(
            "aats.data_platform.production_workflow.release_registry.try_governance_db",
            return_value=(None, False),
        ),
    ):
        result = rollback_active_parameter_set(
            tmp_path,
            family="independent",
            timeframe="15m",
            to_parameter_set_id="ps_live_0",
            actor="operator",
        )

    assert result["ok"] is True
    assert result["release_id"] == "rel_active_1"

    release_history = json.loads(
        (tmp_path / "artifacts/production_workflow/parameter_release_history.json").read_text(encoding="utf-8"),
    )
    release = release_history["releases"][0]
    assert release["observation_status"] == "rolled_back"
    assert release["rollback_to_parameter_set_id"] == "ps_live_0"
    assert release["rollback_operation_id"] == result["operation_id"]
    assert release["rolled_back_at"]


def test_gate_rules_block_prod_on_critical_alerts_and_warn_dev_on_live_db_failure() -> None:
    alert_result = check_current_alerts(
        {
            "environment": "prod",
            "current_alerts": {
                "overall_status": "critical",
                "critical_alerts": 1,
                "warning_alerts": 0,
            },
        }
    )
    live_db_result = check_live_db_health(
        {
            "environment": "dev",
            "live_db_health": {
                "healthy": False,
                "errors": ["RDP_LIVE_DATABASE_URL 未配置"],
            },
        }
    )

    assert alert_result.passed is False
    assert alert_result.severity == "block"
    assert live_db_result.passed is True
    assert live_db_result.severity == "warn"


def test_query_rdp_health_blocks_in_prod_when_daemon_status_missing(tmp_path: Path) -> None:
    _seed_rdp_health_artifacts(tmp_path)

    with (
        patch.dict(os.environ, {"RDP_ENV": "prod"}, clear=False),
        patch(
            "aats.services.operator.rdp_queries._query_governance_runtime_state",
            return_value={
                "connection_ok": True,
                "task_queue": {
                    "pending_count": 0,
                    "running_count": 0,
                    "failed_count": 0,
                    "done_count": 0,
                },
                "runtime_components": [],
                "errors": [],
            },
        ),
        patch(
            "aats.data_platform.live_query_adapter.check_live_db_health",
            return_value={"healthy": True, "connection_ok": True, "tables_checked": {}},
        ),
        patch(
            "aats.bootstrap.active_parameters.load_all_active_parameter_sets",
            return_value={},
        ),
    ):
        payload = query_rdp_health(tmp_path)

    assert payload["overall_health"] == "blocked"
    assert "rdp_daemon_status_missing" in payload["blocking_reasons"]


def test_pre_apply_gate_fails_closed_on_rule_exception_in_prod(tmp_path: Path) -> None:
    def broken_rule(_ctx: dict) -> None:
        raise RuntimeError("boom")

    with patch.dict(os.environ, {"RDP_ENV": "prod"}, clear=False):
        result = run_pre_apply_gate(
            tmp_path,
            "rec_prod_broken",
            rules=[broken_rule],
            save_result=False,
        )

    assert result["allow_apply"] is False
    assert result["gate_status"] == "block"
    assert any("boom" in item for item in result["blocking_reasons"])


def test_pre_apply_gate_keeps_warn_semantics_on_rule_exception_in_dev(tmp_path: Path) -> None:
    def broken_rule(_ctx: dict) -> None:
        raise RuntimeError("boom")

    with patch.dict(os.environ, {"RDP_ENV": "dev"}, clear=False):
        result = run_pre_apply_gate(
            tmp_path,
            "rec_dev_broken",
            rules=[broken_rule],
            save_result=False,
        )

    assert result["allow_apply"] is True
    assert result["gate_status"] == "warn"
    assert any("boom" in item for item in result["warnings"])


def test_gate_rules_can_read_runtime_contract_without_legacy_scattered_fields() -> None:
    alert_result = check_current_alerts(
        {
            "runtime_contract": {
                "environment": "prod",
                "strict_environment": True,
                "current_alerts": {
                    "overall_status": "critical",
                    "critical_alerts": 1,
                    "warning_alerts": 0,
                },
            }
        }
    )
    live_db_result = check_live_db_health(
        {
            "runtime_contract": {
                "environment": "dev",
                "strict_environment": False,
                "live_db_health": {
                    "healthy": False,
                    "errors": ["RDP_LIVE_DATABASE_URL missing"],
                },
            }
        }
    )

    assert alert_result.passed is False
    assert alert_result.severity == "block"
    assert live_db_result.passed is True
    assert live_db_result.severity == "warn"


def test_build_gate_context_includes_runtime_contract(tmp_path: Path) -> None:
    _seed_rdp_health_artifacts(tmp_path)

    with (
        patch.dict(os.environ, {"RDP_ENV": "prod"}, clear=False),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.load_recommendation_registry",
            return_value={"recommendations": []},
        ),
        patch(
            "aats.data_platform.decision_system.recommendation_registry.find_recommendation",
            return_value=None,
        ),
        patch(
            "aats.data_platform.governance.parameter_registry.load_registry",
            return_value={"parameter_sets": [{"parameter_set_id": "ps_1"}]},
        ),
        patch(
            "aats.data_platform.live_query_adapter.check_live_db_health",
            return_value={"healthy": True, "connection_ok": True, "tables_checked": {}},
        ),
    ):
        ctx = build_gate_context(tmp_path, "rec_contract")

    runtime_contract = ctx["runtime_contract"]
    assert runtime_contract["version"] == 1
    assert runtime_contract["environment"] == "prod"
    assert runtime_contract["strict_environment"] is True
    assert "current_alerts" in runtime_contract
    assert "latest_workflow_runs" in runtime_contract
    assert "live_db_health" in runtime_contract
    assert ctx["current_alerts"] == runtime_contract["current_alerts"]
    assert ctx["latest_workflow_runs"] == runtime_contract["latest_workflow_runs"]
    assert ctx["live_db_health"] == runtime_contract["live_db_health"]
