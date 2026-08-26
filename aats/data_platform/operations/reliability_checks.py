"""可靠性检查模块.

工作包 C: 定义一组可靠性检查规则，检测 RDP 各层健康状态。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from aats.data_platform.governance.snapshot_db import (
    SNAPSHOT_QUALITY_MONITOR,
    load_governance_snapshot,
)


@dataclass(frozen=True)
class ReliabilityCheckResult:
    """单条检查结果."""
    name: str
    category: str          # data / governance / decision / operations
    passed: bool
    severity: str          # critical / warning / info
    detail: str


# ── 检查规则函数 ─────────────────────────────────────────────

def check_quality_monitor_exists(root: Path) -> ReliabilityCheckResult:
    """检查 quality_monitor_summary.json 是否存在且非空."""
    payload = load_governance_snapshot(root, snapshot_type=SNAPSHOT_QUALITY_MONITOR)
    fp = root / "artifacts" / "governance" / "quality_monitor_summary.json"
    if payload is None and not fp.exists():
        return ReliabilityCheckResult(
            name="quality_monitor_exists",
            category="governance",
            passed=False,
            severity="critical",
            detail="quality_monitor_summary.json not found",
        )
    try:
        data = payload if payload is not None else json.loads(fp.read_text(encoding="utf-8"))
        if not data:
            return ReliabilityCheckResult(
                name="quality_monitor_exists",
                category="governance",
                passed=False,
                severity="warning",
                detail="quality_monitor_summary.json is empty",
            )
    except Exception as e:
        return ReliabilityCheckResult(
            name="quality_monitor_exists",
            category="governance",
            passed=False,
            severity="critical",
            detail=f"quality_monitor_summary.json parse error: {e}",
        )
    return ReliabilityCheckResult(
        name="quality_monitor_exists",
        category="governance",
        passed=True,
        severity="info",
        detail="quality_monitor_summary.json exists and is valid",
    )


def check_active_decisions_exists(root: Path) -> ReliabilityCheckResult:
    """检查 DB-first active decision registry 是否可读取."""
    from aats.data_platform.decision_system.recommendation_registry import (
        load_active_decision_registry,
    )

    fp = root / "artifacts" / "decision_system" / "active_decision_registry.json"
    try:
        data = load_active_decision_registry(fp)
        decisions = data.get("decisions", [])
        if not isinstance(decisions, list):
            raise ValueError("decisions must be a list")
        if not decisions:
            return ReliabilityCheckResult(
                name="active_decisions_exists",
                category="decision",
                passed=False,
                severity="warning",
                detail="active decision registry contains no decisions",
            )
        return ReliabilityCheckResult(
            name="active_decisions_exists",
            category="decision",
            passed=True,
            severity="info",
            detail=f"active decision registry has {len(decisions)} decisions",
        )
    except Exception as e:
        return ReliabilityCheckResult(
            name="active_decisions_exists",
            category="decision",
            passed=False,
            severity="warning",
            detail=f"active decision registry unavailable: {e}",
        )


def check_workflow_configs_exist(root: Path) -> ReliabilityCheckResult:
    """检查所有 workflow 配置文件是否存在."""
    config_dir = root / "configs" / "rdp_workflows"
    expected = [
        "data_maintenance.json",
        "research_cycle.json",
        "governance_cycle.json",
        "decision_cycle.json",
    ]
    missing = [n for n in expected if not (config_dir / n).exists()]
    if missing:
        return ReliabilityCheckResult(
            name="workflow_configs_exist",
            category="operations",
            passed=False,
            severity="critical",
            detail=f"missing workflow configs: {missing}",
        )
    return ReliabilityCheckResult(
        name="workflow_configs_exist",
        category="operations",
        passed=True,
        severity="info",
        detail=f"all {len(expected)} workflow configs present",
    )


def check_artifact_directories(root: Path) -> ReliabilityCheckResult:
    """检查关键 artifact 目录是否存在."""
    dirs = [
        "artifacts/operations/workflow_runs",
        "artifacts/operations/alerts",
        "artifacts/governance",
        "artifacts/decision_system",
    ]
    missing = [d for d in dirs if not (root / d).exists()]
    if missing:
        return ReliabilityCheckResult(
            name="artifact_directories",
            category="operations",
            passed=False,
            severity="warning",
            detail=f"missing directories: {missing}",
        )
    return ReliabilityCheckResult(
        name="artifact_directories",
        category="operations",
        passed=True,
        severity="info",
        detail=f"all {len(dirs)} artifact directories present",
    )


def check_open_failures(root: Path) -> ReliabilityCheckResult:
    """检查是否有未处理的 workflow 失败."""
    fp = root / "artifacts" / "operations" / "workflow_failures.json"
    if not fp.exists():
        return ReliabilityCheckResult(
            name="open_failures",
            category="operations",
            passed=True,
            severity="info",
            detail="no workflow_failures.json (no failures tracked)",
        )
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        open_count = sum(
            1 for f in data.get("failures", []) if f.get("status") == "open"
        )
        if open_count > 0:
            return ReliabilityCheckResult(
                name="open_failures",
                category="operations",
                passed=False,
                severity="warning",
                detail=f"{open_count} open failure(s) need attention",
            )
    except Exception as e:
        return ReliabilityCheckResult(
            name="open_failures",
            category="operations",
            passed=False,
            severity="warning",
            detail=f"workflow_failures.json parse error: {e}",
        )
    return ReliabilityCheckResult(
        name="open_failures",
        category="operations",
        passed=True,
        severity="info",
        detail="no open failures",
    )


def check_release_history_exists(root: Path) -> ReliabilityCheckResult:
    """检查 parameter_release_history.json 是否存在."""
    fp = root / "artifacts" / "production_workflow" / "parameter_release_history.json"
    if not fp.exists():
        return ReliabilityCheckResult(
            name="release_history_exists",
            category="decision",
            passed=True,
            severity="info",
            detail="no release history yet (expected for new deployments)",
        )
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        releases = data.get("releases", [])
        observing = sum(
            1 for r in releases
            if r.get("observation_status") == "observing"
        )
        detail = f"{len(releases)} releases total"
        if observing > 0:
            detail += f", {observing} currently observing"
        return ReliabilityCheckResult(
            name="release_history_exists",
            category="decision",
            passed=True,
            severity="info" if observing == 0 else "warning",
            detail=detail,
        )
    except Exception as e:
        return ReliabilityCheckResult(
            name="release_history_exists",
            category="decision",
            passed=False,
            severity="warning",
            detail=f"release history parse error: {e}",
        )


def check_active_parameters(root: Path) -> ReliabilityCheckResult:
    """检查 active_parameters_registry.json 是否存在."""
    fp = root / "artifacts" / "decision_system" / "active_parameters_registry.json"
    if not fp.exists():
        return ReliabilityCheckResult(
            name="active_parameters",
            category="decision",
            passed=True,
            severity="info",
            detail="no active parameters registry (no parameters applied yet)",
        )
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        entries = data.get("active_parameters", [])
        return ReliabilityCheckResult(
            name="active_parameters",
            category="decision",
            passed=True,
            severity="info",
            detail=f"{len(entries)} active parameter set(s)",
        )
    except Exception as e:
        return ReliabilityCheckResult(
            name="active_parameters",
            category="decision",
            passed=False,
            severity="warning",
            detail=f"active_parameters_registry.json parse error: {e}",
        )


def check_data_governance_monitoring(root: Path) -> ReliabilityCheckResult:
    """Promote the bounded data-governance monitor into the hourly alert cycle."""

    from aats.api.rdp_data_governance import build_data_governance_snapshot

    snapshot = build_data_governance_snapshot(root)
    monitoring = snapshot.get("monitoring") or {}
    status = str(monitoring.get("status") or "unknown")
    alert_count = int(monitoring.get("alert_count") or 0)
    if snapshot.get("status") != "ready" or status == "unknown":
        return ReliabilityCheckResult(
            name="rdp_data_governance_monitoring",
            category="data",
            passed=False,
            severity="critical",
            detail="data governance snapshot or monitoring evidence unavailable",
        )
    if status == "critical":
        return ReliabilityCheckResult(
            name="rdp_data_governance_monitoring",
            category="data",
            passed=False,
            severity="critical",
            detail=f"data governance has {alert_count} active alert(s)",
        )
    if status == "warning":
        return ReliabilityCheckResult(
            name="rdp_data_governance_monitoring",
            category="data",
            passed=False,
            severity="warning",
            detail=f"data governance has {alert_count} warning(s)",
        )
    return ReliabilityCheckResult(
        name="rdp_data_governance_monitoring",
        category="data",
        passed=True,
        severity="info",
        detail="data governance monitoring has no active alerts",
    )


# ── 默认检查列表 ──────────────────────────────────────────────

DEFAULT_RELIABILITY_CHECKS: list[Callable[[Path], ReliabilityCheckResult]] = [
    check_quality_monitor_exists,
    check_active_decisions_exists,
    check_workflow_configs_exist,
    check_artifact_directories,
    check_open_failures,
    check_release_history_exists,
    check_active_parameters,
    check_data_governance_monitoring,
]


# ── 执行所有检查 ──────────────────────────────────────────────

def run_all_checks(
    root: Path,
    checks: list[Callable] | None = None,
) -> list[ReliabilityCheckResult]:
    """执行所有可靠性检查."""
    checks = checks or DEFAULT_RELIABILITY_CHECKS
    results = []
    for check_fn in checks:
        try:
            result = check_fn(root)
            results.append(result)
        except Exception as e:
            results.append(ReliabilityCheckResult(
                name=check_fn.__name__,
                category="unknown",
                passed=False,
                severity="critical",
                detail=f"check crashed: {e}",
            ))
    return results
