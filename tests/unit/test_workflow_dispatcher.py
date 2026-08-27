from __future__ import annotations

import io
import json
import runpy
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from aats.data_platform.operations.workflow_dispatcher import (
    _classify_task_failure,
    describe_manual_trigger_availability,
    run_workflow,
)


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_workflow_dispatcher_captures_output_tail_for_successful_task(tmp_path: Path) -> None:
    script = tmp_path / "emit.py"
    script.write_text(
        "print('phase2 ok')\n"
        "print('phase3 ok')\n"
        "print('phase4 ok')\n"
        "print('phase5 ok')\n"
        "print('decision ok')\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "demo.json",
        {
            "workflow": "demo",
            "tasks": [
                {
                    "name": "full_pipeline",
                    "command": f"python {script}",
                    "enabled": True,
                    "allow_failure": False,
                    "timeout_seconds": 30,
                },
            ],
        },
    )

    report = run_workflow(tmp_path, "demo")

    assert report["overall_status"] == "success"
    task = report["tasks"][0]
    assert task["status"] == "success"
    assert "decision ok" in (task.get("output_tail") or "")
    assert "phase2 ok" in (task.get("stdout_tail") or "")


def test_workflow_dispatcher_fails_when_success_marker_missing(tmp_path: Path) -> None:
    script = tmp_path / "emit.py"
    script.write_text("print('phase ok')\n", encoding="utf-8")
    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "demo.json",
        {
            "workflow": "demo",
            "tasks": [
                {
                    "name": "decision_round",
                    "command": f"python {script}",
                    "enabled": True,
                    "allow_failure": False,
                    "timeout_seconds": 30,
                    "success_markers": ["Phase 6 Decision Round completed"],
                },
            ],
        },
    )

    report = run_workflow(tmp_path, "demo")

    assert report["overall_status"] == "failed"
    task = report["tasks"][0]
    assert task["status"] == "failed"
    assert task["missing_success_markers"] == ["Phase 6 Decision Round completed"]


def test_workflow_dispatcher_preserves_failed_duplicate_task_position(
    monkeypatch,
    tmp_path: Path,
) -> None:
    duplicate = {
        "name": "duplicate",
        "command": "python noop.py",
        "enabled": True,
        "allow_failure": False,
    }
    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "demo.json",
        {
            "workflow": "demo",
            "tasks": [duplicate, duplicate, {**duplicate, "name": "tail"}],
        },
    )
    call_count = 0

    def _run(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        return {
            "name": "duplicate",
            "status": "success" if call_count == 1 else "failed",
            "allow_failure": False,
            "exit_code": 1 if call_count == 2 else 0,
            "error": "deterministic failure" if call_count == 2 else None,
        }

    monkeypatch.setattr(
        "aats.data_platform.operations.workflow_dispatcher._run_task",
        _run,
    )

    report = run_workflow(tmp_path, "demo")

    assert [item["status"] for item in report["tasks"]] == [
        "success",
        "failed",
        "skipped_due_to_failure",
    ]


def test_managed_failed_workflow_cannot_silently_leave_old_db_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from aats.data_platform.governance._exceptions import DBUnavailableError

    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "demo.json",
        {
            "workflow": "demo",
            "tasks": [
                {
                    "name": "failed_task",
                    "command": "python noop.py",
                    "enabled": True,
                    "allow_failure": False,
                }
            ],
        },
    )
    monkeypatch.setattr(
        "aats.data_platform.operations.workflow_dispatcher._run_task",
        lambda *_args, **_kwargs: {
            "name": "failed_task",
            "status": "failed",
            "allow_failure": False,
            "exit_code": 1,
            "error": "deterministic failure",
        },
    )
    monkeypatch.setattr(
        "aats.data_platform.operations.workflow_dispatcher."
        "has_explicit_governance_db_configuration",
        lambda _root: True,
    )
    monkeypatch.setattr(
        "aats.data_platform.operations.workflow_dispatcher.try_governance_db",
        lambda: (None, False),
    )

    with pytest.raises(
        DBUnavailableError,
        match="managed workflow run report persistence unavailable",
    ):
        run_workflow(tmp_path, "demo")
    report_dir = tmp_path / "artifacts/operations/workflow_runs"
    assert not list(report_dir.glob("*.json"))


