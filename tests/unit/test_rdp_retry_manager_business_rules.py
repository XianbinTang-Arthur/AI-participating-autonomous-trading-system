from __future__ import annotations

import json
from pathlib import Path

from aats.data_platform.operations import retry_manager


def _write_workflow(root: Path, name: str, *, enabled: bool = True) -> None:
    path = root / "configs" / "rdp_workflows" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "workflow": name,
                "tasks": [
                    {
                        "name": "task_a",
                        "command": "python task_a.py",
                        "enabled": enabled,
                    },
                ],
            },
        ),
        encoding="utf-8",
    )


def test_retry_single_task_uses_dispatcher_execution_contract(monkeypatch, tmp_path: Path) -> None:
    _write_workflow(tmp_path, "governance_cycle")
    monkeypatch.setattr(
        retry_manager,
        "find_failure",
        lambda *_args: {
            "failure_id": "fail_1",
            "status": "open",
            "workflow": "governance_cycle",
            "task_name": "task_a",
        },
    )
    captured: dict[str, object] = {}

    def _run(root, task):
        captured["root"] = root
        captured["task"] = task
        return {
            "status": "success",
            "finished_at": "2026-08-25T00:00:00+00:00",
            "output_tail": "ok",
        }

    monkeypatch.setattr(retry_manager, "_run_task", _run)
    monkeypatch.setattr(retry_manager, "record_retry_attempt", lambda *_a, **_kw: None)

    result = retry_manager.retry_single_task(
        tmp_path,
        "fail_1",
        timeout_override=42,
    )

    assert result["success"] is True
    assert captured["root"] == tmp_path
    assert captured["task"]["timeout_seconds"] == 42


def test_retry_manager_cannot_bypass_release_cycle_freeze(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        retry_manager,
        "find_failure",
        lambda *_args: {
            "failure_id": "fail_release",
            "status": "open",
            "workflow": "release_cycle",
            "task_name": "release",
        },
    )

    result = retry_manager.retry_single_task(tmp_path, "fail_release")

    assert result["success"] is False
    assert result["blocked"] is True
    assert "golden-path freeze" in result["detail"]
