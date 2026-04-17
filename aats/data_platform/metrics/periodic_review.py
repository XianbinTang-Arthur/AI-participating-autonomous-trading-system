"""周期复盘模块.

工作包 D: 按周/月生成长周期复盘报告。

复盘内容:
  1. Metrics snapshot 汇总
  2. Release history 汇总
  3. Rollback history 汇总
  4. Top alerts / top failure modes
  5. Family/timeframe ranking
  6. Improvement backlog 建议
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from aats.data_platform.governance._db_util import try_governance_db


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


def _parse_iso(s: str | None) -> datetime | None:
    """Thin illegal-as-None wrapper around parse_iso_datetime_utc.

    Periodic review iterates historical artefacts where a single corrupt
    timestamp should downgrade the row to "out of window", not abort the job.
    Gate checks must use :func:`parse_iso_datetime_utc` directly so illegal
    inputs raise.
    """
    from aats.data_platform.governance._time_util import parse_iso_datetime_utc

    try:
        return parse_iso_datetime_utc(s, context="periodic_review")
    except ValueError:
        return None


def _in_window(ts_str: str | None, window_start: datetime) -> bool:
    """检查时间戳是否在窗口内."""
    ts = _parse_iso(ts_str)
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= window_start


# ── 数据收集 ──────────────────────────────────────────────────

def _collect_releases_in_window(
    root: Path, window_start: datetime,
    family: str | None = None, timeframe: str | None = None,
) -> list[dict]:
    from aats.data_platform.production_workflow.release_registry import (
        load_release_history,
    )

    data = load_release_history(root)
    releases = data.get("releases", []) if data else []
    result = []
    for r in releases:
        if not _in_window(r.get("created_at"), window_start):
            continue
        if family and r.get("family") != family:
            continue
        if timeframe and r.get("timeframe") != timeframe:
            continue
        result.append(r)
    return result


def _collect_operations_in_window(
    root: Path, window_start: datetime,
    family: str | None = None, timeframe: str | None = None,
) -> list[dict]:
    from aats.data_platform.decision_system.active_parameter_apply import (
        load_apply_history,
    )

    data = load_apply_history(root)
    ops = data.get("operations", []) if data else []
    result = []
    for o in ops:
        if not _in_window(o.get("created_at"), window_start):
            continue
        if family and o.get("family") != family:
            continue
        if timeframe and o.get("timeframe") != timeframe:
            continue
        result.append(o)
    return result


def _collect_failures_in_window(
    root: Path, window_start: datetime,
) -> list[dict]:
    data = _load_json(
        root / "artifacts" / "operations" / "workflow_failures.json"
    )
    failures = data.get("failures", []) if data else []
    return [f for f in failures if _in_window(f.get("recorded_at"), window_start)]


def _collect_workflow_runs_in_window(
    root: Path, window_start: datetime,
) -> list[dict]:
    workflow_runs: list[dict] = []
    engine, ok = try_governance_db()
    if ok:
        try:
            from aats.data_platform.governance.operational_state_db import (
                db_list_workflow_runs,
            )

            with Session(engine) as session:
                workflow_runs = db_list_workflow_runs(session, started_after=window_start)
        except Exception:
            workflow_runs = []
        finally:
            if engine is not None:
                engine.dispose()
    if workflow_runs:
        return workflow_runs

    runs_dir = root / "artifacts" / "operations" / "workflow_runs"
    results = []
    if not runs_dir.exists():
        return results
    for fp in runs_dir.iterdir():
        if fp.suffix != ".json":
            continue
        data = _load_json(fp)
        if data and _in_window(data.get("started_at"), window_start):
            results.append(data)
    return results


def _collect_effectiveness_in_window(
    root: Path, window_start: datetime,
    family: str | None = None, timeframe: str | None = None,
) -> list[dict]:
    from aats.data_platform.metrics.release_effectiveness import (
        load_effectiveness_registry,
    )

    data = load_effectiveness_registry(root)
    evals = data.get("evaluations", []) if data else []
    result = []
    for e in evals:
        if not _in_window(e.get("evaluated_at"), window_start):
            continue
        if family and e.get("family") != family:
            continue
        if timeframe and e.get("timeframe") != timeframe:
            continue
        result.append(e)
    return result


# ── Family/Timeframe Ranking ─────────────────────────────────

def _build_combo_ranking(
    releases: list[dict], effectiveness: list[dict]
) -> list[dict]:
    """按 family_timeframe combo 统计排名."""
    combos: dict[str, dict] = {}
    for r in releases:
        key = r.get("combo_key", f"{r.get('family')}_{r.get('timeframe')}")
        if key not in combos:
            combos[key] = {
                "combo_key": key,
                "family": r.get("family"),
                "timeframe": r.get("timeframe"),
                "release_count": 0,
                "apply_success": 0,
                "rollback_count": 0,
            }
        combos[key]["release_count"] += 1
        if r.get("apply_result") == "success":
            combos[key]["apply_success"] += 1

    # 加入 effectiveness 信息
    eff_by_release = {e["release_id"]: e for e in effectiveness}
    for r in releases:
        key = r.get("combo_key", f"{r.get('family')}_{r.get('timeframe')}")
        eff = eff_by_release.get(r.get("release_id"))
        if eff:
            conclusion = eff.get("conclusion", "")
            combos[key].setdefault("effective_count", 0)
            combos[key].setdefault("ineffective_count", 0)
            if conclusion == "effective":
                combos[key]["effective_count"] += 1
            elif conclusion in ("ineffective", "rollback_triggered"):
                combos[key]["ineffective_count"] += 1

    ranking = sorted(combos.values(), key=lambda c: c["release_count"], reverse=True)
    return ranking


# ── 主复盘函数 ────────────────────────────────────────────────

def run_periodic_review(
    root: Path,
    window: str = "weekly",
    family: str | None = None,
    timeframe: str | None = None,
) -> dict:
    """生成周期复盘报告.

    Args:
        window: "weekly" or "monthly"

    Returns:
        review dict
    """
    now = datetime.now(timezone.utc)
    if window == "weekly":
        window_start = now - timedelta(days=7)
    elif window == "monthly":
        window_start = now - timedelta(days=30)
    else:
        raise ValueError(f"unsupported window: {window}")

    review_id = f"review_{window}_{now.strftime('%Y%m%d_%H%M%S')}"

    # 收集数据
    releases = _collect_releases_in_window(root, window_start, family, timeframe)
    operations = _collect_operations_in_window(root, window_start, family, timeframe)
    failures = _collect_failures_in_window(root, window_start)
    wf_runs = _collect_workflow_runs_in_window(root, window_start)
    effectiveness = _collect_effectiveness_in_window(root, window_start, family, timeframe)

    # 统计
    apply_count = sum(1 for o in operations if o.get("operation_type") == "apply")
    rollback_count = sum(1 for o in operations if o.get("operation_type") == "rollback")
    success_releases = sum(1 for r in releases if r.get("apply_result") == "success")
    wf_success = sum(1 for w in wf_runs if w.get("overall_status") == "success")
    open_failures = sum(1 for f in failures if f.get("status") == "open")

    eff_effective = sum(1 for e in effectiveness if e.get("conclusion") == "effective")
    eff_mixed = sum(1 for e in effectiveness if e.get("conclusion") == "mixed")
    eff_ineffective = sum(
        1 for e in effectiveness
        if e.get("conclusion") in ("ineffective", "rollback_triggered")
    )
    eff_insufficient = sum(
        1 for e in effectiveness if e.get("conclusion") == "insufficient_evidence"
    )

    # Family/timeframe ranking
    combo_ranking = _build_combo_ranking(releases, effectiveness)

    # Metrics snapshot
    from aats.data_platform.metrics.metric_registry import (
        build_metrics_snapshot,
    )
    snapshot = build_metrics_snapshot(root, family, timeframe)

    # 构建改进建议
    improvement_suggestions = _generate_suggestions(
        releases, operations, failures, wf_runs, effectiveness, combo_ranking
    )

    review = {
        "review_id": review_id,
        "window": window,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "generated_at": now.isoformat(),
        "filter": {"family": family, "timeframe": timeframe},
        "summary": {
            "total_releases": len(releases),
            "successful_releases": success_releases,
            "total_applies": apply_count,
            "total_rollbacks": rollback_count,
            "rollback_ratio": round(rollback_count / max(apply_count, 1), 4),
            "workflow_runs": len(wf_runs),
            "workflow_success": wf_success,
            "workflow_success_ratio": round(wf_success / max(len(wf_runs), 1), 4),
            "open_failures": open_failures,
            "effectiveness": {
                "total_evaluated": len(effectiveness),
                "effective": eff_effective,
                "mixed": eff_mixed,
                "ineffective": eff_ineffective,
                "insufficient_evidence": eff_insufficient,
            },
        },
        "combo_ranking": combo_ranking,
        "metrics_snapshot_id": snapshot.get("snapshot_id"),
        "improvement_suggestions": improvement_suggestions,
    }

    # 保存
    _save_review(root, window, review_id, review)
    return review


def _generate_suggestions(
    releases, operations, failures, wf_runs, effectiveness, combo_ranking
) -> list[dict]:
    """基于复盘数据生成改进建议."""
    suggestions = []

    # 高 rollback ratio
    rollback_count = sum(1 for o in operations if o.get("operation_type") == "rollback")
    apply_count = sum(1 for o in operations if o.get("operation_type") == "apply")
    if apply_count > 0 and rollback_count / apply_count > 0.3:
        suggestions.append({
            "category": "operations",
            "priority": "high",
            "problem": f"高 rollback 率 ({rollback_count}/{apply_count})",
            "suggested_action": "审查 recommendation 质量和 gate 规则是否足够严格",
        })

    # 高失败率的 workflow
    wf_failed = sum(1 for w in wf_runs if w.get("overall_status") == "failed")
    if len(wf_runs) > 0 and wf_failed / len(wf_runs) > 0.2:
        suggestions.append({
            "category": "reliability",
            "priority": "high",
            "problem": f"workflow 失败率偏高 ({wf_failed}/{len(wf_runs)})",
            "suggested_action": "检查失败原因，优化 timeout 设置和依赖",
        })

    # open failures
    open_failures = sum(1 for f in failures if f.get("status") == "open")
    if open_failures > 0:
        suggestions.append({
            "category": "reliability",
            "priority": "medium",
            "problem": f"{open_failures} 个未处理的失败记录",
            "suggested_action": "处理或关闭 open 失败记录",
        })

    # ineffective releases
    ineff = sum(
        1 for e in effectiveness
        if e.get("conclusion") in ("ineffective", "rollback_triggered")
    )
    if ineff > 0:
        suggestions.append({
            "category": "research",
            "priority": "medium",
            "problem": f"{ineff} 次 release 被评为 ineffective/rollback",
            "suggested_action": "审查相关 family/timeframe 的研究质量和 evidence 完整性",
        })

    # 如果没有 releases
    if len(releases) == 0:
        suggestions.append({
            "category": "operations",
            "priority": "low",
            "problem": "周期内无新 release",
            "suggested_action": "检查 decision cycle 是否正常运行，是否有可升级的 recommendation",
        })

    return suggestions


def _save_review(root: Path, window: str, review_id: str, data: dict) -> None:
    """保存复盘报告."""
    out_dir = root / "artifacts" / "reviews" / window / review_id
    _atomic_write_json(out_dir / "review_summary.json", data)

    # Markdown 报告
    md = _generate_review_md(data)
    (out_dir / "review_report.md").parent.mkdir(parents=True, exist_ok=True)
    (out_dir / "review_report.md").write_text(md, encoding="utf-8")


def _generate_review_md(data: dict) -> str:
    """生成 markdown 复盘报告."""
    s = data.get("summary", {})
    lines = [
        f"# {data.get('window', '').capitalize()} Review Report",
        "",
        f"**Review ID:** {data.get('review_id')}",
        f"**Period:** {data.get('window_start', '?')} ~ {data.get('window_end', '?')}",
        f"**Generated:** {data.get('generated_at')}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Releases | {s.get('total_releases', 0)} (success: {s.get('successful_releases', 0)}) |",
        f"| Applies | {s.get('total_applies', 0)} |",
        f"| Rollbacks | {s.get('total_rollbacks', 0)} (ratio: {s.get('rollback_ratio', 0):.1%}) |",
        f"| Workflow Runs | {s.get('workflow_runs', 0)} (success: {s.get('workflow_success', 0)}) |",
        f"| Open Failures | {s.get('open_failures', 0)} |",
        "",
    ]

    # Effectiveness
    eff = s.get("effectiveness", {})
    if eff.get("total_evaluated", 0) > 0:
        lines.extend([
            "## Release Effectiveness",
            "",
            "| Result | Count |",
            "|--------|-------|",
            f"| Effective | {eff.get('effective', 0)} |",
            f"| Mixed | {eff.get('mixed', 0)} |",
            f"| Ineffective | {eff.get('ineffective', 0)} |",
            f"| Insufficient Evidence | {eff.get('insufficient_evidence', 0)} |",
            "",
        ])

    # Combo ranking
    ranking = data.get("combo_ranking", [])
    if ranking:
        lines.extend([
            "## Family / Timeframe Ranking",
            "",
            "| Combo | Releases | Success | Effective | Ineffective |",
            "|-------|----------|---------|-----------|-------------|",
        ])
        for c in ranking:
            lines.append(
                f"| {c.get('combo_key', '?')} "
                f"| {c.get('release_count', 0)} "
                f"| {c.get('apply_success', 0)} "
                f"| {c.get('effective_count', 0)} "
                f"| {c.get('ineffective_count', 0)} |"
            )
        lines.append("")

    # Improvement suggestions
    suggestions = data.get("improvement_suggestions", [])
    if suggestions:
        lines.extend([
            "## Improvement Suggestions",
            "",
        ])
        for sg in suggestions:
            lines.append(
                f"- **[{sg.get('priority', '?').upper()}]** ({sg.get('category', '?')}) "
                f"{sg.get('problem', '')} → {sg.get('suggested_action', '')}"
            )
        lines.append("")

    return "\n".join(lines)
