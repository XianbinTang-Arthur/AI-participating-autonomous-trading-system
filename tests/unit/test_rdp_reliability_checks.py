from __future__ import annotations

from pathlib import Path

from aats.data_platform.operations.reliability_checks import (
    check_active_decisions_exists,
    check_data_governance_monitoring,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_active_decision_check_uses_db_first_registry(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "aats.data_platform.decision_system.recommendation_registry."
        "load_active_decision_registry",
        lambda _path: {"decisions": [{"combo_key": "independent_15m"}]},
    )

    result = check_active_decisions_exists(tmp_path)

    assert result.passed is True
    assert "1 decisions" in result.detail


def test_runtime_image_precreates_reliability_output_directories() -> None:
    dockerfile = (_REPO_ROOT / "deploy/wsl2-dev/Dockerfile").read_text(encoding="utf-8")

    assert "/app/artifacts/operations/workflow_runs" in dockerfile
    assert "/app/artifacts/operations/alerts" in dockerfile


def test_data_governance_critical_monitor_becomes_reliability_alert(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "aats.api.rdp_data_governance.build_data_governance_snapshot",
        lambda _root: {
            "status": "ready",
            "monitoring": {"status": "critical", "alert_count": 2},
        },
    )

    result = check_data_governance_monitoring(tmp_path)

    assert result.passed is False
    assert result.severity == "critical"
    assert result.detail == "data governance has 2 active alert(s)"
