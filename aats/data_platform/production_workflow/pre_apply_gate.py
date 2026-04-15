"""Pre-Apply Policy Gate.

工作包 A: 在 apply active parameter set 之前运行的统一门禁检查。

即使 recommendation 已经 approved，也不代表应该"现在立刻 apply"。
gate 会检查治理状态、artifact 新鲜度、决策一致性、round 健康度。

输出:
  - allow_apply: bool
  - gate_status: "pass" / "warn" / "block"
  - checks: list[GateCheckResult]
  - blocking_reasons: list[str]
  - warnings: list[str]
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from aats.data_platform.production_workflow.gate_rules import (
    DEFAULT_GATE_RULES,
    GateCheckResult,
    _strict_gate_environment,
)
from aats.data_platform.production_workflow.gate_runtime_contract import (
    build_gate_runtime_contract,
)

log = logging.getLogger(__name__)

# ── 路径常量 ───────────────────────────────────────────────────────

_GOVERNANCE_DIR = "artifacts/governance"
_DECISION_SYSTEM_DIR = "artifacts/decision_system"
_DECISION_ROUNDS_DIR = "artifacts/decision_rounds"
_GATES_DIR = "artifacts/production_workflow/gates"


def _make_gate_run_id() -> str:
    return f"gate_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


# ── 上下文构建 ─────────────────────────────────────────────────────


def _safe_load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _find_latest_round_dir(rounds_root: Path) -> Path | None:
    if not rounds_root.exists():
        return None
    dirs = sorted(
        (d for d in rounds_root.iterdir() if d.is_dir()),
        key=lambda d: d.name,
        reverse=True,
    )
    return dirs[0] if dirs else None


def build_gate_context(
    project_root: Path,
    recommendation_id: str,
) -> dict[str, Any]:
    """构建 gate 检查所需的上下文."""
    from aats.data_platform.decision_system.recommendation_registry import (
        find_recommendation,
        load_recommendation_registry,
    )
    from aats.data_platform.governance.parameter_registry import load_registry
    from aats.data_platform.operations.environment_guard import (
        get_current_environment,
        get_policy,
    )

    ctx: dict[str, Any] = {"project_root": str(project_root)}
    env = get_current_environment()
    ctx["environment"] = env
    ctx["environment_policy"] = get_policy(env)

    # recommendation
    rec_path = project_root / _DECISION_SYSTEM_DIR / "recommendation_registry.json"
    rec_reg = load_recommendation_registry(rec_path)
    rec = find_recommendation(rec_reg, recommendation_id)
    ctx["recommendation"] = rec or {}

    # quality monitor
    qm = _safe_load_json(project_root / _GOVERNANCE_DIR / "quality_monitor_summary.json")
    ctx["quality_monitor"] = qm

    # active decisions
    dec_reg = _safe_load_json(
        project_root / _DECISION_SYSTEM_DIR / "active_decision_registry.json",
    )
    ctx["active_decisions"] = (dec_reg or {}).get("decisions", [])

    # latest decision round
    rounds_root = project_root / _DECISION_ROUNDS_DIR
    latest_dir = _find_latest_round_dir(rounds_root)
    if latest_dir:
        manifest = _safe_load_json(latest_dir / "round_manifest.json")
        ctx["latest_decision_round"] = {
            "round_id": latest_dir.name,
            "round_manifest": manifest or {},
        }
    else:
        ctx["latest_decision_round"] = {}

    # parameter sets
    gov_reg = load_registry(project_root / _GOVERNANCE_DIR / "current_parameter_registry.json")
    ctx["parameter_sets"] = gov_reg.get("parameter_sets", [])

    runtime_contract = build_gate_runtime_contract(project_root, environment=env)
    ctx["runtime_contract"] = runtime_contract

    # backward-compatible aliases for existing callers/tests
    ctx["current_alerts"] = runtime_contract.get("current_alerts")
    ctx["latest_workflow_runs"] = runtime_contract.get("latest_workflow_runs", {})
    ctx["live_db_health"] = runtime_contract.get("live_db_health", {})

    return ctx


# ── Gate 执行 ──────────────────────────────────────────────────────


def run_pre_apply_gate(
    project_root: Path,
    recommendation_id: str,
    *,
    rules: list | None = None,
    save_result: bool = True,
) -> dict[str, Any]:
    """运行 pre-apply gate 检查.

    Parameters
    ----------
    project_root : Path
    recommendation_id : str
    rules : list, optional
        自定义规则集，默认 DEFAULT_GATE_RULES
    save_result : bool
        是否保存 gate 结果到 artifacts

    Returns
    -------
    dict  包含 allow_apply, gate_status, checks, blocking_reasons, warnings
    """
    if rules is None:
        rules = DEFAULT_GATE_RULES

    gate_run_id = _make_gate_run_id()
    ctx = build_gate_context(project_root, recommendation_id)
    strict_environment = _strict_gate_environment(ctx)

    checks: list[dict[str, Any]] = []
    blocking_reasons: list[str] = []
    warnings: list[str] = []

    for rule_fn in rules:
        try:
            result: GateCheckResult = rule_fn(ctx)
        except Exception as exc:
            log.warning("gate rule %s 执行异常: %s", rule_fn.__name__, exc)
            result = GateCheckResult(
                name=rule_fn.__name__,
                category="error",
                passed=False,
                severity="block" if strict_environment else "warn",
                detail=f"规则执行异常: {exc}",
            )

        check_dict = {
            "name": result.name,
            "category": result.category,
            "passed": result.passed,
            "severity": result.severity,
            "detail": result.detail,
        }
        checks.append(check_dict)

        if not result.passed and result.severity == "block":
            blocking_reasons.append(f"[{result.name}] {result.detail}")
        elif result.severity == "warn" and not result.passed:
            warnings.append(f"[{result.name}] {result.detail}")
        elif result.severity == "warn" and result.passed:
            # passed but warn severity → 低优先级 warning
            warnings.append(f"[{result.name}] {result.detail}")

    # 判定 gate_status
    if blocking_reasons:
        gate_status = "block"
        allow_apply = False
    elif warnings:
        gate_status = "warn"
        allow_apply = True
    else:
        gate_status = "pass"
        allow_apply = True

    gate_result = {
        "gate_run_id": gate_run_id,
        "recommendation_id": recommendation_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "allow_apply": allow_apply,
        "gate_status": gate_status,
        "total_checks": len(checks),
        "passed_checks": sum(1 for c in checks if c["passed"]),
        "checks": checks,
        "blocking_reasons": blocking_reasons,
        "warnings": warnings,
    }

    if save_result:
        _save_gate_result(project_root, gate_run_id, gate_result)

    return gate_result


def _save_gate_result(
    project_root: Path,
    gate_run_id: str,
    result: dict[str, Any],
) -> Path:
    """保存 gate 结果到 artifacts."""
    gate_dir = project_root / _GATES_DIR / gate_run_id
    gate_dir.mkdir(parents=True, exist_ok=True)

    result_path = gate_dir / "pre_apply_gate_result.json"
    with result_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    # 生成人可读报告
    report_path = gate_dir / "pre_apply_gate_report.md"
    _write_gate_report(result, report_path)

    log.info("Gate result saved: %s", gate_dir)
    return gate_dir


def _write_gate_report(result: dict[str, Any], path: Path) -> None:
    """生成 gate 检查的 Markdown 报告."""
    lines = [
        "# Pre-Apply Gate Report",
        "",
        f"- Gate Run ID: `{result['gate_run_id']}`",
        f"- Recommendation: `{result['recommendation_id']}`",
        f"- Time: {result['created_at']}",
        f"- **Status: {result['gate_status'].upper()}**",
        f"- Allow Apply: {'Yes' if result['allow_apply'] else 'No'}",
        "",
        f"## Checks ({result['passed_checks']}/{result['total_checks']} passed)",
        "",
    ]

    for check in result["checks"]:
        icon = "PASS" if check["passed"] else "FAIL"
        lines.append(f"- [{icon}] **{check['name']}** ({check['severity']})")
        lines.append(f"  - {check['detail']}")
        lines.append("")

    if result["blocking_reasons"]:
        lines.append("## Blocking Reasons")
        lines.append("")
        for reason in result["blocking_reasons"]:
            lines.append(f"- {reason}")
        lines.append("")

    if result["warnings"]:
        lines.append("## Warnings")
        lines.append("")
        for warning in result["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")

    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
