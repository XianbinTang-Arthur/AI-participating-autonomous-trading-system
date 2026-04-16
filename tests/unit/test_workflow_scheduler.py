from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from aats.data_platform.operations.workflow_scheduler import (
    enqueue_due_workflows,
    load_scheduler_state,
)


@contextmanager
def _fake_session():
    yield object()


def _write_state(root: Path, payload: dict) -> None:
    path = root / "artifacts/operations/workflow_scheduler_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_scheduler_initializes_without_backfilling(tmp_path: Path) -> None:
    now = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
    data_schedule = {"enabled": True, "frequency": "daily", "hour_utc": 4, "minute_utc": 0}
    research_schedule = {
        "enabled": True,
        "frequency": "weekly",
        "weekday_utc": "SUN",
        "hour_utc": 8,
        "minute_utc": 0,
    }
    governance_schedule = {"enabled": True, "frequency": "daily", "hour_utc": 7, "minute_utc": 0}
    schedules = {
        "data_maintenance": data_schedule,
        "research_cycle": research_schedule,
        "governance_cycle": governance_schedule,
    }

    with (
        patch(
            "aats.data_platform.operations.workflow_scheduler.list_available_workflows",
            return_value=list(schedules.keys()),
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.load_workflow_config",
            side_effect=lambda _root, workflow: {"schedule": schedules[workflow]},
        ),
    ):
        result = enqueue_due_workflows(tmp_path, now=now, save_state=True)

    assert result["initialized"] is True
    assert result["enqueued"] == []
    state = load_scheduler_state(tmp_path)
    assert state["initialized_at"] == now.isoformat()
    assert state["bootstrap_stage"] == "data_maintenance"
    assert state["workflows"]["data_maintenance"]["last_action"] == "bootstrap_pending"
    assert state["workflows"]["research_cycle"]["last_action"] == "bootstrap_pending"
    assert "governance_cycle" in state["workflows"]
    assert state["workflows"]["governance_cycle"]["last_action"] == "initialized"


def test_scheduler_cold_start_bootstrap_only_enqueues_data_maintenance(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        {
            "initialized_at": "2026-04-16T12:00:00+00:00",
            "bootstrap_stage": "data_maintenance",
            "workflows": {
                "data_maintenance": {"last_action": "bootstrap_pending"},
                "research_cycle": {"last_action": "bootstrap_pending"},
                "governance_cycle": {
                    "last_processed_slot": "2026-04-16T07:00:00+00:00",
                    "last_action": "initialized",
                },
            },
        },
    )
    now = datetime(2026, 4, 16, 12, 10, tzinfo=UTC)
    schedules = {
        "data_maintenance": {"enabled": True, "frequency": "daily", "hour_utc": 4, "minute_utc": 0},
        "research_cycle": {
            "enabled": True,
            "frequency": "weekly",
            "weekday_utc": "SUN",
            "hour_utc": 8,
            "minute_utc": 0,
        },
        "governance_cycle": {"enabled": True, "frequency": "daily", "hour_utc": 7, "minute_utc": 0},
    }

    with (
        patch(
            "aats.data_platform.operations.workflow_scheduler.list_available_workflows",
            return_value=list(schedules.keys()),
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.load_workflow_config",
            side_effect=lambda _root, workflow: {"schedule": schedules[workflow]},
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.guard_workflow_execution",
            return_value=type("Guard", (), {"allowed": True, "reason": None})(),
        ),
        patch("aats.data_platform.db.get_session", _fake_session),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_get_latest_task_for_workflow",
            return_value=None,
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_has_active_task",
            return_value=None,
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_create_task",
            return_value="task_bootstrap_data_1",
        ) as create_task_mock,
    ):
        result = enqueue_due_workflows(tmp_path, now=now, save_state=True, initialize_if_missing=False)

    assert [item["workflow"] for item in result["enqueued"]] == ["data_maintenance"]
    create_task_mock.assert_called_once()
    state = load_scheduler_state(tmp_path)
    assert state["workflows"]["data_maintenance"]["last_action"] == "bootstrap_enqueued"


