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


def test_scheduler_keeps_slot_pending_when_active_task_exists(tmp_path: Path) -> None:
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
        == "2026-04-15T10:00:00+00:00"
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


# ─────────────────────────────────────────────────────────────────────────
# P0-c Option A (2026-04-20): candles_rolling_15m workflow
# 验证 configs/rdp_workflows/candles_rolling_15m.json 被 scheduler 正确识别,
# slot 对齐到 15min 边界 (与 microstructure_silver_15m peer), 并且 schedule
# 配置 / 命令字段符合 "只 collect, 不跑 Gold/Gap/Funding" 的设计.
# 参见 docs/review/p0c_candles_silver_stale_diagnosis_2026_04_20.md §4 Option A
# ─────────────────────────────────────────────────────────────────────────


def _load_workflow_config(name: str) -> dict:
    """Helper: load configs/rdp_workflows/<name>.json as dict."""
    root = Path(__file__).resolve().parents[2]
    path = root / "configs" / "rdp_workflows" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_candles_rolling_15m_config_is_valid_schedule() -> None:
    """新 workflow 配置文件 schedule 字段能被 get_workflow_schedule 接受."""
    from aats.data_platform.operations.workflow_scheduler import get_workflow_schedule

    cfg = _load_workflow_config("candles_rolling_15m")
    schedule = get_workflow_schedule(cfg)
    assert schedule is not None
    assert schedule["enabled"] is True
    assert schedule["frequency"] == "custom"
    assert schedule["interval_minutes"] == 15


def test_candles_rolling_15m_slot_aligns_to_microstructure_cadence() -> None:
    """candles_rolling_15m 与 microstructure_silver_15m 落在同一 15min slot.

    设计意图: 两个 workflow 同 cadence, 为路线 A phase 0 提供 15min 同步的
    OHLC + microstructure 数据对齐. 本测试锁定这个对齐属性.
    """
    from aats.data_platform.operations.workflow_scheduler import _latest_slot_for_schedule

    cfg = _load_workflow_config("candles_rolling_15m")
    micro_cfg = _load_workflow_config("microstructure_silver_15m")

    now = datetime(2026, 4, 20, 3, 37, 42, tzinfo=UTC)  # 与 P0-a 实施时刻对齐
    candles_slot = _latest_slot_for_schedule(cfg["schedule"], now=now)
    micro_slot = _latest_slot_for_schedule(micro_cfg["schedule"], now=now)

    assert candles_slot == micro_slot, (
        f"candles_rolling_15m slot {candles_slot} 与 microstructure {micro_slot} 不同步; "
        f"两者应在 15min 边界严格对齐, 否则 OHLC/microstructure 对比分析会出现 T±15min 错位."
    )
    # 具体值: 03:37:42 应落到 03:30:00
    assert candles_slot == datetime(2026, 4, 20, 3, 30, tzinfo=UTC)


def test_candles_rolling_15m_command_excludes_gold_gap_funding() -> None:
    """rolling 只做 collect, Gold/Gap/Funding 留给 data_maintenance.

    断言 command 含 --no-gold / --no-gap-check / --no-funding, 并限制 --timeframes 15m.
    保留这个测试防止后续无意改回"全量 pipeline 每 15min 跑一次"导致:
      - 每天多 2304 次 Gold 重建 (vs 4 次, 576x 冗余)
      - funding 8h cadence 的 OKX REST 每 15min 拉一次 (64x 冗余)
    """
    cfg = _load_workflow_config("candles_rolling_15m")
    assert len(cfg["tasks"]) == 1
    cmd = cfg["tasks"][0]["command"]
    assert "rdp_run_daily_ingest.py" in cmd
    assert "--timeframes 15m" in cmd
    assert "--no-gold" in cmd
    assert "--no-gap-check" in cmd
    assert "--no-funding" in cmd


def test_candles_rolling_15m_task_allows_failure() -> None:
    """单次 15min tick 失败不应阻塞 daily 主链路, 与 microstructure_silver_15m 一致."""
    cfg = _load_workflow_config("candles_rolling_15m")
    task = cfg["tasks"][0]
    assert task["allow_failure"] is True
    assert task["enabled"] is True


# ─────────────────────────────────────────────────────────────────────────
# Gap backfill (2026-04-23): daemon 停机后重启能把错过的 slot 补齐,
# 不只是跳到 latest. 对应 _iter_due_slots 与 steady-state 主循环的新行为.
# ─────────────────────────────────────────────────────────────────────────


def test_iter_due_slots_returns_all_missing_slots_between_last_and_latest() -> None:
    from aats.data_platform.operations.workflow_scheduler import _iter_due_slots

    schedule = {"enabled": True, "frequency": "custom", "interval_minutes": 15}
    now = datetime(2026, 4, 23, 12, 2, tzinfo=UTC)
    last_processed = datetime(2026, 4, 23, 11, 0, tzinfo=UTC)
    assert _iter_due_slots(schedule, now=now, last_processed=last_processed) == [
        datetime(2026, 4, 23, 11, 15, tzinfo=UTC),
        datetime(2026, 4, 23, 11, 30, tzinfo=UTC),
        datetime(2026, 4, 23, 11, 45, tzinfo=UTC),
        datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
    ]


def test_iter_due_slots_empty_when_last_processed_is_latest() -> None:
    from aats.data_platform.operations.workflow_scheduler import _iter_due_slots

    schedule = {"enabled": True, "frequency": "hourly", "interval_hours": 1}
    now = datetime(2026, 4, 23, 12, 5, tzinfo=UTC)
    last_processed = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    assert _iter_due_slots(schedule, now=now, last_processed=last_processed) == []