def test_queue_workflow_report_uses_exact_run_and_attempt_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from aats.data_platform.operations import workflow_dispatcher
    from aats.data_platform.operations.rdp_run_observer import RdpRunObserver

    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "demo.json",
        {
            "workflow": "demo",
            "tasks": [
                {
                    "name": "task",
                    "command": "python noop.py",
                    "enabled": True,
                    "allow_failure": False,
                }
            ],
        },
    )
    monkeypatch.setenv("AATS_RDP_RUN_ID", "logical_run_42")
    monkeypatch.setenv("AATS_RDP_ATTEMPT_NO", "3")
    monkeypatch.setattr(RdpRunObserver, "initialize", lambda *_args: None)
    monkeypatch.setattr(RdpRunObserver, "step_started", lambda *_args: None)
    monkeypatch.setattr(RdpRunObserver, "step_finished", lambda *_args: None)
    monkeypatch.setattr(
        workflow_dispatcher,
        "_run_task",
        lambda *_args, **_kwargs: {
            "name": "task",
            "status": "success",
            "allow_failure": False,
            "exit_code": 0,
        },
    )
    persisted: list[dict] = []
    monkeypatch.setattr(
        workflow_dispatcher,
        "_save_run_report",
        lambda _root, report: persisted.append(dict(report)),
    )

    report = run_workflow(tmp_path, "demo")

    assert report["run_id"] == "logical_run_42"
    assert report["attempt_no"] == 3
    assert report["queue_bound"] is True
    assert persisted[0]["run_id"] == "logical_run_42"
    assert persisted[0]["attempt_no"] == 3


def test_partial_queue_identity_fails_before_report_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "demo.json",
        {"workflow": "demo", "tasks": []},
    )
    monkeypatch.setenv("AATS_RDP_RUN_ID", "logical_run_without_attempt")
    monkeypatch.delenv("AATS_RDP_ATTEMPT_NO", raising=False)

    with pytest.raises(ValueError, match="must form a valid queue identity"):
        run_workflow(tmp_path, "demo")


def test_dry_run_never_persists_fresh_success_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from aats.data_platform.operations import workflow_dispatcher

    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "demo.json",
        {
            "workflow": "demo",
            "tasks": [
                {
                    "name": "preview",
                    "command": "python noop.py",
                    "enabled": True,
                    "allow_failure": False,
                }
            ],
        },
    )
    persisted: list[dict] = []
    monkeypatch.setattr(
        workflow_dispatcher,
        "_save_run_report",
        lambda _root, report: persisted.append(dict(report)),
    )

    report = run_workflow(tmp_path, "demo", dry_run=True)

    assert report["overall_status"] == "success"
    assert report["dry_run"] is True
    assert report["persistence_status"] == "skipped_dry_run"
    assert persisted == []
    assert not (tmp_path / "artifacts/operations/workflow_runs").exists()


def test_workflow_dispatcher_propagates_pipeline_warning(tmp_path: Path) -> None:
    script = tmp_path / "pipeline.py"
    script.write_text(
        "print('RDP_PIPELINE_RESULT_JSON={\"status\":\"succeeded_with_warnings\"}')\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "demo.json",
        {
            "workflow": "demo",
            "tasks": [
                {
                    "name": "full_pipeline",
                    "command": f"python {script}",
                    "enabled": True,
                    "allow_failure": False,
                },
            ],
        },
    )

    report = run_workflow(tmp_path, "demo")

    assert report["overall_status"] == "degraded"
    assert report["tasks"][0]["status"] == "success_with_warnings"
    assert "部分阶段" in report["error_summary"]


def test_workflow_dispatcher_extracts_direct_decision_result(tmp_path: Path) -> None:
    script = tmp_path / "decision.py"
    script.write_text(
        "print('Phase 6 Decision Round completed')\n"
        "print('RDP_DECISION_RESULT_JSON={\"round_id\":\"round_1\","
        "\"research_outcome\":\"blocked_by_attribution\"}')\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "demo.json",
        {
            "workflow": "demo",
            "tasks": [
                {
                    "name": "decision_round",
                    "command": f"python {script}",
                    "enabled": True,
                    "allow_failure": False,
                    "success_markers": ["Phase 6 Decision Round completed"],
                },
            ],
        },
    )

    report = run_workflow(tmp_path, "demo")

    assert report["overall_status"] == "success"
    assert report["tasks"][0]["decision_result"] == {
        "round_id": "round_1",
        "research_outcome": "blocked_by_attribution",
    }


