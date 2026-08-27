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
from datetime import datetime, timedelta, timezone
from typing import Any

from aats.data_platform.governance._time_util import parse_iso_datetime_utc
from aats.data_platform.production_workflow.gate_runtime_contract import (
    runtime_current_alerts,
    runtime_latest_workflow_runs,
    runtime_live_db_health,
    runtime_strict_environment,
)

log = logging.getLogger(__name__)

_QUALITY_MONITOR_MAX_AGE_HOURS = 36
_CURRENT_ALERTS_MAX_AGE_HOURS = 2
_MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class GateCheckResult:
    """单条 gate 检查结果."""
    name: str
    category: str
    passed: bool
    severity: str  # "block" | "warn" | "info"
    detail: str = ""
    reason_code: str | None = None


def _strict_gate_environment(ctx: dict[str, Any]) -> bool:
    return runtime_strict_environment(ctx)


def _timestamp_freshness_error(
    value: Any,
    *,
    context: str,
    max_age_hours: int,
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return f"{context}_generated_at_missing"
    try:
        generated_at = parse_iso_datetime_utc(value, context=context)
    except ValueError:
        return f"{context}_generated_at_invalid"
    now = datetime.now(timezone.utc)
    if generated_at > now + _MAX_FUTURE_CLOCK_SKEW:
        return f"{context}_generated_at_future"
    age = now - generated_at
    if age > timedelta(hours=max_age_hours):
        return f"{context}_stale"
    return None


def check_promotion_qualification(ctx: dict[str, Any]) -> GateCheckResult:
    """Require the recommendation's exact evidence round to qualify."""
    recommendation = ctx.get("recommendation")
    verdict = ctx.get("promotion_qualification")
    if verdict is None:
        return GateCheckResult(
            name="promotion_qualification",
            category="promotion",
            passed=False,
            severity="block",
            detail="promotion_qualification_missing: 精确证据资格判定缺失",
            reason_code="promotion_qualification_missing",
        )
    if not isinstance(recommendation, dict) or not recommendation:
        return GateCheckResult(
            name="promotion_qualification",
            category="promotion",
            passed=False,
            severity="block",
            detail="promotion_qualification_invalid: recommendation 上下文缺失",
            reason_code="promotion_qualification_invalid",
        )

    from aats.data_platform.decision_system.promotion_guard import (
        validate_promotion_qualification_verdict,
    )

    verdict = validate_promotion_qualification_verdict(verdict, recommendation)
    required = verdict.required
    eligible = verdict.eligible
    reason_code = verdict.reason_code
    detail = verdict.detail
    return GateCheckResult(
        name="promotion_qualification",
        category="promotion",
        passed=(not required) or eligible,
        severity="block" if required else "info",
        detail=f"{reason_code}: {detail}",
        reason_code=reason_code,
    )


# ── 1. Governance Health ──────────────────────────────────────────


def check_quality_monitor_health(ctx: dict[str, Any]) -> GateCheckResult:
    """quality_monitor_summary.json 是否 healthy."""
    if ctx.get("quality_monitor_available") is False:
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=False,
            severity="block",
            detail=(
                "quality_monitor_truth_unavailable: 受管质量快照缺失或数据库不可验证，"
                "拒绝使用陈旧文件继续 apply"
            ),
            reason_code="quality_monitor_truth_unavailable",
        )

    strict = _strict_gate_environment(ctx)
    qm = ctx.get("quality_monitor")
    if not isinstance(qm, dict):
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="quality_monitor_schema_invalid: 质量快照不存在或结构无效",
            reason_code="quality_monitor_schema_invalid",
        )

    if (
        ctx.get("quality_monitor_managed_truth") is True
        and qm.get("data_source") != "db"
    ):
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=False,
            severity="block",
            detail="quality_monitor_source_invalid: 受管 Gate 只接受 DB 质量快照",
            reason_code="quality_monitor_source_invalid",
        )
    freshness_error = _timestamp_freshness_error(
        qm.get("generated_at"),
        context="quality_monitor",
        max_age_hours=_QUALITY_MONITOR_MAX_AGE_HOURS,
    )
    if freshness_error is not None:
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=False,
            severity="block",
            detail=f"{freshness_error}: 质量快照时间不可验证或已超过 36 小时",
            reason_code=freshness_error,
        )

    summary = qm.get("summary")
    if not isinstance(summary, dict):
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="quality_monitor_schema_invalid: summary 必须是对象",
            reason_code="quality_monitor_schema_invalid",
        )

    health = summary.get("health")
    critical = summary.get("critical_failures")
    warning = summary.get("warning_failures")
    if health not in {"healthy", "degraded", "unhealthy"}:
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="quality_monitor_health_invalid: health 必须是受支持的显式状态",
            reason_code="quality_monitor_health_invalid",
        )
    if (
        type(critical) is not int
        or critical < 0
        or type(warning) is not int
        or warning < 0
    ):
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=not strict,
            severity="block" if strict else "warn",
            detail=(
                "quality_monitor_failure_counts_invalid: "
                "critical_failures 与 warning_failures 必须是非负整数"
            ),
            reason_code="quality_monitor_failure_counts_invalid",
        )

    status_consistent = (
        (health == "healthy" and critical == 0 and warning == 0)
        or (health == "degraded" and critical == 0 and warning > 0)
        or (health == "unhealthy" and critical > 0)
    )
    if not status_consistent:
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=not strict,
            severity="block" if strict else "warn",
            detail=(
                "quality_monitor_health_inconsistent: health 与失败数量不一致"
            ),
            reason_code="quality_monitor_health_inconsistent",
        )

    if health == "unhealthy":
        return GateCheckResult(
            name="quality_monitor_health",
            category="governance",
            passed=False,
            severity="block",
            detail=(
                f"health={health}, critical_failures={critical}, "
                f"warning_failures={warning}"
            ),
        )

    passed = health in ("healthy", "degraded")
    severity = "info" if health == "healthy" else "warn"
    return GateCheckResult(
        name="quality_monitor_health",
        category="governance",
        passed=passed,
        severity=severity,
        detail=(
            f"health={health}, critical_failures={critical}, "
            f"warning_failures={warning}"
        ),
    )


