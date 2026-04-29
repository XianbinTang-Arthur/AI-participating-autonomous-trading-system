from __future__ import annotations

import io
import json
import runpy
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from aats.data_platform.operations.workflow_dispatcher import (
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
