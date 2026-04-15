from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aats.services.operator.rdp_queries as rdp_queries


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_query_latest_decision_round_prefers_db_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    round_dir = tmp_path / "artifacts" / "decision_rounds" / "20260401_000000_deadbeef"
    round_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        rdp_queries,
        "_load_latest_decision_round_from_db",
        lambda: {
            "available": True,
            "data_source": "db",
            "round_id": "20260415_190910_d412ef64",
            "round_dir": None,
            "started_at": "2026-04-15T19:07:32+00:00",
            "finished_at": "2026-04-15T19:09:10+00:00",
            "parameter_upgrade_candidates": [{"combo_key": "directional_1h"}],
            "family_timeframe_decisions": [{"combo_key": "directional_1h"}],
            "promotion_readiness_assessment": {"overall_status": "blocked"},
            "has_conclusion_report": True,
        },
    )

    result = rdp_queries.query_latest_decision_round(tmp_path)

    assert result["available"] is True
    assert result["data_source"] == "db"
    assert result["round_id"] == "20260415_190910_d412ef64"
    assert result["parameter_upgrade_candidates"][0]["combo_key"] == "directional_1h"


def test_query_latest_decision_round_file_fallback_supports_real_output_filenames(
    tmp_path: Path,
    monkeypatch,
) -> None:
    round_dir = tmp_path / "artifacts" / "decision_rounds" / "20260415_190910_d412ef64"
    round_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        round_dir / "round_manifest.json",
        {
            "round_id": "20260415_190910_d412ef64",
            "started_at": "2026-04-15T19:07:32+00:00",
            "finished_at": "2026-04-15T19:09:10+00:00",
        },
    )
    _write_json(round_dir / "evidence_summary.json", {"summary": "ok"})
    _write_json(round_dir / "parameter_upgrade_candidates.json", [{"combo_key": "directional_1h"}])
    _write_json(round_dir / "family_timeframe_decisions.json", [{"combo_key": "directional_1h"}])
    _write_json(round_dir / "promotion_readiness_report.json", {"overall_status": "blocked"})
    (round_dir / "phase6_closed_loop_decision_conclusion.md").write_text(
        "# conclusion\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(rdp_queries, "_load_latest_decision_round_from_db", lambda: None)

    result = rdp_queries.query_latest_decision_round(tmp_path)

    assert result["available"] is True
    assert result["data_source"] == "file"
    assert result["round_id"] == "20260415_190910_d412ef64"
    assert result["evidence_bundle_summary"] == {"summary": "ok"}
    assert result["promotion_readiness_assessment"] == {"overall_status": "blocked"}
    assert result["has_conclusion_report"] is True


def test_query_rdp_health_uses_decision_round_snapshot_for_governance_and_decision_freshness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "aats.data_platform.operations.environment_guard.get_current_environment",
        lambda: "dev",
    )
    monkeypatch.setattr(
        rdp_queries,
        "_query_governance_runtime_state",
        lambda: {
            "connection_ok": True,
            "errors": [],
            "runtime_components": [],
            "task_queue": {
                "pending_count": 0,
                "running_count": 0,
                "failed_count": 0,
            },
        },
    )
    monkeypatch.setattr(rdp_queries, "_check_db_initialization", lambda *_args, **_kwargs: (True, True))
    monkeypatch.setattr(
        rdp_queries,
        "_collect_latest_workflow_runs",
        lambda _root: {
            "data_maintenance": {
                "workflow": "data_maintenance",
                "overall_status": "success",
                "finished_at": (now - timedelta(hours=1)).isoformat(),
            },
        },
    )
    monkeypatch.setattr(
        rdp_queries,
        "_load_latest_decision_round_from_db",
        lambda: {
            "available": True,
            "data_source": "db",
            "round_id": "20260415_190910_d412ef64",
            "started_at": (now - timedelta(hours=2)).isoformat(),
            "finished_at": (now - timedelta(hours=1)).isoformat(),
        },
    )
    monkeypatch.setattr(
        "aats.data_platform.live_query_adapter.check_live_db_health",
        lambda: {"healthy": True, "connection_ok": True, "tables_checked": {}, "errors": []},
    )
    monkeypatch.setattr(
        "aats.bootstrap.active_parameters.load_all_active_parameter_sets",
        lambda project_root: {"independent_15m": {"parameter_set_id": "ps_live_1"}},
    )

    health = rdp_queries.query_rdp_health(tmp_path)
    workflow_check = next(item for item in health["checks"] if item["category"] == "workflow_runs")

    assert workflow_check["status"] == "ok"
    assert "governance_cycle=ok:" in workflow_check["detail"]
    assert "decision_cycle=ok:" in workflow_check["detail"]
    assert "workflow_runs_stale_or_missing" not in health["warnings"]
