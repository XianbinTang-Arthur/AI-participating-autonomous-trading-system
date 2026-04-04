"""Observation Window 管理.

工作包 C: 参数生效后自动进入观察窗口。

观察窗口状态:
  - observing:             正在观察中
  - completed:             观察期结束，无异常
  - rollback_recommended:  观察期内发现异常，建议回滚

观察指标:
  1. Live 行为层: attribution failure mode 变化
  2. 执行层: execution realism 变化
  3. 治理层: quality monitor 变化
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_OBSERVATIONS_DIR = "artifacts/production_workflow/observations"


# ── 观察指标检查 ────────────────────────────────────────────────────


def _check_quality_monitor_regression(project_root: Path) -> dict[str, Any]:
    """检查 quality monitor 是否退化."""
    qm_path = project_root / "artifacts/governance/quality_monitor_summary.json"
    if not qm_path.exists():
        return {"name": "quality_monitor", "status": "unknown", "detail": "文件不存在"}

    try:
        with qm_path.open(encoding="utf-8") as f:
            qm = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"name": "quality_monitor", "status": "unknown", "detail": "无法读取"}

    summary = qm.get("summary", {})
    health = summary.get("health", "unknown")
    critical = summary.get("critical_failures", 0)

    if health == "unhealthy" or critical > 0:
        return {
            "name": "quality_monitor",
            "status": "regression",
            "detail": f"health={health}, critical={critical}",
            "severity": "high",
        }
    if health == "degraded":
        return {
            "name": "quality_monitor",
            "status": "warn",
            "detail": f"health=degraded",
            "severity": "medium",
        }
    return {
        "name": "quality_monitor",
        "status": "ok",
        "detail": f"health={health}",
        "severity": "none",
    }


def _check_decision_regression(
    project_root: Path, family: str, timeframe: str,
) -> dict[str, Any]:
    """检查 family/tf decision 是否退化."""
    dec_path = project_root / "artifacts/decision_system/active_decision_registry.json"
    if not dec_path.exists():
        return {"name": "decision_status", "status": "unknown", "detail": "无 decision registry"}

    try:
        with dec_path.open(encoding="utf-8") as f:
            reg = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"name": "decision_status", "status": "unknown", "detail": "无法读取"}

    combo_key = f"{family}_{timeframe.lower()}"
    for d in reg.get("decisions", []):
        if d.get("combo_key") == combo_key or (
            d.get("family") == family and d.get("timeframe", "").lower() == timeframe.lower()
        ):
            status = d.get("current_status", "unknown")
            if status == "pause":
                return {
                    "name": "decision_status",
                    "status": "regression",
                    "detail": f"{combo_key} 状态为 pause",
                    "severity": "high",
                }
            if status == "require_review":
                return {
                    "name": "decision_status",
                    "status": "warn",
                    "detail": f"{combo_key} 状态为 require_review",
                    "severity": "medium",
                }
            return {
                "name": "decision_status",
                "status": "ok",
                "detail": f"{combo_key} 状态为 {status}",
                "severity": "none",
            }

    return {"name": "decision_status", "status": "ok", "detail": "无 decision 记录"}


def _check_attribution_regression(project_root: Path) -> dict[str, Any]:
    """检查 attribution failure mode 是否恶化."""
    rounds_dir = project_root / "artifacts/research/attribution_rounds"
    if not rounds_dir.exists():
        return {"name": "attribution", "status": "unknown", "detail": "无 attribution rounds"}

    dirs = sorted(
        (d for d in rounds_dir.iterdir() if d.is_dir()),
        key=lambda d: d.name, reverse=True,
    )
    if not dirs:
        return {"name": "attribution", "status": "unknown", "detail": "无 attribution round 数据"}

    latest = dirs[0]
    summary_path = latest / "attribution_summary.json"
    if not summary_path.exists():
        return {
            "name": "attribution",
            "status": "unknown",
            "detail": f"最新 round {latest.name} 无 summary",
        }

    return {
        "name": "attribution",
        "status": "ok",
        "detail": f"latest round: {latest.name}",
        "severity": "none",
    }


def _check_execution_regression(project_root: Path) -> dict[str, Any]:
    """检查 execution realism 是否恶化."""
    rounds_dir = project_root / "artifacts/research/execution_rounds"
    if not rounds_dir.exists():
        return {"name": "execution_realism", "status": "unknown", "detail": "无 execution rounds"}

    dirs = sorted(
        (d for d in rounds_dir.iterdir() if d.is_dir()),
        key=lambda d: d.name, reverse=True,
    )
    if not dirs:
        return {"name": "execution_realism", "status": "unknown", "detail": "无 execution round 数据"}

    return {
        "name": "execution_realism",
        "status": "ok",
        "detail": f"latest round: {dirs[0].name}",
        "severity": "none",
    }


# ── 观察窗口评估 ──────────────────────────────────────────────────


def run_observation(
    project_root: Path,
    *,
    release_id: str,
    family: str,
    timeframe: str,
    window_hours: int = 24,
    save_result: bool = True,
) -> dict[str, Any]:
    """运行 post-apply 观察检查.

    Returns
    -------
    dict  observation summary 包含:
      - release_id
      - family / timeframe
      - started_at
      - observation_window_hours
      - status: observing / completed / rollback_recommended
      - checklist: list of check results
      - recommendation: keep / review / rollback_recommended
    """
    now = datetime.now(timezone.utc)

    # 检查 release 时间判断窗口
    from aats.data_platform.production_workflow.release_registry import (
        find_release,
        load_release_history,
    )
    history = load_release_history(project_root)
    release = find_release(history, release_id)

    window_active = True
    started_at = now.isoformat()
    if release:
        created_str = release.get("created_at")
        if created_str:
            try:
                created_at = datetime.fromisoformat(created_str)
                elapsed_hours = (now - created_at).total_seconds() / 3600
                started_at = created_str
                if elapsed_hours >= window_hours:
                    window_active = False
            except (ValueError, TypeError):
                pass

    # 运行各项检查
    checklist = [
        _check_quality_monitor_regression(project_root),
        _check_decision_regression(project_root, family, timeframe),
        _check_attribution_regression(project_root),
        _check_execution_regression(project_root),
    ]

    # 判定观察状态
    regressions = [c for c in checklist if c.get("status") == "regression"]
    warns = [c for c in checklist if c.get("status") == "warn"]

    if regressions:
        observation_status = "rollback_recommended"
        recommendation = "rollback_recommended"
    elif warns:
        if window_active:
            observation_status = "observing"
            recommendation = "review"
        else:
            observation_status = "completed"
            recommendation = "review"
    else:
        if window_active:
            observation_status = "observing"
            recommendation = "keep"
        else:
            observation_status = "completed"
            recommendation = "keep"

    result = {
        "release_id": release_id,
        "family": family,
        "timeframe": timeframe,
        "combo_key": f"{family}_{timeframe.lower()}",
        "evaluated_at": now.isoformat(),
        "started_at": started_at,
        "observation_window_hours": window_hours,
        "window_active": window_active,
        "status": observation_status,
        "recommendation": recommendation,
        "checklist": checklist,
        "regression_count": len(regressions),
        "warning_count": len(warns),
    }

    if save_result:
        _save_observation(project_root, release_id, result)

    # 更新 release history
    if release:
        from aats.data_platform.production_workflow.release_registry import (
            save_release_history,
            update_release_status,
        )
        update_release_status(
            history, release_id,
            observation_status=observation_status,
        )
        save_release_history(history, project_root)

    return result


def _save_observation(
    project_root: Path,
    release_id: str,
    result: dict[str, Any],
) -> Path:
    obs_dir = project_root / _OBSERVATIONS_DIR / release_id
    obs_dir.mkdir(parents=True, exist_ok=True)

    summary_path = obs_dir / "observation_summary.json"
    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    report_path = obs_dir / "observation_report.md"
    _write_observation_report(result, report_path)

    log.info("Observation saved: %s", obs_dir)
    return obs_dir


def _write_observation_report(result: dict[str, Any], path: Path) -> None:
    lines = [
        "# Post-Apply Observation Report",
        "",
        f"- Release ID: `{result['release_id']}`",
        f"- Combo: {result['combo_key']}",
        f"- Evaluated: {result['evaluated_at']}",
        f"- Window Active: {'Yes' if result['window_active'] else 'No'}",
        f"- **Status: {result['status'].upper()}**",
        f"- **Recommendation: {result['recommendation'].upper()}**",
        "",
        "## Checklist",
        "",
    ]

    for check in result["checklist"]:
        status_icon = {"ok": "OK", "warn": "WARN", "regression": "REGRESSION", "unknown": "?"}.get(
            check.get("status", "?"), "?"
        )
        lines.append(f"- [{status_icon}] **{check['name']}**: {check.get('detail', '')}")

    lines.append("")
    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines))
