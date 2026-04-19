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
            "aats.data_platform.operations.workflow_scheduler.db_create_task_if_idle",
            return_value=("task_bootstrap_data_1", None),
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
            "aats.data_platform.operations.workflow_scheduler.db_create_task_if_idle",
            return_value=("task_bootstrap_research_1", None),
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
            "aats.data_platform.operations.workflow_scheduler.db_create_task_if_idle",
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
        # db_create_task_if_idle 返回 (None, existing_dict) —— atomic insert
        # 在并发 / 已有活跃任务时走 ON CONFLICT DO NOTHING 分支。
        patch(
            "aats.data_platform.operations.workflow_scheduler.db_create_task_if_idle",
            return_value=(None, {"task_id": "task_existing"}),
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
            "aats.data_platform.operations.workflow_scheduler.db_create_task_if_idle",
        ) as create_task_mock,
    ):
        result = enqueue_due_workflows(tmp_path, now=now, save_state=True, initialize_if_missing=False)

    assert result["enqueued"] == []
    assert result["skipped"][0]["reason"] == "当前窗口已处理"
    create_task_mock.assert_not_called()


def test_scheduler_bootstrap_blocks_when_data_maintenance_failed(tmp_path: Path) -> None:
    """回归：data_maintenance 上一次 failed 时，bootstrap 不能推进到 research_cycle，
    也不能误判"已完成"把 research_cycle 当成下一阶段入队；应继续在 data_maintenance
    阶段重试（由 _enqueue_single_workflow + db_has_active_task 控制去重）。
    """
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
    now = datetime(2026, 4, 16, 12, 30, tzinfo=UTC)
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

    def _latest_task(_session, workflow, **kwargs):
        # 关键：latest_success 过滤 statuses=("done",) —— failed 任务不会被返回，
        # 等同于"没完成"，所以 bootstrap_stage 必须继续卡在 data_maintenance。
        requested_statuses = kwargs.get("statuses") or ()
        if workflow == "data_maintenance" and "done" not in requested_statuses:
            return {"task_id": "task_failed_data_1", "status": "failed"}
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
            "aats.data_platform.operations.workflow_scheduler.db_create_task_if_idle",
            return_value=("task_bootstrap_data_retry", None),
        ),
    ):
        result = enqueue_due_workflows(tmp_path, now=now, save_state=True, initialize_if_missing=False)

    # 只允许 data_maintenance 继续重试，绝对不能放行 research_cycle 进队。
    enqueued_workflows = [item["workflow"] for item in result["enqueued"]]
    assert enqueued_workflows == ["data_maintenance"]
    assert "research_cycle" not in enqueued_workflows
    state = load_scheduler_state(tmp_path)
    assert state["bootstrap_stage"] == "data_maintenance"
    assert "bootstrap_completed_at" not in state or state.get("bootstrap_completed_at") is None


def test_scheduler_bootstrap_emits_warning_when_stage_failed(tmp_path: Path) -> None:
    """M2 回归：bootstrap 阶段最近一次任务 failed 时，report.warnings 必须出现告警。

    bootstrap 只认 status=done，failed 任务既不会推进阶段、也不会自动撤回。
    必须让运营者通过 API report 的 warnings 字段看到"卡在失败任务"的信号，
    否则系统会安静地永远停在该阶段。
    """
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
    now = datetime(2026, 4, 16, 12, 40, tzinfo=UTC)
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

    def _latest_task(_session, workflow, **kwargs):
        requested_statuses = kwargs.get("statuses") or ()
        if workflow == "data_maintenance" and "done" not in requested_statuses:
            # 查"任意状态"时返回 failed 任务 —— 触发 warning 路径
            return {
                "task_id": "task_failed_boot_42",
                "status": "failed",
                "error_message": "okx fetch timeout",
                "finished_at": "2026-04-16T12:30:00+00:00",
            }
        # 查 done 过滤时返回 None（表示 bootstrap 尚未完成）
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
            "aats.data_platform.operations.workflow_scheduler.db_create_task_if_idle",
            return_value=("task_bootstrap_data_retry", None),
        ),
    ):
        result = enqueue_due_workflows(
            tmp_path, now=now, save_state=True, initialize_if_missing=False
        )

    warnings = result.get("warnings") or []
    failure_warnings = [w for w in warnings if w.get("workflow") == "data_maintenance"]
    assert failure_warnings, "failed 任务必须在 report.warnings 中暴露为运营信号"
    warning = failure_warnings[0]
    assert "失败" in warning.get("reason", ""), "warning.reason 必须说明是失败原因"
    assert warning.get("task_id") == "task_failed_boot_42"
    assert warning.get("error_message") == "okx fetch timeout"
    # 同时验证该阶段仍然被重新入队（warning 不能阻断重试，
    # 否则运营者看到告警但 workflow 没有任何进一步尝试）
    enqueued_workflows = [item["workflow"] for item in result["enqueued"]]
    assert "data_maintenance" in enqueued_workflows


