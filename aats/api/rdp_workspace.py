"""RDP Platform V3 unified operator read model.

The established RDP registries remain the source of truth.  This module projects
their current state into one versioned snapshot so the UI does not have to join
seven independently refreshed panels or recreate safety rules in JavaScript.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Request

from aats.api.rdp_control_summary import (
    _RELEASE_ALLOWED_DECISION_STATUSES,
    _promotion_qualification_allows,
    build_rdp_control_summary,
    build_rdp_workbench_bundle,
)
from aats.api.rdp_v2 import build_rdp_runs_panel
from aats.api.rdp_data_governance import build_data_governance_snapshot
from aats.data_platform.operations.workflow_dispatcher import (
    describe_manual_trigger_availability,
)
from aats.schemas.common import utc_now

log = logging.getLogger(__name__)

RDP_WORKSPACE_SCHEMA_VERSION = "rdp.workspace.v3"
_ACTIVE_RUN_STATUSES = frozenset({"queued", "running", "cancellation_requested"})
_TERMINAL_SUCCESS_STATUSES = frozenset({"succeeded", "done"})
_PRIORITY_ORDER = {
    "operator_recovery": 0,
    "operator": 1,
    "retry": 2,
    "scheduled": 3,
}
_TRIGGER_PRIORITY_CLASS = {
    "recovery": "operator_recovery",
    "manual": "operator",
    "auto_retry": "retry",
    "schedule": "scheduled",
}
_WORKFLOW_LABELS = {
    "candles_rolling_15m": "15 分钟 K 线采集",
    "data_maintenance": "数据维护",
    "decision_cycle": "决策周期",
    "governance_cycle": "治理周期",
    "microstructure_silver_15m": "微观结构 Silver",
    "observation_cycle": "发布观察",
    "okx_rest_history_rolling_1h": "OKX 历史窗口",
    "release_cycle": "自动发布",
    "reliability_cycle": "可靠性检查",
    "research_cycle": "完整 RDP 研究",
}
_RUN_STATUS_LABELS = {
    "queued": "等待执行",
    "running": "运行中",
    "cancellation_requested": "正在取消",
    "succeeded": "已成功",
    "succeeded_with_warnings": "成功（有警告）",
    "partially_succeeded": "部分成功",
    "failed": "失败",
    "cancelled": "已取消",
}


def _project_root(request: Request) -> Path:
    try:
        from aats.data_platform.config import get_settings as get_rdp_settings

        root = Path(get_rdp_settings().project_root).resolve()
        if root.exists():
            return root
    except Exception:
        log.exception("RDP workspace 无法从 settings 解析 project_root")
    return Path(".").resolve()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _daemon_projection(health: dict[str, Any]) -> dict[str, Any]:
    components = {
        str(item.get("component") or ""): item
        for item in _as_items(health.get("runtime_components"))
    }
    raw = _as_dict(components.get("rdp-daemon"))
    heartbeat = _parse_datetime(raw.get("heartbeat_at"))
    age_seconds = None
    if heartbeat is not None:
        age_seconds = max(0.0, (datetime.now(timezone.utc) - heartbeat).total_seconds())
    state = str(raw.get("status") or "unknown")
    return {
        "status": state,
        "status_label": {
            "idle": "空闲",
            "busy": "执行中",
            "starting": "启动中",
            "degraded": "降级",
            "error": "异常",
            "stopped": "已停止",
        }.get(state, "状态未知"),
        "heartbeat_at": raw.get("heartbeat_at"),
        "heartbeat_age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "fresh": bool(age_seconds is not None and age_seconds < 45),
        "active_task": raw.get("active_task"),
        "last_task": raw.get("last_task"),
    }


def _task_by_run(control: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for lane in _as_dict(control.get("tasks")).values():
        if not isinstance(lane, dict):
            continue
        for key in ("running_task", "pending_task"):
            task = _as_dict(lane.get(key))
            run_id = str(task.get("run_id") or "").strip()
            if run_id:
                result[run_id] = task
    return result


def _run_sort_key(run: dict[str, Any]) -> tuple[int, str, int, str, str]:
    now = datetime.now(timezone.utc)
    eligible_at = _parse_datetime(run.get("task_eligible_at") or run.get("eligible_at"))
    in_backoff = eligible_at is not None and eligible_at > now
    priority_class = str(run.get("priority_class") or "")
    return (
        1 if in_backoff else 0,
        eligible_at.isoformat() if in_backoff and eligible_at is not None else "",
        _PRIORITY_ORDER.get(priority_class, 4),
        str(run.get("task_created_at") or run.get("created_at") or ""),
        str(run.get("run_id") or ""),
    )


def _execution_projection(
    runs_payload: dict[str, Any],
    health: dict[str, Any],
    control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runs = _as_items(runs_payload.get("items"))
    task_by_run = _task_by_run(control or {})

    def with_task_truth(run: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(run)
        task = task_by_run.get(str(run.get("run_id") or ""), {})
        enriched.update(
            {
                "task_id": task.get("task_id"),
                "attempt_no": task.get("attempt_no"),
                "priority_class": task.get("priority_class")
                or _TRIGGER_PRIORITY_CLASS.get(str(run.get("trigger_kind") or "")),
                "task_eligible_at": task.get("earliest_start_at") or run.get("eligible_at"),
                "task_created_at": task.get("requested_at") or run.get("created_at"),
            }
        )
        return enriched

    active_runs = [
        with_task_truth(run)
        for run in runs
        if str(run.get("status") or "") in {"running", "cancellation_requested"}
    ]
    active_run = active_runs[0] if active_runs else None
    queued = sorted(
        [
            with_task_truth(run)
            for run in runs
            if str(run.get("status") or "") == "queued"
        ],
        key=_run_sort_key,
    )
    daemon = _daemon_projection(health)
    now = datetime.now(timezone.utc)
    for position, run in enumerate(queued, start=1):
        eligible_at = _parse_datetime(run.get("task_eligible_at") or run.get("eligible_at"))
        if eligible_at is not None and eligible_at > now:
            reason_code = "retry_backoff"
            wait_reason = f"重试冷却中，最早可于 {eligible_at.isoformat()} 执行。"
        elif active_run is not None:
            reason_code = "execution_slot_busy"
            active_label = _WORKFLOW_LABELS.get(
                str(active_run.get("workflow") or ""),
                str(active_run.get("workflow") or "当前任务"),
            )
            wait_reason = f"唯一研究执行槽正在运行“{active_label}”。"
        elif not daemon.get("fresh"):
            reason_code = "daemon_unavailable"
            wait_reason = "RDP daemon 心跳不新鲜，任务已保存但暂时无法被领取。"
        else:
            reason_code = "awaiting_daemon_claim"
            wait_reason = "任务已就绪，等待 RDP daemon 下一次领取。"
        run.update(
            {
                "status_label": _RUN_STATUS_LABELS["queued"],
                "queue_position": position,
                "waiting_reason_code": reason_code,
                "waiting_reason": wait_reason,
            }
        )
    if active_run is not None:
        status = str(active_run.get("status") or "running")
        active_run["status_label"] = _RUN_STATUS_LABELS.get(status, "运行中")
        active_run["queue_position"] = 0

    recent_runs: list[dict[str, Any]] = []
    for run in runs:
        enriched = dict(run)
        status = str(enriched.get("status") or "unknown")
        enriched["status_label"] = _RUN_STATUS_LABELS.get(status, "状态未知")
        if status != "queued":
            recent_runs.append(enriched)

    if active_run is not None:
        explanation = "RDP 使用单执行槽保护共享 artifact、checkpoint 和数据库连接预算。"
    elif queued and not daemon.get("fresh"):
        explanation = "队列已持久化，但 daemon 当前不具备可验证的领取能力。"
    elif queued:
        explanation = "队首任务已就绪，通常会在下一个 daemon 轮询周期被领取。"
    else:
        explanation = "当前执行槽空闲，手工 Run 会立即创建并等待 daemon 领取。"
    return {
        "capacity": 1,
        "active_count": len(active_runs),
        "capacity_violation": len(active_runs) > 1,
        "queued_count": len(queued),
        "active_run": active_run,
        "queued_runs": queued,
        "recent_runs": recent_runs[:12],
        "daemon": daemon,
        "queue_explanation": explanation,
    }


def _workflow_catalog(
    root: Path,
    control: dict[str, Any],
    execution: dict[str, Any],
) -> list[dict[str, Any]]:
    definitions_dir = root / "configs" / "rdp_workflows"
    active_runs = [
        item
        for item in [execution.get("active_run"), *execution.get("queued_runs", [])]
        if isinstance(item, dict)
    ]
    active_by_workflow = {
        str(item.get("workflow") or ""): item for item in active_runs if item.get("workflow")
    }
    task_lanes = _as_dict(control.get("tasks"))
    catalog: list[dict[str, Any]] = []
    for path in sorted(definitions_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("RDP workspace 读取 workflow 失败: path=%s error=%s", path, exc)
            continue
        if not isinstance(payload, dict):
            continue
        workflow = str(payload.get("workflow") or path.stem).strip()
        availability = describe_manual_trigger_availability(root, workflow)
        active = active_by_workflow.get(workflow)
        enabled = bool(availability.get("enabled")) and active is None
        disabled_reason = availability.get("disabled_reason")
        if active is not None:
            disabled_reason = (
                "该 workflow 已有运行中或等待执行的 Run，"
                "请直接查看现有 Run。"
            )
        schedule = _as_dict(payload.get("schedule"))
        tasks = _as_items(payload.get("tasks"))
        lane = _as_dict(task_lanes.get(workflow))
        catalog.append(
            {
                "workflow": workflow,
                "label": _WORKFLOW_LABELS.get(workflow, workflow),
                "description": payload.get("description"),
                "enabled": bool(schedule.get("enabled")),
                "manual_trigger_enabled": enabled,
                "disabled_reason": disabled_reason,
                "schedule": schedule,
                "task_count": len([item for item in tasks if item.get("enabled", True)]),
                "task_names": [str(item.get("name")) for item in tasks if item.get("name")],
                "active_run": active,
                "latest_task": lane.get("latest_task"),
                "action": {
                    "key": "trigger_workflow",
                    "ui_action": "rdp-trigger-workflow",
                    "value": workflow,
                    "enabled": enabled,
                    "disabled_reason": disabled_reason,
                },
            }
        )
    return catalog


def _stage_status_from_run(run: dict[str, Any] | None) -> str:
    if not run:
        return "idle"
    status = str(run.get("status") or "")
    if status in {"running", "cancellation_requested"}:
        return "running"
    if status == "queued":
        return "queued"
    if status in _TERMINAL_SUCCESS_STATUSES:
        return "complete"
    if status in {"succeeded_with_warnings", "partially_succeeded"}:
        return "action_required"
    if status in {"failed", "cancelled"}:
        return "blocked"
    return "idle"


def _lifecycle_projection(
    control: dict[str, Any],
    overview: dict[str, Any],
    workbench: dict[str, Any],
    alerts: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    active = _as_dict(execution.get("active_run"))
    recent = _as_items(execution.get("recent_runs"))

    def latest_run(workflow: str) -> dict[str, Any] | None:
        if active.get("workflow") == workflow:
            return active
        return next((item for item in recent if item.get("workflow") == workflow), None)

    integrity_alerts = _as_items(alerts.get("integrity_alerts"))
    governance_state = _as_dict(control.get("governance_state"))
    governance_blocked = (
        governance_state.get("audit_only") is True
        or governance_state.get("db_load_failed") is True
        or bool(_as_dict(governance_state.get("quarantined_combos")))
    )
    research_items = _as_items(workbench.get("items"))
    release_payload = _as_dict(workbench.get("release_candidates"))
    release_candidates = _as_items(release_payload.get("items"))
    audit_only_items = _as_items(release_payload.get("audit_only_items"))
    governance_items = [*research_items, *audit_only_items]
    observations = _as_items(control.get("observation_queue"))
    rollback_count = sum(
        1 for item in observations if item.get("observation_status") == "rollback_recommended"
    )
    observing_count = sum(
        1 for item in observations if item.get("observation_status") == "observing"
    )
    active_parameter_count = len(_as_dict(control.get("active_parameters")))
    health = _as_dict(control.get("health"))
    stages = [
        {
            "key": "data",
            "label": "数据准备",
            "status": _stage_status_from_run(latest_run("data_maintenance")),
            "evidence_count": len(_as_items(health.get("checks"))),
            "summary": "采集、质量、Silver/Gold 与 artifact 索引。",
        },
        {
            "key": "research",
            "label": "研究与回放",
            "status": _stage_status_from_run(latest_run("research_cycle")),
            "evidence_count": len(_as_items(control.get("latest_research_conclusions"))),
            "summary": "Phase 2/3/4/5/6 及决策结论。",
        },
        {
            "key": "governance",
            "label": "治理审阅",
            "status": (
                "blocked"
                if integrity_alerts or governance_blocked
                else "action_required"
                if governance_items
                else "idle"
            ),
            "evidence_count": len(governance_items),
            "summary": "完整性、建议、批准/拒绝与 tuning。",
        },
        {
            "key": "release",
            "label": "Gate 与发布",
            "status": (
                "blocked"
                if governance_blocked
                else "action_required"
                if release_candidates
                else "idle"
            ),
            "evidence_count": len(release_candidates),
            "summary": "只处理已批准且可映射的参数候选。",
        },
        {
            "key": "observation",
            "label": "模拟观察",
            "status": "blocked" if rollback_count else ("running" if observing_count else "idle"),
            "evidence_count": len(observations),
            "summary": "发布后效果、偏差与回滚建议。",
        },
        {
            "key": "runtime",
            "label": "运行参数",
            "status": (
                "blocked"
                if governance_blocked
                else "complete"
                if active_parameter_count
                else "idle"
            ),
            "evidence_count": active_parameter_count,
            "summary": "Postgres active parameter 与 runtime provenance。",
        },
    ]
    if rollback_count:
        current_stage = "observation"
    elif governance_blocked or integrity_alerts or governance_items:
        current_stage = "governance"
    elif release_candidates:
        current_stage = "release"
    elif active.get("workflow") == "research_cycle":
        current_stage = "research"
    elif active.get("workflow") == "data_maintenance":
        current_stage = "data"
    else:
        current_stage = "runtime" if active_parameter_count else "data"
    return {
        "current_stage": current_stage,
        "overall_status": (
            "blocked"
            if governance_blocked
            else overview.get("overall_status") or "idle"
        ),
        "stages": stages,
    }


def _release_projection(
    control: dict[str, Any],
    workbench: dict[str, Any],
) -> dict[str, Any]:
    release_candidates_payload = _as_dict(workbench.get("release_candidates"))
    source_candidates = [
        *_as_items(release_candidates_payload.get("items")),
        *_as_items(release_candidates_payload.get("audit_only_items")),
    ]
    release_history_status = _as_dict(control.get("release_history_status"))
    gate_history_status = _as_dict(control.get("gate_history_status"))
    histories_fresh = (
        release_history_status.get("source") == "db"
        and release_history_status.get("stale") is False
        and gate_history_status.get("available") is True
    )
    governance_state = _as_dict(control.get("governance_state"))
    governance_forward_available = (
        governance_state.get("audit_only") is not True
        and governance_state.get("db_load_failed") is not True
    )
    decision_truth_available = (
        governance_state.get("decision_truth_available") is True
    )
    decision_state_by_combo = {
        str(item.get("combo_key")): item
        for item in _as_items(governance_state.get("combo_states"))
        if item.get("combo_key")
    }
    candidates: list[dict[str, Any]] = []
    for source in source_candidates:
        candidate = dict(source)
        combo_key = str(candidate.get("combo_key") or "")
        combo_decision = _as_dict(decision_state_by_combo.get(combo_key))
        decision_status = combo_decision.get("decision_status")
        decision_allows_release = (
            governance_forward_available
            and combo_decision.get("audit_only") is not True
            and decision_truth_available
            and combo_decision.get("decision_truth_available") is True
            and decision_status in _RELEASE_ALLOWED_DECISION_STATUSES
        )
        gate_passed = (
            str(candidate.get("gate_status") or "") == "pass"
            and candidate.get("allow_apply") is True
        )
        promotion_eligible = _promotion_qualification_allows(candidate)
        approved = candidate.get("status") == "approved"
        audit_only = bool(candidate.get("audit_only")) or not promotion_eligible
        allowed_action_keys = (
            {"supersede"}
            if approved and audit_only
            else {"run_gate", "create_release"}
            if approved and promotion_eligible
            else set()
        )
        expected_ui_actions = {
            "supersede": "rdp-supersede-recommendation",
            "run_gate": "rdp-run-gate",
            "create_release": "rdp-create-release",
        }
        normalized_actions: list[dict[str, Any]] = []
        for source_action in _as_items(candidate.get("actions")):
            action = dict(source_action)
            action_key = action.get("key")
            if (
                action_key not in allowed_action_keys
                or action.get("ui_action") != expected_ui_actions.get(action_key)
                or action.get("value") != candidate.get("recommendation_id")
                or type(action.get("enabled")) is not bool
            ):
                continue
            if action.get("key") == "create_release" and not (
                promotion_eligible
                and decision_allows_release
                and gate_passed
                and histories_fresh
            ):
                action["enabled"] = False
                action["disabled_reason"] = (
                    "该建议缺少现行精确证据资格，仅供审计，不能创建发布。"
                    if not promotion_eligible
                    else (
                        "当前决策真源不可验证，或该组合处于暂停/无效状态；"
                        "确认现行治理决策后才能创建发布。"
                    )
                    if not decision_allows_release
                    else "先运行并通过最新门禁，且确保治理历史可用后，才能创建发布。"
                )
            normalized_actions.append(action)
        candidate["actions"] = normalized_actions
        candidate["audit_only"] = audit_only
        candidate["decision_status"] = decision_status
        candidate["decision_truth_available"] = decision_truth_available
        candidate["decision_allows_release"] = decision_allows_release
        candidate["eligible_for_release_review"] = (
            approved
            and promotion_eligible
            and not audit_only
            and decision_allows_release
            and gate_passed
            and histories_fresh
        )
        candidates.append(candidate)
    eligible = [
        dict(item)
        for item in candidates
        if item.get("eligible_for_release_review")
    ]
    eligible.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    selected = eligible[0] if eligible else None
    if selected:
        selection_status = "eligible_for_release_review"
        selection_explanation = (
            "这是最新的精确证据合格、现行决策允许、已批准且 Gate=pass 候选；"
            "它仍需要在实际发布时"
            "重新执行权限、token、映射、Gate 和 rollback 检查。"
        )
    else:
        selection_status = "no_eligible_candidate"
        selection_explanation = (
            "当前没有同时满足精确证据资格、现行决策允许、已批准、Gate=pass 和"
            "治理读模型新鲜的候选；"
            "不会从失败候选中强制选一个应用。"
        )
    return {
        "candidates": candidates,
        "eligible_candidates": eligible,
        "eligible_candidate": selected,
        "selection_status": selection_status,
        "selection_basis": (
            "latest_promotion_qualified_decision_allowed_approved_gate_pass"
        ),
        "selection_explanation": selection_explanation,
        "observations": _as_items(control.get("observation_queue")),
        "active_parameters": _as_dict(control.get("active_parameters")),
        "release_history_status": release_history_status,
        "gate_history_status": gate_history_status,
    }


def _next_action(
    overview: dict[str, Any],
    lifecycle: dict[str, Any],
    execution: dict[str, Any],
    release: dict[str, Any],
) -> dict[str, Any]:
    active = _as_dict(execution.get("active_run"))
    if active:
        return {
            "kind": "inspect_run",
            "label": "查看当前运行",
            "description": "先监控已经占用研究执行槽的 Run。",
            "ui_action": "rdp-open-run",
            "value": active.get("run_id"),
            "enabled": True,
        }
    if release.get("selection_status") == "eligible_for_release_review":
        candidate = _as_dict(release.get("eligible_candidate"))
        return {
            "kind": "review_release_candidate",
            "label": "复核发布候选",
            "description": release.get("selection_explanation"),
            "ui_action": "rdp-run-gate",
            "value": candidate.get("recommendation_id"),
            "enabled": True,
        }
    audit_candidates = [
        item
        for item in _as_items(release.get("candidates"))
        if bool(item.get("audit_only"))
    ]
    audit_candidates.sort(
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    for candidate in audit_candidates:
        action = next(
            (
                item
                for item in _as_items(candidate.get("actions"))
                if item.get("key") == "supersede" and item.get("enabled") is True
            ),
            None,
        )
        if action is not None:
            return {
                "kind": "supersede_audit_recommendation",
                "label": action.get("label") or "归档历史建议",
                "description": "该建议仅供审计，归档后将不再出现在前向治理待办中。",
                "ui_action": "rdp-supersede-recommendation",
                "value": candidate.get("recommendation_id"),
                "enabled": True,
            }
    primary = _as_dict(overview.get("primary_action"))
    if primary:
        return {
            "kind": primary.get("key") or "workflow",
            "label": primary.get("label") or "继续 RDP 流程",
            "description": primary.get("disabled_reason") or overview.get("subheadline"),
            "ui_action": primary.get("ui_action"),
            "value": primary.get("value"),
            "enabled": bool(primary.get("enabled")),
        }
    return {
        "kind": "none",
        "label": "当前无待执行动作",
        "description": "继续观察数据、研究证据与运行参数。",
        "ui_action": None,
        "value": None,
        "enabled": False,
    }


def build_rdp_workspace(request: Request, *, run_limit: int = 20) -> dict[str, Any]:
    """Build the single authoritative RDP operator workspace projection."""
    root = _project_root(request)
    control = build_rdp_control_summary(request)
    bundle = build_rdp_workbench_bundle(request, control_summary=control)
    overview = _as_dict(bundle.get("overview"))
    workbench = _as_dict(bundle.get("workbench"))
    alerts = _as_dict(bundle.get("alerts"))
    tuning_overview = _as_dict(bundle.get("tuning_overview"))
    tuning_proposals = _as_dict(bundle.get("tuning_proposals"))
    runs = build_rdp_runs_panel(limit=run_limit)
    health = dict(_as_dict(control.get("health")))
    governance_state = dict(_as_dict(control.get("governance_state")))
    governance_blocked = (
        governance_state.get("audit_only") is True
        or governance_state.get("db_load_failed") is True
        or bool(_as_dict(governance_state.get("quarantined_combos")))
    )
    if governance_blocked:
        reason_code = str(
            governance_state.get("reason_code")
            or "governance_runtime_quarantine"
        )
        governance_alert = {
            "code": reason_code,
            "severity": "danger",
            "scope": "runtime",
            "phase": "governance",
            "title": "运行参数治理已失败关闭",
            "message": (
                "治理数据库不可用或 canonical active 参数已被隔离；"
                "完成对账前禁止前向发布。"
            ),
            "blocks_approval": True,
            "quarantined_combos": _as_dict(
                governance_state.get("quarantined_combos")
            ),
        }
        alerts = dict(alerts)
        integrity_alerts = _as_items(alerts.get("integrity_alerts"))
        if not any(item.get("code") == reason_code for item in integrity_alerts):
            integrity_alerts.append(governance_alert)
        alerts["integrity_alerts"] = integrity_alerts
        alerts["governance_alerts"] = [governance_alert]
        health["overall_health"] = "blocked"
        health_reasons = [
            str(item) for item in health.get("blocking_reasons") or []
        ]
        if reason_code not in health_reasons:
            health_reasons.append(reason_code)
        health["blocking_reasons"] = health_reasons
    execution = _execution_projection(runs, health, control)
    if execution.get("capacity_violation"):
        health["overall_health"] = "blocked"
        reasons = [str(item) for item in health.get("blocking_reasons") or []]
        if "rdp_execution_capacity_violation" not in reasons:
            reasons.append("rdp_execution_capacity_violation")
        health["blocking_reasons"] = reasons
    workflows = _workflow_catalog(root, control, execution)
    lifecycle = _lifecycle_projection(control, overview, workbench, alerts, execution)
    release = _release_projection(control, workbench)
    next_action = _next_action(overview, lifecycle, execution, release)
    data_governance = build_data_governance_snapshot(root)
    return {
        "schema_version": RDP_WORKSPACE_SCHEMA_VERSION,
        "generated_at": utc_now().isoformat(),
        "environment": _as_dict(control.get("environment")),
        "governance_state": governance_state,
        "health": health,
        "lifecycle": lifecycle,
        "next_action": next_action,
        "execution": execution,
        "workflows": workflows,
        "data_governance": data_governance,
        "research": {
            "overview": overview,
            "items": _as_items(workbench.get("items")),
            "alerts": alerts,
        },
        "release": release,
        "tuning": {
            "overview": tuning_overview,
            "proposals": _as_items(tuning_proposals.get("items")),
            "step2_incomplete_reason": tuning_proposals.get("step2_incomplete_reason"),
        },
        "compatibility": {
            "legacy_read_api_supported": True,
            "legacy_run_api_supported": True,
            "write_api_version": "existing-protected-contracts",
        },
    }


__all__ = ["RDP_WORKSPACE_SCHEMA_VERSION", "build_rdp_workspace"]