def test_iter_due_slots_none_last_processed_returns_only_latest() -> None:
    from aats.data_platform.operations.workflow_scheduler import _iter_due_slots

    schedule = {"enabled": True, "frequency": "daily", "hour_utc": 4, "minute_utc": 0}
    now = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    assert _iter_due_slots(schedule, now=now, last_processed=None) == [
        datetime(2026, 4, 23, 4, 0, tzinfo=UTC),
    ]


def test_iter_due_slots_weekly_spans_multiple_weeks() -> None:
    from aats.data_platform.operations.workflow_scheduler import _iter_due_slots

    schedule = {
        "enabled": True,
        "frequency": "weekly",
        "weekday_utc": "SUN",
        "hour_utc": 8,
        "minute_utc": 0,
    }
    # 2026-04-05 Sun → 2026-04-26 Sun, three missing Sundays in between
    now = datetime(2026, 4, 26, 10, 0, tzinfo=UTC)
    last_processed = datetime(2026, 4, 5, 8, 0, tzinfo=UTC)
    assert _iter_due_slots(schedule, now=now, last_processed=last_processed) == [
        datetime(2026, 4, 12, 8, 0, tzinfo=UTC),
        datetime(2026, 4, 19, 8, 0, tzinfo=UTC),
        datetime(2026, 4, 26, 8, 0, tzinfo=UTC),
    ]


def test_scheduler_coalesces_missing_custom_slots_into_latest_window(
    tmp_path: Path,
) -> None:
    """滚动窗口命令不接受历史 slot，停机缺口必须合并为一次最新窗口执行。"""
    _write_state(
        tmp_path,
        {
            "initialized_at": "2026-04-20T00:00:00+00:00",
            "bootstrap_completed_at": "2026-04-20T00:10:00+00:00",
            "workflows": {
                "candles_rolling_15m": {
                    "last_processed_slot": "2026-04-23T11:00:00+00:00",
                    "last_action": "enqueued",
                },
            },
        },
    )
    now = datetime(2026, 4, 23, 12, 2, tzinfo=UTC)
    schedule = {"enabled": True, "frequency": "custom", "interval_minutes": 15}

    task_counter = {"n": 0}

    def _create_task(_session, **_kwargs):
        task_counter["n"] += 1
        return (f"task_backfill_{task_counter['n']}", None)

    with (
        patch(
            "aats.data_platform.operations.workflow_scheduler.list_available_workflows",
            return_value=["candles_rolling_15m"],
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
            "aats.data_platform.operations.workflow_scheduler.db_create_task_if_idle",
            side_effect=_create_task,
        ),
    ):
        result = enqueue_due_workflows(
            tmp_path, now=now, save_state=True, initialize_if_missing=False,
        )

    enqueued_slots = [item["slot"] for item in result["enqueued"]]
    assert enqueued_slots == [
        "2026-04-23T12:00:00+00:00",
    ]
    assert task_counter["n"] == 1
    assert result["coalesced"] == [
        {
            "workflow": "candles_rolling_15m",
            "slot_count": 4,
            "from_slot": "2026-04-23T11:15:00+00:00",
            "to_slot": "2026-04-23T12:00:00+00:00",
            "reason": "滚动窗口工作流合并为一次最新窗口执行",
        },
    ]
    state = load_scheduler_state(tmp_path)
    assert (
        state["workflows"]["candles_rolling_15m"]["last_processed_slot"]
        == "2026-04-23T12:00:00+00:00"
    )


def test_scheduler_coalesced_slot_remains_pending_when_active_task_exists(
    tmp_path: Path,
) -> None:
    """已有任务不代表最新窗口已覆盖，不能提前推进调度水位。"""
    _write_state(
        tmp_path,
        {
            "initialized_at": "2026-04-20T00:00:00+00:00",
            "bootstrap_completed_at": "2026-04-20T00:10:00+00:00",
            "workflows": {
                "candles_rolling_15m": {
                    "last_processed_slot": "2026-04-23T11:00:00+00:00",
                    "last_action": "enqueued",
                },
            },
        },
    )
    now = datetime(2026, 4, 23, 11, 46, tzinfo=UTC)  # latest = 11:45
    schedule = {"enabled": True, "frequency": "custom", "interval_minutes": 15}

    def _create_task(_session, **_kwargs):
        return (None, {"task_id": "task_existing"})

    with (
        patch(
            "aats.data_platform.operations.workflow_scheduler.list_available_workflows",
            return_value=["candles_rolling_15m"],
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
            "aats.data_platform.operations.workflow_scheduler.db_create_task_if_idle",
            side_effect=_create_task,
        ),
    ):
        result = enqueue_due_workflows(
            tmp_path, now=now, save_state=True, initialize_if_missing=False,
        )

    assert result["enqueued"] == []
    skipped_slots = [
        item["slot"]
        for item in result["skipped"]
        if item.get("workflow") == "candles_rolling_15m"
    ]
    assert skipped_slots == ["2026-04-23T11:45:00+00:00"]
    state = load_scheduler_state(tmp_path)
    assert (
        state["workflows"]["candles_rolling_15m"]["last_processed_slot"]
        == "2026-04-23T11:00:00+00:00"
    )


def test_rdp_run_daily_ingest_has_no_funding_flag() -> None:
    """--no-funding flag 必须存在 — candles_rolling_15m command 依赖它."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "rdp_run_daily_ingest.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True, text=True, timeout=30,
    )
    # --help 退出码 0
    assert result.returncode == 0, f"--help exit {result.returncode}: {result.stderr}"
    assert "--no-funding" in result.stdout, (
        "rdp_run_daily_ingest.py --no-funding flag 缺失; "
        "candles_rolling_15m workflow 依赖此 flag 跳过重复 funding 拉取."
    )