def test_scheduler_bootstrap_blocks_when_research_cycle_failed(tmp_path: Path) -> None:
    """回归：data_maintenance 已完成、research_cycle 上一次 failed 时，bootstrap 不能
    误标"bootstrap_completed_at"，也不能放行非 bootstrap workflow。
    """
    _write_state(
        tmp_path,
        {
            "initialized_at": "2026-04-16T12:00:00+00:00",
            "bootstrap_stage": "research_cycle",
            "workflows": {
                "data_maintenance": {"last_action": "bootstrap_completed"},
                "research_cycle": {"last_action": "bootstrap_enqueued"},
                "governance_cycle": {"last_action": "initialized"},
            },
        },
    )
    now = datetime(2026, 4, 16, 13, 30, tzinfo=UTC)
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

    def _latest_task(_session, workflow, **kwargs):
        requested_statuses = kwargs.get("statuses") or ()
        if workflow == "research_cycle" and "done" not in requested_statuses:
            return {"task_id": "task_failed_research_1", "status": "failed"}
        # 查 done 的时候都返回 None —— research_cycle 没完成过
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
            "aats.data_platform.operations.workflow_scheduler.db_create_task_if_idle",
            return_value=("task_bootstrap_research_retry", None),
        ),
    ):
        result = enqueue_due_workflows(tmp_path, now=now, save_state=True, initialize_if_missing=False)

    enqueued_workflows = [item["workflow"] for item in result["enqueued"]]
    # research_cycle 可以重试，但 governance_cycle 绝对不能被当作"bootstrap 之后的
    # 正常 workflow"放行
    assert "governance_cycle" not in enqueued_workflows
    assert enqueued_workflows == ["research_cycle"]
    state = load_scheduler_state(tmp_path)
    assert state["bootstrap_stage"] == "research_cycle"
    assert state.get("bootstrap_completed_at") is None


# ─────────────────────────────────────────────────────────────────────────
# P1-D Phase 1A (2026-04-20): custom frequency 单元测试
# 支持 `microstructure_silver_15m` 的 interval_minutes=15 调度.
# 参见 docs/operations/p1d_phase1a_predeploy_checklist.md §2.3
# ─────────────────────────────────────────────────────────────────────────


def test_get_workflow_schedule_accepts_custom_frequency() -> None:
    from aats.data_platform.operations.workflow_scheduler import get_workflow_schedule

    schedule = {"enabled": True, "frequency": "custom", "interval_minutes": 15}
    result = get_workflow_schedule({"schedule": schedule})
    assert result == schedule


def test_get_workflow_schedule_rejects_unknown_frequency() -> None:
    from aats.data_platform.operations.workflow_scheduler import get_workflow_schedule

    schedule = {"enabled": True, "frequency": "monthly", "day": 1}
    assert get_workflow_schedule({"schedule": schedule}) is None


def test_custom_frequency_latest_slot_aligns_to_interval() -> None:
    from aats.data_platform.operations.workflow_scheduler import _latest_slot_for_schedule

    schedule = {"enabled": True, "frequency": "custom", "interval_minutes": 15}

    # 12:37:42 应该落到 12:30 slot
    now = datetime(2026, 4, 20, 12, 37, 42, tzinfo=UTC)
    assert _latest_slot_for_schedule(schedule, now=now) == datetime(
        2026, 4, 20, 12, 30, tzinfo=UTC,
    )

    # 12:00:00 应该落到 12:00 slot (边界)
    now = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
    assert _latest_slot_for_schedule(schedule, now=now) == datetime(
        2026, 4, 20, 12, 0, tzinfo=UTC,
    )

    # 12:14:59 应该落到 12:00 slot (未到 12:15)
    now = datetime(2026, 4, 20, 12, 14, 59, tzinfo=UTC)
    assert _latest_slot_for_schedule(schedule, now=now) == datetime(
        2026, 4, 20, 12, 0, tzinfo=UTC,
    )


def test_custom_frequency_slot_uses_default_interval_when_missing() -> None:
    from aats.data_platform.operations.workflow_scheduler import _latest_slot_for_schedule

    # 没写 interval_minutes 默认 15
    schedule = {"enabled": True, "frequency": "custom"}
    now = datetime(2026, 4, 20, 12, 37, 42, tzinfo=UTC)
    assert _latest_slot_for_schedule(schedule, now=now) == datetime(
        2026, 4, 20, 12, 30, tzinfo=UTC,
    )


def test_format_schedule_custom_frequency_string() -> None:
    from aats.data_platform.operations.workflow_scheduler import format_schedule

    schedule = {"enabled": True, "frequency": "custom", "interval_minutes": 15}
    assert format_schedule(schedule) == "custom every 15min (UTC aligned)"

    schedule = {"enabled": True, "frequency": "custom", "interval_minutes": 5}
    assert format_schedule(schedule) == "custom every 5min (UTC aligned)"
