"""Rollback Recommendation Policy.

工作包 D: 把 rollback 从"人工临时决定"变成有规则支撑的推荐流程。

系统给出:
  - 是否建议 rollback
  - 为什么建议 rollback
  - rollback 到哪个 parameter set

触发条件:
  1. Attribution Regression — failure mode 明显恶化
  2. Execution Regression — fill ratio / cost / edge 恶化
  3. Governance Regression — quality monitor 退化
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ROLLBACK_DIR = "artifacts/production_workflow/rollback_recommendations"


# ── 回滚条件评估 ──────────────────────────────────────────────────


def _evaluate_attribution_regression(
    project_root: Path, family: str, timeframe: str,
) -> dict[str, Any]:
    """评估 attribution failure mode 是否恶化."""
    rounds_dir = project_root / "artifacts/research/attribution_rounds"
    if not rounds_dir.exists():
        return {"trigger": "attribution_regression", "fired": False, "detail": "无 attribution 数据"}

    dirs = sorted(
        (d for d in rounds_dir.iterdir() if d.is_dir()),
        key=lambda d: d.name, reverse=True,
    )
    if len(dirs) < 1:
        return {"trigger": "attribution_regression", "fired": False, "detail": "数据不足"}

    latest = dirs[0]
    combo_key = f"{family}_{timeframe.lower()}"

    # 尝试 combo 级 summary
    combo_summary_path = latest / combo_key / "attribution_summary.json"
    if not combo_summary_path.exists():
        combo_summary_path = latest / "attribution_summary.json"

    if not combo_summary_path.exists():
        return {"trigger": "attribution_regression", "fired": False, "detail": "无 summary 文件"}

    try:
        with combo_summary_path.open(encoding="utf-8") as f:
            summary = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"trigger": "attribution_regression", "fired": False, "detail": "无法读取 summary"}

    # 分析 failure mode 变化
    failure_modes = summary.get("failure_modes", summary.get("top_failure_modes", []))
    strategy_failure_pct = summary.get("strategy_failure_pct", 0)
    risk_failure_pct = summary.get("risk_failure_pct", 0)
    execution_failure_pct = summary.get("execution_failure_pct", 0)

    # 高失败率触发
    total_failure = strategy_failure_pct + risk_failure_pct + execution_failure_pct
    if total_failure > 80:
        return {
            "trigger": "attribution_regression",
            "fired": True,
            "severity": "high",
            "detail": f"总失败率 {total_failure:.0f}% (strategy={strategy_failure_pct:.0f}%, "
                     f"risk={risk_failure_pct:.0f}%, execution={execution_failure_pct:.0f}%)",
        }

    return {
        "trigger": "attribution_regression",
        "fired": False,
        "detail": f"总失败率 {total_failure:.0f}% (正常范围)",
    }


def _evaluate_execution_regression(
    project_root: Path, family: str, timeframe: str,
) -> dict[str, Any]:
    """评估 execution realism 是否恶化."""
    rounds_dir = project_root / "artifacts/research/execution_rounds"
    if not rounds_dir.exists():
        return {"trigger": "execution_regression", "fired": False, "detail": "无 execution 数据"}

    dirs = sorted(
        (d for d in rounds_dir.iterdir() if d.is_dir()),
        key=lambda d: d.name, reverse=True,
    )
    if not dirs:
        return {"trigger": "execution_regression", "fired": False, "detail": "数据不足"}

    latest = dirs[0]
    combo_key = f"{family}_{timeframe.lower()}"

    combo_path = latest / combo_key / "execution_summary.json"
    if not combo_path.exists():
        combo_path = latest / "execution_summary.json"

    if not combo_path.exists():
        return {"trigger": "execution_regression", "fired": False, "detail": "无 summary"}

    try:
        with combo_path.open(encoding="utf-8") as f:
            summary = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"trigger": "execution_regression", "fired": False, "detail": "无法读取"}

    full_fill = summary.get("full_fill_ratio", 1.0)
    cost_bps = summary.get("mean_total_execution_cost_bps", 0)
    positive_edge = summary.get("positive_adjusted_edge_ratio", 1.0)

    reasons = []
    if full_fill < 0.5:
        reasons.append(f"full_fill_ratio={full_fill:.2f} (<0.5)")
    if cost_bps > 10:
        reasons.append(f"mean_execution_cost={cost_bps:.1f}bps (>10)")
    if positive_edge < 0.3:
        reasons.append(f"positive_edge_ratio={positive_edge:.2f} (<0.3)")

    if reasons:
        return {
            "trigger": "execution_regression",
            "fired": True,
            "severity": "high" if len(reasons) >= 2 else "medium",
            "detail": "; ".join(reasons),
        }

    return {
        "trigger": "execution_regression",
        "fired": False,
        "detail": f"fill={full_fill:.2f}, cost={cost_bps:.1f}bps, edge={positive_edge:.2f}",
    }


def _evaluate_governance_regression(project_root: Path) -> dict[str, Any]:
    """评估 governance 层是否退化."""
    qm_path = project_root / "artifacts/governance/quality_monitor_summary.json"
    if not qm_path.exists():
        return {"trigger": "governance_regression", "fired": False, "detail": "无 quality monitor"}

    try:
        with qm_path.open(encoding="utf-8") as f:
            qm = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"trigger": "governance_regression", "fired": False, "detail": "无法读取"}

    summary = qm.get("summary", {})
    health = summary.get("health", "unknown")
    critical = summary.get("critical_failures", 0)

    if health == "unhealthy" or critical > 0:
        return {
            "trigger": "governance_regression",
            "fired": True,
            "severity": "high",
            "detail": f"health={health}, critical_failures={critical}",
        }
    if health == "degraded":
        return {
            "trigger": "governance_regression",
            "fired": True,
            "severity": "medium",
            "detail": f"health=degraded",
        }

    return {
        "trigger": "governance_regression",
        "fired": False,
        "detail": f"health={health}",
    }


# ── 综合评估 ──────────────────────────────────────────────────────


def evaluate_rollback_recommendation(
    project_root: Path,
    *,
    release_id: str,
    family: str,
    timeframe: str,
    save_result: bool = True,
) -> dict[str, Any]:
    """评估是否建议 rollback.

    Returns
    -------
    dict  包含:
      - rollback_recommended: bool
      - severity: "none" / "medium" / "high"
      - reasons: list[str]
      - suggested_target_parameter_set_id: str | None
      - triggers: list[dict]
    """
    now = datetime.now(timezone.utc)

    triggers = [
        _evaluate_attribution_regression(project_root, family, timeframe),
        _evaluate_execution_regression(project_root, family, timeframe),
        _evaluate_governance_regression(project_root),
    ]

    fired = [t for t in triggers if t.get("fired")]
    reasons = [t["detail"] for t in fired]

    # 判定 severity
    high_count = sum(1 for t in fired if t.get("severity") == "high")
    medium_count = sum(1 for t in fired if t.get("severity") == "medium")

    if high_count > 0:
        severity = "high"
        rollback_recommended = True
    elif medium_count >= 2:
        severity = "high"
        rollback_recommended = True
    elif medium_count == 1:
        severity = "medium"
        rollback_recommended = True
    else:
        severity = "none"
        rollback_recommended = False

    # 查找 rollback 目标
    suggested_target = None
    if rollback_recommended:
        from aats.data_platform.production_workflow.release_registry import (
            find_release,
            load_release_history,
        )
        history = load_release_history(project_root)
        release = find_release(history, release_id)
        if release:
            suggested_target = release.get("previous_parameter_set_id")

    result = {
        "release_id": release_id,
        "family": family,
        "timeframe": timeframe,
        "combo_key": f"{family}_{timeframe.lower()}",
        "evaluated_at": now.isoformat(),
        "rollback_recommended": rollback_recommended,
        "severity": severity,
        "reasons": reasons,
        "suggested_target_parameter_set_id": suggested_target,
        "triggers": triggers,
        "fired_trigger_count": len(fired),
    }

    if save_result:
        _save_rollback_recommendation(project_root, release_id, result)

    return result


def _save_rollback_recommendation(
    project_root: Path,
    release_id: str,
    result: dict[str, Any],
) -> Path:
    rb_dir = project_root / _ROLLBACK_DIR / release_id
    rb_dir.mkdir(parents=True, exist_ok=True)

    json_path = rb_dir / "rollback_recommendation.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    report_path = rb_dir / "rollback_recommendation_report.md"
    _write_rollback_report(result, report_path)

    log.info("Rollback recommendation saved: %s", rb_dir)
    return rb_dir


def _write_rollback_report(result: dict[str, Any], path: Path) -> None:
    rec = "YES" if result["rollback_recommended"] else "NO"
    lines = [
        "# Rollback Recommendation Report",
        "",
        f"- Release ID: `{result['release_id']}`",
        f"- Combo: {result['combo_key']}",
        f"- Evaluated: {result['evaluated_at']}",
        f"- **Rollback Recommended: {rec}**",
        f"- Severity: {result['severity']}",
        "",
    ]

    if result["suggested_target_parameter_set_id"]:
        lines.append(f"- Suggested Target: `{result['suggested_target_parameter_set_id']}`")
        lines.append("")

    lines.append("## Triggers")
    lines.append("")
    for t in result["triggers"]:
        icon = "FIRED" if t.get("fired") else "OK"
        lines.append(f"- [{icon}] **{t['trigger']}**: {t.get('detail', '')}")
    lines.append("")

    if result["reasons"]:
        lines.append("## Reasons")
        lines.append("")
        for r in result["reasons"]:
            lines.append(f"- {r}")
        lines.append("")

    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
