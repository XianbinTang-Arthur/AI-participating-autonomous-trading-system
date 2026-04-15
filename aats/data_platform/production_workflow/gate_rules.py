"""Pre-Apply Gate 规则定义.

每条规则是一个检查函数，接收上下文 dict，返回 GateCheckResult。
规则按 severity 分为 block / warn / info 三级:
  - block: 检查不通过时禁止 apply
  - warn:  检查不通过时发出警告但允许 apply
  - info:  仅供参考
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aats.data_platform.production_workflow.gate_runtime_contract import (
    runtime_current_alerts,
    runtime_latest_workflow_runs,
    runtime_live_db_health,
    runtime_strict_environment,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateCheckResult:
    """单条 gate 检查结果."""
    name: str
    category: str
    passed: bool
    severity: str  # "block" | "warn" | "info"
    detail: str = ""


def _strict_gate_environment(ctx: dict[str, Any]) -> bool:
    return runtime_strict_environment(ctx)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# ── 1. Governance Health ──────────────────────────────────────────


def check_quality_monitor_health(ctx: dict[str, Any]) -> GateCheckResult:
    """quality_monitor_summary.json 是否 healthy."""
    qm = ctx.get("quality_monitor")
    if qm is None:
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=False,
            severity="warn",
            detail="quality_monitor_summary.json 不存在或不可读",
        )

    summary = qm.get("summary", {})
    health = summary.get("health", "unknown")
    critical = summary.get("critical_failures", 0)

    if health == "unhealthy" or critical > 0:
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=False,
            severity="block",
            detail=f"health={health}, critical_failures={critical}",
        )

    passed = health in ("healthy", "degraded")
    severity = "info" if health == "healthy" else "warn"
    return GateCheckResult(
        name="quality_monitor_health",
        category="governance",
        passed=passed,
        severity=severity,
        detail=f"health={health}, critical_failures={critical}",
    )


# ── 2. Artifact Freshness ─────────────────────────────────────────


def check_evidence_freshness(ctx: dict[str, Any]) -> GateCheckResult:
    """recommendation 引用的 evidence 是否过于陈旧."""
    rec = ctx.get("recommendation", {})
    evidence_ref = rec.get("evidence_bundle_ref")
    created_at_str = rec.get("created_at")

    if not evidence_ref or not created_at_str:
        return GateCheckResult(
            name="evidence_freshness",
            category="freshness",
            passed=True,
            severity="info",
            detail="无 evidence 引用或创建时间",
        )

    try:
        created_at = datetime.fromisoformat(created_at_str)
        age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
    except (ValueError, TypeError):
        age_hours = -1

    if age_hours > 168:  # > 7 days
        return GateCheckResult(
            name="evidence_freshness",
            category="freshness",
            passed=False,
            severity="block",
            detail=f"recommendation 创建于 {age_hours:.0f}h 前（>168h），证据可能过期",
        )
    if age_hours > 72:  # > 3 days
        return GateCheckResult(
            name="evidence_freshness",
            category="freshness",
            passed=True,
            severity="warn",
            detail=f"recommendation 创建于 {age_hours:.0f}h 前（>72h），建议使用更新证据",
        )

    return GateCheckResult(
        name="evidence_freshness",
        category="freshness",
        passed=True,
        severity="info",
        detail=f"recommendation 创建于 {age_hours:.0f}h 前",
    )


def check_evidence_completeness(ctx: dict[str, Any]) -> GateCheckResult:
    """evidence completeness 是否足够."""
    latest_round = ctx.get("latest_decision_round", {})
    manifest = latest_round.get("round_manifest", {})
    evidence = manifest.get("evidence_completeness", {})
    ratio = evidence.get("completeness_ratio", 0)

    if ratio < 0.25:
        return GateCheckResult(
            name="evidence_completeness",
            category="freshness",
            passed=False,
            severity="block",
            detail=f"completeness_ratio={ratio:.2f}（<0.25），证据严重不足",
        )
    if ratio < 0.5:
        return GateCheckResult(
            name="evidence_completeness",
            category="freshness",
            passed=True,
            severity="warn",
            detail=f"completeness_ratio={ratio:.2f}（<0.5），建议补充更多证据",
        )

    return GateCheckResult(
        name="evidence_completeness",
        category="freshness",
        passed=True,
        severity="info",
        detail=f"completeness_ratio={ratio:.2f}",
    )


# ── 3. Decision Consistency ────────────────────────────────────────


def check_decision_consistency(ctx: dict[str, Any]) -> GateCheckResult:
    """recommendation 是否与当前 family/tf decision 冲突."""
    rec = ctx.get("recommendation", {})
    decisions = ctx.get("active_decisions", [])

    target_family = rec.get("family")
    target_tf = rec.get("timeframe", "").lower()
    combo_key = f"{target_family}_{target_tf}"

    # 查找该 combo 的 decision
    combo_decision = None
    for d in decisions:
        if d.get("combo_key") == combo_key or (
            d.get("family") == target_family
            and d.get("timeframe", "").lower() == target_tf
        ):
            combo_decision = d
            break

    if combo_decision is None:
        return GateCheckResult(
            name="decision_consistency",
            category="decision",
            passed=True,
            severity="info",
            detail=f"{combo_key} 无 active decision 记录",
        )

    status = combo_decision.get("current_status", "")

    if status == "pause":
        return GateCheckResult(
            name="decision_consistency",
            category="decision",
            passed=False,
            severity="block",
            detail=f"{combo_key} 当前状态为 pause，不应 apply 新参数",
        )
    if status == "require_review":
        return GateCheckResult(
            name="decision_consistency",
            category="decision",
            passed=True,
            severity="warn",
            detail=f"{combo_key} 当前状态为 require_review，apply 需额外谨慎",
        )

    return GateCheckResult(
        name="decision_consistency",
        category="decision",
        passed=True,
        severity="info",
        detail=f"{combo_key} 当前状态为 {status}",
    )


# ── 4. Active Round Health ─────────────────────────────────────────


def check_latest_round_health(ctx: dict[str, Any]) -> GateCheckResult:
    """最近 decision round 是否成功."""
    latest_round = ctx.get("latest_decision_round", {})
    manifest = latest_round.get("round_manifest", {})
    status = manifest.get("status", "unknown")

    if status == "failed":
        return GateCheckResult(
            name="latest_round_health",
            category="round",
            passed=False,
            severity="block",
            detail="最近 decision round 状态为 failed",
        )
    if status in ("partial_success", "unknown"):
        return GateCheckResult(
            name="latest_round_health",
            category="round",
            passed=True,
            severity="warn",
            detail=f"最近 decision round 状态为 {status}",
        )

    return GateCheckResult(
        name="latest_round_health",
        category="round",
        passed=True,
        severity="info",
        detail=f"最近 decision round 状态为 {status}",
    )


def check_recommendation_status(ctx: dict[str, Any]) -> GateCheckResult:
    """recommendation 是否处于 approved 状态."""
    rec = ctx.get("recommendation", {})
    status = rec.get("status", "unknown")

    if status != "approved":
        return GateCheckResult(
            name="recommendation_status",
            category="approval",
            passed=False,
            severity="block",
            detail=f"recommendation 状态为 '{status}'，必须为 approved",
        )

    return GateCheckResult(
        name="recommendation_status",
        category="approval",
        passed=True,
        severity="info",
        detail="recommendation 已审批通过",
    )


def check_parameter_set_exists(ctx: dict[str, Any]) -> GateCheckResult:
    """target parameter set 是否在 governance registry 中存在."""
    rec = ctx.get("recommendation", {})
    ps_id = rec.get("target_parameter_set_id")
    param_sets = ctx.get("parameter_sets", [])

    if not ps_id:
        return GateCheckResult(
            name="parameter_set_exists",
            category="approval",
            passed=True,
            severity="info",
            detail="recommendation 无 target_parameter_set_id（非参数升级类型）",
        )

    found = any(ps.get("parameter_set_id") == ps_id for ps in param_sets)
    if not found:
        return GateCheckResult(
            name="parameter_set_exists",
            category="approval",
            passed=False,
            severity="block",
            detail=f"parameter_set_id {ps_id} 不在 governance registry 中",
        )

    return GateCheckResult(
        name="parameter_set_exists",
        category="approval",
        passed=True,
        severity="info",
        detail=f"parameter_set_id {ps_id} 存在",
    )


def check_current_alerts(ctx: dict[str, Any]) -> GateCheckResult:
    """当前 reliability alerts 是否允许 apply."""
    alerts = runtime_current_alerts(ctx)
    strict = _strict_gate_environment(ctx)

    if alerts is None:
        return GateCheckResult(
            name="current_alerts",
            category="operations",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="current_alerts.json 不存在，无法确认当前 reliability 状态",
        )

    overall = alerts.get("overall_status", "unknown")
    critical_alerts = int(alerts.get("critical_alerts", 0) or 0)
    warning_alerts = int(alerts.get("warning_alerts", 0) or 0)

    if overall == "critical" or critical_alerts > 0:
        return GateCheckResult(
            name="current_alerts",
            category="operations",
            passed=False,
            severity="block",
            detail=f"存在 {critical_alerts} 条 critical alert，禁止 apply",
        )
    if overall == "warning" or warning_alerts > 0:
        return GateCheckResult(
            name="current_alerts",
            category="operations",
            passed=True,
            severity="warn",
            detail=f"存在 {warning_alerts} 条 warning alert，建议人工复核",
        )

    return GateCheckResult(
        name="current_alerts",
        category="operations",
        passed=True,
        severity="info",
        detail=f"overall_status={overall}",
    )


def check_live_db_health(ctx: dict[str, Any]) -> GateCheckResult:
    """live DB 只读链路是否健康."""
    live_db_health = runtime_live_db_health(ctx)
    strict = _strict_gate_environment(ctx)

    if live_db_health.get("healthy"):
        checked_tables = len(live_db_health.get("tables_checked", {}))
        return GateCheckResult(
            name="live_db_health",
            category="production",
            passed=True,
            severity="info",
            detail=f"live DB healthy, tables_checked={checked_tables}",
        )

    errors = live_db_health.get("errors") or []
    detail = "; ".join(str(item) for item in errors) if errors else "live DB 不健康"
    return GateCheckResult(
        name="live_db_health",
        category="production",
        passed=not strict,
        severity="block" if strict else "warn",
        detail=detail,
    )


def check_workflow_freshness(ctx: dict[str, Any]) -> GateCheckResult:
    """关键 workflow 最近一次运行是否成功且不过期."""
    strict = _strict_gate_environment(ctx)
    latest_runs = runtime_latest_workflow_runs(ctx)
    thresholds = {
        "data_maintenance": 36,
        "governance_cycle": 36,
        "decision_cycle": 168,
    }
    now = datetime.now(timezone.utc)
    issues: list[str] = []
    warnings: list[str] = []

    for workflow, max_age_hours in thresholds.items():
        latest = latest_runs.get(workflow)
        if latest is None:
            issues.append(f"{workflow}: missing latest run")
            continue
        status = str(latest.get("overall_status") or "unknown")
        finished_at = _parse_iso_datetime(
            str(latest.get("finished_at") or latest.get("started_at") or ""),
        )
        if status not in {"success", "partial"}:
            issues.append(f"{workflow}: status={status}")
            continue
        if finished_at is None:
            warnings.append(f"{workflow}: missing finished_at")
            continue
        age_hours = (now - finished_at).total_seconds() / 3600
        if age_hours > max_age_hours:
            issues.append(f"{workflow}: stale {age_hours:.1f}h>{max_age_hours}h")

    if issues:
        return GateCheckResult(
            name="workflow_freshness",
            category="operations",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="; ".join(issues),
        )
    if warnings:
        return GateCheckResult(
            name="workflow_freshness",
            category="operations",
            passed=True,
            severity="warn",
            detail="; ".join(warnings),
        )

    return GateCheckResult(
        name="workflow_freshness",
        category="operations",
        passed=True,
        severity="info",
        detail="关键 workflow 新鲜度正常",
    )


# ── 默认规则集 ─────────────────────────────────────────────────────

DEFAULT_GATE_RULES = [
    check_recommendation_status,
    check_parameter_set_exists,
    check_quality_monitor_health,
    check_current_alerts,
    check_live_db_health,
    check_workflow_freshness,
    check_evidence_freshness,
    check_evidence_completeness,
    check_decision_consistency,
    check_latest_round_health,
]
