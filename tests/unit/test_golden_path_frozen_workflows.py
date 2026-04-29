"""
Frozen golden-path workflow 去冲突语义锁定测试。

对应 task: 关闭 decision_cycle / release_cycle 的自动调度,
并关闭 governance_cycle.auto_import_candidates 自动导入, 同时保留
observation_cycle / reliability_cycle / governance_cycle.quality_monitor
这些 frozen golden-path 仍需运行的 workflow / task. research_cycle
恢复 weekly 自动调度，因为完整 RDP 只产生治理建议，发布仍需审批和 gate。

这些测试直接读取 configs/rdp_workflows/*.json, 锁定 semantic_freeze 状态。
research_cycle 的 full_pipeline 保持自动与人工均可触发，因为 UI 的
“运行完整 RDP”语义必须真实执行完整研究闭环。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_CONFIG_DIR = (
    Path(__file__).resolve().parents[2] / "configs" / "rdp_workflows"
)


def _load(name: str) -> dict:
    return json.loads((_CONFIG_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _task(cfg: dict, name: str) -> dict:
    for task in cfg.get("tasks", []):
        if task.get("name") == name:
            return task
    raise AssertionError(f"{cfg.get('workflow')} 中找不到 task {name!r}")


# ── research_cycle ──────────────────────────────────────────────

def test_research_cycle_schedule_enabled() -> None:
    assert _load("research_cycle")["schedule"]["enabled"] is True


def test_research_cycle_full_pipeline_task_enabled_for_manual_trigger() -> None:
    cfg = _load("research_cycle")
    assert _task(cfg, "full_pipeline")["enabled"] is True


# ── decision_cycle ──────────────────────────────────────────────

def test_decision_cycle_schedule_disabled() -> None:
    assert _load("decision_cycle")["schedule"]["enabled"] is False


def test_decision_cycle_strategy_tuning_review_disabled() -> None:
    cfg = _load("decision_cycle")
    assert _task(cfg, "strategy_tuning_review")["enabled"] is False


@pytest.mark.parametrize("task_name", ["reliability_check", "observation_check"])
def test_decision_cycle_fallback_tasks_still_enabled(task_name: str) -> None:
    """reliability / observation 兜底 tick 仍需可用, 只锁"未被此 patch 误关"。"""
    cfg = _load("decision_cycle")
    assert _task(cfg, task_name)["enabled"] is True


# ── release_cycle ───────────────────────────────────────────────

def test_release_cycle_schedule_disabled() -> None:
    assert _load("release_cycle")["schedule"]["enabled"] is False


def test_release_cycle_config_still_present() -> None:
    """release_cycle 只去自动化, 不删 workflow 本身, 未来可手动触发。"""
    cfg = _load("release_cycle")
    assert cfg["workflow"] == "release_cycle"
    assert cfg.get("tasks"), "release_cycle 不能被清空"


# ── governance_cycle ────────────────────────────────────────────

def test_governance_cycle_auto_import_candidates_disabled() -> None:
    cfg = _load("governance_cycle")
    assert _task(cfg, "auto_import_candidates")["enabled"] is False


@pytest.mark.parametrize(
    "task_name",
    ["quality_monitor", "artifact_validation", "active_rounds_refresh"],
)
def test_governance_cycle_other_tasks_still_enabled(task_name: str) -> None:
    cfg = _load("governance_cycle")
    assert _task(cfg, task_name)["enabled"] is True


def test_governance_cycle_schedule_still_enabled() -> None:
    """governance 本身 daily schedule 不关, 仅关 auto_import_candidates 子任务。"""
    assert _load("governance_cycle")["schedule"]["enabled"] is True


# ── 对照：observation / reliability workflow 仍在自动运行 ────────

@pytest.mark.parametrize("workflow", ["observation_cycle", "reliability_cycle"])
def test_peer_cycles_still_enabled(workflow: str) -> None:
    assert _load(workflow)["schedule"]["enabled"] is True