def test_workflow_dispatcher_extracts_first_pipeline_traceback_summary(
    tmp_path: Path,
) -> None:
    script = tmp_path / "pipeline_failure.py"
    script.write_text(
        "print('Traceback (most recent call last):')\n"
        "print('  File \"phase4.py\", line 1, in <module>')\n"
        "print(\"TypeError: '<=' not supported between instances of 'str' and 'int'\")\n"
        "print('Phase 6 completed after --no-stop-on-failure')\n"
        "print('RDP_PIPELINE_RESULT_JSON={\"status\":\"partially_succeeded\","
        "\"first_failure\":{\"phase\":\"phase4\","
        "\"phase_label\":\"Phase 4\",\"status\":\"failed\","
        "\"exit_code\":1,\"error\":null}}')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "demo.json",
        {
            "workflow": "demo",
            "tasks": [
                {
                    "name": "full_pipeline",
                    "command": f"python {script}",
                    "enabled": True,
                    "allow_failure": False,
                },
            ],
        },
    )

    report = run_workflow(tmp_path, "demo")

    assert "TypeError:" in report["error_summary"]
    assert "Phase 4 failed" in report["error_summary"]


def test_failure_classifier_only_marks_known_infrastructure_errors_retryable() -> None:
    assert _classify_task_failure(
        status="failed",
        exit_code=1,
        output="sqlalchemy.exc.OperationalError: connection refused",
    ) == "transient_infrastructure"
    assert _classify_task_failure(
        status="failed",
        exit_code=1,
        output="Traceback (most recent call last): TypeError: invalid comparison",
    ) == "deterministic_code_or_contract"
    assert _classify_task_failure(
        status="failed",
        exit_code=1,
        output="research gate blocked: insufficient_data",
    ) == "business_or_data_block"
    assert _classify_task_failure(
        status="failed",
        exit_code=1,
        output='RDP_PIPELINE_RESULT_JSON={"first_failure":{"status":"timeout"}}',
    ) == "transient_infrastructure"


def test_manual_research_cycle_trigger_disabled_when_full_pipeline_frozen(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "research_cycle.json",
        {
            "workflow": "research_cycle",
            "tasks": [
                {"name": "refresh_recent_data", "command": "python refresh.py", "enabled": True},
                {"name": "full_pipeline", "command": "python full.py", "enabled": False},
            ],
        },
    )

    availability = describe_manual_trigger_availability(tmp_path, "research_cycle")

    assert availability["enabled"] is False
    assert "full_pipeline 任务已禁用" in availability["disabled_reason"]
    assert availability["enabled_task_names"] == ["refresh_recent_data"]


def test_manual_research_cycle_trigger_enabled_when_full_pipeline_enabled(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "research_cycle.json",
        {
            "workflow": "research_cycle",
            "tasks": [
                {"name": "refresh_recent_data", "command": "python refresh.py", "enabled": True},
                {"name": "full_pipeline", "command": "python full.py", "enabled": True},
            ],
        },
    )

    availability = describe_manual_trigger_availability(tmp_path, "research_cycle")

    assert availability["enabled"] is True
    assert availability["disabled_reason"] is None
    assert availability["enabled_task_names"] == ["refresh_recent_data", "full_pipeline"]


def test_manual_release_cycle_trigger_respects_golden_path_freeze(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "configs" / "rdp_workflows" / "release_cycle.json",
        {
            "workflow": "release_cycle",
            "tasks": [
                {"name": "release", "command": "python release.py", "enabled": True},
            ],
        },
    )

    availability = describe_manual_trigger_availability(tmp_path, "release_cycle")

    assert availability["enabled"] is False
    assert "golden-path freeze" in availability["disabled_reason"]


def test_rdp_run_scheduled_workflow_prints_task_output_tail(monkeypatch, tmp_path: Path) -> None:
    fake_report = {
        "run_id": "run_demo",
        "workflow": "research_cycle",
        "overall_status": "success",
        "succeeded": 2,
        "failed": 0,
        "skipped": 0,
        "tasks": [
            {
                "name": "full_pipeline",
                "status": "success",
                "output_tail": "Phase 5 ok\nPhase 6 ok",
            },
        ],
    }
    script_path = Path("scripts/rdp_run_scheduled_workflow.py").resolve()
    stdout = io.StringIO()
    argv = sys.argv[:]
    monkeypatch.setattr(
        "aats.data_platform.operations.workflow_dispatcher.run_workflow",
        lambda *args, **kwargs: fake_report,
    )
    try:
        sys.argv = [str(script_path), "--workflow", "research_cycle"]
        with redirect_stdout(stdout):
            with pytest.raises(SystemExit) as excinfo:
                runpy.run_path(str(script_path), run_name="__main__")
    finally:
        sys.argv = argv

    assert excinfo.value.code == 0
    rendered = stdout.getvalue()
    assert "Phase 5 ok" in rendered
    assert "Phase 6 ok" in rendered
    assert "RDP_WORKFLOW_RESULT_JSON=" in rendered