# ── 2. Artifact Freshness ─────────────────────────────────────────


def check_evidence_freshness(ctx: dict[str, Any]) -> GateCheckResult:
    """精确 qualification round 的 canonical 完成时间是否过于陈旧."""
    strict = _strict_gate_environment(ctx)
    rec = ctx.get("recommendation", {})
    evidence_ref = rec.get("evidence_bundle_ref")
    verdict = ctx.get("promotion_qualification")

    if not evidence_ref or verdict is None:
        return GateCheckResult(
            name="evidence_freshness",
            category="freshness",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="evidence_freshness_missing: evidence 引用或精确资格判定缺失",
            reason_code="evidence_freshness_missing",
        )

    from aats.data_platform.decision_system.promotion_guard import (
        validate_promotion_qualification_verdict,
    )

    verdict = validate_promotion_qualification_verdict(verdict, rec)
    qualified_round_id = getattr(verdict, "qualified_round_id", None)
    finished_at_str = getattr(verdict, "qualified_finished_at", None)
    if (
        getattr(verdict, "required", None) is not True
        or getattr(verdict, "eligible", None) is not True
        or qualified_round_id != evidence_ref
        or not isinstance(finished_at_str, str)
        or not finished_at_str.strip()
    ):
        return GateCheckResult(
            name="evidence_freshness",
            category="freshness",
            passed=False,
            severity="block",
            detail="evidence_exact_round_unqualified: 无可信精确 round 完成时间",
            reason_code="evidence_exact_round_unqualified",
        )

    try:
        finished_at = parse_iso_datetime_utc(
            finished_at_str,
            context="gate_rules.check_evidence_freshness.finished_at",
        )
        if finished_at is None:
            raise ValueError("finished_at parsed to None")
        age_hours = (
            datetime.now(timezone.utc) - finished_at
        ).total_seconds() / 3600
    except (ValueError, TypeError):
        return GateCheckResult(
            name="evidence_freshness",
            category="freshness",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="evidence_finished_at_invalid: 精确 round 完成时间不可解析",
            reason_code="evidence_finished_at_invalid",
        )

    if finished_at > datetime.now(timezone.utc):
        return GateCheckResult(
            name="evidence_freshness",
            category="freshness",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="evidence_finished_at_future: 精确 round 完成时间位于未来",
            reason_code="evidence_finished_at_future",
        )

    if age_hours > 168:  # > 7 days
        return GateCheckResult(
            name="evidence_freshness",
            category="freshness",
            passed=False,
            severity="block",
            detail=f"精确 evidence round 完成于 {age_hours:.0f}h 前（>168h），证据已过期",
            reason_code="evidence_round_stale",
        )
    if age_hours > 72:  # > 3 days
        return GateCheckResult(
            name="evidence_freshness",
            category="freshness",
            passed=True,
            severity="warn",
            detail=f"精确 evidence round 完成于 {age_hours:.0f}h 前（>72h），建议使用更新证据",
        )

    return GateCheckResult(
        name="evidence_freshness",
        category="freshness",
        passed=True,
        severity="info",
        detail=f"精确 evidence round 完成于 {age_hours:.0f}h 前",
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
    if ctx.get("active_decisions_available") is False:
        return GateCheckResult(
            name="decision_consistency",
            category="decision",
            passed=False,
            severity="block",
            detail=(
                "active_decision_truth_unavailable: 治理数据库当前决策不可验证，"
                "拒绝使用陈旧文件继续 apply"
            ),
            reason_code="active_decision_truth_unavailable",
        )

    rec = ctx.get("recommendation", {})
    decisions = ctx.get("active_decisions", [])

    target_family = rec.get("family")
    target_tf = rec.get("timeframe", "").lower()
    combo_key = f"{target_family}_{target_tf}"

    if ctx.get("pending_rollback_truth_available") is False:
        strict = _strict_gate_environment(ctx)
        return GateCheckResult(
            name="decision_consistency",
            category="decision",
            passed=False,
            severity="block" if strict else "warn",
            detail=(
                "pending_rollback_truth_unavailable: 未能核验待回滚/对账状态"
            ),
            reason_code="pending_rollback_truth_unavailable",
        )

    pending_rollbacks = ctx.get("pending_rollback_combos") or {}
    pending_release_id = (
        pending_rollbacks.get(combo_key)
        if isinstance(pending_rollbacks, dict)
        else None
    )
    if pending_release_id:
        return GateCheckResult(
            name="decision_consistency",
            category="decision",
            passed=False,
            severity="block",
            detail=(
                f"{combo_key} 存在未收口回滚/对账 {pending_release_id}，禁止 apply"
            ),
            reason_code="pending_rollback_unresolved",
        )

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
        strict = _strict_gate_environment(ctx)
        return GateCheckResult(
            name="decision_consistency",
            category="decision",
            passed=not strict,
            severity="block" if strict else "warn",
            detail=f"{combo_key} 无 active decision 记录，无法证明可发布状态",
            reason_code="active_decision_missing",
        )

    status = combo_decision.get("current_status")
    valid_statuses = {"keep_active", "lower_priority", "pause", "require_review"}
    if not isinstance(status, str) or status not in valid_statuses:
        strict = _strict_gate_environment(ctx)
        return GateCheckResult(
            name="decision_consistency",
            category="decision",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="active_decision_status_invalid: 当前决策状态缺失或不受支持",
            reason_code="active_decision_status_invalid",
        )

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

    freshness_error = _timestamp_freshness_error(
        alerts.get("generated_at"),
        context="current_alerts",
        max_age_hours=_CURRENT_ALERTS_MAX_AGE_HOURS,
    )
    if freshness_error is not None:
        return GateCheckResult(
            name="current_alerts",
            category="operations",
            passed=not strict,
            severity="block" if strict else "warn",
            detail=f"{freshness_error}: reliability 告警已过期或时间无效",
            reason_code=freshness_error,
        )

    overall = alerts.get("overall_status")
    critical_alerts = alerts.get("critical_alerts")
    warning_alerts = alerts.get("warning_alerts")
    if overall not in {"healthy", "warning", "critical"}:
        return GateCheckResult(
            name="current_alerts",
            category="operations",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="current_alerts_status_invalid: overall_status 必须是受支持的显式状态",
            reason_code="current_alerts_status_invalid",
        )
    if (
        type(critical_alerts) is not int
        or critical_alerts < 0
        or type(warning_alerts) is not int
        or warning_alerts < 0
    ):
        return GateCheckResult(
            name="current_alerts",
            category="operations",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="current_alerts_count_invalid: 告警数量必须是非负整数",
            reason_code="current_alerts_count_invalid",
        )

    status_consistent = (
        (overall == "healthy" and critical_alerts == 0 and warning_alerts == 0)
        or (overall == "warning" and critical_alerts == 0 and warning_alerts > 0)
        or (overall == "critical" and critical_alerts > 0)
    )
    if not status_consistent:
        return GateCheckResult(
            name="current_alerts",
            category="operations",
            passed=not strict,
            severity="block" if strict else "warn",
            detail="current_alerts_inconsistent: overall_status 与告警数量不一致",
            reason_code="current_alerts_inconsistent",
        )

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

    healthy = live_db_health.get("healthy")
    connection_ok = live_db_health.get("connection_ok")
    if healthy is True and connection_ok is True:
        tables_checked = live_db_health.get("tables_checked")
        checked_tables = len(tables_checked) if isinstance(tables_checked, dict) else 0
        return GateCheckResult(
            name="live_db_health",
            category="production",
            passed=True,
            severity="info",
            detail=f"live DB healthy, tables_checked={checked_tables}",
        )

    errors = live_db_health.get("errors")
    error_count = len(errors) if isinstance(errors, list) else 0
    return GateCheckResult(
        name="live_db_health",
        category="production",
        passed=not strict,
        severity="block" if strict else "warn",
        detail=(
            "live_db_health_invalid: live DB 健康或连接状态未被显式证明"
            f"（errors={error_count}）"
        ),
        reason_code="live_db_health_invalid",
    )


def check_workflow_freshness(ctx: dict[str, Any]) -> GateCheckResult:
    """关键 workflow 最近一次运行是否成功且不过期."""
    contract = ctx.get("runtime_contract")
    if isinstance(contract, dict) and contract.get("workflow_runs_available") is False:
        return GateCheckResult(
            name="workflow_freshness",
            category="operations",
            passed=False,
            severity="block",
            detail=(
                "workflow_truth_unavailable: 受管 workflow 历史不可验证，"
                "拒绝使用陈旧文件继续 apply"
            ),
            reason_code="workflow_truth_unavailable",
        )
    strict = _strict_gate_environment(ctx)
    latest_runs = runtime_latest_workflow_runs(ctx)
    thresholds = {
        "reliability_cycle": 2,
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
        if status != "success":
            issues.append(f"{workflow}: status={status}")
            continue
        raw_finished = str(latest.get("finished_at") or "")
        if not raw_finished:
            issues.append(
                f"{workflow}: missing finished_at/started_at fallback denied"
            )
            continue
        try:
            finished_at = parse_iso_datetime_utc(
                raw_finished, context=f"gate_rules.check_workflow_freshness.{workflow}"
            )
        except ValueError as exc:
            issues.append(f"{workflow}: illegal timestamp {raw_finished!r} ({exc})")
            continue
        if finished_at is None:
            issues.append(
                f"{workflow}: missing finished_at/started_at fallback denied"
            )
            continue
        if finished_at > now + _MAX_FUTURE_CLOCK_SKEW:
            issues.append(f"{workflow}: finished_at is in the future")
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
    check_promotion_qualification,
    check_parameter_set_exists,
    check_quality_monitor_health,
    check_current_alerts,
    check_live_db_health,
    check_workflow_freshness,
    check_evidence_freshness,
    check_decision_consistency,
]
