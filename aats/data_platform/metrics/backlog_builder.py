"""Improvement Backlog 自动生成器.

工作包 E: 把 metrics / review 结果转成可执行的改进任务。

Backlog 来源:
  - 高失败率的 workflow
  - 高频 rollback recommendation
  - attribution 长期集中某 failure mode
  - execution realism 长期较差
  - stale recommendations
  - low readiness family/timeframe
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_json(fp: Path) -> dict | None:
    if not fp.exists():
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _backlog_path(root: Path) -> Path:
    return root / "artifacts" / "metrics" / "improvement_backlog.json"


def load_backlog(root: Path) -> dict:
    fp = _backlog_path(root)
    if not fp.exists():
        return {"items": [], "generated_at": None}
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)


def save_backlog(root: Path, data: dict) -> None:
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(_backlog_path(root), data)


def _make_item(
    *,
    source: str,
    category: str,
    family: str | None = None,
    timeframe: str | None = None,
    priority: str,
    problem_statement: str,
    suggested_action: str,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "backlog_id": f"bl_{now.strftime('%Y%m%d_%H%M%S')}_{source[:8]}",
        "created_at": now.isoformat(),
        "source": source,
        "category": category,
        "family": family,
        "timeframe": timeframe,
        "priority": priority,
        "problem_statement": problem_statement,
        "suggested_action": suggested_action,
        "status": "open",
    }


# ── 各来源检测 ────────────────────────────���───────────────────

def _check_workflow_failures(root: Path) -> list[dict]:
    """检测高失败率 workflow."""
    items = []
    runs_dir = root / "artifacts" / "operations" / "workflow_runs"
    if not runs_dir.exists():
        return items

    wf_stats: dict[str, dict] = {}
    for fp in runs_dir.iterdir():
        if fp.suffix != ".json":
            continue
        data = _load_json(fp)
        if not data:
            continue
        wf_name = data.get("workflow", "unknown")
        if wf_name not in wf_stats:
            wf_stats[wf_name] = {"total": 0, "failed": 0}
        wf_stats[wf_name]["total"] += 1
        if data.get("overall_status") == "failed":
            wf_stats[wf_name]["failed"] += 1

    for wf, stats in wf_stats.items():
        if stats["total"] >= 3 and stats["failed"] / stats["total"] > 0.3:
            items.append(_make_item(
                source="workflow_failure_analysis",
                category="reliability",
                priority="high",
                problem_statement=(
                    f"Workflow '{wf}' 失败率 {stats['failed']}/{stats['total']} "
                    f"({stats['failed']/stats['total']:.0%})"
                ),
                suggested_action=f"审查 {wf} 的失败原因，优化配置或修复根因",
            ))

    return items


def _check_rollback_frequency(root: Path) -> list[dict]:
    """检测高频 rollback."""
    items = []
    data = _load_json(
        root / "artifacts" / "decision_system" / "parameter_apply_history.json"
    )
    if not data:
        return items

    ops = data.get("operations", [])
    combo_stats: dict[str, dict] = {}
    for o in ops:
        key = f"{o.get('family')}_{o.get('timeframe')}"
        if key not in combo_stats:
            combo_stats[key] = {
                "apply": 0, "rollback": 0,
                "family": o.get("family"),
                "timeframe": o.get("timeframe"),
            }
        if o.get("operation_type") == "apply":
            combo_stats[key]["apply"] += 1
        elif o.get("operation_type") == "rollback":
            combo_stats[key]["rollback"] += 1

    for key, stats in combo_stats.items():
        total = stats["apply"] + stats["rollback"]
        if total >= 2 and stats["rollback"] / total > 0.3:
            items.append(_make_item(
                source="rollback_frequency",
                category="operations",
                family=stats["family"],
                timeframe=stats["timeframe"],
                priority="high",
                problem_statement=(
                    f"{key} 的 rollback 比率偏高 "
                    f"({stats['rollback']}/{total})"
                ),
                suggested_action="审查该 combo 的 recommendation 质量和 evidence 完整性",
            ))

    return items


def _check_stale_recommendations(root: Path) -> list[dict]:
    """检测 stale (长期 draft) recommendations."""
    items = []
    data = _load_json(
        root / "artifacts" / "decision_system" / "recommendation_registry.json"
    )
    if not data:
        return items

    recs = data.get("recommendations", [])
    draft_count = sum(1 for r in recs if r.get("status") == "draft")
    total = len(recs)

    if total >= 5 and draft_count / total > 0.5:
        items.append(_make_item(
            source="stale_recommendations",
            category="research",
            priority="medium",
            problem_statement=(
                f"{draft_count}/{total} recommendations 仍为 draft 状态"
            ),
            suggested_action="审查 draft recommendations，approve 或 reject 过期的",
        ))

    return items


def _check_ineffective_releases(root: Path) -> list[dict]:
    """检测 ineffective releases."""
    items = []
    data = _load_json(
        root / "artifacts" / "metrics" / "release_effectiveness_registry.json"
    )
    if not data:
        return items

    evals = data.get("evaluations", [])
    ineffective_combos: dict[str, int] = {}
    for e in evals:
        if e.get("conclusion") in ("ineffective", "rollback_triggered"):
            key = f"{e.get('family')}_{e.get('timeframe')}"
            ineffective_combos[key] = ineffective_combos.get(key, 0) + 1

    for key, count in ineffective_combos.items():
        if count >= 2:
            parts = key.split("_", 1)
            items.append(_make_item(
                source="ineffective_releases",
                category="research",
                family=parts[0] if len(parts) > 0 else None,
                timeframe=parts[1] if len(parts) > 1 else None,
                priority="high",
                problem_statement=f"{key} 有 {count} 次 ineffective release",
                suggested_action="审查该 combo 的研究流程和参数选择标准",
            ))

    return items


def _check_low_evidence_completeness(root: Path) -> list[dict]:
    """检测 evidence completeness 偏低."""
    items = []
    rounds_dir = root / "artifacts" / "decision_rounds"
    if not rounds_dir.exists():
        return items

    dirs = sorted(
        [d for d in rounds_dir.iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    for d in dirs[:3]:
        es = _load_json(d / "evidence_summary.json")
        if not es:
            continue
        ec = es.get("evidence_completeness", {})
        ratio = ec.get("completeness_ratio", 0)
        if ratio < 0.5:
            items.append(_make_item(
                source="low_evidence_completeness",
                category="research",
                priority="medium",
                problem_statement=(
                    f"Decision round {d.name} 的 evidence completeness "
                    f"仅 {ratio:.0%}"
                ),
                suggested_action="补充缺失的 phase 数据（归因、执行真实性）",
            ))
            break  # 只报最新一个

    return items


def _check_open_alerts(root: Path) -> list[dict]:
    """检测长期未处理的 alerts."""
    items = []
    data = _load_json(
        root / "artifacts" / "operations" / "alerts" / "current_alerts.json"
    )
    if not data:
        return items

    critical = sum(
        1 for a in data.get("alerts", [])
        if not a.get("acknowledged") and a.get("severity") == "critical"
    )
    if critical > 0:
        items.append(_make_item(
            source="open_critical_alerts",
            category="reliability",
            priority="high",
            problem_statement=f"{critical} 个未处理的 critical 告警",
            suggested_action="立即处理 critical 告警，确保系统健康",
        ))

    return items


# ── 主函数 ────────────────────────────────────────────────────

def generate_improvement_backlog(
    root: Path,
    merge_with_existing: bool = True,
) -> dict:
    """生成 improvement backlog.

    Args:
        merge_with_existing: 如果 True，保留已有 items 的状态

    Returns:
        backlog dict
    """
    # 收集新 items
    new_items: list[dict] = []
    new_items.extend(_check_workflow_failures(root))
    new_items.extend(_check_rollback_frequency(root))
    new_items.extend(_check_stale_recommendations(root))
    new_items.extend(_check_ineffective_releases(root))
    new_items.extend(_check_low_evidence_completeness(root))
    new_items.extend(_check_open_alerts(root))

    if merge_with_existing:
        existing = load_backlog(root)
        existing_items = existing.get("items", [])

        # 保留 non-open 的已有 items
        kept = [i for i in existing_items if i.get("status") != "open"]

        # 替换所有 open items 为新生成的
        merged = kept + new_items
    else:
        merged = new_items

    backlog = {
        "items": merged,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "total": len(merged),
            "open": sum(1 for i in merged if i.get("status") == "open"),
            "in_progress": sum(
                1 for i in merged if i.get("status") == "in_progress"
            ),
            "resolved": sum(
                1 for i in merged if i.get("status") == "resolved"
            ),
            "ignored": sum(
                1 for i in merged if i.get("status") == "ignored"
            ),
            "high_priority": sum(
                1 for i in merged
                if i.get("status") == "open" and i.get("priority") == "high"
            ),
        },
    }

    save_backlog(root, backlog)
    return backlog


def update_backlog_item_status(
    root: Path,
    backlog_id: str,
    status: str,
    notes: str = "",
) -> dict | None:
    """更新 backlog item 状态."""
    data = load_backlog(root)
    for item in data.get("items", []):
        if item.get("backlog_id") == backlog_id:
            item["status"] = status
            if notes:
                item["resolution_notes"] = notes
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_backlog(root, data)
            return item
    return None


def backlog_from_review(
    root: Path,
    review: dict,
) -> list[dict]:
    """从 periodic review 的 improvement_suggestions 转成 backlog items."""
    items = []
    for sg in review.get("improvement_suggestions", []):
        items.append(_make_item(
            source=f"review_{review.get('review_id', 'unknown')}",
            category=sg.get("category", "unknown"),
            priority=sg.get("priority", "medium"),
            problem_statement=sg.get("problem", ""),
            suggested_action=sg.get("suggested_action", ""),
        ))
    return items