def test_scheduler_cold_start_bootstrap_advances_to_research_after_data_refresh(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        {
            "initialized_at": "2026-04-16T12:00:00+00:00",
            "bootstrap_stage": "data_maintenance",
            "workflows": {
                "data_maintenance": {"last_action": "bootstrap_enqueued"},
                "research_cycle": {"last_action": "bootstrap_pending"},
            },
        },
    )
    now = datetime(2026, 4, 16, 12, 20, tzinfo=UTC)
    schedules = {
        "data_maintenance": {"enabled": True, "frequency": "daily", "hour_utc": 4, "minute_utc": 0},
        "research_cycle": {
            "enabled": True,
            "frequency": "weekly",
            "weekday_utc": "SUN",
            "hour_utc": 8,
            "minute_utc": 0,
        },
    }

    def _latest_task(_session, workflow, **_kwargs):
        if workflow == "data_maintenance":
            return {"task_id": "task_done_data_1", "status": "done"}
        return None

    with (
        patch(
            "aats.data_platform.operations.workflow_scheduler.list_available_workflows",
            return_value=list(schedules.keys()),
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.load_workflow_config",
            side_effect=lambda _root, workflow: {"schedule": schedules[workflow]},
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.guard_workflow_execution",
            return_value=type("Guard", (), {"allowed": True, "reason": None})(),
        ),
        patch("aats.data_platform.db.get_session", _fake_session),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_get_latest_task_for_workflow",
            side_effect=_latest_task,
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_has_active_task",
            return_value=None,
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_create_task",
            return_value="task_bootstrap_research_1",
        ),
    ):
        result = enqueue_due_workflows(tmp_path, now=now, save_state=True, initialize_if_missing=False)

    assert [item["workflow"] for item in result["enqueued"]] == ["research_cycle"]
    state = load_scheduler_state(tmp_path)
    assert state["bootstrap_stage"] == "research_cycle"
    assert state["workflows"]["research_cycle"]["last_action"] == "bootstrap_enqueued"


def test_scheduler_dry_run_does_not_create_task_or_write_state(tmp_path: Path) -> None:
    now = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
    schedule = {"enabled": True, "frequency": "daily", "hour_utc": 7, "minute_utc": 0}

    with (
        patch(
            "aats.data_platform.operations.workflow_scheduler.list_available_workflows",
            return_value=["governance_cycle"],
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.load_workflow_config",
            return_value={"schedule": schedule},
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.guard_workflow_execution",
            return_value=type("Guard", (), {"allowed": True, "reason": None})(),
        ),
        patch("aats.data_platform.db.get_session", _fake_session),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_has_active_task",
            return_value=None,
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_create_task",
        ) as create_task_mock,
    ):
        result = enqueue_due_workflows(
            tmp_path,
            now=now,
            dry_run=True,
            save_state=False,
            initialize_if_missing=False,
        )

    assert result["enqueued"][0]["workflow"] == "governance_cycle"
    assert not (tmp_path / "artifacts/operations/workflow_scheduler_state.json").exists()
    create_task_mock.assert_not_called()


def test_scheduler_marks_slot_processed_when_active_task_exists(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        {
            "initialized_at": "2026-04-15T00:00:00+00:00",
            "bootstrap_completed_at": "2026-04-15T02:00:00+00:00",
            "workflows": {
                "decision_cycle": {
                    "last_processed_slot": "2026-04-15T10:00:00+00:00",
                }
            },
        },
    )
    now = datetime(2026, 4, 16, 12, 0, tzinfo=UTC)
    schedule = {
        "enabled": True,
        "frequency": "daily",
        "hour_utc": 10,
        "minute_utc": 0,
    }

    with (
        patch(
            "aats.data_platform.operations.workflow_scheduler.list_available_workflows",
            return_value=["decision_cycle"],
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.load_workflow_config",
            return_value={"schedule": schedule},
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.guard_workflow_execution",
            return_value=type("Guard", (), {"allowed": True, "reason": None})(),
        ),
        patch("aats.data_platform.db.get_session", _fake_session),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_has_active_task",
            return_value={"task_id": "task_existing"},
        ),
    ):
        result = enqueue_due_workflows(tmp_path, now=now, save_state=True, initialize_if_missing=False)

    assert result["skipped"][0]["reason"] == "已有 active task"
    state = load_scheduler_state(tmp_path)
    assert (
        state["workflows"]["decision_cycle"]["last_processed_slot"]
        == "2026-04-16T10:00:00+00:00"
    )


def test_scheduler_treats_equivalent_offset_slot_as_already_processed(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        {
            "initialized_at": "2026-04-15T00:00:00+00:00",
            "bootstrap_completed_at": "2026-04-15T02:00:00+00:00",
            "workflows": {
                "data_maintenance": {
                    "last_processed_slot": "2026-04-16T12:00:00+08:00",
                    "last_action": "done",
                }
            },
        },
    )
    now = datetime(2026, 4, 16, 23, 10, tzinfo=UTC)
    schedule = {
        "enabled": True,
        "frequency": "daily",
        "hour_utc": 4,
        "minute_utc": 0,
    }

    with (
        patch(
            "aats.data_platform.operations.workflow_scheduler.list_available_workflows",
            return_value=["data_maintenance"],
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.load_workflow_config",
            return_value={"schedule": schedule},
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.guard_workflow_execution",
            return_value=type("Guard", (), {"allowed": True, "reason": None})(),
        ),
        patch("aats.data_platform.db.get_session", _fake_session),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_has_active_task",
            return_value=None,
        ),
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_create_task",
        ) as create_task_mock,
    ):
        result = enqueue_due_workflows(tmp_path, now=now, save_state=True, initialize_if_missing=False)

    assert result["enqueued"] == []
    assert result["skipped"][0]["reason"] == "当前窗口已处理"
    create_task_mock.assert_not_called()
